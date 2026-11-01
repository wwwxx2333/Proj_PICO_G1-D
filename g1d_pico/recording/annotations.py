"""Editable host annotations live OUTSIDE immutable, checksum-verified episodes."""
import json
from pathlib import Path

from .quality import finite
from .storage import pretty, sha256, verify_episode

LABEL_SCHEMA = 'g1d-lab-labels-v1'
OUTCOMES = ('task_success', 'navigation_success', 'grasp_success', 'placement_success')


def time_bounds(metadata, manifest):
    start = metadata.get('recording_started_monotonic_s', metadata.get('clock_anchor', {}).get('pc2_monotonic_s'))
    if 'recovery' in manifest:
        end = manifest['recovery'].get('last_source_monotonic_s')
        if not finite(start) or not finite(end) or end < start:
            raise ValueError('Recovered episode has no trustworthy source time interval')
        return start, end
    end = manifest.get('recording_requested_stop_monotonic_s')
    if not finite(end) or not finite(start) or end < start:
        end = manifest.get('recording_stopped_monotonic_s')
    if not finite(start) or not finite(end) or end < start:
        raise ValueError('Episode has no usable monotonic recording interval')
    return start, end


def template(episode, metadata, manifest):
    start, end = time_bounds(metadata, manifest)
    return {'schema': LABEL_SCHEMA, 'episode_id': episode.name,
            'source_manifest_sha256': sha256(episode/'manifest.json'),
            'time_origin': 'seconds from metadata.recording_started_monotonic_s (v1 fallback: clock_anchor)',
            'duration_s': end-start, 'review_status': 'unreviewed',
            **dict.fromkeys(OUTCOMES),
            'instruction': metadata.get('instruction', ''), 'object_ids': [],
            'task_id': metadata.get('dataset_settings', {}).get('task_id'),
            'route_id': metadata.get('dataset_settings', {}).get('route_id'),
            'stages': [], 'interventions': [], 'exclude_intervals': [], 'notes': ''}


def create_annotations(episode, output_root):
    episode = Path(episode).resolve()
    manifest = verify_episode(episode)
    root = Path(output_root).expanduser().resolve()
    if root.is_relative_to(episode):
        raise ValueError('Annotations must be outside the immutable episode')
    metadata = json.loads((episode/'metadata.json').read_text())
    value = template(episode, metadata, manifest)
    destination = root/episode.name/'labels.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never silently overwrite work done by an annotator.
    with destination.open('xb') as handle:
        handle.write(pretty(value))
    return destination


def load_annotations(path, episode, metadata, manifest):
    expected = template(episode, metadata, manifest)
    if path is None:
        return expected
    path = Path(path)
    if path.stat().st_size > 2*1024*1024:
        raise ValueError('Oversized annotations')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('Labels have missing or unknown fields')
    for key in ('schema', 'episode_id', 'source_manifest_sha256', 'time_origin', 'duration_s'):
        if value[key] != expected[key]:
            raise ValueError(f'Labels do not match this raw episode: {key}')
    if value['review_status'] not in ('unreviewed', 'accepted', 'rejected'):
        raise ValueError('review_status must be unreviewed, accepted or rejected')
    for key in OUTCOMES:
        if value[key] is not None and type(value[key]) is not bool:
            raise ValueError(f'{key} must be true, false or null')
    for key in ('instruction', 'notes'):
        if not isinstance(value[key], str):
            raise ValueError(f'{key} must be text')
    for key in ('task_id','route_id'):
        if value[key] is not None and (not isinstance(value[key],str) or not value[key].strip()):
            raise ValueError(f'{key} must be null or nonempty text')
    if not isinstance(value['object_ids'],list) or any(not isinstance(v,str) or not v for v in value['object_ids']):
        raise ValueError('object_ids must be a list of object identifiers')
    for key in ('stages','interventions','exclude_intervals'):
        if not isinstance(value[key],list):
            raise ValueError(f'{key} must be a list')
        previous_end = 0
        for interval in value[key]:
            if not isinstance(interval,dict) or set(interval) != {'start_s','end_s','label'}:
                raise ValueError(f'{key} entries require start_s, end_s and label')
            begin, end = interval['start_s'], interval['end_s']
            if (not finite(begin) or not finite(end) or not 0 <= begin < end <= expected['duration_s']
                    or not isinstance(interval['label'],str) or not interval['label'].strip()):
                raise ValueError(f'Invalid {key} interval')
            if key == 'stages' and begin < previous_end:
                raise ValueError('Stages must be ordered and nonoverlapping')
            previous_end = end
    return value


def active_intervals(labels, key, when):
    return [v['label'] for v in labels[key] if v['start_s'] <= when < v['end_s']]
