"""Internal launch wrapper. Public entry scripts expose --full-log."""
import argparse
import sys
from .runner import run_logged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        return run_logged(command)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'LOG STARTUP_ERROR 无法启动自动运行日志，未完成启动：{exc}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
