"""
Deployment pipeline state — reads/writes pipeline_state.json
inside each deployment folder.
"""
import json
import os
from datetime import datetime

STAGES = ['dbd_to_dat', 'dat_to_profile_nc', 'dat_to_trajectory_nc', 'fix_metadata', 'qc', 'report']

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
    state[stage] = {
        'status': status,
        'timestamp': datetime.now().isoformat(timespec='seconds'),
    }
    save_state(deployment_dir, state)


def get_stage_status(deployment_dir, stage):
    return load_state(deployment_dir).get(stage, {}).get('status', STATUS_NOT_RUN)
