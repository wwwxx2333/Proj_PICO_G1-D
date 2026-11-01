"""Explicit, after-task rsync pull with a remote activity lock and verification."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

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


@contextmanager
def shared_transfer_lease(host, command):
    """Authenticate the lease once; rsync must reuse its private OpenSSH master.

    No password is stored, no SSH config is edited, and no persistent master is
    left after fetch. BatchMode plus a failing ProxyCommand prevents a slave
    silently opening a new connection if the master disappears.
    """
    with tempfile.TemporaryDirectory(prefix='g1d-ssh-', dir='/tmp') as directory:
        socket = str(Path(directory) / 'control')
        common = ['ssh', '-S', socket, '-o', 'ConnectTimeout=10']
        master = common + ['-o', 'ControlMaster=yes', '-o', 'ControlPersist=no',
                           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                           '-T', host, command]
        lease = subprocess.Popen(master, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True, bufsize=1)
        slave = common + ['-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
                          '-o', 'ProxyCommand=false']
        try:
            info = read_reply(lease)
            if info.get('state') != 'ready':
                raise RuntimeError(f'Invalid transfer response: {info}')
            # A ready remote helper is insufficient if the local mux listener failed.
            check = subprocess.run(common + ['-O', 'check', host],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   text=True, timeout=15)
            if check.returncode:
                raise RuntimeError('SSH connection sharing failed: ' + check.stderr.strip())
            print('SSH connected: this fetch reuses one authenticated connection.', flush=True)
            yield lease, info, shlex.join(slave)
        finally:
            if not lease.stdin.closed:
                try:
                    lease.stdin.close()  # EOF releases the remote lease; never delete on failure.
                except OSError:
                    pass  # A disconnected pipe must not mask the original transfer error.
            if lease.poll() is None:
                try:
                    lease.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    lease.terminate()
                    try:
                        lease.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        lease.kill()
                        lease.wait()
            lease.stdout.close()


def fetch(host, remote_root, remote_python, destination, delete_after_verify=False,
          with_video=False, video_output_root=None):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
        raise ValueError('Use a hostname/IPv4 address, optionally user@host (no SSH options)')
    for executable in ('ssh', 'rsync'):
        if shutil.which(executable) is None:
            raise RuntimeError(f'{executable} is required on this host')
    if not Path(remote_root).is_absolute() or not Path(remote_python).is_absolute():
        raise ValueError('Remote project and Python paths must be absolute')
    if video_output_root is not None and not with_video:
        raise ValueError('--video-output-root requires --with-video')
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    videos = Path(video_output_root).expanduser().resolve() if video_output_root else destination.parent/'videos'
    if with_video:
        from .video import check_video_dependencies, validate_output_root
        validate_output_root(videos)
        if videos == destination:
            raise ValueError('Video output must be separate from the raw destination')
        check_video_dependencies()  # Fail before connecting if the host lacks a usable encoder.
    command = ('cd ' + shlex.quote(remote_root) + ' && ' + shlex.join([
        remote_python, '-m', 'g1d_pico.recording', 'transfer-lease']))
    completed = []
    transfer_error = None
    with FileLock(destination / '.fetch.lock'):
        try:
            with shared_transfer_lease(host, command) as (lease, info, rsync_shell):
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
                        try:
                            subprocess.run(['rsync', '-a', '--partial', '--checksum', '--protect-args',
                                            '--info=progress2', '-e', rsync_shell, '--',
                                            f'{host}:{spool}/{name}/', str(staging) + '/'], check=True)
                        except subprocess.CalledProcessError as exc:
                            raise RuntimeError(
                                f'Transfer interrupted for {name} (rsync exit {exc.returncode}). '
                                'This episode was NOT deleted on PC2; keep .incoming and rerun fetch. '
                                'Already verified episodes remain saved.') from exc
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
                    completed.append(final)
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
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            transfer_error = str(exc)
        # Release PC2 locks/SSH before any CPU-heavy, host-only video work.
        video_errors = []
        if with_video:
            from .video import make_video
            for episode in completed:
                try:
                    make_video(episode, videos)
                except (OSError, RuntimeError, ValueError) as exc:
                    video_errors.append(f'{episode.name}: {exc}')
                    print(f'VIDEO ERROR {episode.name}: {exc}. Verified raw data remains saved.', flush=True)
        errors = ([transfer_error] if transfer_error else []) + video_errors
        if errors:
            raise RuntimeError('\n'.join(errors))
        if not completed:
            print('No finalized episodes to fetch. Existing host data can be converted with the video command.', flush=True)
