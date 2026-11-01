"""Observation quality only: never changes control freshness or robot commands."""
from collections import Counter
import math


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def age_status(source_time, received_time, max_age):
    if not finite(source_time) or source_time <= 0 or not finite(received_time):
        return {'valid': False, 'age_s': None, 'reason': 'missing_source_time'}
    age = received_time-source_time
    return {'valid': 0 <= age <= max_age, 'age_s': age,
            'reason': 'ok' if 0 <= age <= max_age else 'future_time' if age < 0 else 'stale'}


def vector_valid(value, size):
    return isinstance(value, list) and len(value) == size and all(finite(x) for x in value)


def event_quality(event, max_age=.25):
    data = event.get('data', {})
    state = data.get('state', data)
    when = event.get('recorder_received_monotonic_s', event.get('emitted_monotonic_s'))
    stream = event.get('stream')
    sources = {}
    if stream in ('arm_state', 'hand_state', 'arm_dynamics', 'hand_dynamics'):
        source = age_status(state.get('received_at'), when, max_age)
        size = 14 if stream.startswith('arm_') else 7
        source['position_valid'] = vector_valid(state.get('q'), size)
        source['valid'] &= source['position_valid']
        if not source['position_valid']:
            source['reason'] = 'invalid_or_missing_position'
        for key in ('dq', 'tau_est'):
            if key in state:
                source[key+'_finite'] = vector_valid(state[key], size)
        sources[data.get('side', 'arms')] = source
        if stream == 'arm_state':
            sources['pitch'] = age_status(state.get('pitch_received_at'), when, max_age)
    elif stream in ('agv_state_action', 'body_state'):
        sources['column'] = age_status(data.get('height_received_monotonic_s'), when, max_age)
        sources['column']['value_valid'] = finite(data.get('height_m'))
        sources['column']['valid'] &= sources['column']['value_valid']
    elif stream == 'control_cycle':
        sources['pico'] = {'valid': data.get('pico_fresh') is True, 'age_s': None,
                           'reason': 'reported_fresh' if data.get('pico_fresh') is True else 'stale_or_unknown'}
    return {'reference_time_monotonic_s': when, 'max_source_age_s': max_age,
            'sources': sources, 'all_sources_valid': all(v['valid'] for v in sources.values()) if sources else None}


class QualitySummary:
    def __init__(self):
        self.rows = Counter()
        self.invalid = Counter()
        self.max_age = {}
        self.invalid_fields = Counter()

    def observe(self, event):
        stream = event['stream']
        self.rows[stream] += 1
        for source, status in event.get('quality', {}).get('sources', {}).items():
            name = stream + '/' + source
            self.invalid[name] += not status['valid']
            for field in ('dq', 'tau_est'):
                if status.get(field+'_finite') is False:
                    self.invalid_fields[name+'/'+field] += 1
            age = status.get('age_s')
            if finite(age):
                self.max_age[name] = max(age, self.max_age.get(name, age))

    def report(self):
        return {'observed_stream_rows': dict(self.rows),
                'invalid_or_stale_source_rows': dict(self.invalid),
                'max_source_age_s': self.max_age,
                'nonfinite_or_missing_dynamics_rows': dict(self.invalid_fields),
                'joint_dynamics_received': {s: self.rows[s] > 0 for s in ('arm_dynamics', 'hand_dynamics')},
                'base_localization_available': False,
                'camera_exposure_timestamps_available': False,
                'task_outcome': 'unreviewed',
                'note': 'Transport validity is not proof of task success or calibrated physical accuracy.'}
