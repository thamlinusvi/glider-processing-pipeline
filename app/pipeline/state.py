"""
Deployment pipeline state — reads/writes pipeline_state.json
inside each deployment folder.
"""
import json
import os
import tempfile
import threading
from datetime import datetime

STAGES = ['dbd_to_dat', 'dbd_to_dat_flight', 'dat_to_profile_nc', 'dat_to_trajectory_nc', 'dat_to_trajectory_flight_nc', 'fix_metadata', 'preflight', 'qc', 'report']

STATUS_NOT_RUN      = 'not_run'
STATUS_RUNNING      = 'running'
STATUS_DONE         = 'done'
STATUS_FAILED       = 'failed'
STATUS_NEEDS_REVIEW = 'needs_review'

# Guards the load -> mutate -> save cycle in set_stage_status against concurrent
# stage runs for the same deployment (e.g. dbd_to_dat / dbd_to_dat_flight, or
# dat_to_trajectory_nc / dat_to_trajectory_flight_nc, dispatched close together --
# nothing in the UI or run_stage() prevents this). In-process only: this does not
# protect against the Electron server and the standalone PyQt5 app writing the same
# pipeline_state.json concurrently from two separate processes.
_state_lock = threading.Lock()


def _state_path(deployment_dir):
    return os.path.join(deployment_dir, 'pipeline_state.json')


def load_state(deployment_dir):
    path = _state_path(deployment_dir)
    if os.path.exists(path):
        with open(path, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as e:
                print(f'[pipeline.state] Warning: {path} is corrupt ({e}); '
                      f'treating as not_run. Repair or delete the file to clear this.')
    return {s: {'status': STATUS_NOT_RUN, 'timestamp': None} for s in STAGES}


def save_state(deployment_dir, state):
    os.makedirs(deployment_dir, exist_ok=True)
    path = _state_path(deployment_dir)
    # Write to a temp file in the same directory, then atomically replace the
    # real file -- a crash or concurrent read mid-write can never observe a
    # truncated/partial pipeline_state.json this way.
    fd, tmp_path = tempfile.mkstemp(dir=deployment_dir, prefix='.pipeline_state_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def set_stage_status(deployment_dir, stage, status):
    with _state_lock:
        state = load_state(deployment_dir)
        now   = datetime.now().isoformat(timespec='seconds')
        prev  = state.get(stage) or {}
        entry = {'status': status, 'timestamp': now}
        if status == STATUS_RUNNING:
            entry['started_at'] = now
        else:
            if 'started_at' in prev:
                entry['started_at'] = prev['started_at']
            if status in (STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_REVIEW):
                entry['finished_at'] = now
        state[stage] = entry
        save_state(deployment_dir, state)


def get_stage_status(deployment_dir, stage):
    return load_state(deployment_dir).get(stage, {}).get('status', STATUS_NOT_RUN)
