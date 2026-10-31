"""Record on PC2, then explicitly fetch on the host after control has stopped."""
import argparse
import json
from pathlib import Path
import sys

from ..config_paths import RECORDING_CONFIG
from .runner import load_settings, run
from .storage import verify_episode


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        epilog="Recording settings always come from project config/recording.jsonc.")
    commands = parser.add_subparsers(dest='operation', required=True)
    record = commands.add_parser('run', allow_abbrev=False, help='Run control with local-only recording')
    record.add_argument('--instruction', default='')
    record.add_argument('--scene', default='lab')
    record.add_argument('--objects', default='')
    record.add_argument('command', nargs=argparse.REMAINDER)
    verify = commands.add_parser('verify', allow_abbrev=False, help='Verify a finalized episode offline')
    verify.add_argument('episode', type=Path)
    recovery = commands.add_parser('recover', allow_abbrev=False, help='Offline audit; optionally write a NEW recovery copy')
    recovery.add_argument('episode', type=Path)
    recovery.add_argument('--output-root', type=Path, help='Separate destination, normally on the host')
    recovery.add_argument('--allow-truncated', action='store_true',
                          help='Allow dropping an incomplete final JSONL line in the copy only')
    transfer = commands.add_parser('fetch', allow_abbrev=False, help='Host: pull completed PC2 recordings')
    transfer.add_argument('--host', default='unitree@192.168.1.118')
    transfer.add_argument('--remote-root', default='/home/unitree/Proj/Proj_PICO_G1-D')
    transfer.add_argument('--remote-python', default='/home/unitree/miniconda3/envs/pico_g1d/bin/python')
    transfer.add_argument('--destination', type=Path, required=True)
    transfer.add_argument('--delete-after-verify', action='store_true')
    commands.add_parser('transfer-lease', allow_abbrev=False, help='Internal PC2 transfer-lock helper')
    args = parser.parse_args()
    try:
        if args.operation == 'run':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            return run(load_settings(RECORDING_CONFIG), command, args.instruction, args.scene, args.objects)
        if args.operation == 'verify':
            result = verify_episode(args.episode)
            print(json.dumps({k: result[k] for k in ('episode_id', 'recording_stop_reason',
                             'frame_counts', 'stream_counts', 'sequence_gaps')}, indent=2, ensure_ascii=False))
        elif args.operation == 'recover':
            from .recovery import recover_episode
            report = recover_episode(args.episode, args.output_root, args.allow_truncated,
                                     notice=lambda text: print(text, file=sys.stderr, flush=True))
            print(json.dumps({k: v for k, v in report.items() if k != 'metadata'}, indent=2, ensure_ascii=False))
        elif args.operation == 'fetch':
            from .transfer import fetch
            fetch(args.host, args.remote_root, args.remote_python,
                  args.destination, args.delete_after_verify)
        elif args.operation == 'transfer-lease':
            from .transfer import transfer_lease
            transfer_lease(load_settings(RECORDING_CONFIG))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Recording: {exc}', file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
