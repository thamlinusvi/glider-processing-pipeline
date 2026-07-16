"""
Deployment pipeline state — reads/writes pipeline_state.json
inside each deployment folder.
"""
import json
import os
from datetime import datetime

STAGES = ['dbd_to_dat', 'dbd_to_dat_flight', 'dat_to_profile_nc', 'dat_to_trajectory_nc', 'dat_to_trajectory_flight_nc', 'fix_metadata', 'preflight', 'qc', 'report']

STATUS_NOT_RUN  = 'not_run'
STATUS_RUNNING  = 'running'
STATUS_DONE     = 'done'
STATUS_FAILED   = 'failed'


def _state_path(deployment_dir):
    return os.path.join(deployment_dir, 'pipeline_state.json')


def load_state(deployment_dir):
    path = _state_path(deployment_dir)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {s: {'status': STATUS_NOT_RUN, 'timestamp': None} for s in STAGES}


def save_state(deployment_dir, state):
    os.makedirs(deployment_dir, exist_ok=True)
    with open(_state_path(deployment_dir), 'w') as f:
        json.dump(state, f, indent=2)


def set_stage_status(deployment_dir, stage, status):
    state = load_state(deployment_dir)
    now   = datetime.now().isoformat(timespec='seconds')
    prev  = state.get(stage) or {}
    entry = {'status': status, 'timestamp': now}
    if status == STATUS_RUNNING:
        entry['started_at'] = now
    else:
        if 'started_at' in prev:
            entry['started_at'] = prev['started_at']
        if status in (STATUS_DONE, STATUS_FAILED):
            entry['finished_at'] = now
    state[stage] = entry
    save_state(deployment_dir, state)


def get_stage_status(deployment_dir, stage):
    return load_state(deployment_dir).get(stage, {}).get('status', STATUS_NOT_RUN)
