"""Configuration parsing only: no robot, camera, socket or subprocess."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from g1d_pico.arm_config import ArmConfig
from g1d_pico.body_config import BodyConfig
from g1d_pico.config_io import load_config
from g1d_pico.recording.runner import code_snapshot, load_settings

ROOT = Path(__file__).resolve().parents[1]


class ConfigCommentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'config.jsonc'

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')
        return self.path

    def test_comments_above_values_inline_crlf_and_at_eof(self):
        self.write('// 说明\r\n{\r\n // 建议 10～20\r\n "fps": 20, // 单位 FPS\r\n'
                   ' "cameras": ["head", // 头部\r\n "left_wrist"]\r\n} // 末尾')
        self.assertEqual(load_config(self.path), {'fps': 20, 'cameras': ['head', 'left_wrist']})

    def test_slashes_quotes_backslashes_and_comment_markers_in_strings(self):
        values = {'url': 'https://example.com/path//item',
                  'quote': '含有 "引号" // 仍然是字符串',
                  'path': 'C:\\config\\', 'markers': '/* unchanged */ # unchanged',
                  'escaped': '\\"//literal', 'empty': ''}
        self.write('// 标头\n' + json.dumps(values, ensure_ascii=False) + '\n// 结尾')
        self.assertEqual(load_config(self.path), values)

    def test_invalid_json_is_not_repaired_and_error_position_is_preserved(self):
        for text in ('{"fps": 20,}', '{"fps": 1/*x*/0}', '{"fps": 1//x\n0}',
                     '{"fps": "unfinished // string}', '{"fps": "invalid\nstring"}'):
            with self.subTest(text=text), self.assertRaises(json.JSONDecodeError):
                load_config(self.write(text))
        self.write('{\n // 说明\n "fps": invalid\n}')
        with self.assertRaises(json.JSONDecodeError) as failure:
            load_config(self.path)
        self.assertEqual((failure.exception.lineno, failure.exception.colno), (3, 9))

    def test_old_plain_and_metadata_configs_still_work(self):
        self.assertEqual(ArmConfig.load(self.write('{}')), ArmConfig())
        self.assertEqual(ArmConfig.load(self.write('{"_comments": {"说明": "旧格式"}}')), ArmConfig())
        settings = load_config(ROOT / 'config/recording.jsonc')
        self.write(json.dumps(settings))
        plain = load_settings(self.path)
        settings['_comments'] = {'说明': '旧格式'}
        self.assertEqual(load_settings(self.write(json.dumps(settings))), plain)

    def test_schema_validation_still_rejects_typos_missing_keys_and_bad_values(self):
        for values in ({'joint_speed_rad_s': 0}, {'joint_speed_rad_s': True},
                       {'joint_speed_rad_s': 2}, {'joint_speed_typo': 0.25}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ArmConfig.load(self.write('// comments\n' + json.dumps(values)))
        original = load_config(ROOT / 'config/recording.jsonc')
        missing = dict(original)
        del missing['spool']
        for values in ({**original, 'image_fps': 0}, {**original, 'image_fps': True},
                       {**original, 'image_fps_typo': 20}, missing):
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_settings(self.write('// comments\n' + json.dumps(values)))

    def test_real_files_load_and_recording_snapshot_excludes_line_comments(self):
        arm = ArmConfig.load(ROOT / 'config/arms.jsonc')
        load_settings(ROOT / 'config/recording.jsonc')
        expected = asdict(arm)
        expected['filter_weights'] = list(expected['filter_weights'])
        snapshot = code_snapshot()
        self.assertEqual(snapshot['arm_config'], expected)
        self.assertEqual(snapshot['body_config'], asdict(BodyConfig.load(ROOT / 'config/body.jsonc')))
        self.assertNotIn('_comments', snapshot['arm_config'])
        from g1d_pico.recording.storage import sha256
        for name in ('arms', 'body', 'recording'):
            relative = f'config/{name}.jsonc'
            self.assertEqual(snapshot['source_sha256'][relative], sha256(ROOT / relative))


    def test_default_control_config_is_jsonc_and_loadable(self):
        from g1d_pico.__main__ import parse_args
        with patch('sys.argv', ['g1d_pico']):
            path = parse_args().arm_config
        self.assertEqual(path, ROOT / 'config/arms.jsonc')
        ArmConfig.load(path)

    def test_recording_and_lease_defaults_load_jsonc(self):
        from g1d_pico.recording import __main__ as cli
        for operation in ('run', 'transfer-lease'):
            with self.subTest(operation=operation), \
                 patch('sys.argv', ['recording', operation]), \
                 patch.object(cli, 'load_settings', wraps=load_settings) as loader, \
                 patch.object(cli, 'run', return_value=0) as run, \
                 patch('g1d_pico.recording.transfer.transfer_lease') as lease:
                self.assertEqual(cli.main(), 0)
                loader.assert_called_once_with(ROOT / 'config/recording.jsonc')
                (run if operation == 'run' else lease).assert_called_once()

    def test_host_fetch_default_points_to_remote_jsonc(self):
        from g1d_pico.recording import __main__ as cli
        with patch('sys.argv', ['recording', 'fetch', '--destination', str(self.path.parent)]), \
             patch('g1d_pico.recording.transfer.fetch') as fetch:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(fetch.call_args.args[3], self.path.parent)
        self.assertEqual(len(fetch.call_args.args), 5)  # No config-path parameter.

    def test_recording_cli_rejects_config_selection_for_all_operations(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from g1d_pico.recording import __main__ as cli
        for operation in ('run', 'fetch', 'transfer-lease'):
            argv = ['recording', operation, '--config', '/tmp/unused.jsonc']
            if operation == 'fetch': argv += ['--destination', '/tmp/unused']
            with self.subTest(operation=operation), patch('sys.argv', argv), \
                 redirect_stderr(StringIO()), patch.object(cli, 'load_settings') as loader:
                with self.assertRaises(SystemExit) as error: cli.main()
                self.assertEqual(error.exception.code, 2)
                loader.assert_not_called()

    def test_paths_are_independent_of_working_directory(self):
        import os
        from g1d_pico.__main__ import parse_args
        previous = Path.cwd()
        try:
            os.chdir(self.path.parent)
            with patch('sys.argv', ['g1d_pico']): args = parse_args()
        finally:
            os.chdir(previous)
        self.assertEqual(args.body_config, ROOT / 'config/body.jsonc')
        self.assertEqual(args.body_settings, BodyConfig.load(ROOT / 'config/body.jsonc'))



if __name__ == '__main__':
    unittest.main()
