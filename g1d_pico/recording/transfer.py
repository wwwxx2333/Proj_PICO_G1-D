"""Explicit, after-task rsync pull with a remote activity lock and verification."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from .runner import CONTROL_LOCK
from .storage import EPISODE_RE, FileLock, regular_files, sha256, sync_tree, verify_episode


def transfer_lease(settings):
    """Remote helper. Hold BOTH locks until the host closes this SSH session.

    No network service or background transfer daemon is installed. Stdout is a
    JSON-line protocol. Delete requests are accepted only for listed, finalized,
    unchanged, verified episodes and only while this exclusive lease is held.
    """
    root = Path(settings['spool'])
    with FileLock(CONTROL_LOCK), FileLock(root / '.lock'):
        episodes = []
        unfinished = []
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not EPISODE_RE.fullmatch(path.name):
                continue
            if path.is_symlink():
                raise ValueError('Symlink episode rejected')
            if (path / 'IN_PROGRESS').exists() or not (path / 'manifest.json').is_file():
                unfinished.append(path.name)
                continue
            episodes.append({'name': path.name, 'manifest_sha256': sha256(path / 'manifest.json'),
                             'bytes': sum(p.stat().st_size for p in regular_files(path))})
        available = {item['name']: item['manifest_sha256'] for item in episodes}
        print(json.dumps({'state': 'ready', 'spool': str(root), 'episodes': episodes,
                          'unfinished': unfinished}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get('op') == 'done':
                return
            name = request.get('episode', '')
            digest = request.get('manifest_sha256')
            if request.get('op') != 'delete_verified' or name not in available or digest != available[name]:
                raise ValueError('Invalid transfer acknowledgement')
            path = root / name
            if sha256(path / 'manifest.json') != digest:
                raise ValueError('Source manifest changed')
            manifest = verify_episode(path)
            if manifest.get('automatic_source_deletion_allowed') is False:
                raise ValueError('Recovered episodes require manual review; automatic deletion refused')
            shutil.rmtree(path)  # Explicit --delete-after-verify only; exact allowlisted episode.
            del available[name]
            print(json.dumps({'state': 'deleted', 'episode': name}), flush=True)


def read_reply(lease):
    line = lease.stdout.readline()
    if not line:
        raise RuntimeError('PC2 refused/disconnected. Check its stderr; control or another transfer may be active.')
    return json.loads(line)


def fetch(host, remote_root, remote_python, destination, delete_after_verify=False):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
        raise ValueError('Use a hostname/IPv4 address, optionally user@host (no SSH options)')
    for executable in ('ssh', 'rsync'):
        if shutil.which(executable) is None:
            raise RuntimeError(f'{executable} is required on this host')
    if not Path(remote_root).is_absolute() or not Path(remote_python).is_absolute():
        raise ValueError('Remote project and Python paths must be absolute')
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # No key installation/password storage. Use the user's normal SSH authentication.
    command = ('cd ' + shlex.quote(remote_root) + ' && ' + shlex.join([
        remote_python, '-m', 'g1d_pico.recording', 'transfer-lease']))
    with FileLock(destination / '.fetch.lock'):
        lease = subprocess.Popen(['ssh', '-T', '-o', 'ConnectTimeout=10', host, command],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            info = read_reply(lease)
            if info.get('state') != 'ready':
                raise RuntimeError(f'Invalid transfer response: {info}')
            spool = info['spool']
            if not Path(spool).is_absolute() or '\n' in spool:
                raise ValueError('Invalid remote spool path')
            if info['unfinished']:
                print('Unfinalized episodes retained on PC2 (not transferred/deleted):', info['unfinished'])
            for item in info['episodes']:
                name = item['name']
                if not EPISODE_RE.fullmatch(name):
                    raise ValueError('Invalid remote episode name')
                final = destination / name
                if final.is_symlink():
                    raise ValueError('Local episode must not be a symlink')
                if final.exists():
                    verify_episode(final)
                    if sha256(final / 'manifest.json') != item['manifest_sha256']:
                        raise ValueError(f'Existing local episode differs: {final}')
                    print(f'Already verified: {final}')
                else:
                    staging = destination / '.incoming' / name
                    if staging.is_symlink() or (destination / '.incoming').is_symlink():
                        raise ValueError('Incoming directory must not be a symlink')
                    staging.mkdir(parents=True, exist_ok=True)
                    if shutil.disk_usage(destination).free < item['bytes'] + 100 * 1024 * 1024:
                        raise RuntimeError('Not enough free space on the host for this episode')
                    subprocess.run(['rsync', '-a', '--partial', '--checksum', '--protect-args',
                                    '--info=progress2', '--', f'{host}:{spool}/{name}/', str(staging) + '/'],
                                   check=True)
                    verify_episode(staging)
                    if sha256(staging / 'manifest.json') != item['manifest_sha256']:
                        raise ValueError('Transferred manifest differs from PC2 inventory')
                    sync_tree(staging)
                    staging.rename(final)
                    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    print(f'Verified and saved: {final}', flush=True)
                if delete_after_verify:
                    if verify_episode(final).get('automatic_source_deletion_allowed') is False:
                        print(f'Recovered episode retained on PC2 for manual review: {name}', flush=True)
                        continue
                    sync_tree(final)
                    lease.stdin.write(json.dumps({'op': 'delete_verified', 'episode': name,
                                      'manifest_sha256': item['manifest_sha256']}) + '\n')
                    lease.stdin.flush()
                    answer = read_reply(lease)
                    if answer != {'state': 'deleted', 'episode': name}:
                        raise RuntimeError(f'Deletion was not acknowledged: {answer}')
                    print(f'PC2 cache cleared after verification: {name}', flush=True)
            lease.stdin.write('{"op":"done"}\n')
            lease.stdin.flush()
            lease.stdin.close()
            if lease.wait(timeout=10) != 0:
                raise RuntimeError('Remote transfer session failed')
        finally:
            if lease.poll() is None:
                if not lease.stdin.closed:
                    lease.stdin.close()  # EOF releases the remote lease; never delete on failure.
                try:
                    lease.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    lease.terminate()
                    lease.wait(timeout=5)
            lease.stdout.close()
