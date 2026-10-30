"""Optional offline stress check: 10 minutes x 20 FPS x three camera files.

Writes synthetic JPEG-boundary payloads, not camera imagery. No DDS or robot.
Run from the project root: python -m tests.recording_large_episode_check
"""
import json
import tempfile
import time
from g1d_pico.recording.storage import EpisodeStore, verify_episode


def main():
    with tempfile.TemporaryDirectory(prefix='g1d-large-recording-') as root:
        start = time.monotonic()
        store = EpisodeStore(root, 400_000_000, 0, {'instruction': 'synthetic stress fixture'})
        jpg = b'\xff\xd8' + b'x'*1024 + b'\xff\xd9'
        for i in range(12000):
            for camera in ('head', 'left_wrist', 'right_wrist'):
                store.frame(camera, jpg, i/20, i+1)
        wrote = time.monotonic()
        store.finish()
        synced = time.monotonic()
        manifest = verify_episode(store.path)
        assert manifest['frame_counts'] == dict(head=12000, left_wrist=12000, right_wrist=12000)
        print(json.dumps(dict(files=36000, write_s=round(wrote-start, 2),
                              finish_s=round(synced-wrote, 2),
                              verify_s=round(time.monotonic()-synced, 2), verified=True)))


if __name__ == '__main__': main()
