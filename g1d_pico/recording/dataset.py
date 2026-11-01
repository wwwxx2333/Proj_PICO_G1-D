"""Dataset metadata and immutable robot/calibration snapshots, independent of DDS."""
import hashlib
import math
from pathlib import Path

from ..config_io import load_config
from ..config_paths import DATASET_CONFIG, CALIBRATION_CONFIG, PROJECT_ROOT
from .layout import SCHEMA, STREAM_PATHS

IDS = ('collector_id', 'robot_id', 'task_id', 'layout_id', 'route_id', 'map_id', 'start_area', 'goal_area')
NUMBERS = {'max_state_age_s': (.01, 2), 'export_fps': (1, 60),
           'export_max_gap_s': (.001, 1), 'export_action_offset_s': (0, 1)}


def load_dataset_settings(path=DATASET_CONFIG):
    data = load_config(path)
    if not isinstance(data, dict) or set(data) != set(IDS) | set(NUMBERS):
        raise ValueError('dataset.jsonc has missing or unknown fields')
    for name in IDS:
        if data[name] is not None and (not isinstance(data[name], str) or not data[name].strip()):
            raise ValueError(f'{name} must be null or a non-empty string')
    for name, (low, high) in NUMBERS.items():
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{name} must be a finite number in [{low}, {high}]')
    return data


def matrix(value, size, name):
    if value is None:
        return
    if not isinstance(value, list) or len(value) != size or any(not isinstance(row, list) or len(row) != size for row in value):
        raise ValueError(f'{name} must be null or {size}x{size}')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for row in value for x in row):
        raise ValueError(f'{name} must contain finite numbers')
    if size == 4:
        if any(abs(a-b) > 1e-6 for a,b in zip(value[3], (0,0,0,1))):
            raise ValueError(f'{name} must be a homogeneous transform')
        rotation = [r[:3] for r in value[:3]]
        if any(abs(sum(rotation[k][i]*rotation[k][j] for k in range(3)) - int(i == j)) > 1e-3 for i in range(3) for j in range(3)):
            raise ValueError(f'{name} has a non-orthonormal rotation')
        a,b,c = rotation
        det = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
        if abs(det-1) > 1e-3:
            raise ValueError(f'{name} must use a proper rotation')


def load_calibration(path=CALIBRATION_CONFIG):
    data = load_config(path)
    expected = {'calibration_id','transform_convention','cameras','tools','column_mapping','localization'}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError('calibration.jsonc has missing or unknown fields')
    if data['calibration_id'] is not None and (not isinstance(data['calibration_id'],str) or not data['calibration_id'].strip()):
        raise ValueError('calibration_id must be null or nonempty text')
    if data['transform_convention'] != 'T_parent_child, row-major 4x4, metres':
        raise ValueError('Unsupported calibration transform convention')
    if not isinstance(data['cameras'],dict) or not isinstance(data['tools'],dict) or set(data['cameras']) != {'head_left','head_right','left_wrist','right_wrist'} or set(data['tools']) != {'left','right'}:
        raise ValueError('Calibration must specify all cameras and both tools')
    for name, camera in data['cameras'].items():
        if not isinstance(camera,dict) or set(camera) != {'image_size','K','distortion','parent_link','T_parent_camera'}:
            raise ValueError(f'Invalid camera fields: {name}')
        matrix(camera['K'],3,name+'.K')
        matrix(camera['T_parent_camera'],4,name+'.T_parent_camera')
        size = camera['image_size']
        if size is not None and (not isinstance(size,list) or len(size)!=2 or any(type(n) is not int or n<=0 for n in size)):
            raise ValueError('image_size must be null or [width,height] with positive integers')
        if camera['K'] is not None and (camera['K'][0][0] <= 0 or camera['K'][1][1] <= 0):
            raise ValueError('Camera focal lengths must be positive')
        distortion = camera['distortion']
        if distortion is not None:
            if (not isinstance(distortion,dict) or set(distortion)!={'model','coefficients'}
                    or not isinstance(distortion['model'],str) or not distortion['model']
                    or not isinstance(distortion['coefficients'],list)
                    or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in distortion['coefficients'])):
                raise ValueError('distortion requires a named model and finite coefficients')
    for side, tool in data['tools'].items():
        if not isinstance(tool,dict) or set(tool) != {'parent_link','T_parent_tcp'}:
            raise ValueError('Invalid TCP fields')
        matrix(tool['T_parent_tcp'],4,side+'.T_parent_tcp')
    for item in (*data['cameras'].values(), *data['tools'].values()):
        if item['parent_link'] is not None and (not isinstance(item['parent_link'],str) or not item['parent_link']):
            raise ValueError('parent_link must be null or a URDF link name')
    localization = data['localization']
    if not isinstance(localization,dict) or set(localization) != {'source','frame_id','validated'}:
        raise ValueError('localization requires source, frame_id and validated')
    if type(localization['validated']) is not bool:
        raise ValueError('localization.validated must be boolean')
    for key in ('source','frame_id'):
        value = localization[key]
        if value is not None and (not isinstance(value,str) or not value.strip()):
            raise ValueError(f'localization.{key} must be null or nonempty text')
    if localization['validated'] and not all(localization[k] for k in ('source','frame_id')):
        raise ValueError('Validated localization needs a source and frame_id; it still does not enable a subscriber')
    # A note can document future calibration; no unverified kinematic conversion is applied.
    if data['column_mapping'] is not None and (not isinstance(data['column_mapping'],str) or not data['column_mapping'].strip()):
        raise ValueError('column_mapping must be null or a calibration note string')
    return data


def schema_description():
    return {
        'schema': SCHEMA, 'stream_files': STREAM_PATHS,
        'unlisted_streams': 'events/control.jsonl',
        'envelope': {
            'stream':'source event type', 'producer':'process identity', 'seq':'producer sequence (not global)',
            'emitted_monotonic_s':'PC2 producer CLOCK_MONOTONIC seconds',
            'recorder_received_monotonic_s':'PC2 recorder receipt time',
            'derived_from':'only on writer-derived views; producer/seq refer to original packet, not a new transport packet',
            'data':'unaltered source fields (except explicitly derived views); actions and observations are distinguished below',
            'quality':'receipt-age and numeric validity; never changes robot control'},
        'streams': {
            'arm_state':'data.state.q[14] measured joint angles (rad); waist[2]=yaw,pitch (rad), independent source times',
            'hand_state':'data.side; state.q[7] measured (rad); requested_q desired (rad); sent_q last DDS submission (rad); sent_at is not an execution ACK',
            'arm_dynamics':'q,dq,tau_est for 14 arms; separate yaw/pitch snapshots, source PC2 DDS callback times',
            'hand_dynamics':'q,dq,tau_est for 7 motors of data.side, original DDS callback time and selected topic',
            'arm_output':'requested_q[14] desired, output_q[14] rate-limited output; output_tau_ff (N m) is COMMAND feedforward, not measured torque; waist_output_q[2]',
            'agv_state_action':'requested/submitted=[vx m/s,yaw rad/s,lift normalized]; includes supporting height_m feedback, SDK results; pose and measured base velocity unavailable',
            'control_cycle':'PICO raw XR input, operator enable/homing/mode and requested command; never treat PICO tracking as robot pose',
            'hand_output':'recorder-derived view of hand_state: data.side, last_write_monotonic_s, output_q, requested_q, masks; derived_from identifies original packet',
            'body_state':'recorder-derived height feedback view of agv_state_action; unavailable base pose and velocity stay null',
            'bridge_command':'raw Python-to-C++ command protocol; diagnostic, not a fixed-dimension learning target'},
        'joint_dynamics_units': {'q':'rad','dq':'rad/s','tau_est':'N m, vendor estimate, NOT contact-force measurement'},
        'dynamics_missing_values':'null means nonfinite/unavailable; finite SDK defaults are not independently sensor-validated',
        'images':'JPEG; head is concatenated stereo (left/right according to camera setup); index uses PC2 ZMQ receipt time, not exposure time',
        'coordinate_frames':'XR input, robot joint coordinates and optional calibrated frames are distinct; no map-frame pose is inferred',
        'compatibility':'Image/state/action row numbers are independent. Align by timestamps and validity, never by line number.'}


def snapshot_artifacts(metadata, command):
    settings = load_dataset_settings()
    calibration = load_calibration()
    model = PROJECT_ROOT/'assets/g1d/g1_d.urdf'
    if '--arm-urdf' in command:
        i = command.index('--arm-urdf')
        if i+1 >= len(command):
            raise ValueError('--arm-urdf needs a path')
        model = Path(command[i+1]).expanduser()
        if not model.is_absolute(): model = PROJECT_ROOT/model
    for argument in command:
        if str(argument).startswith('--arm-urdf='):
            model = Path(str(argument).split('=',1)[1]).expanduser()
            if not model.is_absolute(): model = PROJECT_ROOT/model
    if not model.is_file() or model.stat().st_size > 4*1024*1024:
        raise ValueError(f'Missing or oversized robot model: {model}')
    model_text = model.read_bytes().decode("utf-8")
    import xml.etree.ElementTree as ET
    document = ET.fromstring(model_text)
    links = {link.attrib['name'] for link in document.findall('link')}
    for item in (*calibration['cameras'].values(), *calibration['tools'].values()):
        if item['parent_link'] is not None and item['parent_link'] not in links:
            raise ValueError(f"Calibration references unknown model link: {item['parent_link']}")
    artifacts = {
        'robot/model.urdf': model_text,
        'robot/joint_order.json': {'arms':metadata['arm_joint_order'], 'arm_lowstate_indices':list(range(15,29)),
            'hands':{'left':list(range(7)),'right':list(range(7))}, 'hand_order':'SDK motor_state order; do not infer from URDF document order',
            'waist':['yaw','pitch'], 'column':'rt/hispeed_state.y; metres; URDF linkage mapping not yet validated'},
        'robot/control_config.json': {'body':metadata['body_config'],'arms':metadata['arm_config']},
        'calibration/calibration.json': calibration,
        'schema/fields.json': schema_description(),
        'task/task.json': {**{k:settings[k] for k in IDS}, 'instruction':metadata['instruction'],
            'scene':metadata['scene'], 'objects_text':metadata['object_ids'],
            'task_success':None, 'navigation_success':None, 'grasp_success':None, 'placement_success':None,
            'start_pose':None, 'goal_pose':None, 'stages':[], 'interventions':[], 'review_status':'unreviewed'},
    }
    metadata.update(schema=SCHEMA, dataset_settings=settings,
                    calibration_status={
                        'camera_intrinsics_supplied':{name:item['K'] is not None for name,item in calibration['cameras'].items()},
                        'camera_extrinsics_supplied':{name:item['T_parent_camera'] is not None for name,item in calibration['cameras'].items()},
                        'tcp_supplied':{name:item['T_parent_tcp'] is not None for name,item in calibration['tools'].items()},
                        'physical_accuracy':'not automatically verified'},
                    model_sha256=hashlib.sha256(model_text.encode()).hexdigest(),
                    model_scope='archived project/control URDF; mesh visualization assets not embedded; column/TCP calibration pending',
                    availability={'base_localization':False,'measured_base_velocity':False,'depth':False,
                                  'camera_exposure_time':False,'tactile':False,
                                  'joint_dynamics':'supported by updated bridges; actual received streams reported in quality/summary.json'},
                    _artifacts=artifacts)
    return metadata
