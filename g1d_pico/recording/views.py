"""Readable derived files made in the disk writer, never in the control loop.

Original source events remain unchanged. Every view carries its source identity;
its seq is the original source seq, not another packet from the producer.
"""


def derived_views(event):
    stream=event['stream']
    data=event['data']
    source={'stream':stream,'producer':event.get('producer'),'seq':event.get('seq')}
    envelope={k:v for k,v in event.items() if k not in ('stream','data','quality')}
    if stream=='hand_state':
        state=data.get('state',{})
        yield {**envelope,'stream':'hand_output','derived_from':source,'data':{
            'side':data.get('side'),'status':state.get('status'),
            'last_write_monotonic_s':state.get('sent_at'),
            'output_q':state.get('sent_q'),'requested_q':state.get('requested_q'),
            'blocked_mask':state.get('blocked_mask'),'opening_mask':state.get('opening_mask'),
            'limited_mask':state.get('limited_mask')}}
    elif stream=='agv_state_action':
        yield {**envelope,'stream':'body_state','derived_from':source,'data':{
            'height_m':data.get('height_m'),
            'height_received_monotonic_s':data.get('height_received_monotonic_s'),
            'height_fresh':data.get('height_fresh'),
            'base_world_pose':None,'base_measured_velocity':None}}
