"""Wait for durable recording completion, independently of robot shutdown."""
import time


def wait_for_recorder(worker, receive, notice, abort_requested, stall_after=60.0, *,
                      finished=False, failed=False, on_message=None):
    """No total-time kill: slow, progressing fsync is valid work.

    A stall warning is informational. Only an explicit interruption during this
    phase terminates the writer. EOF is not completion, nor a reason to kill it.
    """
    last_progress = time.monotonic()
    last_warning = last_progress
    last_key = None
    eof = False
    stage = 'waiting_for_finalization'

    def read_status():
        nonlocal last_progress, last_key, finished, failed, eof, stage
        if eof or not receive.poll(0.1):
            return
        try:
            message = receive.recv()
        except EOFError:
            eof = True
            return
        if on_message is None:
            notice('RECORDER ' + str(message))
        else:
            on_message(message)
        state = message.get('state')
        if state == 'finalizing':
            stage = message.get('stage', 'unknown')
            key = (stage, message.get('completed'), message.get('total'))
            if key != last_key:
                last_key = key
                last_progress = time.monotonic()
        elif state == 'finished':
            finished = True
            stage = 'worker_cleanup'
            last_progress = time.monotonic()
        elif state == 'error' or (state == 'stopped' and message.get('reason') == 'WRITE_ERROR'):
            failed = True

    while worker.is_alive():
        if abort_requested():
            notice('Recorder finalization explicitly interrupted; preserve original data for recovery.')
            worker.terminate()
            worker.join(timeout=3)
            if worker.is_alive():
                worker.kill()
                # Keep activity/spool locks until the writer really exits.
                worker.join()
            return False
        read_status()
        now = time.monotonic()
        if now - last_progress >= stall_after and now - last_warning >= stall_after:
            notice(f'Recorder has made no reported progress for {now-last_progress:.0f}s '
                   f'(stage={stage}). Still waiting; Ctrl+C cancels finalization and keeps recovery data.')
            last_warning = now
        if eof:
            time.sleep(0.05)
    worker.join()
    while not eof and receive.poll():
        read_status()
    if not finished or failed or worker.exitcode != 0:
        notice(f'Recording was not confirmed complete (worker exit={worker.exitcode}); '
               'keep the episode and use the offline recovery tool.')
        return False
    return True
