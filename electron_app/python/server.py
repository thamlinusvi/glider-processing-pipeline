"""
UVI Glider Pipeline — Flask API backend for the Electron app.

Wraps the existing pipeline/stages.py and pipeline/state.py without any
modification to those files.  Streams subprocess output as SSE so the
Electron renderer can show live Docker logs.

Usage (called by Electron main.js):
    python server.py <base_dir> <port>
"""
import sys
import os
import json
import glob
import re
import shutil
import subprocess
import threading

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.normpath(sys.argv[1]) if len(sys.argv) > 1 else \
           os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
PORT     = int(sys.argv[2]) if len(sys.argv) > 2 else 5757

# Add the existing app/ directory so we can reuse stages + state unchanged
APP_DIR = os.path.join(BASE_DIR, 'app')
sys.path.insert(0, APP_DIR)

from flask import Flask, jsonify, request, Response, send_file, abort

from pipeline.stages import STAGE_INFO
from pipeline.state  import (
    STAGES, load_state, set_stage_status,
    STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_NOT_RUN,
)

app = Flask(__name__)

# Active subprocesses: {(deployment, stage): Popen}
_procs = {}
_lock  = threading.Lock()


# ── CORS (no external package needed) ────────────────────────────────────────
@app.after_request
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin']  = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def _options(_path=''):
    return '', 204


# ── Helpers ───────────────────────────────────────────────────────────────────
def _dep_dir(deployment):
    year = deployment.split('-')[1][:4] if '-' in deployment else '2025'
    d = os.path.join(BASE_DIR, 'deployments', year, deployment)
    os.makedirs(d, exist_ok=True)
    return d


def _find_deployments():
    incoming = os.path.join(BASE_DIR, 'incoming_data')
    if not os.path.isdir(incoming):
        return []
    return sorted(
        n for n in os.listdir(incoming)
        if os.path.isdir(os.path.join(incoming, n))
    )


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})


@app.route('/api/state')
def api_state():
    """App-level state: deployments list + stage definitions."""
    stage_meta = {
        k: {
            'label':       v['label'],
            'description': v['description'],
            'manual':      v.get('manual', False),
            'manual_note': v.get('manual_note', ''),
        }
        for k, v in STAGE_INFO.items()
    }
    return jsonify({
        'base_dir':   BASE_DIR,
        'deployments': _find_deployments(),
        'stage_info': stage_meta,
        'stages':     STAGES,
    })


@app.route('/api/deployment/<dep>/state')
def dep_state(dep):
    dep_dir  = _dep_dir(dep)
    state    = load_state(dep_dir)
    nc_dir   = os.path.join(dep_dir, 'data', 'out', 'nc', 'sci-profile', 'delayed')
    nc_count = len(glob.glob(os.path.join(nc_dir, '*.nc'))) if os.path.isdir(nc_dir) else 0
    return jsonify({'state': state, 'nc_count': nc_count, 'nc_dir': nc_dir})


@app.route('/api/deployment/<dep>/set-status/<stage>', methods=['POST'])
def set_status(dep, stage):
    status = (request.json or {}).get('status', STATUS_NOT_RUN)
    set_stage_status(_dep_dir(dep), stage, status)
    return jsonify({'ok': True})


@app.route('/api/deployment/<dep>/run/<stage>', methods=['POST'])
def run_stage(dep, stage):
    """Start a stage process; stream stdout as SSE."""
    if stage not in STAGE_INFO:
        return abort(404)
    info = STAGE_INFO[stage]
    if info.get('manual'):
        return abort(400, 'Manual stage — use set-status instead')

    cmd, cwd, env = info['command_fn'](BASE_DIR, dep)
    dep_dir = _dep_dir(dep)
    set_stage_status(dep_dir, stage, STATUS_RUNNING)

    def _generate():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            key = (dep, stage)
            with _lock:
                _procs[key] = proc

            for raw in proc.stdout:
                line = raw.rstrip()
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"

            proc.wait()
            with _lock:
                _procs.pop(key, None)

            ok     = (proc.returncode == 0)
            status = STATUS_DONE if ok else STATUS_FAILED
            set_stage_status(dep_dir, stage, status)
            yield f"data: {json.dumps({'type': 'done', 'success': ok, 'code': proc.returncode})}\n\n"

        except Exception as exc:
            set_stage_status(dep_dir, stage, STATUS_FAILED)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/deployment/<dep>/abort/<stage>', methods=['POST'])
def abort_stage(dep, stage):
    key = (dep, stage)
    with _lock:
        proc = _procs.get(key)
    killed = False
    if proc:
        proc.terminate()
        killed = True
    set_stage_status(_dep_dir(dep), stage, STATUS_FAILED)
    return jsonify({'ok': True, 'killed': killed})


# ── File serving ──────────────────────────────────────────────────────────────
@app.route('/api/deployment/<dep>/report')
def serve_report(dep):
    dep_dir = _dep_dir(dep)
    path    = os.path.join(dep_dir, 'reports', f'qc_report_{dep}.html')
    if not os.path.isfile(path):
        return '<h2 style="font-family:sans-serif;color:#888">Report not yet generated.</h2>', 404
    return send_file(path)


@app.route('/api/file')
def serve_file():
    """Serve any file within BASE_DIR (used for plot images)."""
    fpath = request.args.get('path', '')
    real  = os.path.realpath(fpath)
    if not real.startswith(os.path.realpath(BASE_DIR)):
        return abort(403)
    if not os.path.isfile(real):
        return abort(404)
    return send_file(real)


@app.route('/api/deployment/<dep>/plots')
def list_plots(dep):
    dep_dir     = _dep_dir(dep)
    figs_dir    = os.path.join(dep_dir, 'figs')
    pngs        = sorted(glob.glob(os.path.join(figs_dir, '*.png')))
    prof_pngs   = sorted(glob.glob(os.path.join(figs_dir, 'profiles', '*.png')))
    return jsonify({'pngs': pngs, 'profile_pngs': prof_pngs[:20], 'figs_dir': figs_dir})


@app.route('/api/deployment/<dep>/dat-files')
def dat_files(dep):
    dat_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'merged_data')
    files   = sorted(os.listdir(dat_dir)) if os.path.isdir(dat_dir) else []
    return jsonify({'files': files, 'dir': dat_dir})


@app.route('/api/deployment/<dep>/dat-sensors')
def dat_sensors(dep):
    """
    Scan all DAT files in merged_data and return the union of sensor names.
    Only reads the first ~20 lines of each file (the ASCII header), so it's
    fast even for large datasets.
    """
    dat_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'merged_data')
    if not os.path.isdir(dat_dir):
        return jsonify({'sensors': [], 'files_scanned': 0, 'error': 'No dat_files directory found'})

    all_sensors = set()
    files_scanned = 0
    errors = []

    for fname in sorted(os.listdir(dat_dir)):
        if not fname.endswith('.dat'):
            continue
        fpath = os.path.join(dat_dir, fname)
        try:
            num_tags = 14  # Slocum default; overridden by header value
            with open(fpath, 'r', errors='replace') as f:
                for i, raw in enumerate(f):
                    line = raw.strip()
                    # Pick up num_ascii_tags if present
                    if line.startswith('num_ascii_tags:'):
                        try:
                            num_tags = int(line.split(':', 1)[1].strip())
                        except ValueError:
                            pass
                    # Sensor names line immediately follows the header block
                    if i == num_tags:
                        names = line.split()
                        all_sensors.update(names)
                        break
                    # Safety: stop if we've read far past a normal header
                    if i > 30:
                        break
            files_scanned += 1
        except Exception as exc:
            errors.append(f'{fname}: {exc}')

    return jsonify({
        'sensors':       sorted(all_sensors),
        'files_scanned': files_scanned,
        'errors':        errors[:5],  # cap error list
    })


@app.route('/api/deployment/<dep>/metadata')
def metadata(dep):
    glider     = dep.split('-')[0]
    config_dir = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep)
    files      = {}
    for f in glob.glob(os.path.join(config_dir, '**', '*.json'), recursive=True):
        rel = os.path.relpath(f, config_dir)
        try:
            with open(f) as fp:
                files[rel] = json.load(fp)
        except Exception:
            files[rel] = None
    return jsonify({'files': files, 'config_dir': config_dir})


@app.route('/api/deployment/<dep>/metadata/save', methods=['POST'])
def save_metadata(dep):
    data     = request.json or {}
    rel_path = data.get('path', '').replace('/', os.sep).replace('\\', os.sep)
    content  = data.get('content', '')

    # Validate JSON before writing
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Invalid JSON: {e}'}), 400

    glider     = dep.split('-')[0]
    config_dir = os.path.realpath(os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep))
    full_path  = os.path.realpath(os.path.join(config_dir, rel_path))

    # Security: must stay within config_dir
    if not full_path.startswith(config_dir):
        return jsonify({'error': 'Invalid path'}), 403
    if not full_path.endswith('.json'):
        return jsonify({'error': 'Only .json files allowed'}), 403

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w') as fp:
        fp.write(content)

    return jsonify({'ok': True})


# ── Metadata bulk operations ─────────────────────────────────────────────────
@app.route('/api/deployment/<dep>/metadata/copy-all', methods=['POST'])
def copy_all_metadata(dep):
    """Copy every JSON file from another deployment's config dir into this one."""
    data   = request.json or {}
    source = data.get('source', '').strip()
    if not source:
        return jsonify({'error': 'No source deployment provided'}), 400

    glider     = dep.split('-')[0]
    src_glider = source.split('-')[0]
    config_dir = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep)
    src_dir    = os.path.join(BASE_DIR, 'glider_config_files_json', src_glider, source)

    if not os.path.isdir(src_dir):
        return jsonify({'error': f'Source config not found: {src_dir}'}), 404

    copied = []
    for f in glob.glob(os.path.join(src_dir, '**', '*.json'), recursive=True):
        rel = os.path.relpath(f, src_dir)
        dst = os.path.join(config_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(f, dst)
        copied.append(rel)

    return jsonify({'ok': True, 'copied': copied})


@app.route('/api/deployment/<dep>/metadata/import-files', methods=['POST'])
def import_metadata_files(dep):
    """
    Copy JSON files from absolute disk paths into the deployment's config dir.
    Body: {"files": [{"path": "/abs/path/file.json", "subdir": "science_profile"}, ...]}
    """
    data       = request.json or {}
    files      = data.get('files', [])
    glider     = dep.split('-')[0]
    config_dir = os.path.realpath(os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep))

    results = []
    for item in files:
        src    = os.path.normpath(item.get('path', ''))
        subdir = item.get('subdir', 'science_profile')
        name   = os.path.basename(src)

        if not name.lower().endswith('.json'):
            results.append({'path': src, 'ok': False, 'error': 'Only .json files allowed'})
            continue
        if not os.path.isfile(src):
            results.append({'path': src, 'ok': False, 'error': 'File not found'})
            continue

        dst_dir = os.path.realpath(os.path.join(config_dir, subdir))
        if not dst_dir.startswith(config_dir):
            results.append({'path': src, 'ok': False, 'error': 'Invalid subdir'})
            continue

        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name)
        try:
            shutil.copy2(src, dst)
            results.append({'path': src, 'ok': True, 'name': name, 'dest': dst})
        except Exception as exc:
            results.append({'path': src, 'ok': False, 'error': str(exc)})

    return jsonify({'results': results})


# ── Sensor Sets ───────────────────────────────────────────────────────────────
_RAW_JSON_DIR      = os.path.join(BASE_DIR, 'glider_config_files_json', 'raw_json')
_SENSOR_SETS_CFG   = os.path.join(BASE_DIR, 'glider_config_files_json', 'sensor_sets.json')


def _load_raw_sensor_universe():
    """Return {name: {source, long_name, units}} for all sensors in raw_json."""
    base_path = os.path.join(_RAW_JSON_DIR, 'trajectory-sensor_defs.json')
    sci_path  = os.path.join(_RAW_JSON_DIR, 'sci-sensor_defs.json')
    sensors   = {}
    for src_label, path in [('base', base_path), ('sci', sci_path)]:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            defs = json.load(f)
        for name, body in defs.items():
            if name not in sensors:
                attrs = body.get('attrs', {}) if isinstance(body, dict) else {}
                sensors[name] = {
                    'source':    src_label,
                    'long_name': attrs.get('long_name', ''),
                    'units':     attrs.get('units', ''),
                }
    return sensors


def _current_sensor_sets():
    """Return {'profile': [...], 'trajectory': [...]} — from JSON config or script defaults."""
    if os.path.isfile(_SENSOR_SETS_CFG):
        with open(_SENSOR_SETS_CFG) as f:
            cfg = json.load(f)
        return {
            'profile':    sorted(cfg.get('profile', [])),
            'trajectory': sorted(cfg.get('trajectory', [])),
        }
    # Fall back to build_sensor_defs.py hardcoded defaults
    try:
        import importlib.util
        bsd_path = os.path.join(BASE_DIR, 'build_sensor_defs.py')
        spec = importlib.util.spec_from_file_location('bsd', bsd_path)
        bsd  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bsd)
        return {
            'profile':    sorted(bsd.PROFILE_SENSORS),
            'trajectory': sorted(bsd.TRAJECTORY_SENSORS),
        }
    except Exception:
        return {'profile': [], 'trajectory': []}


@app.route('/api/sensor-sets')
def get_sensor_sets():
    sets    = _current_sensor_sets()
    sensors = _load_raw_sensor_universe()
    return jsonify({
        'profile':    sets['profile'],
        'trajectory': sets['trajectory'],
        'all_sensors': sensors,
    })


@app.route('/api/sensor-sets', methods=['POST'])
def save_sensor_sets():
    data    = request.json or {}
    profile = sorted(set(data.get('profile', [])))
    traj    = sorted(set(data.get('trajectory', [])))
    cfg     = {'profile': profile, 'trajectory': traj}
    with open(_SENSOR_SETS_CFG, 'w') as f:
        json.dump(cfg, f, indent=2)
    return jsonify({'ok': True, 'profile_count': len(profile), 'traj_count': len(traj)})


# ── Build Sensor Defs ─────────────────────────────────────────────────────────
@app.route('/api/build-sensor-defs', methods=['POST'])
def build_sensor_defs():
    script = os.path.join(BASE_DIR, 'build_sensor_defs.py')
    if not os.path.isfile(script):
        return jsonify({'error': 'build_sensor_defs.py not found'}), 404
    try:
        result = subprocess.run(
            [sys.executable, script],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=30
        )
        return jsonify({
            'ok':     result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/deployment/<dep>/copy-sensor-defs', methods=['POST'])
def copy_sensor_defs(dep):
    glider   = dep.split('-')[0]
    src_root = os.path.join(BASE_DIR, 'glider_config_files_json', 'updated_sensor_defs')
    dst_root = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep)
    mapping  = {
        'profile':    'science_profile',
        'trajectory': 'science_trajectory',
    }
    copied = []
    for src_sub, dst_sub in mapping.items():
        src = os.path.join(src_root, src_sub, 'sensor_defs.json')
        dst = os.path.join(dst_root, dst_sub, 'sensor_defs.json')
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst_sub + '/sensor_defs.json')
    return jsonify({'ok': True, 'copied': copied})


# ── Import Raw Files ──────────────────────────────────────────────────────────
_ALLOWED_RAW_EXT = {'.dbd', '.ebd', '.sbd', '.tbd', '.mbd', '.nbd'}

@app.route('/api/deployment/<dep>/import-raw', methods=['POST'])
def import_raw(dep):
    """
    Copy raw glider binary files into incoming_data/<dep>/.
    Expects JSON body: {"paths": ["/abs/path/to/file.dbd", ...]}
    Returns per-file results so the UI can show status.
    """
    data  = request.json or {}
    paths = data.get('paths', [])
    dest  = os.path.join(BASE_DIR, 'incoming_data', dep)

    if not os.path.isdir(dest):
        return jsonify({'error': f'Destination not found: {dest}'}), 404

    results = []
    for src in paths:
        src  = os.path.normpath(src)
        name = os.path.basename(src)
        ext  = os.path.splitext(name)[1].lower()

        if ext not in _ALLOWED_RAW_EXT:
            results.append({'path': src, 'ok': False, 'error': f'Extension {ext!r} not allowed'})
            continue
        if not os.path.isfile(src):
            results.append({'path': src, 'ok': False, 'error': 'File not found'})
            continue

        dst = os.path.join(dest, name)
        try:
            shutil.copy2(src, dst)
            results.append({'path': src, 'ok': True, 'name': name, 'dest': dst})
        except Exception as exc:
            results.append({'path': src, 'ok': False, 'error': str(exc)})

    return jsonify({'results': results})


# ── New Deployment ────────────────────────────────────────────────────────────
@app.route('/api/deployment/create', methods=['POST'])
def create_deployment():
    data     = request.json or {}
    name     = data.get('name', '').strip()
    template = data.get('template', '').strip()

    if not name:
        return jsonify({'error': 'No deployment name provided'}), 400
    if not re.match(r'^[\w]+-\d{8}T\d{4}$', name):
        return jsonify({'error': 'Invalid format — expected glider-YYYYMMDDThhmm'}), 400

    glider = name.split('-')[0]

    # Check it doesn't already exist
    if os.path.isdir(os.path.join(BASE_DIR, 'incoming_data', name)):
        return jsonify({'error': f'Deployment {name!r} already exists'}), 409

    # Create all required directories
    dirs = [
        os.path.join(BASE_DIR, 'incoming_data', name),
        os.path.join(BASE_DIR, 'dat_files', name, 'merged_data'),
        os.path.join(BASE_DIR, 'dat_files', name, 'flight_data'),
        os.path.join(BASE_DIR, 'glider_config_files_json', glider, name, 'science_profile'),
        os.path.join(BASE_DIR, 'glider_config_files_json', glider, name, 'science_trajectory'),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    # Copy JSON config files from template deployment
    if template and template != name:
        tmpl_glider = template.split('-')[0]
        for sub in ['science_profile', 'science_trajectory']:
            src = os.path.join(BASE_DIR, 'glider_config_files_json', tmpl_glider, template, sub)
            dst = os.path.join(BASE_DIR, 'glider_config_files_json', glider, name, sub)
            if os.path.isdir(src):
                for f in os.listdir(src):
                    if f.endswith('.json'):
                        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))

    return jsonify({'ok': True, 'deployment': name})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f'[server] base_dir = {BASE_DIR}', flush=True)
    print(f'[server] listening on 127.0.0.1:{PORT}', flush=True)
    app.run(host='127.0.0.1', port=PORT, threaded=True, use_reloader=False)
