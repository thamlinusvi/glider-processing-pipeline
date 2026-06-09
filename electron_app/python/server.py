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
import datetime

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

# Live log buffer for reconnect — populated line-by-line while any stage runs.
# Key: (dep, stage)  Value: list of log lines (strings)
_live_logs  = {}
# True while the process is still producing output; False once it's done/failed.
_live_done  = {}   # key -> bool (True = finished)


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


def _save_run_log(dep, stage, start_ts, lines, success):
    """Write buffered subprocess output to logs/<dep>/<stage>/pass|fail/<ts>.log."""
    result  = 'pass' if success else 'fail'
    log_dir = os.path.join(BASE_DIR, 'logs', dep, stage, result)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f'{start_ts}.log')
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        print(f'[server] Warning: could not save run log: {e}', flush=True)


def _container_name(dep, stage):
    """Stable Docker container name for a (deployment, stage) pair."""
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '-', f'glider-{dep}-{stage}')
    return safe[:128]   # Docker name limit


@app.route('/api/deployment/<dep>/run/<stage>', methods=['POST'])
def run_stage(dep, stage):
    """Start a stage process; stream stdout as SSE."""
    if stage not in STAGE_INFO:
        return abort(404)
    info = STAGE_INFO[stage]
    if info.get('manual'):
        return abort(400, 'Manual stage — use set-status instead')

    key = (dep, stage)
    with _lock:
        if key in _procs:
            return abort(409, f'Stage {stage} is already running for {dep}')

    # For the report stage, read optional config params from the request body
    cmd_kwargs = {}
    if stage == 'report':
        body = request.get_json(silent=True) or {}
        try:
            cmd_kwargs['min_depth'] = float(body.get('min_depth', 10))
        except (TypeError, ValueError):
            cmd_kwargs['min_depth'] = 10.0
        try:
            cmd_kwargs['nprofiles'] = int(body.get('nprofiles', 20))
        except (TypeError, ValueError):
            cmd_kwargs['nprofiles'] = 20

    cmd, cwd, env = info['command_fn'](BASE_DIR, dep, **cmd_kwargs)
    dep_dir = _dep_dir(dep)

    # For Docker commands, inject --name so we can kill the container on abort
    cname = _container_name(dep, stage)
    if cmd and cmd[0] == 'docker' and 'run' in cmd:
        # Insert --name <cname> right after 'docker run'
        run_idx = cmd.index('run')
        cmd = cmd[:run_idx + 1] + ['--name', cname] + cmd[run_idx + 1:]
        # Remove any existing container with that name (leftover from a prior aborted run)
        subprocess.run(['docker', 'rm', '-f', cname],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        cname = None

    set_stage_status(dep_dir, stage, STATUS_RUNNING)

    # Initialise live-log buffer for this run so reconnect can start immediately
    _live_logs[key] = []
    _live_done[key] = False

    def _generate():
        log_lines = []
        start_ts  = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
            with _lock:
                _procs[key] = proc

            for raw in proc.stdout:
                line = raw.rstrip()
                log_lines.append(line)
                _live_logs.setdefault(key, []).append(line)
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"

            proc.wait()
            with _lock:
                _procs.pop(key, None)

            ok     = (proc.returncode == 0)
            status = STATUS_DONE if ok else STATUS_FAILED
            set_stage_status(dep_dir, stage, status)
            _save_run_log(dep, stage, start_ts, log_lines, ok)
            _live_done[key] = True
            yield f"data: {json.dumps({'type': 'done', 'success': ok, 'code': proc.returncode})}\n\n"

        except Exception as exc:
            set_stage_status(dep_dir, stage, STATUS_FAILED)
            _save_run_log(dep, stage, start_ts, log_lines, False)
            _live_done[key] = True
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            with _lock:
                _procs.pop(key, None)

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/deployment/<dep>/abort/<stage>', methods=['POST'])
def abort_stage(dep, stage):
    key = (dep, stage)
    with _lock:
        proc = _procs.pop(key, None)
    killed = False
    if proc:
        proc.terminate()
        killed = True
    # Also kill the Docker container by name (terminate() only kills 'docker run',
    # not the container itself)
    cname = _container_name(dep, stage)
    subprocess.run(['docker', 'kill', cname],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    set_stage_status(_dep_dir(dep), stage, STATUS_FAILED)
    return jsonify({'ok': True, 'killed': killed})


@app.route('/api/deployment/<dep>/stage-logs/<stage>')
def stream_stage_logs(dep, stage):
    """Reconnect to a running stage's log stream.

    For Docker stages (Stage 4 QC): attaches via `docker logs -f`.
    For all other stages: replays the in-memory live-log buffer then tails
    new lines as they arrive, so reconnect works after any page refresh.
    """
    key     = (dep, stage)
    dep_dir = _dep_dir(dep)

    # ── Docker path (Stage 4 QC) ─────────────────────────────────────────────
    cname = _container_name(dep, stage)
    check = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Running}}', cname],
        capture_output=True, text=True)
    if check.stdout.strip() == 'true':
        def _docker_generate():
            proc = subprocess.Popen(
                ['docker', 'logs', '-f', cname],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            try:
                for raw in proc.stdout:
                    yield f"data: {json.dumps({'type': 'log', 'line': raw.rstrip()})}\n\n"
                proc.wait()
                ok = (proc.returncode == 0)
                state_now = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.ExitCode}}', cname],
                    capture_output=True, text=True).stdout.strip()
                if state_now != '':
                    set_stage_status(dep_dir, stage, STATUS_DONE if ok else STATUS_FAILED)
                yield f"data: {json.dumps({'type': 'done', 'success': ok})}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        return Response(_docker_generate(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    # ── Python-subprocess path (all other stages) ────────────────────────────
    # If we have no record of this run at all, return 404 so the frontend
    # falls back to polling the status endpoint.
    if key not in _live_logs:
        return jsonify({'error': 'No active process found for this stage'}), 404

    import time

    def _buffer_generate():
        # 1. Replay all lines buffered so far
        cursor = 0
        snapshot = list(_live_logs.get(key, []))
        for line in snapshot:
            yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
        cursor = len(snapshot)

        # 2. Tail new lines until the process finishes
        while not _live_done.get(key, True):
            current = _live_logs.get(key, [])
            new_lines = current[cursor:]
            for line in new_lines:
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
            cursor += len(new_lines)
            if not new_lines:
                time.sleep(0.15)

        # 3. Flush any final lines written between last poll and done flag
        current = _live_logs.get(key, [])
        for line in current[cursor:]:
            yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"

        # 4. Send final done event based on persisted status
        state = load_state(dep_dir)
        st    = (state.get(stage) or {}).get('status', 'done')
        ok    = (st == STATUS_DONE)
        yield f"data: {json.dumps({'type': 'done', 'success': ok})}\n\n"

        # Clean up buffer — keep memory tidy
        _live_logs.pop(key, None)
        _live_done.pop(key, None)

    return Response(_buffer_generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Pre-flight config apply ───────────────────────────────────────────────────
@app.route('/api/deployment/<dep>/preflight/apply', methods=['POST'])
def preflight_apply(dep):
    """
    Write QC config files for a deployment based on the JSON produced by
    the analysis phase of pre_qaqc_config_check.py.
    Body: the parsed PREFLIGHT_JSON object from the analysis run.
    """
    data   = request.json or {}
    script = os.path.join(BASE_DIR, 'app', 'pre_qaqc_config_check.py')
    if not os.path.isfile(script):
        return jsonify({'error': 'pre_qaqc_config_check.py not found'}), 404
    dep_dir = _dep_dir(dep)
    try:
        result = subprocess.run(
            [sys.executable, script, BASE_DIR, dep, '--apply', json.dumps(data)],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
        )
        ok = (result.returncode == 0)
        if ok:
            set_stage_status(dep_dir, 'preflight', STATUS_DONE)
        return jsonify({
            'ok':     ok,
            'output': result.stdout,
            'error':  result.stderr if not ok else '',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Run logs ──────────────────────────────────────────────────────────────────
@app.route('/api/logs')
def list_run_logs():
    """Return all saved run logs as a flat list sorted newest-first."""
    logs_root = os.path.join(BASE_DIR, 'logs')
    entries   = []
    if not os.path.isdir(logs_root):
        return jsonify({'logs': []})

    for dep in sorted(os.listdir(logs_root)):
        dep_path = os.path.join(logs_root, dep)
        if not os.path.isdir(dep_path):
            continue
        for stage in sorted(os.listdir(dep_path)):
            stage_path = os.path.join(dep_path, stage)
            if not os.path.isdir(stage_path):
                continue
            for result in ('pass', 'fail'):
                result_path = os.path.join(stage_path, result)
                if not os.path.isdir(result_path):
                    continue
                for fname in sorted(os.listdir(result_path), reverse=True):
                    if not fname.endswith('.log'):
                        continue
                    fpath = os.path.join(result_path, fname)
                    entries.append({
                        'dep':       dep,
                        'stage':     stage,
                        'result':    result,
                        'filename':  fname,
                        'path':      fpath,
                        'timestamp': fname[:-4].replace('_', ' ', 1),  # 2026-06-07 143022
                        'size':      os.path.getsize(fpath),
                    })

    # Sort newest-first across all entries
    entries.sort(key=lambda e: e['filename'], reverse=True)
    return jsonify({'logs': entries})


@app.route('/api/logs/read')
def read_run_log():
    """Return the text content of a specific run log file."""
    fpath     = request.args.get('path', '')
    logs_root = os.path.realpath(os.path.join(BASE_DIR, 'logs'))
    real      = os.path.realpath(fpath)
    # Use normcase for comparison so Windows drive-letter case differences don't
    # cause a spurious 403 (normcase lowercases and normalises separators on Windows).
    logs_nc = os.path.normcase(logs_root)
    real_nc = os.path.normcase(real)
    if not (real_nc == logs_nc or real_nc.startswith(logs_nc + os.sep)):
        return abort(403)
    if not os.path.isfile(real):
        return abort(404)
    with open(real, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    return jsonify({'content': content})


# ── File serving ──────────────────────────────────────────────────────────────
@app.route('/api/deployment/<dep>/report')
def serve_report(dep):
    dep_dir = _dep_dir(dep)
    path    = os.path.join(dep_dir, 'reports', f'qc_report_{dep}.html')
    if not os.path.isfile(path):
        return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background: #f4f6f9;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .empty {{
    text-align: center;
    color: #90a4ae;
  }}
  .empty-icon {{ font-size: 48px; margin-bottom: 16px; opacity: 0.4; }}
  .empty-title {{ font-size: 16px; font-weight: 600; color: #607d8b; margin-bottom: 8px; }}
  .empty-hint  {{ font-size: 13px; color: #90a4ae; }}
  code {{ background:#e8edf2; padding:2px 6px; border-radius:3px; font-size:12px; }}
</style>
</head>
<body>
  <div class="empty">
    <div class="empty-icon">📋</div>
    <div class="empty-title">QC report not yet generated</div>
    <div class="empty-hint">Run the <code>QC</code> stage in the Pipeline tab to produce the report.</div>
  </div>
</body>
</html>''', 404
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
    dep_dir   = _dep_dir(dep)
    figs_dir  = os.path.join(dep_dir, 'figs')
    # deployment-wide plots (HTML from Plotly, PNG fallback)
    htmls     = sorted(glob.glob(os.path.join(figs_dir, '*.html')))
    pngs      = sorted(glob.glob(os.path.join(figs_dir, '*.png')))
    # per-profile plots
    prof_htmls = sorted(glob.glob(os.path.join(figs_dir, 'profiles', '*.html')))
    prof_pngs  = sorted(glob.glob(os.path.join(figs_dir, 'profiles', '*.png')))
    return jsonify({
        'htmls':        htmls,
        'pngs':         pngs,
        'profile_htmls': prof_htmls[:20],
        'profile_pngs':  prof_pngs[:20],
        'figs_dir':     figs_dir,
    })


@app.route('/api/deployment/<dep>/nc-files')
def nc_files_list(dep):
    """List NetCDF output files produced by stages 2a, 2b, 2c, and 4."""
    year = dep.split('-')[1][:4] if '-' in dep else '2025'

    def _list(path):
        files = []
        if os.path.isdir(path):
            for f in sorted(os.listdir(path)):
                fp = os.path.join(path, f)
                if os.path.isfile(fp) and f.endswith('.nc'):
                    st = os.stat(fp)
                    files.append({'name': f, 'size': st.st_size,
                                  'mtime': st.st_mtime, 'path': fp})
        return {'files': files, 'dir': path, 'exists': os.path.isdir(path)}

    profile_dir  = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')
    traj_sci_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
    traj_flt_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_flight')
    qc_dir       = os.path.join(BASE_DIR, 'deployments', year, dep,
                                 'data', 'out', 'nc', 'sci-profile', 'delayed')

    # QC output: only top-level .nc files, not qc_queue/ subdirectory
    qc_files = []
    if os.path.isdir(qc_dir):
        for f in sorted(os.listdir(qc_dir)):
            fp = os.path.join(qc_dir, f)
            if os.path.isfile(fp) and f.endswith('.nc'):
                st = os.stat(fp)
                qc_files.append({'name': f, 'size': st.st_size,
                                  'mtime': st.st_mtime, 'path': fp})

    return jsonify({
        'profile':  _list(profile_dir),
        'traj_sci': _list(traj_sci_dir),
        'traj_flt': _list(traj_flt_dir),
        'qc':       {'files': qc_files, 'dir': qc_dir, 'exists': os.path.isdir(qc_dir)},
    })


@app.route('/api/deployment/<dep>/dat-files')
def dat_files(dep):
    base = os.path.join(BASE_DIR, 'dat_files', dep)
    result = {}
    for sub in ('merged_data', 'flight_data'):
        d = os.path.join(base, sub)
        files = []
        if os.path.isdir(d):
            files = [
                {'name': n, 'size': os.path.getsize(os.path.join(d, n))}
                for n in sorted(os.listdir(d))
                if os.path.isfile(os.path.join(d, n)) and n.endswith('.dat')
            ]
        result[sub] = {'files': files, 'dir': d}
    return jsonify(result)


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
            with open(f, encoding='utf-8-sig') as fp:
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
        with open(path, encoding='utf-8-sig') as f:
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
    """Return {'profile': [...], 'trajectory': [...], 'trajectory_flight': [...]} — from JSON config or script defaults."""
    if os.path.isfile(_SENSOR_SETS_CFG):
        with open(_SENSOR_SETS_CFG, encoding='utf-8-sig') as f:
            cfg = json.load(f)
        traj = sorted(cfg.get('trajectory', []))
        return {
            'profile':            sorted(cfg.get('profile', [])),
            'trajectory':         traj,
            # trajectory_flight defaults to trajectory if not explicitly configured
            'trajectory_flight':  sorted(cfg.get('trajectory_flight', traj)),
        }
    # Fall back to build_sensor_defs.py hardcoded defaults
    try:
        import importlib.util
        bsd_path = os.path.join(BASE_DIR, 'build_sensor_defs.py')
        spec = importlib.util.spec_from_file_location('bsd', bsd_path)
        bsd  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bsd)
        traj = sorted(bsd.TRAJECTORY_SENSORS)
        return {
            'profile':           sorted(bsd.PROFILE_SENSORS),
            'trajectory':        traj,
            'trajectory_flight': traj,
        }
    except Exception:
        return {'profile': [], 'trajectory': [], 'trajectory_flight': []}


@app.route('/api/sensor-sets')
def get_sensor_sets():
    sets    = _current_sensor_sets()
    sensors = _load_raw_sensor_universe()
    return jsonify({
        'profile':            sets['profile'],
        'trajectory':         sets['trajectory'],
        'trajectory_flight':  sets['trajectory_flight'],
        'all_sensors': sensors,
    })


@app.route('/api/sensor-sets', methods=['POST'])
def save_sensor_sets():
    data              = request.json or {}
    profile           = sorted(set(data.get('profile', [])))
    traj              = sorted(set(data.get('trajectory', [])))
    traj_flight       = sorted(set(data.get('trajectory_flight', [])))
    cfg               = {'profile': profile, 'trajectory': traj, 'trajectory_flight': traj_flight}
    with open(_SENSOR_SETS_CFG, 'w') as f:
        json.dump(cfg, f, indent=2)
    return jsonify({'ok': True, 'profile_count': len(profile), 'traj_count': len(traj), 'traj_flight_count': len(traj_flight)})


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
        'profile':              'science_profile',
        'trajectory':           'science_trajectory',
        'trajectory_flight':    'trajectory_flight',
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


# ── Raw Files (list / delete / import) ───────────────────────────────────────
_ALLOWED_RAW_EXT = {'.dbd', '.ebd', '.sbd', '.tbd', '.mbd', '.nbd'}


@app.route('/api/deployment/<dep>/raw-files')
def list_raw_files(dep):
    """List all raw binary files in incoming_data/<dep>/ with sizes and extensions."""
    src_dir = os.path.join(BASE_DIR, 'incoming_data', dep)
    if not os.path.isdir(src_dir):
        return jsonify({'files': [], 'dir': src_dir})
    files = []
    for name in sorted(os.listdir(src_dir)):
        fpath = os.path.join(src_dir, name)
        if os.path.isfile(fpath):
            ext = os.path.splitext(name)[1].lower()
            files.append({'name': name, 'size': os.path.getsize(fpath), 'ext': ext})
    return jsonify({'files': files, 'dir': src_dir})


@app.route('/api/deployment/<dep>/raw-files/<filename>', methods=['DELETE'])
def delete_raw_file(dep, filename):
    """Delete a single raw binary file from incoming_data/<dep>/."""
    if os.sep in filename or '/' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    fpath = os.path.join(BASE_DIR, 'incoming_data', dep, filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_RAW_EXT:
        return jsonify({'error': 'Not a raw binary file'}), 400
    os.remove(fpath)
    return jsonify({'ok': True})

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
        os.path.join(BASE_DIR, 'glider_config_files_json', glider, name, 'trajectory_flight'),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    # Copy JSON config files from template deployment
    if template and template != name:
        tmpl_glider = template.split('-')[0]
        for sub in ['science_profile', 'science_trajectory', 'trajectory_flight']:
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
