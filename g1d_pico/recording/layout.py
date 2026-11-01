"""Versioned on-disk paths. v1 stays readable; new production sessions use v2."""
from pathlib import Path

LEGACY_SCHEMA = 'g1d-lab-recording-v1'
SCHEMA = 'g1d-lab-recording-v2'
SUPPORTED = (LEGACY_SCHEMA, SCHEMA)
ARTIFACTS = ('robot/model.urdf', 'robot/joint_order.json', 'robot/control_config.json',
             'calibration/calibration.json', 'schema/fields.json', 'task/task.json')
CAMERAS = ('head', 'left_wrist', 'right_wrist')
STREAM_PATHS = {
    'control_cycle': 'observations/operator/pico.jsonl',
    'arm_state': 'observations/robot/arms.jsonl',
    'hand_state': 'observations/robot/hands.jsonl',
    'body_state': 'observations/robot/body.jsonl',
    'hand_output': 'actions/hands.jsonl',
    'arm_dynamics': 'observations/robot/arm_dynamics.jsonl',
    'hand_dynamics': 'observations/robot/hand_dynamics.jsonl',
    'arm_output': 'actions/arms.jsonl',
    'agv_state_action': 'actions/body.jsonl',
    'bridge_command': 'actions/bridge_commands.jsonl',
    'console': 'events/console.jsonl',
}


class Layout:
    def __init__(self, schema):
        if schema not in SUPPORTED:
            raise ValueError(f'Unsupported recording schema: {schema}')
        self.schema = schema
        self.legacy = schema == LEGACY_SCHEMA
        self.frames = 'frames.jsonl' if self.legacy else 'observations/images/index.jsonl'
        self.console = 'console.log' if self.legacy else 'logs/console.log'
        self.image_root = 'images' if self.legacy else 'observations/images'

    def image(self, camera, index):
        if camera not in CAMERAS:
            raise ValueError(f'Unknown camera: {camera}')
        return f'{self.image_root}/{camera}/{index:08d}.jpg'

    def events(self, stream):
        return 'telemetry.jsonl' if self.legacy else STREAM_PATHS.get(stream, 'events/control.jsonl')

    def telemetry_files(self, root):
        names = ['telemetry.jsonl'] if self.legacy else sorted(set(STREAM_PATHS.values()) | {'events/control.jsonl'})
        return [name for name in names if (Path(root)/name).is_file()]
