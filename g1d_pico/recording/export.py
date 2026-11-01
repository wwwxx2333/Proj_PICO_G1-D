"""Host-only causal observation/action alignment; bounded memory via a disk index.

This is a documented intermediate dataset, not a claim of LeRobot/RLDS format
compatibility. No robot API, sockets, interpolation or synthetic measurements.
"""
from collections import Counter
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .annotations import active_intervals, load_annotations, time_bounds
from .dataset import load_dataset_settings
from .layout import ARTIFACTS, Layout
from .quality import finite, vector_valid
from .recovery import rows, json_object
from .storage import encode, pretty, safe_file, sha256, verify_episode


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE samples (name TEXT, t REAL, available REAL, payload TEXT)')
        self.names = set()
        self.pending = 0

    def put(self, name, t, available, payload):
        if not finite(t) or t <= 0 or not finite(available) or t > available:
            return
        self.db.execute('INSERT INTO samples VALUES (?,?,?,?)',
                        (name, t, available, json.dumps(payload, allow_nan=False, separators=(',',':'))))
        self.names.add(name)
        self.pending += 1
        if self.pending >= 2000:
            self.db.commit()
            self.pending = 0

    def ready(self):
        self.db.execute('CREATE INDEX by_name_time ON samples(name,t,available)')
        self.db.commit()

    def select(self, name, t, gap, action=False):
        # Observations must already exist at t. Actions are the next actual
        # submitted targets at/after t+offset, never measured q relabelled as action.
        if action:
            sql = 'SELECT t,available,payload FROM samples WHERE name=? AND t>=? AND t<=? ORDER BY t,available LIMIT 1'
            args = (name,t,t+gap)
        else:
            sql = 'SELECT t,available,payload FROM samples WHERE name=? AND t<=? AND t>=? AND available<=? ORDER BY t DESC,available DESC LIMIT 1'
            args = (name,t,t-gap,t)
        row = self.db.execute(sql,args).fetchone()
        if row is None:
            return {'valid':False,'time_delta_s':None,'source_monotonic_s':None,'data':None}
        source, available, payload = row
        value = json.loads(payload)
        return {'valid':value.pop('_valid',True), 'time_delta_s':source-t,
                'source_monotonic_s':source, 'available_monotonic_s':available, 'data':value}

    def close(self):
        self.db.close()


def add_event(index, event):
    stream = event['stream']
    data = event['data']
    state = data.get('state', data)
    emitted = event.get('emitted_monotonic_s')
    received = event.get('recorder_received_monotonic_s', emitted)
    if not finite(emitted) or not finite(received):
        return
    # Acquisition timestamp and availability timestamp are deliberately distinct.
    put = lambda name, t, value: index.put(name,t,received,value)
    if stream == 'control_cycle':
        put('operator', emitted, {'armed':data.get('armed'), 'homing':data.get('homing'),
            'pico_fresh':data.get('pico_fresh'), 'arm_status':data.get('arm_status'),
            'hand_status':data.get('hand_status'), 'right_y_mode':data.get('right_y_mode'),
            '_valid':data.get('pico_fresh') is True})
    elif stream == 'arm_state':
        put('arms',state.get('received_at'), {'q':state.get('q'), '_valid':vector_valid(state.get('q'),14)})
        waist=state.get('waist')
        for i, name in enumerate(('waist_yaw','waist_pitch')):
            q=waist[i] if isinstance(waist,list) and len(waist)==2 else None
            t=state.get('received_at' if i==0 else 'pitch_received_at')
            put(name,t,{'q':q,'_valid':finite(q)})
    elif stream in ('arm_dynamics','hand_dynamics'):
        name='arms_dynamics' if stream=='arm_dynamics' else 'hand_'+str(data.get('side'))+'_dynamics'
        size=14 if stream=='arm_dynamics' else 7
        # dq and tau_est can be unavailable separately; keep per-field validity.
        put(name,state.get('received_at'), {k:state.get(k) for k in ('q','dq','tau_est')} |
            {'fields_valid':{k:vector_valid(state.get(k),size) for k in ('q','dq','tau_est')},
             '_valid':vector_valid(state.get('q'),size)})
        if stream=='arm_dynamics':
            for waist in state.get('waist',[]):
                if not isinstance(waist,dict) or waist.get('joint') not in ('yaw','pitch'): continue
                put('waist_'+waist['joint']+'_dynamics',waist.get('received_at'),
                    {k:waist.get(k) for k in ('q','dq','tau_est')} |
                    {'fields_valid':{k:finite(waist.get(k)) for k in ('q','dq','tau_est')},
                     '_valid':finite(waist.get('q'))})
    elif stream == 'hand_state' and data.get('side') in ('left','right'):
        name='hand_'+data['side']
        put(name,state.get('received_at'), {'q':state.get('q'),'_valid':vector_valid(state.get('q'),7)})
        put('action_'+name,state.get('sent_at'), {'q':state.get('sent_q'),
            'requested_q':state.get('requested_q'), 'blocked_mask':state.get('blocked_mask'),
            'limited_mask':state.get('limited_mask'),
            '_valid':state.get('status') in (1,2) and vector_valid(state.get('sent_q'),7)})
    elif stream == 'arm_output':
        put('action_arms',data.get('last_write_monotonic_s'), {'q':data.get('output_q'),
            'requested_q':data.get('requested_q'), 'tau_ff':data.get('output_tau_ff'),
            '_valid':data.get('owned') is True and vector_valid(data.get('output_q'),14)})
        put('action_waist',data.get('last_write_monotonic_s'), {'q':data.get('waist_output_q'),
            '_valid':data.get('owned') is True and vector_valid(data.get('waist_output_q'),2)})
    elif stream == 'agv_state_action':
        put('column',data.get('height_received_monotonic_s'), {'height_m':data.get('height_m'),
            '_valid':finite(data.get('height_m'))})
        command=data.get('submitted')
        mode=data.get('mode')
        valid=vector_valid(command,3) and data.get('input_fresh') is True
        # A mode that did not invoke an SDK method must not produce its action label.
        if mode in ('base','both'):
            put('action_base',emitted,{'vx':command[0] if valid else None,
                'yaw_rate':command[1] if valid else None,'_valid':valid and data.get('move_result')==0})
        if mode in ('lift','both'):
            put('action_column',emitted,{'normalized':command[2] if valid else None,
                '_valid':valid and data.get('lift_result')==0})


def build_index(index, episode, layout):
    for name in layout.telemetry_files(episode):
        for raw in rows(episode/name):
            add_event(index,json_object(raw))
    for raw in rows(episode/layout.frames):
        value=json_object(raw)
        t=value['received_monotonic_s']
        index.put('image_'+value['camera'],t,t,value)
    index.ready()


def required_channels(index, metadata):
    observation=['operator']
    action=[]
    if 'arms' in index.names or 'action_arms' in index.names:
        observation+=['arms','waist_yaw','waist_pitch']
        action+=['action_arms','action_waist']
    if any(k in index.names for k in ('hand_left','hand_right','action_hand_left','action_hand_right')):
        observation+=['hand_left','hand_right']
        action+=['action_hand_left','action_hand_right']
    if 'action_base' in index.names:
        action+=['action_base']
    if 'action_column' in index.names:
        observation+=['column']
        action+=['action_column']
    cameras=metadata.get('recording_settings',{}).get('cameras')
    if cameras is None:  # Legacy recordings lacking settings use observed cameras.
        cameras=[k[6:] for k in sorted(index.names) if k.startswith('image_')]
    observation += ['image_'+name for name in cameras]
    # All-hand/arm streams entirely absent cannot masquerade as a base-only capture.
    command=metadata.get('control_command',[])
    if '--with-hands' in command:
        observation+=['hand_left','hand_right']
        action+=['action_hand_left','action_hand_right']
    if '--with-arms' in command or any(str(c).endswith('run_pc2_arms.sh') for c in command):
        observation+=['arms','waist_yaw','waist_pitch']
        action+=['action_arms','action_waist']
    return sorted(set(observation)), sorted(set(action))


def export_episode(episode, output_root, annotations=None, notice=print):
    episode=Path(episode).expanduser().resolve()
    notice('Checking raw episode checksums...')
    manifest=verify_episode(episode)
    source_hash=sha256(episode/'manifest.json')
    metadata=json.loads((episode/'metadata.json').read_text())
    layout=Layout(manifest['schema'])
    labels=load_annotations(annotations,episode,metadata,manifest)
    settings=load_dataset_settings()
    start,end=time_bounds(metadata,manifest)
    root=Path(output_root).expanduser().resolve()
    if root.is_relative_to(episode):
        raise ValueError('Training output must be outside the immutable episode')
    root.mkdir(parents=True,exist_ok=True)
    destination=root/episode.name
    destination.mkdir()  # No overwrite of an earlier export or partial output.
    (destination/'IN_PROGRESS').write_text('Offline export incomplete; do not train on this directory.\n')
    (destination/'data').mkdir()
    invalid=Counter()
    samples=eligible=images=0
    index=None
    try:
        with tempfile.TemporaryDirectory(prefix='.alignment-',dir=root) as temporary:
            index=Index(Path(temporary)/'index.sqlite')
            try:
                build_index(index,episode,layout)
                observation_names,action_names=required_channels(index,metadata)
                optional=sorted(n for n in index.names if n.endswith('_dynamics'))
                reviewed=labels['review_status']=='accepted' and labels['task_success'] is True
                recovered='recovery' in manifest or 'recovered_from' in metadata
                gap=settings['export_max_gap_s']
                fps=settings['export_fps']
                with (destination/'data/samples.jsonl').open('wb') as output, (destination/'data/images.jsonl').open('wb') as image_index:
                    # End-exclusive: no supervision after the operator's stop request.
                    for step in range(math.ceil(max(0,end-start)*fps)):
                        t=start+step/fps
                        if t>=end: break
                        observation={name:index.select(name,t,gap) for name in observation_names+optional}
                        action_t=t+settings['export_action_offset_s']
                        action={name.removeprefix('action_'):index.select(name,action_t,gap,True) for name in action_names}
                        for name,item in observation.items():
                            if not name.startswith('image_') or item['data'] is None: continue
                            frame=item['data']
                            relative='images/'+frame['camera']+'/'+Path(frame['path']).name
                            target=destination/relative
                            if not target.exists():
                                target.parent.mkdir(parents=True,exist_ok=True)
                                shutil.copyfile(safe_file(episode,frame['path']),target)
                                if sha256(target)!=frame['sha256']:
                                    raise ValueError('Source image changed during export')
                                image_index.write(encode({**frame,'path':relative,'source_path':frame['path']}))
                                images+=1
                            item['data']={**frame,'source_path':frame['path'],'path':relative}
                        missing=[name for name in observation_names if not observation[name]['valid']]
                        missing+=['action_'+name for name,item in action.items() if not item['valid']]
                        invalid.update(missing)
                        operator=observation['operator']['data'] or {}
                        excluded=active_intervals(labels,'exclude_intervals',t-start)
                        interventions=active_intervals(labels,'interventions',t-start)
                        reasons=list(missing)
                        if not reviewed: reasons.append('outcome_or_quality_not_accepted')
                        if recovered: reasons.append('recovered_tail_requires_manual_dataset_decision')
                        if operator.get('armed') is not True or operator.get('homing') is not False:
                            reasons.append('operator_disabled_or_homing')
                        if not action_names: reasons.append('no_robot_action_channel')
                        if not any(n.startswith('image_') for n in observation_names): reasons.append('no_camera_channel')
                        if excluded or interventions: reasons.append('annotated_exclusion_or_intervention')
                        okay=not reasons
                        row={'step_index':step,'timestamp_s':t-start,'pc2_monotonic_s':t,
                            'action_reference_monotonic_s':action_t,'observation':observation,'action':action,
                            'instruction':labels['instruction'],'stages':active_intervals(labels,'stages',t-start),
                            'interventions':interventions,'excluded':excluded,
                            'eligible_for_imitation':okay,'ineligible_reasons':reasons}
                        output.write(encode(row))
                        samples+=1
                        eligible+=okay
                        if samples%2000==0: notice(f'Aligned {samples} samples')
            finally:
                index.close()
        # Store reproducibility artifacts with the export; no links back to a PC2 path.
        for name in ('metadata.json',*ARTIFACTS):
            source=episode/name
            if source.is_file():
                target=destination/'source'/name
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,target)
        (destination/'labels.json').write_bytes(pretty(labels))
        description={'schema':'g1d-lab-aligned-v1','source_episode_id':episode.name,
            'source_manifest_sha256':source_hash,'export_settings':settings,
            'required_observation_channels':observation_names,'required_action_channels':action_names,
            'session_id':metadata.get('session_id'),'split_group_hint':{
                k:metadata.get('dataset_settings',{}).get(k) for k in ('layout_id','route_id','collector_id')},
            'observation_alignment':'latest source at/before t AND available by t; max gap enforced; no interpolation',
            'action_alignment':'first submitted target at/after t+offset within gap; not measured state; DDS submission is not execution ACK',
            'missing_values':'missing channels have data=null, valid=false; never zero-filled',
            'units':{'joint_q':'rad','joint_dq':'rad/s','tau_est_and_tau_ff':'N m (estimate vs command)',
                     'column_height':'m','column_command':'normalized SDK HeightAdjust input',
                     'vx':'m/s','yaw_rate':'rad/s'},
            'frames':'head JPEG remains concatenated stereo; time is receive time, not exposure',
            'base_world_pose_available':False,'camera_hardware_sync':False}
        (destination/'schema.json').write_bytes(pretty(description))
        (destination/'quality.json').write_bytes(pretty({'samples':samples,'eligible_samples':eligible,
            'invalid_channel_samples':dict(invalid),'unique_images':images,
            'review_status':labels['review_status'],'task_success':labels['task_success']}))
        if sha256(episode/'manifest.json')!=source_hash:
            raise ValueError('Source manifest changed during export')
        # List the small fixed metadata set; never materialize all JPEG paths.
        names=['data/samples.jsonl','data/images.jsonl','labels.json','schema.json','quality.json']
        names += ['source/'+n for n in ('metadata.json',*ARTIFACTS) if (destination/'source'/n).is_file()]
        files={name:{'bytes':(destination/name).stat().st_size,'sha256':sha256(destination/name)} for name in names}
        (destination/'manifest.json').write_bytes(pretty({'schema':'g1d-lab-aligned-v1','finalized':True,
            'files':files,'images_index':'data/images.jsonl','image_count':images,'samples':samples}))
        (destination/'IN_PROGRESS').unlink()
        return {'directory':str(destination),'samples':samples,'eligible_samples':eligible,'unique_images':images}
    except BaseException:
        notice(f'Export incomplete; raw episode unchanged; partial output retained: {destination}')
        raise
