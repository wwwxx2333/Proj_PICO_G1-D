"""Real OpenSSL certificate checks in temporary projects; no network or robot access."""
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OPENSSL = shutil.which('openssl')


@unittest.skipUnless(OPENSSL and shutil.which('flock'), 'OpenSSL and flock required')
class HttpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        for name in ('prepare_pc2_https.sh', 'run_pc2_base.sh', 'runtime_logging.sh'):
            shutil.copy2(ROOT / 'deploy' / name, self.root / 'deploy' / name)
        self.tls = self.root / '.local/https'
        self.env = os.environ.copy()
        self.env.update(HTTPS_TEST_IP='192.168.1.118', HTTPS_TEST_PORT_BUSY='',
                        HTTPS_TEST_FAIL_GENERATION='', G1D_RUNTIME_LOG_ACTIVE='https-test')
        self.env['PATH'] = str(self.root / 'bin') + os.pathsep + self.env['PATH']
        self.executable('bin/ip', '#!/usr/bin/env bash\n'
                        'if [[ $1 == -4 && -n ${HTTPS_TEST_IP:-} ]]; then\n'
                        '  printf "4: wlan0 inet %s/24\\n" "$HTTPS_TEST_IP"\nfi\n')
        self.executable('bin/ss', '#!/usr/bin/env bash\n'
                        'if [[ -n ${HTTPS_TEST_PORT_BUSY:-} ]]; then echo "LISTEN :8012"; fi\n')
        self.executable('bin/uname', '#!/usr/bin/env bash\necho aarch64\n')
        self.executable('bin/openssl', '#!/usr/bin/env bash\n'
                        'if [[ $1 == req && -n ${HTTPS_TEST_FAIL_GENERATION:-} ]]; then\n'
                        '  echo "simulated generation failure" >&2; exit 1\nfi\n'
                        f'exec {shlex.quote(OPENSSL)} "$@"\n')

    def executable(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        path.chmod(0o755)
        return path

    def prepare(self, *args):
        return subprocess.run(['bash', str(self.root / 'deploy/prepare_pc2_https.sh'), *args],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def pair_bytes(self, directory=None):
        directory = directory or self.tls
        return tuple((directory / name).read_bytes() for name in ('cert.pem', 'key.pem'))

    def assert_valid(self, address):
        result = subprocess.run([OPENSSL, 'verify', '-CAfile', str(self.tls / 'cert.pem'),
                                 '-verify_ip', address, str(self.tls / 'cert.pem')],
                                capture_output=True, text=True)
        self.assert_success(result)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.tls / 'cert.pem', self.tls / 'key.pem')

    def backups(self):
        return list((self.root / '.local').glob('https-backup-*'))

    def test_first_run_creates_a_loadable_certificate_for_detected_ip(self):
        result = self.prepare()
        self.assert_success(result)
        self.assertIn('HTTPS CREATED', result.stdout)
        self.assert_valid('192.168.1.118')
        self.assertEqual(self.tls.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.tls / 'key.pem').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups(), [])

    def test_matching_certificate_is_reused_without_regeneration_or_backup(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env['HTTPS_TEST_FAIL_GENERATION'] = '1'
        result = self.prepare()
        self.assert_success(result)
        self.assertIn('HTTPS REUSE', result.stdout)
        self.assertEqual(self.pair_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_changed_ip_replaces_pair_and_preserves_exact_old_pair(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env['HTTPS_TEST_IP'] = '192.168.50.20'
        self.assert_success(self.prepare())
        self.assert_valid('192.168.50.20')
        self.assertNotEqual(self.pair_bytes(), original)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.pair_bytes(self.backups()[0]), original)
        self.assertEqual(self.backups()[0].stat().st_mode & 0o777, 0o700)

    def test_missing_or_malformed_files_are_backed_up_and_repaired(self):
        self.assert_success(self.prepare())
        for case in ('missing-key', 'missing-cert', 'malformed-cert'):
            with self.subTest(case=case):
                if case == 'missing-key':
                    (self.tls / 'key.pem').unlink()
                elif case == 'missing-cert':
                    (self.tls / 'cert.pem').unlink()
                else:
                    (self.tls / 'cert.pem').write_text('broken certificate')
                remaining = {p.name: p.read_bytes() for p in self.tls.glob('*.pem')}
                previous_backups = set(self.backups())
                self.assert_success(self.prepare())
                self.assert_valid('192.168.1.118')
                backup, = set(self.backups()) - previous_backups
                self.assertEqual({p.name: p.read_bytes() for p in backup.iterdir()}, remaining)

    def test_mismatched_private_key_is_replaced_with_a_valid_pair(self):
        self.assert_success(self.prepare())
        other_key = self.root / 'other-key.pem'
        result = subprocess.run([OPENSSL, 'genrsa', '-out', str(other_key), '2048'],
                                capture_output=True, text=True)
        self.assert_success(result)
        (self.tls / 'key.pem').write_bytes(other_key.read_bytes())
        original = self.pair_bytes()
        self.assert_success(self.prepare())
        self.assert_valid('192.168.1.118')
        self.assertEqual(self.pair_bytes(self.backups()[0]), original)

    def test_expired_certificate_is_renewed(self):
        self.assert_success(self.prepare())
        expired = self.root / 'expired.pem'
        result = subprocess.run([OPENSSL, 'x509', '-in', str(self.tls / 'cert.pem'),
                                 '-signkey', str(self.tls / 'key.pem'), '-days', '-1',
                                 '-out', str(expired)], capture_output=True, text=True)
        self.assert_success(result)
        (self.tls / 'cert.pem').write_bytes(expired.read_bytes())
        original = self.pair_bytes()
        self.assert_success(self.prepare())
        self.assert_valid('192.168.1.118')
        self.assertEqual(self.pair_bytes(self.backups()[0]), original)

    def test_generation_failure_preserves_original_files_and_cleans_temporary_files(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env.update(HTTPS_TEST_IP='192.168.50.20', HTTPS_TEST_FAIL_GENERATION='1')
        result = self.prepare()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('existing files were not replaced', result.stderr)
        self.assertEqual(self.pair_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertEqual(list(self.tls.glob('new.*')), [])

    def test_missing_wifi_or_invalid_ip_does_not_change_certificates(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env['HTTPS_TEST_IP'] = ''
        for args in ((), ('999.1.1.1',), ('192.168.1.1,DNS:unexpected',)):
            with self.subTest(args=args):
                self.assertNotEqual(self.prepare(*args).returncode, 0)
                self.assertEqual(self.pair_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_busy_port_blocks_replacement_but_does_not_prevent_reuse(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env['HTTPS_TEST_PORT_BUSY'] = '1'
        self.assert_success(self.prepare())
        result = self.prepare('192.168.50.20')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('port 8012 is in use', result.stderr)
        self.assertEqual(self.pair_bytes(), original)

    def launch_camera_fixture(self):
        for package, module, symbol in (('televuer', 'televuer', 'TeleVuer'),
                                        ('teleimager', 'image_client', 'ImageClient')):
            directory = self.root / package
            directory.mkdir(exist_ok=True)
            (directory / '__init__.py').touch()
            (directory / f'{module}.py').write_text(f'class {symbol}: pass\n')
        runtime = self.executable('bin/runtime', f'#!{sys.executable}\n' + '''
import os, socket, sys
from pathlib import Path
if sys.argv[1:] == ['-']:
    class PortCheck:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def setsockopt(self, *_): pass
        def bind(self, address):
            if os.environ.get('HTTPS_TEST_PORT_BUSY'):
                raise OSError('address already in use')
    socket.socket = PortCheck
if sys.argv[1] == '-':
    sys.argv = sys.argv[1:]
    exec(compile(sys.stdin.read(), '<preflight>', 'exec'), {'__name__': '__main__'})
elif sys.argv[1:4] == ['-u', '-m', 'g1d_pico']:
    Path(os.environ['HTTPS_TEST_APP_MARKER']).touch()
    print('APP_STARTED_WITH_VALIDATED_TLS')
else:
    raise SystemExit('unexpected runtime command')
''')
        self.env.update(PICO_G1D_PYTHON=str(runtime), PYTHONPATH=str(self.root),
                        HTTPS_TEST_APP_MARKER=str(self.root / 'app-started'))
        return subprocess.run(['bash', 'deploy/run_pc2_base.sh', '--camera-only'],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20)

    def test_launcher_prepares_missing_certificate_before_starting_application(self):
        result = self.launch_camera_fixture()
        self.assert_success(result)
        self.assertIn('APP_STARTED_WITH_VALIDATED_TLS', result.stdout)
        self.assert_valid('192.168.1.118')

    def test_launcher_keeps_old_certificate_and_does_not_start_when_port_is_busy(self):
        self.assert_success(self.prepare())
        original = self.pair_bytes()
        self.env.update(HTTPS_TEST_IP='192.168.50.20', HTTPS_TEST_PORT_BUSY='1')
        result = self.launch_camera_fixture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot bind port 8012', result.stderr)
        self.assertEqual(self.pair_bytes(), original)
        self.assertFalse((self.root / 'app-started').exists())


if __name__ == '__main__':
    unittest.main()
