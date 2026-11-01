"""Record on PC2, then explicitly fetch on the host after control has stopped."""
import argparse
import json
import subprocess
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
    record.add_argument('--full-log', action='store_true', help='Show all runtime logs on terminal')
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
    transfer.add_argument('--with-video', action='store_true', help='Host: create MP4 previews after this fetch closes SSH')
    transfer.add_argument('--video-output-root', type=Path, help='Separate videos directory; default: destination parent / videos')
    video = commands.add_parser('video', allow_abbrev=False, help='Host: convert finalized v1/v2 episodes to MP4 review copies')
    video.add_argument('episodes', type=Path, nargs='+')
    video.add_argument('--output-root', type=Path, required=True)
    video.add_argument('--fps', type=float, default=20, help='Output fps; source timestamps determine playback speed')
    video.add_argument('--width', type=int, default=1280, help='Composite width, divisible by 4')
    video.add_argument('--max-gap', type=float, default=.25, help='Black panel after this many seconds without a fresh frame')
    annotate = commands.add_parser('annotate', allow_abbrev=False, help='Host: create an editable labels template outside raw data')
    annotate.add_argument('episode', type=Path)
    annotate.add_argument('--output-root', type=Path, required=True)
    export = commands.add_parser('export', allow_abbrev=False, help='Host: align immutable raw data into a portable training intermediate')
    export.add_argument('episode', type=Path)
    export.add_argument('--output-root', type=Path, required=True)
    export.add_argument('--annotations', type=Path)
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
                  args.destination, args.delete_after_verify, args.with_video, args.video_output_root)
        elif args.operation == 'video':
            from .video import check_video_dependencies, make_video
            check_video_dependencies()
            errors = []
            for episode in args.episodes:
                try:
                    make_video(episode, args.output_root, args.fps, args.width, args.max_gap,
                               notice=lambda text: print(text, flush=True))
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f'{episode.name}: {exc}')
                    print(f'VIDEO ERROR {episode.name}: {exc}', file=sys.stderr, flush=True)
            if errors:
                raise RuntimeError('Video conversion failed for ' + str(len(errors)) + ' episode(s); raw data unchanged.')
        elif args.operation == 'annotate':
            from .annotations import create_annotations
            print(create_annotations(args.episode, args.output_root))
        elif args.operation == 'export':
            from .export import export_episode
            print(json.dumps(export_episode(args.episode, args.output_root, args.annotations),
                             ensure_ascii=False, indent=2))
        elif args.operation == 'transfer-lease':
            from .transfer import transfer_lease
            transfer_lease(load_settings(RECORDING_CONFIG))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Recording: {exc}', file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print('Cancelled. Verified raw data is retained; unfinished previews are not published.', file=sys.stderr, flush=True)
        return 130
    return 0


if __name__ == '__main__':
    from ..runtime_logging import active
    if not active() and sys.argv[1:2] == ['run']:
        from ..runtime_logging.runner import run_logged
        raise SystemExit(run_logged([sys.executable, '-u', '-m', 'g1d_pico.recording', *sys.argv[1:]]))
    raise SystemExit(main())
