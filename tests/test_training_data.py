"""Training semantics: causal alignment, true submitted actions, recovery and labels."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from g1d_pico.recording.annotations import create_annotations, load_annotations
from g1d_pico.recording.dataset import snapshot_artifacts, load_dataset_settings, load_calibration
from g1d_pico.recording.export import Index, add_event, export_episode
from g1d_pico.recording.layout import SCHEMA, LEGACY_SCHEMA, Layout
from g1d_pico.recording.quality import event_quality
from g1d_pico.recording.recovery import recover_episode
from g1d_pico.recording.storage import EpisodeStore, RecordingStopped, sha256, verify_episode


class TrainingDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def metadata(self):
        return snapshot_artifacts({'session_id':'session_fixture','instruction':'拿起杯子','scene':'lab209',
            'object_ids':'cup_01','arm_joint_order':[f'arm_{i}' for i in range(14)],
            'arm_config':{},'body_config':{},'recording_started_monotonic_s':100,
            'recording_settings':{'cameras':['head']},'control_command':['--with-arms','--with-hands']},[])

    def store(self, legacy=False):
        metadata=self.metadata()
        if legacy:
            metadata['schema']=LEGACY_SCHEMA
            metadata.pop('_artifacts')
        return EpisodeStore(self.root/'raw',8_000_000,0,metadata)

    def event(self, store, stream, data, t, seq):
        store.event({'stream':stream,'data':data,'producer':'fixture','seq':seq,
            'dropped_total':0,'emitted_monotonic_s':t,'recorder_received_monotonic_s':t+.001})

    def populate(self, store):
        seq=0
        for frame in range(10):
            t=100.025+frame*.05
            store.frame('head',b'\xff\xd8fixture\xff\xd9',t,frame+1)
            records=[('control_cycle',{'armed':True,'homing':False,'pico_fresh':True}),
                ('arm_state',{'state':{'q':[.2]*14,'waist':[.1,.1],'received_at':t,'pitch_received_at':t}}),
                ('arm_output',{'owned':True,'last_write_monotonic_s':t,'output_q':[.4]*14,
                    'requested_q':[.5]*14,'output_tau_ff':[1]*14,'waist_output_q':[.1,.1]}),
                ('agv_state_action',{'mode':'both','height_m':.2,'height_received_monotonic_s':t,
                    'input_fresh':True,'submitted':[.1,.2,.3],'move_result':0,'lift_result':0})]
            for side in ('left','right'):
                records.append(('hand_state',{'side':side,'state':{'q':[.2]*7,'received_at':t,
                    'status':1,'sent_at':t,'sent_q':[.6]*7,'requested_q':[.7]*7}}))
            for stream,data in records:
                seq+=1
                self.event(store,stream,data,t,seq)
        self.event(store,'console',{'text':'任务日志\n'},100.48,seq+1)

    def complete(self, legacy=False):
        store=self.store(legacy)
        self.populate(store)
        store.finish('L3_STOP',extra={'recording_requested_stop_monotonic_s':100.5})
        return store.path

    def labels(self, episode):
        path=create_annotations(episode,self.root/'annotations')
        labels=json.loads(path.read_text())
        labels.update(review_status='accepted',task_success=True,object_ids=['cup_01'],
                      stages=[{'start_s':0,'end_s':.5,'label':'grasp'}])
        path.write_text(json.dumps(labels))
        return path

    def test_v2_paths_snapshots_quality_and_hashes(self):
        path=self.complete()
        manifest=verify_episode(path)
        self.assertEqual(manifest['schema'],SCHEMA)
        for name in ('observations/robot/arms.jsonl','observations/robot/hands.jsonl','observations/operator/pico.jsonl',
                     'actions/arms.jsonl','actions/body.jsonl','observations/images/index.jsonl','logs/console.log',
                     'schema/fields.json','robot/model.urdf','robot/joint_order.json','task/task.json','quality/summary.json'):
            self.assertTrue((path/name).is_file(),name)
        self.assertFalse((path/'telemetry.jsonl').exists())
        meta=json.loads((path/'metadata.json').read_text())
        self.assertNotIn('_artifacts',meta)
        self.assertEqual(sha256(path/'robot/model.urdf'),meta['model_sha256'])
        self.assertFalse(meta['availability']['base_localization'])
        row=json.loads((path/'observations/robot/arms.jsonl').read_text().splitlines()[0])
        self.assertEqual(row['data']['state']['q'],[.2]*14)
        self.assertTrue(row['quality']['all_sources_valid'])
        (path/'robot/model.urdf').write_text('modified')
        with self.assertRaisesRegex(ValueError,'Checksum'): verify_episode(path)

    def test_stale_hand_not_refreshed_by_new_emission_and_no_control_effect(self):
        event={'stream':'hand_state','data':{'side':'left','state':{'received_at':100,'q':[0]*7}},
               'emitted_monotonic_s':101,'recorder_received_monotonic_s':101.01}
        result=event_quality(event)
        self.assertFalse(result['sources']['left']['valid'])
        self.assertGreater(result['sources']['left']['age_s'],1)
        self.assertEqual(event['data']['state']['received_at'],100)
        index=Index(self.root/'index.sqlite')
        try:
            add_event(index,event)
            index.ready()
            self.assertIsNone(index.select('hand_left',101.02,.1)['data'])
        finally: index.close()

    def test_causal_index_excludes_future_or_not_yet_received_observations(self):
        index=Index(self.root/'index.sqlite')
        try:
            index.put('arms',10,10.01,{'q':[1]*14})
            index.put('arms',10.02,10.2,{'q':[2]*14})
            index.put('arms',10.08,10.09,{'q':[3]*14})
            index.put('action_arms',10.06,10.09,{'q':[4]*14})
            index.ready()
            self.assertEqual(index.select('arms',10.05,.1)['data']['q'],[1]*14)
            self.assertEqual(index.select('action_arms',10.05,.1,True)['data']['q'],[4]*14)
            self.assertIsNone(index.select('arms',11,.1)['data'])
        finally: index.close()

    def test_export_measured_q_differs_from_sent_actions_and_labels_stay_outside_raw(self):
        path=self.complete()
        before=sha256(path/'manifest.json')
        labels=self.labels(path)
        result=export_episode(path,self.root/'training',labels,notice=lambda text:None)
        self.assertGreater(result['eligible_samples'],0)
        exported=Path(result['directory'])
        row=json.loads((exported/'data/samples.jsonl').read_text().splitlines()[2])
        self.assertEqual(row['observation']['arms']['data']['q'],[.2]*14)
        self.assertEqual(row['action']['arms']['data']['q'],[.4]*14)
        self.assertEqual(row['action']['hand_left']['data']['q'],[.6]*7)
        self.assertEqual(row['stages'],['grasp'])
        image=row['observation']['image_head']['data']['path']
        self.assertTrue((exported/image).is_file())
        self.assertEqual(sha256(path/'manifest.json'),before)
        verify_episode(path)
        with self.assertRaises(FileExistsError): create_annotations(path,self.root/'annotations')
        with self.assertRaises(FileExistsError): export_episode(path,self.root/'training',labels,notice=lambda t:None)

    def test_unknown_success_is_not_positive_training_label_and_legacy_export_works(self):
        for legacy in (False,True):
            path=self.complete(legacy)
            result=export_episode(path,self.root/('legacy' if legacy else 'training'),notice=lambda t:None)
            self.assertEqual(result['eligible_samples'],0)
            self.assertGreater(result['samples'],0)
            verify_episode(path)

    def test_invalid_annotations_and_wrong_raw_episode_are_rejected(self):
        path=self.complete()
        label_path=self.labels(path)
        labels=json.loads(label_path.read_text())
        labels['stages'][0]['end_s']=9
        label_path.write_text(json.dumps(labels))
        with self.assertRaisesRegex(ValueError,'interval'):
            load_annotations(label_path,path,json.loads((path/'metadata.json').read_text()),verify_episode(path))
        labels['stages']=[]
        labels['source_manifest_sha256']='0'*64
        label_path.write_text(json.dumps(labels))
        with self.assertRaisesRegex(ValueError,'match'):
            export_episode(path,self.root/'training',label_path,notice=lambda t:None)
        self.assertFalse((self.root/'training').exists())
        with self.assertRaisesRegex(ValueError,'outside'): create_annotations(path,path/'labels')

    def test_exclusions_and_missing_hand_do_not_get_filled_with_zero(self):
        path=self.complete()
        label_path=self.labels(path)
        labels=json.loads(label_path.read_text())
        labels['exclude_intervals']=[{'start_s':0,'end_s':.5,'label':'regrasp correction'}]
        label_path.write_text(json.dumps(labels))
        result=export_episode(path,self.root/'training',label_path,notice=lambda t:None)
        self.assertEqual(result['eligible_samples'],0)
        index=Index(self.root/'index.sqlite')
        try:
            index.ready()
            row=index.select('hand_left',100,.1)
            self.assertFalse(row['valid'])
            self.assertIsNone(row['data'])
        finally: index.close()

    def test_nonfinite_optional_dynamics_validity_and_disabled_body_action(self):
        index=Index(self.root/'index.sqlite')
        try:
            add_event(index,{'stream':'hand_dynamics','emitted_monotonic_s':10.01,
                'data':{'side':'left','received_at':10,'q':[0]*7,'dq':[None]*7,'tau_est':[1]*7}})
            add_event(index,{'stream':'agv_state_action','emitted_monotonic_s':10.01,
                'data':{'mode':'lift','submitted':[0,0,.2],'input_fresh':True,'move_result':0,'lift_result':0}})
            index.ready()
            state=index.select('hand_left_dynamics',10.02,.1)['data']
            self.assertFalse(state['fields_valid']['dq'])
            self.assertTrue(state['fields_valid']['tau_est'])
            self.assertNotIn('action_base',index.names)
        finally: index.close()

    def test_v2_recovery_routes_streams_and_keeps_model_snapshot(self):
        store=self.store()
        self.populate(store)
        for handle in store.handles.values(): handle.close()
        model_hash=sha256(store.path/'robot/model.urdf')
        raw_hash=sha256(store.path/'observations/robot/arms.jsonl')
        report=recover_episode(store.path,self.root/'recovered',notice=lambda t:None)
        destination=Path(report['recovered_episode'])
        verify_episode(destination)
        self.assertTrue((store.path/'IN_PROGRESS').exists())
        self.assertEqual(sha256(destination/'robot/model.urdf'),model_hash)
        self.assertEqual(sha256(destination/'observations/robot/arms.jsonl'),raw_hash)
        self.assertEqual(report['sequence_gaps'],{})

    def test_quality_finalizes_even_when_quota_already_stopped_recording(self):
        store=self.store()
        self.populate(store)
        store.stop('CACHE_LIMIT')
        with self.assertRaises(RecordingStopped): store.write('extra',b'data')
        store.finish()
        manifest=verify_episode(store.path)
        self.assertEqual(manifest['recording_stop_reason'],'CACHE_LIMIT')
        self.assertIn('quality/summary.json',manifest['files'])

    def test_fixed_configs_load_and_invalid_calibration_does_not_invent_identity(self):
        self.assertEqual(load_dataset_settings()['export_fps'],20)
        calibration=load_calibration()
        self.assertIsNone(calibration['cameras']['head_left']['K'])
        calibration['cameras']['head_left']['T_parent_camera']=[[0]*4]*4
        path=self.root/'bad.json'
        path.write_text(json.dumps(calibration))
        with self.assertRaisesRegex(ValueError,'homogeneous'): load_calibration(path)


if __name__=='__main__': unittest.main()
