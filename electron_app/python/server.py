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
import yaml
import shutil
import io
import subprocess
import threading
import datetime
import time
import secrets
import hashlib
import base64
import ssl
import urllib.parse
import urllib.request as _urlreq
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_NOT_RUN, STATUS_NEEDS_REVIEW,
)

app = Flask(__name__)

# Active subprocesses: {(deployment, stage): Popen}
_procs       = {}
_lock        = threading.Lock()
_prev_status = {}   # status snapshot taken just before a run starts, restored on abort

# Live log buffer for reconnect — populated line-by-line while any stage runs.
# Key: (dep, stage)  Value: list of log lines (strings)
_live_logs  = {}
# True while the process is still producing output; False once it's done/failed.
_live_done  = {}   # key -> bool (True = finished)

# Trajectory report cache: dep -> {'sig': str, 'data': dict}
_traj_report_cache = {}

# File-listing caches (avoid re-statting thousands of files on every tab switch)
_nc_files_cache    = {}  # dep -> {'mtime': float, 'data': dict}
_nc_vars_cache     = {}  # dep -> {'mtime': float, 'data': dict}
_dat_files_cache   = {}  # dep -> {'mtime': float, 'data': dict}
_dat_sensors_cache = {}  # dep -> {'mtime': float, 'data': dict}
_raw_files_cache   = {}  # dep -> {'mtime': float, 'data': dict}
_cloud_files_cache = {}  # dep -> {'mtime': float, 'data': dict}


def _dir_mtime(*paths):
    """Return the max mtime of the given directories (0 for missing dirs)."""
    mt = 0.0
    for p in paths:
        try:
            mt = max(mt, os.stat(p).st_mtime)
        except OSError:
            pass
    return mt

# Number of threads for parallel NC reads.
# Keep conservative on Windows — HDF5 is not thread-safe across concurrent
# Dataset opens (can cause segfaults with >1 concurrent reader).
_TRAJ_WORKERS = 1

# Serialise all HDF5/netCDF4 file opens across threads.  Even with
# _TRAJ_WORKERS=1 this ensures safety if the value is ever raised.
# RLock (not Lock): some AD2CP functions hold one dataset open while opening
# further datasets nested inside that same span, on the same thread -- a plain
# Lock would deadlock the thread against itself in that case.
_nc_lock = threading.RLock()

# ── NC worker functions (called from ThreadPoolExecutor) ──────────────────────
def _nc_read_surf(fpath):
    """Read time/lat/lon/gps_lat from one trajectory NC file. Thread-safe.
    Tries netCDF4 first (faster), falls back to xarray."""
    import numpy as np

    def _align(a, n):
        a = np.asarray(a, dtype=np.float64).ravel()
        if a.size == n:
            return a
        out = np.full(n, np.nan)
        out[:min(a.size, n)] = a[:min(a.size, n)]
        return out

    # ── netCDF4 path ──────────────────────────────────────────────────────────
    try:
        import netCDF4 as _nc4
        with _nc_lock:                          # serialise HDF5 opens
            with _nc4.Dataset(fpath, 'r') as ds:
                vs = ds.variables
                if 'time' not in vs:
                    return None
                t = np.ma.filled(vs['time'][:], np.nan).astype(np.float64).ravel()
                if t.size == 0:
                    return None
                n = t.size
                lat     = np.ma.filled(vs['latitude'][:],   np.nan) if 'latitude'   in vs else np.full(n, np.nan)
                lon     = np.ma.filled(vs['longitude'][:],  np.nan) if 'longitude'  in vs else np.full(n, np.nan)
                gps_lat = np.ma.filled(vs['m_gps_lat'][:],  np.nan) if 'm_gps_lat'  in vs else np.full(n, np.nan)
                return {'t': t, 'lat': _align(lat, n), 'lon': _align(lon, n),
                        'gps_lat': _align(gps_lat, n)}
    except ImportError:
        pass   # netCDF4 not available — fall through to xarray
    except Exception:
        return None

    # ── xarray fallback ───────────────────────────────────────────────────────
    try:
        import xarray as xr
        with _nc_lock:                          # xarray may also use HDF5
            ds = xr.open_dataset(fpath, decode_times=False, mask_and_scale=True)
            if 'time' not in ds:
                ds.close(); return None
            t = np.asarray(ds['time'].values, dtype=np.float64).ravel()
            if t.size == 0:
                ds.close(); return None
            n       = t.size
            lat     = np.asarray(ds['latitude'].values,  dtype=np.float64)  if 'latitude'  in ds else np.full(n, np.nan)
            lon     = np.asarray(ds['longitude'].values, dtype=np.float64)  if 'longitude' in ds else np.full(n, np.nan)
            gps_lat = np.asarray(ds['m_gps_lat'].values, dtype=np.float64)  if 'm_gps_lat' in ds else np.full(n, np.nan)
            ds.close()
        return {'t': t, 'lat': _align(lat, n), 'lon': _align(lon, n),
                'gps_lat': _align(gps_lat, n)}
    except Exception:
        return None


_TRAJ_PHYS_VARS = ['latitude', 'longitude', 'm_gps_lat', 'm_gps_lon',
                    'm_pitch', 'm_roll', 'm_heading',
                    'm_battery', 'm_coulomb_amphr', 'm_vacuum', 'm_depth']

def _nc_read_traj(fpath):
    """Read all flight variables from one trajectory NC file. Thread-safe.
    Tries netCDF4 first (faster), falls back to xarray."""
    import numpy as np

    def _align(a, n):
        a = np.asarray(a, dtype=np.float64).ravel()
        if a.size == n:
            return a
        out = np.full(n, np.nan)
        out[:min(a.size, n)] = a[:min(a.size, n)]
        return out

    # ── netCDF4 path ──────────────────────────────────────────────────────────
    try:
        import netCDF4 as _nc4
        with _nc_lock:                          # serialise HDF5 opens
            with _nc4.Dataset(fpath, 'r') as ds:
                vs = ds.variables
                if 'time' not in vs:
                    return None
                t = np.ma.filled(vs['time'][:], np.nan).astype(np.float64).ravel()
                if t.size == 0:
                    return None
                n      = t.size
                result = {'t': t}
                for v in _TRAJ_PHYS_VARS:
                    raw = np.ma.filled(vs[v][:], np.nan) if v in vs else np.full(n, np.nan)
                    result[v] = _align(raw, n)
                return result
    except ImportError:
        pass
    except Exception:
        return None

    # ── xarray fallback ───────────────────────────────────────────────────────
    try:
        import xarray as xr
        with _nc_lock:                          # xarray may also use HDF5
            ds = xr.open_dataset(fpath, decode_times=False, mask_and_scale=True)
            if 'time' not in ds:
                ds.close(); return None
            t = np.asarray(ds['time'].values, dtype=np.float64).ravel()
            if t.size == 0:
                ds.close(); return None
            n      = t.size
            result = {'t': t}
            for v in _TRAJ_PHYS_VARS:
                raw = np.asarray(ds[v].values, dtype=np.float64) if v in ds else np.full(n, np.nan)
                result[v] = _align(raw, n)
            ds.close()
        return result
    except Exception:
        return None


# ── Disk cache helpers ────────────────────────────────────────────────────────
def _disk_cache_path(dep, name):
    return os.path.join(_dep_dir(dep), 'cache', f'{name}.json')


def _dir_signature(dir_path, files):
    """Signature over (filename, size, mtime) for every file, not just the max
    mtime -- catches a content swap that preserves the original timestamp
    (e.g. shutil.copy2, robocopy's default /COPY:DAT, a timestamp-preserving
    restore from backup) as a cache miss instead of silently treating it as
    unchanged."""
    parts = []
    for f in sorted(files):
        st = os.stat(os.path.join(dir_path, f))
        parts.append(f'{f}:{st.st_size}:{st.st_mtime_ns}')
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def _load_disk_cache(dep, name, sig):
    """Return cached data dict if the on-disk JSON's signature still matches, else None."""
    path = _disk_cache_path(dep, name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        if obj.get('sig') == sig:
            return obj['data']
    except Exception:
        pass
    return None


def _save_disk_cache(dep, name, sig, data):
    """Persist computed data to a JSON file on disk."""
    path = _disk_cache_path(dep, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'sig': sig, 'data': data}, f)
    except Exception as exc:
        print(f'[server] warning: could not write disk cache {path}: {exc}', flush=True)


# ── CORS (no external package needed) ────────────────────────────────────────
# No Access-Control-Allow-Origin here: this server is unauthenticated and binds
# to 127.0.0.1 only, so cross-origin reads from arbitrary web pages must stay
# blocked by the browser's default same-origin policy. The Electron renderer
# talks to this server with webSecurity disabled, so it isn't subject to that
# restriction and needs no CORS headers to function.
@app.after_request
def _cors(resp):
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def _options(_path=''):
    return '', 204


# ── Helpers ───────────────────────────────────────────────────────────────────
def _dep_dir(deployment):
    # Fallback only matters for a hyphen-less name, which every route calling
    # this now rejects via _valid_deployment_name() before it gets here --
    # kept as a non-stale defensive default for any future/internal caller.
    year = deployment.split('-')[1][:4] if '-' in deployment else str(datetime.datetime.now().year)
    d = os.path.join(BASE_DIR, 'deployments', year, deployment)
    os.makedirs(d, exist_ok=True)
    return d


def _is_within(path, root_dir):
    """True if the realpath of `path` is `root_dir` itself or nested inside it."""
    root_real = os.path.realpath(root_dir)
    cand_real = os.path.realpath(path)
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def _dir_has_files(path):
    """True if `path` is a directory containing at least one regular file
    (non-recursive -- every STAGE_INFO source_fn directory is flat)."""
    if not os.path.isdir(path):
        return False
    with os.scandir(path) as it:
        return any(entry.is_file() for entry in it)


_DEPLOYMENT_NAME_RE = re.compile(r'^[\w]+-\d{8}T\d{4}$')

def _valid_deployment_name(dep):
    return bool(_DEPLOYMENT_NAME_RE.match(dep or ''))


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
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
    dep_dir  = _dep_dir(dep)
    state    = load_state(dep_dir)
    nc_dir   = os.path.join(dep_dir, 'data', 'out', 'nc', 'sci-profile', 'delayed')
    nc_count = len(glob.glob(os.path.join(nc_dir, '*.nc'))) if os.path.isdir(nc_dir) else 0
    return jsonify({'state': state, 'nc_count': nc_count, 'nc_dir': nc_dir})


@app.route('/api/deployments/health')
def deployments_health():
    """Return lightweight health indicators for every deployment (one call for the dashboard)."""
    deps = _find_deployments()
    result = {}
    for dep in deps:
        glider     = dep.split('-')[0]
        config_base = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep)
        raw_dir    = os.path.join(BASE_DIR, 'incoming_data', dep)
        merged_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'merged_data')
        flight_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'flight_data')
        nc_dir     = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')

        raw_count  = len(os.listdir(raw_dir))  if os.path.isdir(raw_dir)    else 0
        dat_merged = len(glob.glob(os.path.join(merged_dir, '*.dat'))) if os.path.isdir(merged_dir) else 0
        dat_flight = len(glob.glob(os.path.join(flight_dir, '*.dat'))) if os.path.isdir(flight_dir) else 0
        nc_profile = len(glob.glob(os.path.join(nc_dir, '*.nc')))      if os.path.isdir(nc_dir)     else 0

        required   = ['deployment.json', 'global_attributes.json', 'instruments.json', 'sensor_defs.json']
        meta_ok    = all(os.path.isfile(os.path.join(config_base, 'science_profile', f)) for f in required)

        sd_valid = False
        sd_path  = os.path.join(config_base, 'science_profile', 'sensor_defs.json')
        if os.path.isfile(sd_path):
            try:
                with open(sd_path, encoding='utf-8-sig') as f:
                    sd = json.load(f)
                sd_valid = any(isinstance(v, dict) and v.get('is_dimension') for v in sd.values())
            except Exception:
                pass

        result[dep] = {
            'raw_files':         raw_count,
            'dat_merged':        dat_merged,
            'dat_flight':        dat_flight,
            'metadata_complete': meta_ok,
            'sensor_defs_valid': sd_valid,
            'nc_profile':        nc_profile,
        }
    return jsonify(result)


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


# ── Docker Desktop helpers ─────────────────────────────────────────────────────

_DOCKER_DESKTOP_EXE = r'C:\Program Files\Docker\Docker\Docker Desktop.exe'


def _docker_process_running():
    """True if a Docker Desktop.exe process exists, independent of whether its
    engine is actually responding to `docker info` -- distinguishes 'not
    started yet' (plain Start works) from 'running but engine stuck' (Start
    just launches a redundant instance on top of the hung one; needs a real
    kill + WSL2 reset + relaunch instead)."""
    try:
        r = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq Docker Desktop.exe', '/NH'],
            capture_output=True, text=True, timeout=6)
        return 'Docker Desktop.exe' in r.stdout
    except Exception:
        return False


@app.route('/api/docker/status')
def docker_status():
    try:
        r = subprocess.run(['docker', 'info'],
                           capture_output=True, text=True, timeout=6)
        running = r.returncode == 0
    except Exception:
        running = False
    result = {'running': running}
    if not running:
        result['process_running'] = _docker_process_running()
    return jsonify(result)


@app.route('/api/docker/start', methods=['POST'])
def docker_start():
    if not os.path.exists(_DOCKER_DESKTOP_EXE):
        return jsonify({'ok': False, 'error': f'Docker Desktop not found at {_DOCKER_DESKTOP_EXE}'}), 404
    try:
        subprocess.Popen([_DOCKER_DESKTOP_EXE])
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/docker/force-restart', methods=['POST'])
def docker_force_restart():
    """Recover from 'Docker Desktop process running but engine unresponsive':
    kill every Docker Desktop process, reset the WSL2 backend, then relaunch.
    Plain /api/docker/start can't fix this state -- it only ever launches a
    new instance, which does nothing useful on top of an already-running
    (just stuck) one."""
    for proc_name in ('Docker Desktop.exe', 'com.docker.backend.exe',
                       'com.docker.dev-envs.exe', 'com.docker.build.exe'):
        try:
            subprocess.run(['taskkill', '/F', '/IM', proc_name],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass  # best-effort -- fine if a given process wasn't running

    try:
        subprocess.run(['wsl', '--shutdown'], capture_output=True, text=True, timeout=30)
    except Exception:
        pass  # best-effort -- proceed to relaunch even if this fails/no-ops

    if not os.path.exists(_DOCKER_DESKTOP_EXE):
        return jsonify({'ok': False, 'error': f'Docker Desktop not found at {_DOCKER_DESKTOP_EXE}'}), 404
    try:
        subprocess.Popen([_DOCKER_DESKTOP_EXE])
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/docker/wait')
def docker_wait():
    """SSE: polls every 3 s until Docker responds, or 120 s timeout."""
    import time as _time
    def _gen():
        deadline = _time.time() + 120
        while _time.time() < deadline:
            try:
                r = subprocess.run(['docker', 'info'],
                                   capture_output=True, text=True, timeout=6)
                ready = r.returncode == 0
            except Exception:
                ready = False
            yield f"data: {json.dumps({'ready': ready})}\n\n"
            if ready:
                return
            _time.sleep(3)
        yield f"data: {json.dumps({'ready': False, 'timeout': True})}\n\n"
    return Response(_gen(), mimetype='text/event-stream')


@app.route('/api/deployment/<dep>/run/<stage>', methods=['POST'])
def run_stage(dep, stage):
    """Start a stage process; stream stdout as SSE."""
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
    if stage not in STAGE_INFO:
        return abort(404)
    info = STAGE_INFO[stage]
    if info.get('manual'):
        return jsonify({'error': 'Manual stage — use set-status instead'}), 400

    key = (dep, stage)
    with _lock:
        if key in _procs:
            return jsonify({'error': f'Stage {stage} is already running for {dep}'}), 409

    # Prerequisite gate: STAGE_INFO's source_fn defines each stage's expected
    # input directory, but nothing consumed it to gate execution -- a direct
    # API call or an out-of-order UI click could run a stage against an
    # empty/missing input dir with no warning, leaving whatever the invoked
    # script's own robustness happened to produce (or not).
    source_fn = info.get('source_fn')
    if source_fn:
        source_dir = source_fn(BASE_DIR, dep)
        if not _dir_has_files(source_dir):
            return jsonify({
                'error': f'Input for "{info["label"]}" is missing or empty: {source_dir}. '
                         f'Run the prerequisite stage first.'
            }), 409

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

    # Snapshot current status so abort can restore it
    prev = (load_state(dep_dir).get(stage) or {}).get('status', STATUS_NOT_RUN)
    _prev_status[key] = prev

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

            ok = (proc.returncode == 0)
            if not ok:
                status = STATUS_FAILED
            elif stage == 'preflight':
                # Preflight is two-phase: this subprocess only ran the analysis, not
                # the apply. Default to NEEDS_REVIEW (safe-fail direction) unless the
                # analysis positively reports nothing to apply — only a successful
                # /preflight/apply call (or an explicit Skip) should ever set DONE.
                status = STATUS_NEEDS_REVIEW
                marker = '=== PREFLIGHT_JSON ==='
                for _line in log_lines:
                    if _line.startswith(marker):
                        try:
                            pf = json.loads(_line[len(marker):])
                            any_changes = pf.get('any_changes', True)
                            coverage_ok = pf.get('region_coverage', {}).get('coverage_ok', True)
                            if not any_changes and coverage_ok:
                                status = STATUS_DONE
                        except (json.JSONDecodeError, AttributeError, TypeError):
                            pass  # keep the safe NEEDS_REVIEW default
                        break
            else:
                status = STATUS_DONE
            set_stage_status(dep_dir, stage, status)
            _save_run_log(dep, stage, start_ts, log_lines, ok)
            _prev_status.pop(key, None)
            _live_done[key] = True
            yield f"data: {json.dumps({'type': 'done', 'success': ok, 'code': proc.returncode})}\n\n"

        except Exception as exc:
            set_stage_status(dep_dir, stage, STATUS_FAILED)
            _save_run_log(dep, stage, start_ts, log_lines, False)
            _prev_status.pop(key, None)
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
    restored = _prev_status.pop(key, STATUS_NOT_RUN)
    set_stage_status(_dep_dir(dep), stage, restored)
    return jsonify({'ok': True, 'killed': killed, 'restored_status': restored})


@app.route('/api/shutdown-cleanup', methods=['POST'])
def shutdown_cleanup():
    """Kill every currently-running stage subprocess and its Docker container, and
    restore each one's pre-run status. Called by main.js just before the Electron
    app quits -- killing the 'docker run' process alone does not stop the
    container itself (same reason abort_stage above does an explicit docker kill),
    so without this a mid-stage container is left running invisibly, and the
    deployment would otherwise come back up next launch permanently stuck showing
    RUNNING with no process left to ever finish it.
    """
    with _lock:
        keys = list(_procs.keys())
    killed = []
    for key in keys:
        dep, stage = key
        with _lock:
            proc = _procs.pop(key, None)
        if proc:
            proc.terminate()
        cname = _container_name(dep, stage)
        subprocess.run(['docker', 'kill', cname],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restored = _prev_status.pop(key, STATUS_NOT_RUN)
        set_stage_status(_dep_dir(dep), stage, restored)
        killed.append(f'{dep}/{stage}')
    return jsonify({'ok': True, 'killed': killed})


@app.route('/api/deployment/<dep>/stage-logs/<stage>')
def stream_stage_logs(dep, stage):
    """Reconnect to a running stage's log stream.

    For Docker stages (Stage 4 QC): attaches via `docker logs -f`.
    For all other stages: replays the in-memory live-log buffer then tails
    new lines as they arrive, so reconnect works after any page refresh.
    """
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
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
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400

    data   = request.json or {}
    script = os.path.join(BASE_DIR, 'app', 'pre_qaqc_config_check.py')
    if not os.path.isfile(script):
        return jsonify({'error': 'pre_qaqc_config_check.py not found'}), 404
    dep_dir = _dep_dir(dep)
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False,
                                        encoding='utf-8') as tf:
            json.dump(data, tf)
            tf_path = tf.name
        result = subprocess.run(
            [sys.executable, script, BASE_DIR, dep, '--apply-file', tf_path],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
        )
        try:
            os.unlink(tf_path)
        except OSError:
            pass
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
    """Return saved run logs sorted newest-first. Accepts optional ?dep= filter."""
    logs_root  = os.path.join(BASE_DIR, 'logs')
    filter_dep = request.args.get('dep', '').strip()
    entries    = []
    if not os.path.isdir(logs_root):
        return jsonify({'logs': []})

    dep_names = [filter_dep] if filter_dep else sorted(os.listdir(logs_root))
    for dep in dep_names:
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
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
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
    if not _is_within(real, BASE_DIR):
        return abort(403)
    if not os.path.isfile(real):
        return abort(404)
    return send_file(real)


@app.route('/api/deployment/<dep>/plots')
def list_plots(dep):
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
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

    profile_dir  = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')
    traj_sci_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
    traj_flt_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_flight')
    qc_dir       = os.path.join(BASE_DIR, 'deployments', year, dep,
                                 'data', 'out', 'nc', 'sci-profile', 'delayed')

    newest = _dir_mtime(profile_dir, traj_sci_dir, traj_flt_dir, qc_dir)
    cached = _nc_files_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    def _list(path):
        files = []
        if os.path.isdir(path):
            for entry in sorted(os.scandir(path), key=lambda e: e.name):
                if entry.is_file() and entry.name.endswith('.nc'):
                    st = entry.stat()
                    files.append({'name': entry.name, 'size': st.st_size,
                                  'mtime': st.st_mtime, 'path': entry.path})
        return {'files': files, 'dir': path, 'exists': os.path.isdir(path)}

    # QC output: only top-level .nc files, not qc_queue/ subdirectory
    qc_files = []
    if os.path.isdir(qc_dir):
        for entry in sorted(os.scandir(qc_dir), key=lambda e: e.name):
            if entry.is_file() and entry.name.endswith('.nc'):
                st = entry.stat()
                qc_files.append({'name': entry.name, 'size': st.st_size,
                                  'mtime': st.st_mtime, 'path': entry.path})

    data = {
        'profile':  _list(profile_dir),
        'traj_sci': _list(traj_sci_dir),
        'traj_flt': _list(traj_flt_dir),
        'qc':       {'files': qc_files, 'dir': qc_dir, 'exists': os.path.isdir(qc_dir)},
    }
    _nc_files_cache[dep] = {'mtime': newest, 'data': data}
    return jsonify(data)


@app.route('/api/deployment/<dep>/nc-variables')
def nc_variables(dep):
    """Return variable listings from a representative sample NC file for each stage output."""
    import xarray as xr

    year = dep.split('-')[1][:4] if '-' in dep else '2025'

    dirs = {
        'profile':  os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile'),
        'traj_sci': os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science'),
        'traj_flt': os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_flight'),
        'qc':       os.path.join(BASE_DIR, 'deployments', year, dep,
                                 'data', 'out', 'nc', 'sci-profile', 'delayed'),
    }

    newest = _dir_mtime(*dirs.values())
    cached = _nc_vars_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    result = {}
    for key, d in dirs.items():
        if not os.path.isdir(d):
            result[key] = {'exists': False, 'variables': [], 'file': None}
            continue
        files = [f for f in sorted(os.listdir(d))
                 if f.endswith('.nc') and 'qc_queue' not in f]
        if not files:
            result[key] = {'exists': True, 'variables': [], 'file': None}
            continue
        # Use a mid-deployment file for better variable coverage
        sample = files[len(files) // 2]
        try:
            ds = xr.open_dataset(os.path.join(d, sample), decode_times=False)
            variables = []
            for vname in sorted(ds.data_vars):
                attrs = ds[vname].attrs
                variables.append({
                    'name':      vname,
                    'long_name': attrs.get('long_name', ''),
                    'units':     attrs.get('units', ''),
                    'dtype':     str(ds[vname].dtype),
                    'dims':      list(ds[vname].dims),
                })
            ds.close()
            result[key] = {'exists': True, 'variables': variables, 'file': sample}
        except Exception as e:
            result[key] = {'exists': True, 'variables': [], 'file': sample, 'error': str(e)}

    _nc_vars_cache[dep] = {'mtime': newest, 'data': result}
    return jsonify(result)


@app.route('/api/deployment/<dep>/dat-files')
def dat_files(dep):
    base = os.path.join(BASE_DIR, 'dat_files', dep)
    dirs = [os.path.join(base, s) for s in ('merged_data', 'flight_data')]
    newest = _dir_mtime(*dirs)
    cached = _dat_files_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    result = {}
    for sub, d in zip(('merged_data', 'flight_data'), dirs):
        files = []
        if os.path.isdir(d):
            for entry in sorted(os.scandir(d), key=lambda e: e.name):
                if entry.is_file() and entry.name.endswith('.dat'):
                    files.append({'name': entry.name, 'size': entry.stat().st_size})
        result[sub] = {'files': files, 'dir': d}
    _dat_files_cache[dep] = {'mtime': newest, 'data': result}
    return jsonify(result)


@app.route('/api/deployment/<dep>/dat-files/<subdir>/<filename>', methods=['DELETE'])
def delete_dat_file(dep, subdir, filename):
    """Delete a single DAT file from dat_files/<dep>/<merged_data|flight_data>/."""
    if subdir not in ('merged_data', 'flight_data'):
        return jsonify({'error': 'Invalid subdir'}), 400
    if os.sep in filename or '/' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if not filename.lower().endswith('.dat'):
        return jsonify({'error': 'Not a DAT file'}), 400
    fpath = os.path.join(BASE_DIR, 'dat_files', dep, subdir, filename)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fpath)
    return jsonify({'ok': True})


@app.route('/api/deployment/<dep>/dat-count')
def dat_count(dep):
    base = os.path.join(BASE_DIR, 'dat_files', dep)
    def _count(folder):
        if not os.path.isdir(folder):
            return 0
        return sum(1 for f in os.listdir(folder) if f.lower().endswith('.dat'))
    return jsonify({
        'merged': _count(os.path.join(base, 'merged_data')),
        'flight': _count(os.path.join(base, 'flight_data')),
    })


@app.route('/api/deployment/<dep>/dat-sensors')
def dat_sensors(dep):
    """
    Scan DAT files and return sensor lists per source folder.
    merged_data → used by Profile + Trajectory (Science)
    flight_data → used by Trajectory (Flight)
    Only reads the header of each file so it's fast for large datasets.
    """
    def _scan(dat_dir):
        if not os.path.isdir(dat_dir):
            return {'sensors': [], 'files_scanned': 0}
        all_sensors, files_scanned, errors = set(), 0, []
        for fname in sorted(os.listdir(dat_dir)):
            if not fname.endswith('.dat'):
                continue
            fpath = os.path.join(dat_dir, fname)
            try:
                num_tags = 14
                with open(fpath, 'r', errors='replace') as f:
                    for i, raw in enumerate(f):
                        line = raw.strip()
                        if line.startswith('num_ascii_tags:'):
                            try:
                                num_tags = int(line.split(':', 1)[1].strip())
                            except ValueError:
                                pass
                        if i == num_tags:
                            all_sensors.update(line.split())
                            break
                        if i > 30:
                            break
                files_scanned += 1
            except Exception as exc:
                errors.append(f'{fname}: {exc}')
        return {'sensors': sorted(all_sensors), 'files_scanned': files_scanned}

    base = os.path.join(BASE_DIR, 'dat_files', dep)
    merged_dir = os.path.join(base, 'merged_data')
    flight_dir = os.path.join(base, 'flight_data')
    newest = _dir_mtime(merged_dir, flight_dir)
    cached = _dat_sensors_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    data = {
        'merged': _scan(merged_dir),
        'flight': _scan(flight_dir),
    }
    _dat_sensors_cache[dep] = {'mtime': newest, 'data': data}
    return jsonify(data)


# ── NetCDF config validation ──────────────────────────────────────────────────
_STAGE_CONFIG_SUBDIR = {
    'dat_to_profile_nc':           'science_profile',
    'dat_to_trajectory_nc':        'science_trajectory',
    'dat_to_trajectory_flight_nc': 'trajectory_flight',
}
_REQUIRED_NC_FILES = ['deployment.json', 'global_attributes.json', 'instruments.json', 'sensor_defs.json']
_LLAT_VARS         = ['llat_time', 'llat_depth', 'llat_latitude', 'llat_longitude']


def _validate_netcdf_config(config_dir):
    issues = []
    parsed = {}
    for fname in _REQUIRED_NC_FILES:
        path = os.path.join(config_dir, fname)
        if not os.path.isfile(path):
            issues.append({'file': fname, 'message': 'File not found'})
            continue
        try:
            with open(path, encoding='utf-8-sig') as f:
                parsed[fname] = json.load(f)
        except json.JSONDecodeError as e:
            issues.append({'file': fname, 'message': f'Invalid JSON: {e}'})

    if 'deployment.json' in parsed:
        dep_cfg = parsed['deployment.json']
        if not ('trajectory_name' in dep_cfg or 'trajectory_datetime' in dep_cfg):
            issues.append({'file': 'deployment.json', 'message': 'Missing trajectory_name (or trajectory_datetime)'})
        if not isinstance(dep_cfg.get('platform'), dict):
            issues.append({'file': 'deployment.json', 'message': 'Missing or invalid platform section'})

    if 'instruments.json' in parsed:
        if not isinstance(parsed['instruments.json'], list):
            issues.append({'file': 'instruments.json', 'message': 'Must be a JSON array'})

    if 'sensor_defs.json' in parsed:
        sd = parsed['sensor_defs.json']
        if not isinstance(sd, dict):
            issues.append({'file': 'sensor_defs.json', 'message': 'Must be a JSON object'})
        else:
            dims = [k for k, v in sd.items() if isinstance(v, dict) and v.get('is_dimension')]
            if not dims:
                issues.append({'file': 'sensor_defs.json',
                               'message': 'No record dimension (is_dimension: true) — run Build Sensor Defs'})
            for var in _LLAT_VARS:
                if var not in sd:
                    issues.append({'file': 'sensor_defs.json', 'message': f'Missing required variable: {var}'})
            obs_vars = [k for k, v in sd.items() if isinstance(v, dict) and v.get('dimension') == 'obs']
            if obs_vars:
                issues.append({'file': 'sensor_defs.json',
                               'message': f'{len(obs_vars)} variable(s) use dimension "obs" instead of "time" — run Build Sensor Defs'})

    return issues


@app.route('/api/deployment/<dep>/validate-netcdf-config')
def validate_netcdf_config(dep):
    stage = request.args.get('stage', '')
    subdir = _STAGE_CONFIG_SUBDIR.get(stage)
    if not subdir:
        return jsonify({'ok': True, 'issues': []})
    glider     = dep.split('-')[0]
    config_dir = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep, subdir)
    if not os.path.isdir(config_dir):
        return jsonify({'ok': False, 'issues': [{'file': subdir + '/', 'message': 'Config directory not found'}]})
    issues = _validate_netcdf_config(config_dir)
    return jsonify({'ok': len(issues) == 0, 'issues': issues})


@app.route('/api/deployment/<dep>/metadata')
def metadata(dep):
    glider     = dep.split('-')[0]
    config_dir = os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep)
    files      = {}
    errors     = {}
    for f in glob.glob(os.path.join(config_dir, '**', '*.json'), recursive=True):
        rel = os.path.relpath(f, config_dir)
        try:
            with open(f, encoding='utf-8-sig') as fp:
                raw = fp.read()
            files[rel] = json.loads(raw)
        except Exception as exc:
            # Keep the raw text so the frontend can offer in-place repair
            # instead of silently treating this file as absent/empty.
            try:
                with open(f, encoding='utf-8-sig', errors='replace') as fp:
                    raw = fp.read()
            except Exception:
                raw = ''
            errors[rel] = {'message': str(exc), 'raw': raw}
    return jsonify({'files': files, 'errors': errors, 'config_dir': config_dir})


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
    if not _is_within(full_path, config_dir):
        return jsonify({'error': 'Invalid path'}), 403
    if not full_path.endswith('.json'):
        return jsonify({'error': 'Only .json files allowed'}), 403

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w', encoding='utf-8') as fp:
        fp.write(content)

    return jsonify({'ok': True})


@app.route('/api/deployment/<dep>/metadata/delete', methods=['POST'])
def delete_metadata(dep):
    data     = request.json or {}
    rel_path = data.get('path', '').replace('/', os.sep).replace('\\', os.sep)

    glider     = dep.split('-')[0]
    config_dir = os.path.realpath(os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep))
    full_path  = os.path.realpath(os.path.join(config_dir, rel_path))

    if not _is_within(full_path, config_dir):
        return jsonify({'error': 'Invalid path'}), 403
    if not full_path.endswith('.json'):
        return jsonify({'error': 'Only .json files allowed'}), 403
    if not os.path.isfile(full_path):
        return jsonify({'error': 'File not found'}), 404

    os.remove(full_path)
    return jsonify({'ok': True})


# ── QC config editor (deployment_qc_config_root + global_regional) ────────────
# deployment_qc_config_root is what every Stage 8 rugliderqc script actually
# reads at runtime (qctests.yml/time_shift.yml/ctd_hysteresis.yml/manual_flag.yml
# -- see rugliderqc/scripts/*.py's `deployment_qc_config_root = os.path.join(
# deployment_location, 'config', 'qc')`). qc/config/global_regional/ at the repo
# root is likewise what glider_qartod_qc.py's regional streams tests actually
# read (`qc_config_root = os.path.join(data_home, 'qc', 'config')`, data_home =
# GLIDER_DATA_HOME = /home/glider = the Docker bind-mount of this repo root) --
# NOT the stale, diverged copy vendored inside rugliderqc/qc/config/global_regional/,
# which nothing in the pipeline ever reads.

def _yaml_files(config_dir):
    files = {}
    if os.path.isdir(config_dir):
        for f in sorted(glob.glob(os.path.join(config_dir, '*.yml'))):
            rel = os.path.basename(f)
            try:
                with open(f, encoding='utf-8-sig') as fp:
                    files[rel] = fp.read()
            except Exception:
                files[rel] = None
    return files


def _save_yaml_file(config_dir, rel_path, content):
    """Returns (ok, error_response_tuple_or_None)."""
    if os.sep in rel_path or '/' in rel_path or '..' in rel_path:
        return False, (jsonify({'error': 'Invalid filename'}), 400)
    if not rel_path.endswith('.yml'):
        return False, (jsonify({'error': 'Only .yml files allowed'}), 400)
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return False, (jsonify({'error': f'Invalid YAML: {e}'}), 400)

    config_dir = os.path.realpath(config_dir)
    full_path  = os.path.realpath(os.path.join(config_dir, rel_path))
    if not _is_within(full_path, config_dir):
        return False, (jsonify({'error': 'Invalid path'}), 403)

    os.makedirs(config_dir, exist_ok=True)
    with open(full_path, 'w', encoding='utf-8') as fp:
        fp.write(content)
    return True, None


@app.route('/api/deployment/<dep>/qc-config')
def qc_config(dep):
    config_dir = os.path.join(_dep_dir(dep), 'config', 'qc')
    return jsonify({'files': _yaml_files(config_dir), 'config_dir': config_dir})


@app.route('/api/deployment/<dep>/qc-config/save', methods=['POST'])
def save_qc_config(dep):
    data      = request.json or {}
    rel_path  = data.get('path', '')
    content   = data.get('content', '')
    config_dir = os.path.join(_dep_dir(dep), 'config', 'qc')
    ok, err = _save_yaml_file(config_dir, rel_path, content)
    if not ok:
        return err
    return jsonify({'ok': True})


@app.route('/api/qc-config-regional')
def qc_config_regional():
    config_dir = os.path.join(BASE_DIR, 'qc', 'config', 'global_regional')
    return jsonify({'files': _yaml_files(config_dir), 'config_dir': config_dir})


@app.route('/api/qc-config-regional/save', methods=['POST'])
def save_qc_config_regional():
    data      = request.json or {}
    rel_path  = data.get('path', '')
    content   = data.get('content', '')
    config_dir = os.path.join(BASE_DIR, 'qc', 'config', 'global_regional')
    ok, err = _save_yaml_file(config_dir, rel_path, content)
    if not ok:
        return err
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
        if not _is_within(dst_dir, config_dir):
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
    with open(_SENSOR_SETS_CFG, 'w', encoding='utf-8') as f:
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

    newest = _dir_mtime(src_dir)
    cached = _raw_files_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    files = []
    for entry in sorted(os.scandir(src_dir), key=lambda e: e.name):
        if entry.is_file():
            ext = os.path.splitext(entry.name)[1].lower()
            files.append({'name': entry.name, 'size': entry.stat().st_size, 'ext': ext})
    data = {'files': files, 'dir': src_dir}
    _raw_files_cache[dep] = {'mtime': newest, 'data': data}
    return jsonify(data)


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
        if os.path.exists(dst):
            results.append({'path': src, 'ok': False, 'error': 'Already exists', 'name': name})
            continue
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


_NICKNAMES_FILE = os.path.join(BASE_DIR, 'config', 'deployment_nicknames.json')

def _load_nicknames():
    if os.path.isfile(_NICKNAMES_FILE):
        try:
            with open(_NICKNAMES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_nicknames(data):
    os.makedirs(os.path.dirname(_NICKNAMES_FILE), exist_ok=True)
    with open(_NICKNAMES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


@app.route('/api/nicknames')
def get_nicknames():
    return jsonify(_load_nicknames())


@app.route('/api/deployment/<dep>/nickname', methods=['POST'])
def set_nickname(dep):
    nickname = (request.json or {}).get('nickname', '').strip()
    data = _load_nicknames()
    if nickname:
        data[dep] = nickname
    else:
        data.pop(dep, None)
    _save_nicknames(data)
    return jsonify({'ok': True, 'nickname': nickname or None})


@app.route('/api/deployment/<dep>/rename', methods=['POST'])
def rename_deployment(dep):
    if not dep or os.path.basename(dep) != dep or dep in ('.', '..'):
        return jsonify({'error': 'Invalid deployment name'}), 400

    new_name = (request.json or {}).get('new_name', '').strip()
    if not new_name:
        return jsonify({'error': 'new_name required'}), 400
    if new_name == dep:
        return jsonify({'ok': True, 'name': dep})
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', new_name):
        return jsonify({'error': 'Name contains invalid characters'}), 400

    # Block rename while a stage is running for this deployment
    with _lock:
        running = [k for k in _procs if k[0] == dep and _procs[k].poll() is None]
    if running:
        return jsonify({'error': 'A stage is currently running — stop it before renaming'}), 409

    old_year = dep.split('-')[1][:4]      if '-' in dep      else '2025'
    new_year = new_name.split('-')[1][:4] if '-' in new_name else old_year
    old_glider = dep.split('-')[0]
    new_glider = new_name.split('-')[0]

    # (old_path, new_path, label) for every location keyed by deployment name.
    # incoming_data is the source of truth and is listed (and checked) first.
    pairs = [
        (os.path.join(BASE_DIR, 'incoming_data', dep),
         os.path.join(BASE_DIR, 'incoming_data', new_name), 'incoming_data'),
        (os.path.join(BASE_DIR, 'deployments', old_year, dep),
         os.path.join(BASE_DIR, 'deployments', new_year, new_name), 'deployments'),
    ]
    for folder in ('dat_files', 'gncutils_slocum_nc_output', 'post_processing', 'logs', 'ad2cp'):
        pairs.append((os.path.join(BASE_DIR, folder, dep),
                      os.path.join(BASE_DIR, folder, new_name), folder))
    pairs.append((
        os.path.join(BASE_DIR, 'glider_config_files_json', old_glider, dep),
        os.path.join(BASE_DIR, 'glider_config_files_json', new_glider, new_name),
        'glider_config_files_json',
    ))

    if not os.path.isdir(pairs[0][0]):
        return jsonify({'error': f'Deployment folder not found: {pairs[0][0]}'}), 404

    # Pre-flight: check every destination before touching anything, so a
    # stale/partial name collision (e.g. left over from an earlier failed
    # rename) fails cleanly up front instead of being discovered mid-rename,
    # after some folders have already moved.
    conflicts = [new_path for _, new_path, _ in pairs if os.path.exists(new_path)]
    if conflicts:
        return jsonify({'error': f'"{new_name}" already exists: {conflicts[0]}'}), 409

    renamed, skipped, completed = [], [], []  # completed: (new_path, old_path), for rollback

    for old_path, new_path, label in pairs:
        if not os.path.isdir(old_path):
            skipped.append(label)
            continue
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            os.rename(old_path, new_path)
            renamed.append(label)
            completed.append((new_path, old_path))
        except Exception as exc:
            # Roll back everything already renamed in this batch, in reverse
            # order, so the deployment ends up either fully renamed or fully
            # back on the old name -- never scattered across both.
            rollback_errors = []
            for done_new, done_old in reversed(completed):
                try:
                    os.rename(done_new, done_old)
                except Exception as rb_exc:
                    rollback_errors.append(f'{done_new} -> {done_old}: {rb_exc}')
            if rollback_errors:
                return jsonify({
                    'error': f'{label}: {exc}',
                    'rollback_error': (
                        'Rename failed partway through AND automatic rollback also failed '
                        'for some folders -- manual fix needed: ' + '; '.join(rollback_errors)
                    ),
                    'skipped': skipped,
                }), 500
            return jsonify({
                'error': f'{label}: {exc}',
                'rolled_back': True,
                'renamed': [], 'skipped': skipped,
            }), 500

    # Transfer nickname to the new name
    nicks = _load_nicknames()
    if dep in nicks:
        nicks[new_name] = nicks.pop(dep)
        _save_nicknames(nicks)

    return jsonify({'ok': True, 'name': new_name, 'renamed': renamed, 'skipped': skipped})


@app.route('/api/deployment/<dep>/delete', methods=['POST'])
def delete_deployment(dep):
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400

    with _lock:
        running = [k for k in _procs if k[0] == dep and _procs[k].poll() is None]
    if running:
        return jsonify({'error': 'A stage is currently running — stop it before deleting'}), 409

    glider   = dep.split('-')[0]
    year     = dep.split('-')[1][:4] if '-' in dep else '2025'
    deleted, skipped, errors = [], [], []

    def _try_delete(path, label):
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                deleted.append(label)
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        else:
            skipped.append(label)

    _try_delete(os.path.join(BASE_DIR, 'incoming_data', dep),                          'incoming_data')
    _try_delete(os.path.join(BASE_DIR, 'deployments', year, dep),                      'deployments')
    _try_delete(os.path.join(BASE_DIR, 'dat_files', dep),                              'dat_files')
    _try_delete(os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep),              'gncutils_slocum_nc_output')
    _try_delete(os.path.join(BASE_DIR, 'post_processing', dep),                        'post_processing')
    _try_delete(os.path.join(BASE_DIR, 'logs', dep),                                   'logs')
    _try_delete(os.path.join(BASE_DIR, 'ad2cp', dep),                                  'ad2cp')
    _try_delete(os.path.join(BASE_DIR, 'glider_config_files_json', glider, dep),       'glider_config_files_json')

    # Remove nickname entry
    nicks = _load_nicknames()
    if dep in nicks:
        del nicks[dep]
        _save_nicknames(nicks)

    if errors:
        return jsonify({'error': '; '.join(errors), 'deleted': deleted, 'skipped': skipped}), 500

    return jsonify({'ok': True, 'deleted': deleted, 'skipped': skipped})


# ── CAC file manager ─────────────────────────────────────────────────────────
CAC_DIR = os.path.join(BASE_DIR, 'master_cac')

@app.route('/api/cac-files')
def list_cac_files():
    """List all files in master_cac/."""
    os.makedirs(CAC_DIR, exist_ok=True)
    files = []
    for name in sorted(os.listdir(CAC_DIR)):
        fp = os.path.join(CAC_DIR, name)
        if os.path.isfile(fp):
            st = os.stat(fp)
            files.append({'name': name, 'size': st.st_size, 'mtime': st.st_mtime})
    return jsonify({'files': files, 'dir': CAC_DIR})


@app.route('/api/cac-files/upload', methods=['POST'])
def upload_cac_files():
    """Accept one or more uploaded files and save them to master_cac/.

    Conflict mode (form field 'conflict', default 'skip'), matching the
    'overwrite' | 'skip' convention already used by /api/cloud/upload:
      - 'skip'      — an already-existing file is left untouched and reported
                       in 'skipped' instead of being silently overwritten.
      - 'overwrite' — an already-existing file is replaced, as before.
    """
    os.makedirs(CAC_DIR, exist_ok=True)
    conflict = request.form.get('conflict', 'skip')
    saved = []
    skipped = []
    for f in request.files.getlist('files'):
        if not f.filename:
            continue
        name = os.path.basename(f.filename)
        dest = os.path.join(CAC_DIR, name)
        if conflict == 'skip' and os.path.isfile(dest):
            skipped.append(name)
            continue
        f.save(dest)
        saved.append(name)
    return jsonify({'ok': True, 'saved': saved, 'skipped': skipped})


@app.route('/api/cac-files/<filename>', methods=['DELETE'])
def delete_cac_file(filename):
    """Delete a single file from master_cac/."""
    fp = os.path.join(CAC_DIR, os.path.basename(filename))
    if not os.path.isfile(fp):
        return jsonify({'error': 'File not found'}), 404
    os.remove(fp)
    return jsonify({'ok': True})


# ── Trajectory surfacings (lightweight — lat/lon/time only) ───────────────────
_surf_cache = {}   # dep -> {'sig': str, 'data': dict}

@app.route('/api/deployment/<dep>/surfacings')
def surfacings(dep):
    """
    Fast endpoint: read only latitude, longitude, and time from every
    trajectory-science NC file.  Returns GPS surfacing positions (non-NaN
    lat & lon) plus the total record count.  Cached by newest file mtime.
    """
    import numpy as np

    traj_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
    if not os.path.isdir(traj_dir):
        return jsonify({'error': f'Trajectory directory not found: {traj_dir}'}), 404

    files = sorted(f for f in os.listdir(traj_dir) if f.endswith('.nc'))
    if not files:
        return jsonify({'error': 'No trajectory NC files found'}), 404

    try:
        sig = _dir_signature(traj_dir, files)
    except Exception:
        sig = None

    cached = _surf_cache.get(dep)
    if cached and cached.get('sig') == sig:
        return jsonify(cached['data'])

    # 1 — disk cache (survives server restarts)
    disk_data = _load_disk_cache(dep, 'surfacings', sig)
    if disk_data:
        _surf_cache[dep] = {'sig': sig, 'data': disk_data}
        print(f'[server] surfacings: loaded from disk cache', flush=True)
        return jsonify(disk_data)

    # 2 — parallel read with ThreadPoolExecutor
    import numpy as np
    print(f'[server] surfacings: reading {len(files)} files with {_TRAJ_WORKERS} threads…', flush=True)

    file_paths = [os.path.join(traj_dir, f) for f in files]
    t_chunks, lat_chunks, lon_chunks, gps_chunks = [], [], [], []

    with ThreadPoolExecutor(max_workers=_TRAJ_WORKERS) as ex:
        for result in ex.map(_nc_read_surf, file_paths):
            if result is None:
                continue
            t_chunks.append(result['t'])
            lat_chunks.append(result['lat'])
            lon_chunks.append(result['lon'])
            gps_chunks.append(result['gps_lat'])

    if not t_chunks:
        return jsonify({'error': 'No usable data found'}), 404

    t_all   = np.concatenate(t_chunks)
    lat_all = np.concatenate(lat_chunks)
    lon_all = np.concatenate(lon_chunks)
    gps_lat = np.concatenate(gps_chunks)

    sort_idx = np.argsort(t_all, kind='stable')
    t_all    = t_all[sort_idx]
    lat_all  = lat_all[sort_idx]
    lon_all  = lon_all[sort_idx]
    gps_lat  = gps_lat[sort_idx]

    # Use m_gps_lat (sparse — only present at actual GPS surfacings) to identify
    # valid surfacing positions; latitude/longitude are interpolated everywhere
    gps_valid = np.isfinite(gps_lat) & (gps_lat != 0.0)
    if gps_valid.sum() > 0:
        valid = gps_valid
    else:
        valid = np.isfinite(lat_all) & np.isfinite(lon_all) & (lat_all != 0.0) & (lon_all != 0.0)

    def _to_iso(epoch_arr):
        out = []
        for ts in epoch_arr:
            try:
                out.append(datetime.datetime.utcfromtimestamp(float(ts)).strftime('%Y-%m-%dT%H:%M:%SZ'))
            except (ValueError, OSError, OverflowError):
                out.append('')
        return out

    data = {
        'times':     _to_iso(t_all[valid]),
        'lats':      [round(float(x), 6) for x in lat_all[valid]],
        'lons':      [round(float(x), 6) for x in lon_all[valid]],
        'count':     int(valid.sum()),
        'n_records': int(t_all.size),
        'n_files':   len(files),
    }

    _surf_cache[dep] = {'sig': sig, 'data': data}
    _save_disk_cache(dep, 'surfacings', sig, data)
    print(f'[server] surfacings done: {int(valid.sum())} GPS fixes from {len(files)} files', flush=True)
    return jsonify(data)


# ── Trajectory report + KMZ ───────────────────────────────────────────────────
@app.route('/api/deployment/<dep>/trajectory-report')
def trajectory_report(dep):
    """
    Read all trajectory-science NC files, extract key flight variables, and
    return a JSON report with surfacing positions, subsampled timeseries, and
    summary statistics.  Result is cached in memory keyed by dep + newest mtime.
    """
    import numpy as np

    traj_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
    if not os.path.isdir(traj_dir):
        return jsonify({'error': f'Trajectory directory not found: {traj_dir}'}), 404

    files = sorted(f for f in os.listdir(traj_dir) if f.endswith('.nc'))
    if not files:
        return jsonify({'error': 'No trajectory NC files found'}), 404

    try:
        sig = _dir_signature(traj_dir, files)
    except Exception:
        sig = None

    cached = _traj_report_cache.get(dep)
    if cached and cached.get('sig') == sig:
        return jsonify(cached['data'])

    # 1 — disk cache
    disk_data = _load_disk_cache(dep, 'trajectory_report', sig)
    if disk_data:
        _traj_report_cache[dep] = {'sig': sig, 'data': disk_data}
        print(f'[server] trajectory report: loaded from disk cache', flush=True)
        return jsonify(disk_data)

    # 2 — read files (serialised through _nc_lock to avoid HDF5 thread issues)
    import numpy as np
    import traceback as _tb
    print(f'[server] trajectory report: reading {len(files)} files…', flush=True)

    try:
        file_paths = [os.path.join(traj_dir, f) for f in files]
        t_chunks   = []
        var_chunks = {v: [] for v in _TRAJ_PHYS_VARS}
        skipped    = 0

        with ThreadPoolExecutor(max_workers=_TRAJ_WORKERS) as ex:
            for result in ex.map(_nc_read_traj, file_paths):
                if result is None:
                    skipped += 1
                    continue
                t_chunks.append(result['t'])
                for v in _TRAJ_PHYS_VARS:
                    var_chunks[v].append(result[v])

        if not t_chunks:
            return jsonify({'error': 'No usable data found in trajectory NC files'}), 404

        t_all    = np.concatenate(t_chunks)
        sort_idx = np.argsort(t_all, kind='stable')
        t_all    = t_all[sort_idx]

        vars_all = {}
        for v in _TRAJ_PHYS_VARS:
            vars_all[v] = np.concatenate(var_chunks[v])[sort_idx] if var_chunks[v] else np.full(t_all.size, np.nan)

        n_total = t_all.size

        # Unix-epoch seconds -> ISO-8601 string
        def to_iso(epoch_arr):
            out = []
            for ts in epoch_arr:
                try:
                    out.append(datetime.datetime.utcfromtimestamp(float(ts)).strftime('%Y-%m-%dT%H:%M:%SZ'))
                except (ValueError, OSError, OverflowError):
                    out.append('')
            return out

        # GPS surfacings: use m_gps_lat/lon (sparse, only at actual GPS fixes)
        # latitude/longitude are interpolated everywhere so they can't count surfacings
        lat_all = vars_all['latitude']
        lon_all = vars_all['longitude']
        gps_lat = vars_all['m_gps_lat']
        gps_lon = vars_all['m_gps_lon']
        gps_valid = np.isfinite(gps_lat) & np.isfinite(gps_lon) & (gps_lat != 0.0) & (gps_lon != 0.0)
        if gps_valid.sum() > 0:
            valid = gps_valid
        else:
            # fallback: no m_gps_lat data (older file format), use interpolated lat/lon
            valid = np.isfinite(lat_all) & np.isfinite(lon_all) & (lat_all != 0.0) & (lon_all != 0.0)

        # Subsample timeseries to at most 10 000 points
        MAX_TS = 10_000
        if n_total > MAX_TS:
            sub_idx = np.round(np.linspace(0, n_total - 1, MAX_TS)).astype(int)
        else:
            sub_idx = np.arange(n_total)

        RAD2DEG = 180.0 / np.pi

        def sub_to_list(arr, scale=1.0, decimals=4):
            v = arr[sub_idx] * scale
            return [None if not np.isfinite(x) else round(float(x), decimals) for x in v]

        timeseries = {
            'time':    to_iso(t_all[sub_idx]),
            'pitch':   sub_to_list(vars_all['m_pitch'],         RAD2DEG, 2),
            'roll':    sub_to_list(vars_all['m_roll'],          RAD2DEG, 2),
            'heading': sub_to_list(vars_all['m_heading'],       RAD2DEG, 2),
            'battery': sub_to_list(vars_all['m_battery'],       1.0,     3),
            'coulomb': sub_to_list(vars_all['m_coulomb_amphr'], 1.0,     3),
            'vacuum':  sub_to_list(vars_all['m_vacuum'],        1.0,     3),
            'depth':   sub_to_list(vars_all['m_depth'],         1.0,     2),
        }

        def safe_stats(arr, scale=1.0):
            v = arr[np.isfinite(arr)] * scale
            if v.size == 0:
                return {'min': None, 'max': None, 'mean': None}
            return {'min':  round(float(np.min(v)),  3),
                    'max':  round(float(np.max(v)),  3),
                    'mean': round(float(np.mean(v)), 3)}

        coulomb_valid = vars_all['m_coulomb_amphr'][np.isfinite(vars_all['m_coulomb_amphr'])]
        total_ah = (round(float(np.max(coulomb_valid) - np.min(coulomb_valid)), 2)
                    if coulomb_valid.size > 1 else None)

        data = {
            'surfacings': {
                'times': to_iso(t_all[valid]),
                'lats':  [round(float(x), 6) for x in lat_all[valid]],
                'lons':  [round(float(x), 6) for x in lon_all[valid]],
                'count': int(valid.sum()),
            },
            'timeseries': timeseries,
            'stats': {
                'n_files':       len(files),
                'n_skipped':     skipped,
                'n_records':     int(n_total),
                'n_surfacings':  int(valid.sum()),
                'pitch_deg':     safe_stats(vars_all['m_pitch'],         RAD2DEG),
                'roll_deg':      safe_stats(vars_all['m_roll'],          RAD2DEG),
                'battery_v':     safe_stats(vars_all['m_battery']),
                'coulomb_ah':    safe_stats(vars_all['m_coulomb_amphr']),
                'total_ah_used': total_ah,
                'vacuum_inhg':   safe_stats(vars_all['m_vacuum']),
            },
        }

        _traj_report_cache[dep] = {'sig': sig, 'data': data}
        _save_disk_cache(dep, 'trajectory_report', sig, data)
        print(f'[server] trajectory report done: {n_total:,} records, '
              f'{int(valid.sum())} surfacings, {skipped} files skipped', flush=True)
        return jsonify(data)

    except Exception as exc:
        err_msg = f'{type(exc).__name__}: {exc}'
        print(f'[server] trajectory report ERROR: {err_msg}', flush=True)
        print(_tb.format_exc(), flush=True)
        return jsonify({'error': err_msg}), 500


@app.route('/api/deployment/<dep>/kmz')
def download_kmz(dep):
    """Generate and stream a KMZ file of the glider track (GPS surfacings)."""
    import zipfile

    # Prefer the lightweight surfacings cache; fall back to full trajectory
    # cache; fall back further to disk cache (survives server restarts) if
    # neither is in memory yet -- same pattern as generate_map_image_route
    # and flight_stats_report_html, which already do this.
    surf_cached = _surf_cache.get(dep)
    traj_cached = _traj_report_cache.get(dep)

    if not surf_cached and not traj_cached:
        traj_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
        if os.path.isdir(traj_dir):
            try:
                files = [f for f in os.listdir(traj_dir) if f.endswith('.nc')]
                if files:
                    sig = _dir_signature(traj_dir, files)
                    disk = _load_disk_cache(dep, 'surfacings', sig)
                    if disk:
                        surf_cached = {'data': disk}
                    else:
                        disk = _load_disk_cache(dep, 'trajectory_report', sig)
                        if disk:
                            traj_cached = {'data': disk}
            except Exception:
                pass

    if surf_cached:
        sd    = surf_cached['data']
        lats  = sd.get('lats',  [])
        lons  = sd.get('lons',  [])
        times = sd.get('times', [])
    elif traj_cached:
        sd    = traj_cached['data'].get('surfacings', {})
        lats  = sd.get('lats',  [])
        lons  = sd.get('lons',  [])
        times = sd.get('times', [])
    else:
        return jsonify({'error': 'No data cached — open the Map tab first.'}), 404

    if not lats:
        return jsonify({'error': 'No GPS surfacings available for KMZ export'}), 404

    # KML track LineString
    coord_lines = '\n'.join(f'          {lon},{lat},0' for lat, lon in zip(lats, lons))

    # KML Placemark per surfacing
    pms = []
    for i, (lat, lon, t) in enumerate(zip(lats, lons, times), 1):
        pms.append(f'''    <Placemark>
      <name>#{i}</name>
      <description>{t}</description>
      <styleUrl>#dot</styleUrl>
      <Point><coordinates>{lon},{lat},0</coordinates></Point>
    </Placemark>''')

    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{dep} Glider Track</name>
    <Style id="track">
      <LineStyle><color>ffff8800</color><width>2</width></LineStyle>
      <PolyStyle><fill>0</fill></PolyStyle>
    </Style>
    <Style id="dot">
      <IconStyle>
        <color>ff0066ff</color><scale>0.55</scale>
        <Icon><href>http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png</href></Icon>
      </IconStyle>
      <LabelStyle><scale>0</scale></LabelStyle>
    </Style>
    <Placemark>
      <name>Track</name>
      <styleUrl>#track</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <coordinates>
{coord_lines}
        </coordinates>
      </LineString>
    </Placemark>
{''.join(pms)}
  </Document>
</kml>'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    buf.seek(0)

    return send_file(
        buf,
        mimetype='application/vnd.google-earth.kmz',
        as_attachment=True,
        download_name=f'{dep}_glider_track.kmz',
    )


@app.route('/api/deployment/<dep>/generate-map-image', methods=['POST'])
def generate_map_image_route(dep):
    """Generate a static glider track map PNG from cached surfacing data."""
    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
    import base64, io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    # Use in-memory cache if available, else load disk cache
    cached = _surf_cache.get(dep)
    if not cached:
        traj_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
        if os.path.isdir(traj_dir):
            try:
                files = [f for f in os.listdir(traj_dir) if f.endswith('.nc')]
                if files:
                    sig = _dir_signature(traj_dir, files)
                    disk = _load_disk_cache(dep, 'surfacings', sig)
                    if disk:
                        cached = {'data': disk}
            except Exception:
                pass

    if not cached:
        return jsonify({'error': 'No surfacing data cached. Open the Map tab first.'}), 404

    lats = cached['data'].get('lats', [])
    lons = cached['data'].get('lons', [])
    if not lats:
        return jsonify({'error': 'No GPS surfacing positions found'}), 404

    dep_dir_path = _dep_dir(dep)
    figs_dir = os.path.join(dep_dir_path, 'figs')
    os.makedirs(figs_dir, exist_ok=True)
    out_path = os.path.join(figs_dir, 'glider_track_map.png')

    try:
        fig, ax = plt.subplots(figsize=(10, 7))
        ax.plot(lons, lats, '-', color='#3b82f6', linewidth=1.2, alpha=0.8)
        ax.scatter([lons[0]], [lats[0]], c='#22c55e', s=120, zorder=5, label='Start')
        ax.scatter([lons[-1]], [lats[-1]], c='#ef4444', s=120, zorder=5, label='End')
        ax.set_xlabel('Longitude', fontsize=11)
        ax.set_ylabel('Latitude', fontsize=11)
        ax.set_title(f'Glider Track: {dep}', fontsize=13, fontweight='bold')
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_aspect('equal', adjustable='box')
        fig.tight_layout()
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        return jsonify({'ok': True, 'filename': 'glider_track_map.png'})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/deployment/<dep>/flight-stats-report')
def flight_stats_report_html(dep):
    """Generate a self-contained HTML report for flight statistics."""
    import base64, io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import datetime as _datetime

    # Load trajectory report data from cache
    cached = _traj_report_cache.get(dep)
    if not cached:
        traj_dir = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')
        if os.path.isdir(traj_dir):
            try:
                files = [f for f in os.listdir(traj_dir) if f.endswith('.nc')]
                if files:
                    sig = _dir_signature(traj_dir, files)
                    disk = _load_disk_cache(dep, 'trajectory_report', sig)
                    if disk:
                        cached = {'data': disk}
            except Exception:
                pass

    if not cached:
        html = ('<html><body style="font-family:sans-serif;padding:40px">'
                '<h2>No flight data available</h2>'
                '<p>Open the Flight Stats tab in the app first to load the data, then try again.</p>'
                '</body></html>')
        return Response(html, mimetype='text/html'), 404

    data       = cached['data']
    timeseries = data.get('timeseries', {})
    stats      = data.get('stats', {})

    def _b64(fig):
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight',
                    facecolor='#ffffff')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

    def _fv(v, d=2):
        return 'N/A' if v is None else f'{v:.{d}f}'

    times   = timeseries.get('time', [])
    batt    = timeseries.get('battery', [])
    pitch   = timeseries.get('pitch', [])
    roll    = timeseries.get('roll', [])
    vacuum  = timeseries.get('vacuum', [])

    MUTED = '#64748b'
    charts_html = ''
    tick_n = max(1, len(times) // 8) if times else 1

    if times and batt:
        fig, ax = plt.subplots(figsize=(12, 3.2))
        ax.plot(range(len(times)), batt, color='#3b82f6', linewidth=0.9)
        ax.set_ylabel('Volts', color=MUTED, fontsize=10)
        ax.set_title('Battery Voltage', fontsize=11)
        ax.grid(True, alpha=0.25, linestyle='--')
        ax.set_xticks(range(0, len(times), tick_n))
        ax.set_xticklabels([times[i][:10] for i in range(0, len(times), tick_n)],
                           rotation=28, ha='right', fontsize=9)
        fig.tight_layout()
        charts_html += f'<div class="chart"><h3>Battery Voltage</h3><img src="data:image/png;base64,{_b64(fig)}"></div>'

    if times and pitch and roll:
        fig, ax = plt.subplots(figsize=(12, 3.2))
        ax.plot(range(len(times)), pitch, label='Pitch', color='#f97316', linewidth=0.9)
        ax.plot(range(len(times)), roll,  label='Roll',  color='#8b5cf6', linewidth=0.9)
        ax.axhline(0, color='#94a3b8', linewidth=0.5, linestyle='--')
        ax.set_ylabel('Degrees', color=MUTED, fontsize=10)
        ax.set_title('Pitch & Roll', fontsize=11)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.25, linestyle='--')
        ax.set_xticks(range(0, len(times), tick_n))
        ax.set_xticklabels([times[i][:10] for i in range(0, len(times), tick_n)],
                           rotation=28, ha='right', fontsize=9)
        fig.tight_layout()
        charts_html += f'<div class="chart"><h3>Pitch &amp; Roll</h3><img src="data:image/png;base64,{_b64(fig)}"></div>'

    if times and vacuum:
        fig, ax = plt.subplots(figsize=(12, 3.2))
        ax.plot(range(len(times)), vacuum, color='#22c55e', linewidth=0.9)
        ax.set_ylabel('inHg', color=MUTED, fontsize=10)
        ax.set_title('Internal Vacuum', fontsize=11)
        ax.grid(True, alpha=0.25, linestyle='--')
        ax.set_xticks(range(0, len(times), tick_n))
        ax.set_xticklabels([times[i][:10] for i in range(0, len(times), tick_n)],
                           rotation=28, ha='right', fontsize=9)
        fig.tight_layout()
        charts_html += f'<div class="chart"><h3>Internal Vacuum</h3><img src="data:image/png;base64,{_b64(fig)}"></div>'

    sv = stats.get
    stat_items = [
        ('Records',        f"{sv('n_records', 'N/A'):,}" if isinstance(sv('n_records'), int) else 'N/A'),
        ('Surfacings',     f"{sv('n_surfacings', 'N/A'):,}" if isinstance(sv('n_surfacings'), int) else 'N/A'),
        ('Battery Min',    f"{_fv(sv('battery_v',{}).get('min'))} V"),
        ('Battery Max',    f"{_fv(sv('battery_v',{}).get('max'))} V"),
        ('Amp-Hours Used', f"{_fv(sv('total_ah_used'), 1)} Ah" if sv('total_ah_used') is not None else 'N/A'),
        ('Mean Pitch',     f"{_fv(sv('pitch_deg',{}).get('mean'),1)}&deg;"),
        ('Mean Roll',      f"{_fv(sv('roll_deg', {}).get('mean'),1)}&deg;"),
        ('Mean Vacuum',    f"{_fv(sv('vacuum_inhg',{}).get('mean'))} inHg"),
    ]
    cards_html = ''.join(
        f'<div class="card"><div class="val">{v}</div><div class="lbl">{l}</div></div>'
        for l, v in stat_items)

    now = _datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    html = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Flight Statistics &mdash; {dep}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8fafc;color:#1e293b;padding:24px}}
.hdr{{background:linear-gradient(135deg,#1e3a5f,#2d6a9f);color:#fff;padding:22px 28px;border-radius:10px;margin-bottom:22px}}
.hdr h1{{font-size:21px;font-weight:700;margin-bottom:3px}}.hdr p{{font-size:12px;opacity:.8}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.val{{font-size:20px;font-weight:700;color:#1e40af}}.lbl{{font-size:10px;color:#64748b;margin-top:3px;text-transform:uppercase;letter-spacing:.4px}}
.chart{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.chart h3{{font-size:13px;color:#475569;margin-bottom:10px;font-weight:600}}
.chart img{{width:100%;border-radius:4px}}
.foot{{margin-top:28px;text-align:center;font-size:11px;color:#94a3b8}}
</style></head><body>
<div class="hdr"><h1>Flight Statistics Report</h1><p>{dep} &nbsp;&middot;&nbsp; {now}</p></div>
<div class="grid">{cards_html}</div>
{charts_html}
<div class="foot">UVI Glider Processing Pipeline &nbsp;&middot;&nbsp; {now}</div>
</body></html>'''

    return Response(html, mimetype='text/html; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="flight_stats_{dep}.html"'})


def _adcp_run_stats(run_dir):
    """Compute summary statistics for one AD2CP run from existing output files.
    Returns a nested dict; never raises."""
    stats = {}
    try:
        import numpy as np
    except ImportError:
        return stats

    # ── DAC ────────────────────────────────────────────────────────────────────
    dac_path = _rf(run_dir, 'dac.npz')
    if os.path.exists(dac_path):
        try:
            data    = np.load(dac_path, allow_pickle=True)
            dac     = data['dac'].astype(float)
            valid   = np.isfinite(dac[:, 0]) & np.isfinite(dac[:, 1])
            n_total = len(dac)
            n_valid = int(valid.sum())
            if n_valid > 0:
                spd  = np.sqrt(dac[valid, 0]**2 + dac[valid, 1]**2)
                mu   = float(np.nanmean(dac[valid, 0]))
                mv   = float(np.nanmean(dac[valid, 1]))
                deg  = (90 - np.degrees(np.arctan2(mv, mu))) % 360
                dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                        'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
                card = dirs[int((deg + 11.25) / 22.5) % 16]
                stats['dac'] = {
                    'n_total':   n_total,
                    'n_valid':   n_valid,
                    'mean_spd':  round(float(np.mean(spd)), 3),
                    'max_spd':   round(float(np.max(spd)),  3),
                    'direction': f'{card} ({deg:.0f}°)',
                }
        except Exception:
            pass

    # ── Bias-corrected currents ────────────────────────────────────────────────
    bias_path = _rf(run_dir, 'currents_bias_corrected.nc')
    if os.path.exists(bias_path):
        try:
            import xarray as xr
            import pandas as pd
            with _nc_lock:
                with xr.open_dataset(bias_path, decode_times=True) as ds:
                    vel_E = ds['velocity_E_DAC_reference_sb_corrected'].values
                    vel_N = ds['velocity_N_DAC_reference_sb_corrected'].values
                    depth = ds.depth.values
                    t_raw = ds.time.values   # already datetime64 with decode_times=True

            spd   = np.sqrt(vel_E**2 + vel_N**2)
            valid = np.isfinite(spd)

            try:
                t_pd    = pd.to_datetime(t_raw)
                t_ok    = t_pd[pd.notnull(t_pd)]
                t_start = str(t_ok.min().date()) if len(t_ok) else 'N/A'
                t_end   = str(t_ok.max().date()) if len(t_ok) else 'N/A'
            except Exception:
                t_start = t_end = 'N/A'

            dcov = valid.mean(axis=1)
            good = dcov >= 0.5
            dmin = int(round(float(depth[good].min()), 0)) if good.any() else int(round(float(depth.min()), 0))
            dmax = int(round(float(depth[good].max()), 0)) if good.any() else int(round(float(depth.max()), 0))

            stats['currents'] = {
                'n_profiles': int(valid.any(axis=0).sum()),
                'date_start': t_start,
                'date_end':   t_end,
                'depth_min':  dmin,
                'depth_max':  dmax,
                'fill_pct':   round(100 * float(valid.mean()), 1),
                'mean_spd':   round(float(np.nanmean(spd)), 3),
                'max_spd':    round(float(np.nanmax(spd)),  3),
            }
        except Exception:
            pass

    # ── Sub-tidal / tidal variance partition ───────────────────────────────────
    sub_path = _rf(run_dir, 'currents_subtidal.nc')
    if os.path.exists(sub_path):
        try:
            import xarray as xr
            with _nc_lock:
                with xr.open_dataset(sub_path, decode_times=False) as ds:
                    u_s = ds['velocity_E_subtidal'].values
                    v_s = ds['velocity_N_subtidal'].values
                    u_t = ds['velocity_E_tidal'].values
                    v_t = ds['velocity_N_tidal'].values
            total_var = float(np.nanvar(u_s + u_t) + np.nanvar(v_s + v_t))
            tidal_var = float(np.nanvar(u_t) + np.nanvar(v_t))
            if total_var > 0:
                tp = round(100 * tidal_var / total_var, 1)
                stats['tidal'] = {'tidal_pct': tp, 'subtidal_pct': round(100 - tp, 1)}
        except Exception:
            pass

    # ── Shear ──────────────────────────────────────────────────────────────────
    # Use 99th percentile: absolute max is noise-dominated (derivative amplifies spikes).
    # depth_offset is range-from-glider (negative, upward), not absolute depth — omit it.
    shear_path = _rf(run_dir, 'shear_processed.nc')
    if os.path.exists(shear_path):
        try:
            import xarray as xr
            with _nc_lock:
                with xr.open_dataset(shear_path, decode_times=False) as ds:
                    if 'Sh_E' in ds and 'Sh_N' in ds:
                        sh_mag = np.sqrt(ds['Sh_E'].values**2 + ds['Sh_N'].values**2)
                        finite = sh_mag[np.isfinite(sh_mag)]
                        if finite.size > 0:
                            stats['shear'] = {
                                'p99_shear': round(float(np.percentile(finite, 99)),   4),
                                'p50_shear': round(float(np.percentile(finite, 50)),   4),
                            }
        except Exception:
            pass

    return stats


def _adcp_pipeline_summary_html(base, run_dirs):
    """Return (extra_css, html_snippet) with a per-stage pipeline breakdown."""
    import numpy as np
    try:
        import xarray as xr
    except ImportError:
        return '', ''

    # ── helpers ───────────────────────────────────────────────────────────────
    def _card(label, value, unit='', small=False):
        vs = ' style="font-size:15px"' if small else ''
        uh = f'<span class="stat-unit"> {unit}</span>' if unit else ''
        return (f'<div class="info-card">'
                f'<div class="label">{label}</div>'
                f'<div class="value"{vs}>{value}{uh}</div>'
                f'</div>')

    def _grid(cards):
        return f'<div class="info-grid" style="margin:10px 0 18px">{cards}</div>'

    def _desc(text):
        return f'<p class="ps-desc">{text}</p>'

    def _pct_badge(pct_float):
        cls = 'ret-pass' if pct_float >= 80 else 'ret-warn'
        return f'<span class="{cls}">{pct_float:.1f}%</span>'

    def _tbl(headers, widths, rows):
        ths = ''.join(f'<th style="width:{w}">{h}</th>' for h, w in zip(headers, widths))
        trs = ''
        for ri, row in enumerate(rows):
            cls = ' class="alt"' if ri % 2 else ''
            tds = ''.join(f'<td>{c}</td>' for c in row)
            trs += f'<tr{cls}>{tds}</tr>'
        return f'<table class="ps-tbl"><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>'

    stages_html = ''

    # ── Stage 1 — Load & Combine ──────────────────────────────────────────────
    glider_nc = os.path.join(base, 'glider_ad2cp_datetime_timecoord.nc')
    if os.path.exists(glider_nc):
        try:
            with _nc_lock:
                with xr.open_dataset(glider_nc) as ds:
                    n_pts   = ds.sizes.get('time', 0)
                    t       = ds.time.values
                    t_start = str(t[0])[:10] if len(t) else '?'
                    t_end   = str(t[-1])[:10] if len(t) else '?'
                    pres    = ds.get('pressure')
                    temp    = ds.get('temperature')
                    sal     = ds.get('salinity')
                    lon     = ds.get('longitude')
                    lat     = ds.get('latitude')
                    def _rng(v):
                        if v is None: return '—'
                        a = v.values.astype(float)
                        f = a[np.isfinite(a)]
                        return f'{f.min():.2f} – {f.max():.2f}' if len(f) else '—'
                    def _valid(v):
                        if v is None: return '—', 0
                        a = v.values.astype(float)
                        n = int(np.isfinite(a).sum())
                        return f'{n:,} / {a.size:,}', n
                    sci_valid, _ = _valid(pres)
                    # Build the value strings here, while ds is still open -- _rng()/_valid()
                    # read .values on lazy DataArrays pulled from ds, so this must happen
                    # before the file closes, not after (the original code read them after
                    # close and only worked by luck of xarray's caching).
                    cards = (_card('Total Time Points', f'{n_pts:,}')
                           + _card('Start Date', t_start, small=True)
                           + _card('End Date', t_end, small=True)
                           + _card('Valid Science Samples', sci_valid, small=True)
                           + _card('Pressure', _rng(pres), 'dbar', small=True)
                           + _card('Temperature', _rng(temp), '°C', small=True)
                           + _card('Salinity', _rng(sal), 'PSU', small=True)
                           + _card('Longitude', _rng(lon), '°', small=True)
                           + _card('Latitude', _rng(lat), '°', small=True))
            stages_html += (
                f'<h3>Stage 1 — Load &amp; Combine</h3>'
                + _desc('Merges all glider science files and AD2CP data into a single time-aligned NetCDF. '
                        'GPS positions are interpolated across the full record so every sample has a location.')
                + _grid(cards))
        except Exception:
            pass

    # ── Stage 2 — GPS Pairing ─────────────────────────────────────────────────
    gps_path = os.path.join(base, 'gps_data.npz')
    if os.path.exists(gps_path):
        try:
            gps     = np.load(gps_path, allow_pickle=True)
            n_pre   = len(gps['gps_predive'])
            n_post  = len(gps['gps_postdive'])
            cards = (_card('Pre-Dive GPS Fixes', f'{n_pre:,}')
                   + _card('Post-Dive GPS Fixes', f'{n_post:,}')
                   + _card('Total Dive Pairs', f'{n_pre:,}'))
            stages_html += (
                f'<h3>Stage 2 — GPS Pairing</h3>'
                + _desc('Matches each pre-dive GPS fix to the corresponding post-dive fix. '
                        'Each pair defines one complete dive and is used later to compute depth-averaged currents.')
                + _grid(cards))
        except Exception:
            pass

    # ── Stages 3-7 — per run ─────────────────────────────────────────────────
    multi_run = len(run_dirs) > 1
    for _run_label, run_dir in run_dirs.items():

        run_prefix = f'<span style="font-family:monospace;font-size:13px;color:#5d6d7e;font-weight:400"> — {_run_label}</span>' if multi_run else ''

        # Stage 3 — Process Shear
        shear_path = _rf(run_dir, 'shear_processed.nc')
        if os.path.exists(shear_path):
            try:
                with _nc_lock:
                    with xr.open_dataset(shear_path) as ds:
                        n_pings  = ds.sizes.get('time', 0)
                        n_bins   = ds.sizes.get('bin', 0)
                        n_gbins  = ds.sizes.get('gridded_bin', 0)
                        t        = ds.time.values
                        t_start  = str(t[0])[:10] if len(t) else '?'
                        t_end    = str(t[-1])[:10] if len(t) else '?'
                        e_var    = ds.get('E');  sh_var = ds.get('Sh_E')
                        def _vpct(v):
                            if v is None or not v.size: return '—', '—', 0
                            a = v.values.astype(float)
                            n = int(np.isfinite(a).sum())
                            return f'{n:,}', f'{100*n/a.size:.1f}%', n
                        e_n, e_pct, _ = _vpct(e_var)
                        sh_n, sh_pct, _ = _vpct(sh_var)
                total_bins = n_pings * n_gbins
                cards = (_card('Total ADCP Pings', f'{n_pings:,}')
                       + _card('Raw Bins / Ping', str(n_bins))
                       + _card('Gridded Depth Bins', str(n_gbins))
                       + _card('Total Bin-Pings', f'{total_bins:,}')
                       + _card('Valid Raw Velocity', f'{e_pct}', small=True)
                       + _card('Valid Shear', f'{sh_pct}', small=True)
                       + _card('ADCP Start', t_start, small=True)
                       + _card('ADCP End', t_end, small=True))
                stages_html += (
                    f'<h3>Stage 3 — Process Shear{run_prefix}</h3>'
                    + _desc('Converts raw beam velocities to ocean currents (East/North/Up) and computes vertical shear. '
                            'Pings with acoustic correlation below the quality threshold (~50 counts) are discarded. '
                            'Low valid % is typical in open-ocean deployments with sparse biological scatterers.')
                    + _grid(cards))
            except Exception:
                pass

        # Stage 4 — Write Glider NC  (brief — just the ADCP active window)
        shear_path = _rf(run_dir, 'shear_processed.nc')
        if os.path.exists(shear_path):
            try:
                with _nc_lock:
                    with xr.open_dataset(shear_path) as ds:
                        t = ds.time.values
                gps2  = np.load(gps_path, allow_pickle=True) if os.path.exists(gps_path) else None
                n_gps = len(gps2['gps_predive']) if gps2 is not None else '—'
                cards = (_card('ADCP Active From', str(t[0])[:10], small=True)
                       + _card('ADCP Active Until', str(t[-1])[:10], small=True)
                       + _card('GPS Pairs in Window', f'{n_gps:,}' if isinstance(n_gps, int) else n_gps))
                stages_html += (
                    f'<h3>Stage 4 — Write Glider NC{run_prefix}</h3>'
                    + _desc('Generates a synthetic glider state file (pressure, lat/lon) covering only the AD2CP active '
                            'period. The AD2CP began pinging after the glider settled — typically days into the mission.')
                    + _grid(cards))
            except Exception:
                pass

        # Stage 5 — Compute Currents
        dac_path  = _rf(run_dir, 'dac.npz')
        curr_path = _rf(run_dir, 'currents.nc')
        if os.path.exists(dac_path) and os.path.exists(curr_path):
            try:
                import re as _re
                dac         = np.load(dac_path, allow_pickle=True)
                n_dives     = len(dac['dive_time'])
                n_valid_dac = int(np.isfinite(dac['dac']).all(axis=1).sum())
                n_half      = n_dives * 2
                with _nc_lock:
                    with xr.open_dataset(curr_path) as ds2:
                        n_profiles  = ds2.sizes.get('profile_index', 0)
                        n_depth     = ds2.sizes.get('depth', 0)
                        d_min       = float(ds2.depth.min()) if 'depth' in ds2.coords else 0
                        d_max       = float(ds2.depth.max()) if 'depth' in ds2.coords else 0

                # xaxis from run label (e.g. 'xaxis1_d10-1000' → 1)
                xaxis = 1
                m = _re.search(r'xaxis(\d+)', _run_label)
                if m:
                    xaxis = int(m.group(1))
                expected_profiles = n_half // xaxis
                ret_pct  = min(100.0, 100 * n_profiles / expected_profiles) if expected_profiles else 0
                dac_pct  = 100 * n_valid_dac / n_dives if n_dives else 0

                cards = (_card('GPS Dive Pairs Input', f'{n_dives:,}')
                       + _card('Half-Profiles (dives × 2)', f'{n_half:,}', small=True)
                       + _card(f'Expected bins (xaxis={xaxis})', f'~{expected_profiles:,}', small=True)
                       + _card('Output Profiles', f'{n_profiles:,}  ' + _pct_badge(ret_pct), small=True)
                       + _card('Depth Levels', f'{n_depth:,}')
                       + _card('Depth Range', f'{d_min:.0f} – {d_max:.0f}', 'm', small=True)
                       + _card('Valid DAC Dives', f'{n_valid_dac:,} / {n_dives:,}  ' + _pct_badge(dac_pct), small=True))
                stages_html += (
                    f'<h3>Stage 5 — Compute Absolute Currents{run_prefix}</h3>'
                    + _desc(f'Integrates shear upward to produce absolute velocity profiles anchored to the GPS '
                            f'depth-averaged current (DAC). Each dive leg (downcast or upcast) is treated as a separate '
                            f'half-profile. With xaxis={xaxis}, every {xaxis} consecutive half-profiles are merged into '
                            f'one output column — reducing {n_half:,} half-profiles to ~{expected_profiles:,} expected bins. '
                            f'The output profile count ({n_profiles:,}) reflects this {xaxis}× binning, '
                            f'not data loss from QC.')
                    + _grid(cards))
            except Exception:
                pass

        # Stage 6 — Bias Correction
        bc_path = _rf(run_dir, 'currents_bias_corrected.nc')
        if os.path.exists(bc_path):
            try:
                import pandas as pd
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                import io, base64

                with _nc_lock:
                    with xr.open_dataset(bc_path) as ds3:
                        def _vstats(key):
                            v = ds3.get(key)
                            if v is None: return None
                            a = v.values.astype(float)
                            f = a[np.isfinite(a)]
                            if not len(f): return None
                            p5, p95 = np.nanpercentile(f, [5, 95])
                            return {'mean': f.mean(), 'min': f.min(), 'max': f.max(),
                                    'p5': p5, 'p95': p95, 'valid': len(f), 'total': a.size}
                        ve_pre  = _vstats('velocity_E_DAC_reference')
                        ve_post = _vstats('velocity_E_DAC_reference_sb_corrected')
                        vn_pre  = _vstats('velocity_N_DAC_reference')
                        vn_post = _vstats('velocity_N_DAC_reference_sb_corrected')

                        # Build daily datapoints chart
                        daily_chart_html = ''
                        try:
                            import traceback as _tb
                            df_bc = ds3.to_dataframe().reset_index()
                            df_bc['date'] = pd.to_datetime(df_bc['time']).dt.date
                            daily = df_bc.groupby('date')['velocity_E_DAC_reference_sb_corrected'].count()
                            avg_pts = daily.mean()

                            fig, ax = plt.subplots(figsize=(11, 3.2))
                            low_thresh = avg_pts * 0.5
                            colors = ['#c0392b' if v < low_thresh else '#2471a3' for v in daily.values]
                            ax.bar(range(len(daily)), daily.values, color=colors, width=0.8)
                            ax.axhline(avg_pts, color='#e67e22', linewidth=1.2, linestyle='--',
                                       label=f'Daily avg ({avg_pts:.0f})')
                            tick_positions = [i for i, d in enumerate(daily.index)
                                              if hasattr(d, 'day') and d.day in (1, 8, 15, 22)]
                            tick_labels    = [f'{daily.index[i].strftime("%b")} {daily.index[i].day}'
                                              for i in tick_positions]
                            ax.set_xticks(tick_positions)
                            ax.set_xticklabels(tick_labels, rotation=30, ha='right', fontsize=9)
                            ax.set_ylabel('Valid velocity cells', fontsize=9)
                            ax.set_title(f'Valid datapoints per day — {_run_label}/currents_bias_corrected.nc', fontsize=10)
                            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{int(x):,}'))
                            ax.legend(fontsize=8)
                            ax.grid(axis='y', alpha=0.3)
                            fig.tight_layout()
                            _buf = io.BytesIO()
                            fig.savefig(_buf, format='png', dpi=130, bbox_inches='tight')
                            plt.close(fig)
                            b64 = base64.b64encode(_buf.getvalue()).decode()
                            daily_chart_html = (
                                f'<p class="ps-desc">Valid bias-corrected velocity cells produced each calendar day '
                                f'({len(daily)} days, {daily.sum():,} total). Most days produce 1,700–2,900 cells; '
                                f'lower days reflect fewer dives or partially overlapping dive legs.</p>'
                                f'<img src="data:image/png;base64,{b64}" '
                                f'style="width:100%;max-width:900px;margin:8px 0 16px" alt="Daily datapoints chart"/>'
                            )
                        except Exception:
                            pass

                fill_pct = 100 * ve_post['valid'] / ve_post['total'] if ve_post else 0

                tbl_rows = []
                for lbl, pre, post in [('East (m/s)', ve_pre, ve_post),
                                        ('North (m/s)', vn_pre, vn_post)]:
                    if pre and post:
                        tbl_rows.append([
                            lbl,
                            f'{pre["mean"]:+.3f}',
                            f'{pre["min"]:.3f} to {pre["max"]:.3f}',
                            f'{post["mean"]:+.3f}',
                            f'{post["min"]:.3f} to {post["max"]:.3f}',
                            f'{post["p5"]:.3f} to {post["p95"]:.3f}',
                        ])

                tbl_html = _tbl(
                    ['Component', 'Mean Before', 'Range Before', 'Mean After', 'Range After', '5th–95th pct After'],
                    ['12%','13%','20%','13%','20%','22%'],
                    tbl_rows)

                cards = (_card('Valid Velocity Cells', f'{ve_post["valid"]:,} / {ve_post["total"]:,}  ' + _pct_badge(fill_pct), small=True)
                         if ve_post else '')
                stages_html += (
                    f'<h3>Stage 6 — Bias Correction{run_prefix}</h3>'
                    + _desc('Removes a systematic depth-varying bias that accumulates during shear integration. '
                            'The bias is estimated by comparing the shear-derived profile against the GPS depth-averaged '
                            'current and subtracted at every depth level. NaN cells remain where no shear was observed.')
                    + _grid(cards)
                    + tbl_html
                    + daily_chart_html)
            except Exception:
                pass

        # Stage 7 — Generate Plots (file list)
        plot_files = {
            'velocity_sections.png': ('Eastward &amp; northward velocity sections (±0.5 m/s)', 'currents_bias_corrected.nc'),
            'speed_section.png':     ('Current speed section (0–0.5 m/s)',                     'currents_bias_corrected.nc'),
            'shear_sections.png':    ('Vertical shear sections (s⁻¹)',                         'shear_processed.nc'),
            'backscatter.png':       ('Acoustic backscatter / amplitude (range axis)',           'shear_processed.nc'),
            'correlation.png':       ('Beam correlation (0–100%, depth axis)',                  'shear_processed.nc'),
            'beam_amplitude.png':    ('Beam amplitude / SNR proxy (counts, depth axis)',        'shear_processed.nc'),
            'snr.png':               ('Beam SNR (dB, depth axis)',                              'shear_processed.nc'),
            'dac_timeseries.png':    ('Depth-averaged current time series',                     'dac.npz'),
            'dac_map_cartopy.png':   ('Glider track &amp; current vector map',                  'dac.npz + gps_data.npz'),
            'tidal_filter.png':      ('Tidal partition (40-h low-pass)',                        'currents_bias_corrected.nc'),
            'eke.png':               ('Eddy kinetic energy (EKE)',                              'currents_bias_corrected.nc'),
            'transport.png':         ('Volume transport',                                       'currents_bias_corrected.nc'),
            'richardson_number.png': ('Richardson number',                                      'shear_processed.nc'),
        }
        prows = []
        for fn, (desc, source) in plot_files.items():
            fp = _rf(run_dir, fn)
            exists = os.path.exists(fp)
            icon   = '<span style="color:#1e8449;font-weight:700">&#10003;</span>' if exists else '<span style="color:#aaa">&#8212;</span>'
            prows.append([icon, f'<code>{fn}</code>', desc, f'<code style="font-size:11px;color:#555">{source}</code>'])
        stages_html += (
            f'<h3>Stage 7 — Generate Plots{run_prefix}</h3>'
            + _desc('Produces standard visualisation products. No further data transformation occurs at this stage.')
            + _tbl(['', 'File', 'Description', 'Source'], ['4%','24%','48%','24%'], prows))

    if not stages_html:
        return '', ''

    extra_css = '''
.ps-desc { font-size:13px; color:#5d6d7e; margin:6px 0 10px; line-height:1.5; }
.ps-tbl  { width:100%; border-collapse:collapse; font-size:12.5px; margin:8px 0 20px; }
.ps-tbl thead tr { background:#2c3e50; color:#ecf0f1; }
.ps-tbl th { padding:8px 12px; text-align:left; font-weight:600; font-size:12px; }
.ps-tbl td { padding:7px 12px; border-bottom:1px solid #eaecef; vertical-align:top; }
.ps-tbl tr.alt td { background:#f4f9fd; }
.ret-pass { color:#1e8449; font-weight:700; }
.ret-warn { color:#b7770d; font-weight:700; }
'''

    section_html = f'''
<section>
  <h2>Pipeline Processing Summary</h2>
  <div class="note">Live data read from processed files for this deployment. Each stage shows actual counts and key metrics.</div>
  {stages_html}
</section>'''

    return extra_css, section_html


@app.route('/api/adcp/report-html')
def adcp_report_html():
    """Generate a self-contained HTML AD2CP report styled like the QC report."""
    import base64
    import datetime as _datetime

    dep = request.args.get('dep', '')
    run = request.args.get('run', '')
    is_dl = request.args.get('dl') == '1'
    if not dep:
        return jsonify({'error': 'dep required'}), 400

    base = _adcp_base_dir(dep, False)
    run_dirs = {}
    if run:
        sub = os.path.join(base, run)
        if os.path.isdir(sub):
            run_dirs = {run: sub}
    else:
        if os.path.isdir(base):
            for entry in sorted(os.listdir(base)):
                sub = os.path.join(base, entry)
                if os.path.isdir(sub):
                    run_dirs[entry] = sub

    if not run_dirs:
        html = ('<html><body style="font-family:sans-serif;padding:40px">'
                '<h2>No AD2CP data available</h2>'
                '<p>No AD2CP runs were found for this deployment. Run the AD2CP stages first, '
                'then try again.</p>'
                '</body></html>')
        return Response(html, mimetype='text/html'), 404

    # Serve cached HTML if it exists and is newer than every file in the run dir(s)
    fname     = f'adcp_report_{dep}{"_"+run if run else ""}.html'
    save_path = os.path.join(base, fname)
    if not is_dl and os.path.isfile(save_path):
        cache_mtime = os.path.getmtime(save_path)
        stale = False
        for rd in run_dirs.values():
            try:
                for f in os.listdir(rd):
                    fp = os.path.join(rd, f)
                    if os.path.isfile(fp) and os.path.getmtime(fp) > cache_mtime:
                        stale = True
                        break
            except OSError:
                stale = True
            if stale:
                break
        if not stale:
            try:
                with open(save_path, 'r', encoding='utf-8') as fh:
                    return Response(fh.read(), mimetype='text/html')
            except Exception:
                pass  # fall through to regenerate

    # Plot groups — matches the stage order in the pipeline
    PLOT_GROUPS = [
        ('Bias Correction',            [('bias_diagnostic.png',   'Bias Diagnostic')]),
        ('Velocity &amp; Speed',       [('velocity_sections.png', 'Velocity Sections (East / North)'),
                                        ('speed_section.png',     'Speed Section')]),
        ('Shear &amp; Backscatter',    [('shear_sections.png',    'Shear Sections'),
                                        ('backscatter.png',       'Backscatter (Acoustic Amplitude)')]),
        ('Beam Quality',               [('correlation.png',       'Beam Correlation'),
                                        ('beam_amplitude.png',    'Beam Amplitude (SNR Proxy)'),
                                        ('snr.png',               'Beam SNR (dB)')]),
        ('Depth-Averaged Currents',    [('dac_timeseries.png',    'DAC Time Series'),
                                        ('dac_map_cartopy.png',   'Current Vectors Map')]),
        ('Tidal Analysis',             [('tidal_filter.png',      'Tidal Filter (40-h low-pass)')]),
        ('Diagnostics',                [('eke.png',               'Eddy Kinetic Energy (EKE)'),
                                        ('transport.png',         'Volume Transport'),
                                        ('richardson_number.png', 'Richardson Number')]),
    ]

    STAGE_LABELS = {
        'process_shear':    'Process Shear',
        'compute_currents': 'Compute Currents',
        'bias_correction':  'Bias Correction',
        'generate_plots':   'Generate Plots',
        'tidal_filter':     'Tidal Filter',
        'diagnostics':      'Diagnostics',
    }

    def _embed(path):
        try:
            with open(path, 'rb') as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            return None

    def _stage_done(run_dir, stage):
        files = _ADCP_OUTPUTS.get(stage, [])
        lbl = os.path.basename(os.path.normpath(run_dir)) + '_'
        return bool(files) and all(
            os.path.exists(os.path.join(run_dir, lbl + f)) for f in files)

    now = _datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    n_runs = len(run_dirs)

    funnel_css, funnel_html = _adcp_pipeline_summary_html(base, run_dirs)

    # TOC entries
    toc_html = ''.join(
        f'<a href="#run-{i+1}">{i+1}. {lbl}</a>'
        for i, lbl in enumerate(run_dirs.keys()))

    sections_html = ''
    for i, (run_label, run_dir) in enumerate(run_dirs.items(), 1):
        # Stage status badges
        badges_html = ''
        for stage_key, stage_lbl in STAGE_LABELS.items():
            done = _stage_done(run_dir, stage_key)
            cls  = 'stage-done' if done else 'stage-pend'
            sym  = '&#10003;' if done else '&#8212;'
            badges_html += f'<span class="stage-badge {cls}">{sym} {stage_lbl}</span>'

        # Info cards: count existing files
        nc_files  = [f for f in os.listdir(run_dir) if f.endswith('.nc')]  if os.path.isdir(run_dir) else []
        png_files = [f for f in os.listdir(run_dir) if f.endswith('.png')] if os.path.isdir(run_dir) else []

        info_html = (
            f'<div class="info-card"><div class="label">Run Label</div>'
            f'<div class="value" style="font-size:15px;font-family:monospace">{run_label}</div></div>'
            f'<div class="info-card"><div class="label">NC Files</div>'
            f'<div class="value">{len(nc_files)}</div></div>'
            f'<div class="info-card"><div class="label">Plot Files</div>'
            f'<div class="value">{len(png_files)}</div></div>'
            f'<div class="info-card"><div class="label">Stages Complete</div>'
            f'<div class="value">{sum(_stage_done(run_dir, s) for s in STAGE_LABELS)}'
            f' / {len(STAGE_LABELS)}</div></div>'
        )

        # Scientific statistics
        def _sc(label, value, unit='', small=False):
            vs = ' style="font-size:15px"' if small else ''
            uh = f'<span class="stat-unit"> {unit}</span>' if unit else ''
            return (f'<div class="info-card"><div class="label">{label}</div>'
                    f'<div class="value"{vs}>{value}{uh}</div></div>')

        st = _adcp_run_stats(run_dir)
        stats_html = ''

        cr = st.get('currents')
        if cr:
            cards = (_sc('Start Date',      cr['date_start'], small=True)
                   + _sc('End Date',        cr['date_end'],   small=True)
                   + _sc('Profiles w/ Data', cr['n_profiles'])
                   + _sc('Depth Range',     f"{cr['depth_min']} – {cr['depth_max']}", 'm', small=True)
                   + _sc('Grid Fill',       cr['fill_pct'],   '%')
                   + _sc('Mean Speed',      cr['mean_spd'],   'm/s')
                   + _sc('Max Speed',       cr['max_spd'],    'm/s'))
            stats_html += f'<h3>Current Record</h3><div class="info-grid" style="margin:0 0 16px">{cards}</div>'

        dac_st = st.get('dac')
        if dac_st:
            cards = (_sc('Valid Dives',    f"{dac_st['n_valid']} / {dac_st['n_total']}")
                   + _sc('Mean DAC Speed', dac_st['mean_spd'], 'm/s')
                   + _sc('Max DAC Speed',  dac_st['max_spd'],  'm/s')
                   + _sc('Mean Direction', dac_st['direction'], small=True))
            stats_html += f'<h3>Depth-Averaged Currents</h3><div class="info-grid" style="margin:0 0 16px">{cards}</div>'

        tidal_st = st.get('tidal')
        if tidal_st and tidal_st.get('tidal_pct') is not None:
            cards = (_sc('Tidal Variance',     tidal_st['tidal_pct'],    '%')
                   + _sc('Sub-tidal Variance', tidal_st['subtidal_pct'], '%'))
            stats_html += f'<h3>Tidal Partition (40-h filter)</h3><div class="info-grid" style="margin:0 0 16px">{cards}</div>'

        shear_st = st.get('shear')
        if shear_st:
            cards = (_sc('Peak Shear (p99)', shear_st['p99_shear'], 's⁻¹')
                   + _sc('Median Shear',     shear_st['p50_shear'], 's⁻¹'))
            stats_html += f'<h3>Shear</h3><div class="info-grid" style="margin:0 0 16px">{cards}</div>'

        stats_section = (f'<div class="stats-section">{stats_html}</div>' if stats_html else '')

        # Plot groups
        plot_sections_html = ''
        for group_title, plots in PLOT_GROUPS:
            cards = ''
            for fn, plot_label in plots:
                b64 = _embed(_rf(run_dir, fn))
                if b64:
                    cards += (f'<div class="plot-card">'
                              f'<div class="plot-name">{plot_label}</div>'
                              f'<img src="data:image/png;base64,{b64}" alt="{plot_label}">'
                              f'</div>')
            if cards:
                plot_sections_html += (f'<h3>{group_title}</h3>'
                                       f'<div class="plot-grid">{cards}</div>')

        if not plot_sections_html:
            plot_sections_html = ('<div class="note">No plots have been generated for this run yet. '
                                  'Run the Generate Plots, Tidal Filter, and Diagnostics stages first.</div>')

        sections_html += f'''
<section id="run-{i}">
  <h2>{i}. Run: {run_label}</h2>
  <div class="stage-row">{badges_html}</div>
  <div class="info-grid" style="margin:14px 0 20px">{info_html}</div>
  {stats_section}
  {plot_sections_html}
</section>'''

    CSS = '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 14px;
    background: #f0f2f5;
    color: #2c3e50;
    line-height: 1.5;
}
.page { max-width: 1200px; margin: 0 auto; padding: 20px; }
header {
    background: linear-gradient(135deg, #1a252f 0%, #2c3e50 100%);
    color: #fff;
    padding: 28px 30px 20px;
    border-radius: 8px;
    margin-bottom: 20px;
}
header h1 { font-size: 22px; font-weight: 300; letter-spacing: 1px; color: #bdc3c7; }
header .subtitle { font-size: 24px; font-weight: 700; color: #3498db; margin: 4px 0 14px; font-family: monospace; }
.meta-bar { display: flex; gap: 10px; flex-wrap: wrap; }
.meta-bar span {
    background: rgba(255,255,255,0.1);
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    color: #bdc3c7;
}
.toc { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.toc a {
    color: #3498db; text-decoration: none;
    background: #eaf4fb; padding: 4px 14px;
    border-radius: 20px; font-size: 12px;
    border: 1px solid #d6eaf8;
}
.toc a:hover { background: #d6eaf8; }
section {
    background: #fff;
    padding: 24px;
    border-radius: 8px;
    margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
}
h2 {
    font-size: 17px; color: #2c3e50;
    border-left: 4px solid #3498db;
    padding-left: 12px; margin-bottom: 16px;
}
h3 {
    font-size: 14px; font-weight: 700; color: #34495e;
    margin: 20px 0 10px;
    padding-bottom: 5px;
    border-bottom: 2px solid #ecf0f1;
}
.info-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px;
}
.info-card {
    background: #f8f9fa; border: 1px solid #e8e8e8;
    border-radius: 6px; padding: 12px 16px;
}
.info-card .label { font-size: 11px; color: #7f8c8d; text-transform: uppercase; letter-spacing: 0.5px; }
.info-card .value { font-size: 18px; font-weight: 700; color: #2c3e50; margin-top: 2px; }
.stage-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.stage-badge { padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.stage-done { background: #d5f5e3; color: #1a7a40; }
.stage-pend { background: #f4f6f7; color: #95a5a6; border: 1px solid #e0e0e0; }
.plot-grid {
    display: flex; flex-direction: column; gap: 18px; margin-bottom: 4px;
}
.plot-card {
    border: 1px solid #e8e8e8; border-radius: 6px; overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
}
.plot-name {
    background: #f8f9fa; padding: 8px 14px; font-size: 12px; font-weight: 600;
    color: #555; border-bottom: 1px solid #e8e8e8;
    display: flex; align-items: center; justify-content: space-between;
}
.plot-name::after { content: '⤢'; font-size: 13px; color: #aaa; }
.plot-card img {
    width: 100%; display: block; cursor: zoom-in;
    transition: opacity 0.15s;
}
.plot-card img:hover { opacity: 0.92; }
/* Lightbox */
.lb-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.88); z-index: 9999;
    align-items: center; justify-content: center; cursor: zoom-out;
}
.lb-overlay.open { display: flex; }
.lb-overlay img {
    max-width: 96vw; max-height: 94vh;
    border-radius: 4px; box-shadow: 0 8px 40px rgba(0,0,0,0.6);
    cursor: default;
}
.lb-close {
    position: fixed; top: 16px; right: 20px;
    color: #fff; font-size: 28px; cursor: pointer;
    background: none; border: none; line-height: 1;
    opacity: 0.8;
}
.lb-close:hover { opacity: 1; }
.stat-unit { font-size: 11px; color: #7f8c8d; font-weight: 400; }
.stats-section { margin-bottom: 20px; }
.note {
    background: #eaf4fb; border-left: 4px solid #3498db;
    padding: 10px 16px; border-radius: 0 6px 6px 0;
    margin: 8px 0; font-size: 13px; color: #2c3e50;
}
footer {
    text-align: center; padding: 20px; font-size: 11px; color: #95a5a6;
}
'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AD2CP Report &mdash; {dep}</title>
  <style>{CSS}{funnel_css}</style>
</head>
<body>
<div class="page">

<header>
  <h1>AD2CP Processing Report</h1>
  <div class="subtitle">{dep}</div>
  <div class="meta-bar">
    <span>Generated: {now}</span>
    <span>Runs: {n_runs}</span>
  </div>
</header>

<div class="toc">{toc_html}</div>

{funnel_html}

{sections_html}

<footer>UVI Glider Processing Pipeline &mdash; {now}</footer>
</div>

<!-- Lightbox overlay -->
<div class="lb-overlay" id="lb-overlay" onclick="closeLb(event)">
  <button class="lb-close" onclick="closeLb()">&times;</button>
  <img id="lb-img" src="" alt="">
</div>
<script>
const overlay = document.getElementById('lb-overlay');
const lbImg   = document.getElementById('lb-img');
document.querySelectorAll('.plot-card img').forEach(img => {{
  img.addEventListener('click', () => {{
    lbImg.src = img.src;
    lbImg.alt = img.alt;
    overlay.classList.add('open');
  }});
}});
function closeLb(e) {{
  if (!e || e.target !== lbImg) overlay.classList.remove('open');
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') overlay.classList.remove('open'); }});
</script>
</body>
</html>'''

    # Save to disk so it appears in the Cloud upload section
    fname    = f'adcp_report_{dep}{"_"+run if run else ""}.html'
    save_path = os.path.join(base, fname)
    try:
        with open(save_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        _cloud_files_cache.pop(dep, None)   # invalidate so cloud list picks it up
    except Exception:
        pass

    if request.args.get('dl') == '1':
        return Response(html, mimetype='text/html', headers={
            'Content-Disposition': f'attachment; filename="{fname}"'})
    return Response(html, mimetype='text/html')


# ── Cloud storage ─────────────────────────────────────────────────────────────
_CLOUD_DIR         = os.path.join(BASE_DIR, 'config', 'cloud')
_CLOUD_CREDS_PATH  = os.path.join(_CLOUD_DIR, 'credentials.json')
_CLOUD_TOKENS_PATH = os.path.join(_CLOUD_DIR, 'tokens.json')
_OAUTH_PENDING     = {}   # state -> {verifier}


def _ssl_ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def _http(url, method='GET', data=None, headers=None):
    """Thin urllib wrapper with SSL handling for Windows."""
    import urllib.error as _urlerr
    req = _urlreq.Request(url, data=data, headers=headers or {}, method=method)
    ctx = _ssl_ctx()
    opener = _urlreq.build_opener(_urlreq.HTTPSHandler(context=ctx)) if ctx else _urlreq.build_opener()
    try:
        with opener.open(req) as r:
            return json.loads(r.read().decode('utf-8'))
    except _urlerr.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        try:
            detail = json.loads(body).get('error', {})
            msg = detail.get('message', body) if isinstance(detail, dict) else str(detail)
        except Exception:
            msg = body[:400]
        raise Exception(f'HTTP {exc.code} {exc.reason}: {msg}') from None


# Thresholds / chunk sizes for large-file uploads
_GOOGLE_RESUMABLE_THRESHOLD = 5   * 1024 * 1024   # 5 MB  — use resumable above this
_GOOGLE_CHUNK_SIZE          = 8   * 1024 * 1024   # 8 MB  chunks (multiple of 256 KB)
_DROPBOX_SESSION_THRESHOLD  = 150 * 1024 * 1024   # 150 MB — must use sessions above this
_DROPBOX_CHUNK_SIZE         = 100 * 1024 * 1024   # 100 MB chunks


def _google_resumable_query_offset(session_uri, file_size, opener):
    """Ask the Drive session how many bytes it has received.
    Returns (received_bytes, finished_metadata_or_None)."""
    import urllib.error as _urlerr
    req = _urlreq.Request(session_uri, data=b'', method='PUT',
                          headers={'Content-Range': f'bytes */{file_size}',
                                   'Content-Length': '0'})
    try:
        with opener.open(req) as r:
            raw = r.read()
            return file_size, (json.loads(raw.decode()) if raw.strip() else {})
    except _urlerr.HTTPError as e:
        if e.code == 308:
            rng = e.headers.get('Range') or e.headers.get('range') or ''
            if rng:
                return int(rng.split('-')[1]) + 1, None
            return 0, None
        if e.code == 404:
            raise Exception('Resumable upload session expired')
        raise


def _google_resumable(token, fpath, fname, parent_id, file_size, overwrite_id=None):
    """Upload a file to Google Drive using the resumable upload API with chunk-level retry."""
    import urllib.error as _urlerr
    import time as _time
    ctx    = _ssl_ctx()
    opener = (_urlreq.build_opener(_urlreq.HTTPSHandler(context=ctx))
              if ctx else _urlreq.build_opener())

    if overwrite_id:
        init_url  = (f'https://www.googleapis.com/upload/drive/v3/files/{overwrite_id}'
                     f'?uploadType=resumable')
        init_meta = json.dumps({'name': fname}).encode('utf-8')
        init_meth = 'PATCH'
    else:
        init_url  = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable'
        init_meta = json.dumps({'name': fname, 'parents': [parent_id]}).encode('utf-8')
        init_meth = 'POST'

    init_req = _urlreq.Request(
        init_url, data=init_meta, method=init_meth,
        headers={
            'Authorization':          f'Bearer {token}',
            'Content-Type':           'application/json',
            'X-Upload-Content-Type':  'application/octet-stream',
            'X-Upload-Content-Length': str(file_size),
        })
    with opener.open(init_req) as r:
        session_uri = r.headers.get('Location') or r.headers.get('location')
    if not session_uri:
        raise Exception('Resumable upload: no session URI returned')

    _MAX_CHUNK_RETRIES = 5
    with open(fpath, 'rb') as f:
        offset = 0
        while offset < file_size:
            f.seek(offset)
            chunk = f.read(_GOOGLE_CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1

            for attempt in range(_MAX_CHUNK_RETRIES):
                try:
                    req = _urlreq.Request(
                        session_uri, data=chunk, method='PUT',
                        headers={
                            'Content-Length': str(len(chunk)),
                            'Content-Range':  f'bytes {offset}-{end}/{file_size}',
                        })
                    try:
                        with opener.open(req) as r:
                            raw = r.read()
                            return json.loads(raw.decode('utf-8')) if raw.strip() else {}
                    except _urlerr.HTTPError as e:
                        if e.code == 308:      # chunk accepted, advance
                            offset += len(chunk)
                            break
                        raise
                    break  # no exception → last chunk returned above
                except Exception as exc:
                    err_str = str(exc).lower()
                    is_network = any(k in err_str for k in ('eof', 'ssl', 'connection', 'reset', 'broken pipe', 'timeout'))
                    if not is_network or attempt == _MAX_CHUNK_RETRIES - 1:
                        raise
                    _time.sleep(2 ** attempt)   # 1s, 2s, 4s, 8s back-off
                    # Query how many bytes the server actually received
                    try:
                        new_off, done = _google_resumable_query_offset(session_uri, file_size, opener)
                        if done is not None:
                            return done          # upload already complete
                        if new_off > offset:
                            offset = new_off     # server got more than we thought
                            f.seek(offset)
                            chunk = f.read(_GOOGLE_CHUNK_SIZE)
                            if not chunk:
                                return {}
                            end = offset + len(chunk) - 1
                    except Exception:
                        pass                     # status query failed too; retry chunk as-is
    return {}


def _google_ensure_folder(token, name, parent_id, cache):
    """Return Drive folder ID for `name` inside `parent_id`, creating it if needed."""
    key = (parent_id, name)
    if key in cache:
        return cache[key]
    safe = name.replace("'", "\\'")
    q = (f"name='{safe}' and '{parent_id}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    existing = _http(
        f'https://www.googleapis.com/drive/v3/files'
        f'?q={urllib.parse.quote(q)}&fields=files(id)',
        headers={'Authorization': f'Bearer {token}'}
    ).get('files', [])
    if existing:
        fid = existing[0]['id']
    else:
        r = _http(
            'https://www.googleapis.com/drive/v3/files',
            method='POST',
            data=json.dumps({'name': name,
                             'mimeType': 'application/vnd.google-apps.folder',
                             'parents': [parent_id]}).encode(),
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'}
        )
        fid = r['id']
    cache[key] = fid
    return fid


def _dropbox_ensure_folder(token, folder_path, cache):
    """Create Dropbox folder if it does not already exist."""
    if folder_path in cache:
        return
    try:
        _http(
            'https://api.dropboxapi.com/2/files/create_folder_v2',
            method='POST',
            data=json.dumps({'path': folder_path, 'autorename': False}).encode(),
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'}
        )
    except Exception as exc:
        if 'conflict' not in str(exc).lower():
            raise
    cache[folder_path] = True


def _google_upload(token, fpath, fname, parent_id, existing, conflict):
    """Upload one file to Google Drive (multipart or resumable, with conflict handling)."""
    if existing and conflict == 'skip':
        return {'skipped': True}

    file_size = os.path.getsize(fpath)

    if existing and conflict == 'overwrite':
        file_id = existing[0]['id']
        if file_size > _GOOGLE_RESUMABLE_THRESHOLD:
            resp = _google_resumable(token, fpath, fname, parent_id, file_size,
                                     overwrite_id=file_id)
        else:
            with open(fpath, 'rb') as f:
                content = f.read()
            resp = _http(
                f'https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media',
                method='PATCH', data=content,
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type':  'application/octet-stream'})
        return {'id': resp.get('id'), 'name': fname}

    # New file
    if file_size > _GOOGLE_RESUMABLE_THRESHOLD:
        resp = _google_resumable(token, fpath, fname, parent_id, file_size)
        return {'id': resp.get('id'), 'name': resp.get('name')}

    with open(fpath, 'rb') as f:
        content = f.read()
    boundary = 'gd_' + secrets.token_hex(8)
    meta = json.dumps({'name': fname, 'parents': [parent_id]}).encode('utf-8')
    body = (
        f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
        .encode() + meta + b'\r\n'
        + f'--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n'
        .encode() + content
        + f'\r\n--{boundary}--'.encode()
    )
    resp = _http(
        'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
        method='POST', data=body,
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type':  f'multipart/related; boundary={boundary}'})
    return {'id': resp.get('id'), 'name': resp.get('name')}


def _dropbox_upload(token, fpath, dbx_path, conflict):
    """Upload one file to Dropbox (simple or session, with conflict handling)."""
    if conflict == 'skip':
        try:
            _http('https://api.dropboxapi.com/2/files/get_metadata',
                  method='POST',
                  data=json.dumps({'path': dbx_path}).encode(),
                  headers={'Authorization': f'Bearer {token}',
                           'Content-Type': 'application/json'})
            return {'skipped': True}
        except Exception:
            pass  # file not found — proceed

    file_size = os.path.getsize(fpath)
    if file_size > _DROPBOX_SESSION_THRESHOLD:
        return _dropbox_session_upload(token, fpath, dbx_path)

    with open(fpath, 'rb') as f:
        content = f.read()
    resp = _http(
        'https://content.dropboxapi.com/2/files/upload',
        method='POST', data=content,
        headers={
            'Authorization':   f'Bearer {token}',
            'Content-Type':    'application/octet-stream',
            'Dropbox-API-Arg': json.dumps({'path': dbx_path, 'mode': 'overwrite'}),
        })
    return {'path': resp.get('path_display')}


def _dropbox_session_upload(token, fpath, dbx_path):
    """Upload a large file (>150 MB) to Dropbox via upload sessions."""
    file_size = os.path.getsize(fpath)
    with open(fpath, 'rb') as f:
        first = f.read(_DROPBOX_CHUNK_SIZE)
        start = _http(
            'https://content.dropboxapi.com/2/files/upload_session/start',
            method='POST', data=first,
            headers={
                'Authorization':   f'Bearer {token}',
                'Content-Type':    'application/octet-stream',
                'Dropbox-API-Arg': json.dumps({'close': False}),
            })
        session_id = start['session_id']
        offset     = len(first)

        while offset < file_size:
            chunk      = f.read(_DROPBOX_CHUNK_SIZE)
            offset_end = offset + len(chunk)
            cursor     = {'session_id': session_id, 'offset': offset}
            if offset_end >= file_size:
                resp = _http(
                    'https://content.dropboxapi.com/2/files/upload_session/finish',
                    method='POST', data=chunk,
                    headers={
                        'Authorization':   f'Bearer {token}',
                        'Content-Type':    'application/octet-stream',
                        'Dropbox-API-Arg': json.dumps({
                            'cursor': cursor,
                            'commit': {'path': dbx_path, 'mode': 'overwrite'},
                        }),
                    })
                return {'path': resp.get('path_display')}
            _http(
                'https://content.dropboxapi.com/2/files/upload_session/append_v2',
                method='POST', data=chunk,
                headers={
                    'Authorization':   f'Bearer {token}',
                    'Content-Type':    'application/octet-stream',
                    'Dropbox-API-Arg': json.dumps({'cursor': cursor, 'close': False}),
                })
            offset = offset_end
    return {}


def _load_cloud_creds():
    try:
        with open(_CLOUD_CREDS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cloud_creds(data):
    os.makedirs(_CLOUD_DIR, exist_ok=True)
    with open(_CLOUD_CREDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_cloud_tokens():
    try:
        with open(_CLOUD_TOKENS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cloud_tokens(data):
    os.makedirs(_CLOUD_DIR, exist_ok=True)
    with open(_CLOUD_TOKENS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _pkce_pair():
    verifier  = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b'=').decode()
    return verifier, challenge


def _google_access_token():
    """Return a valid Google access token, refreshing if expired."""
    tokens = _load_cloud_tokens().get('google')
    if not tokens:
        return None
    if time.time() < tokens.get('expires_at', 0) - 60:
        return tokens['access_token']
    creds = _load_cloud_creds().get('google', {})
    try:
        resp = _http('https://oauth2.googleapis.com/token', method='POST',
                     data=urllib.parse.urlencode({
                         'client_id':     creds['client_id'],
                         'client_secret': creds['client_secret'],
                         'refresh_token': tokens['refresh_token'],
                         'grant_type':    'refresh_token',
                     }).encode())
        all_t = _load_cloud_tokens()
        all_t['google']['access_token'] = resp['access_token']
        all_t['google']['expires_at']   = time.time() + resp.get('expires_in', 3600)
        _save_cloud_tokens(all_t)
        return resp['access_token']
    except Exception as exc:
        print(f'[cloud] Google token refresh failed: {exc}', flush=True)
        return None


def _dropbox_access_token():
    """Return a valid Dropbox access token, refreshing if expired."""
    tokens = _load_cloud_tokens().get('dropbox')
    if not tokens:
        return None
    if time.time() < tokens.get('expires_at', 0) - 60:
        return tokens.get('access_token')
    creds = _load_cloud_creds().get('dropbox', {})
    try:
        resp = _http('https://api.dropboxapi.com/oauth2/token', method='POST',
                     data=urllib.parse.urlencode({
                         'grant_type':    'refresh_token',
                         'refresh_token': tokens['refresh_token'],
                         'client_id':     creds['app_key'],
                     }).encode())
        all_t = _load_cloud_tokens()
        all_t['dropbox']['access_token'] = resp['access_token']
        all_t['dropbox']['expires_at']   = time.time() + resp.get('expires_in', 14400)
        _save_cloud_tokens(all_t)
        return resp['access_token']
    except Exception as exc:
        print(f'[cloud] Dropbox token refresh failed: {exc}', flush=True)
        return None


def _oauth_close_page(message, ok=True):
    icon  = '✓' if ok else '✗'
    color = '#4ade80' if ok else '#f87171'
    return (
        '<!DOCTYPE html><html><head>'
        '<style>body{font-family:sans-serif;background:#0f1720;color:#9ab4cc;'
        'display:flex;align-items:center;justify-content:center;height:100vh;'
        'margin:0;flex-direction:column;gap:14px;}</style></head><body>'
        f'<div style="font-size:26px;color:{color}">{icon} {message}</div>'
        '<div style="color:#4a6580;font-size:13px">You can close this window — '
        'the app has been updated.</div>'
        '<script>setTimeout(()=>window.close(),4000)</script>'
        '</body></html>'
    )


# ── Account status ─────────────────────────────────────────────────────────────
@app.route('/api/cloud/accounts')
def cloud_accounts():
    tokens = _load_cloud_tokens()
    creds  = _load_cloud_creds()
    def _svc(name, cred_key):
        t = tokens.get(name, {})
        c = creds.get(name, {})
        return {
            'connected': bool(t.get('access_token')),
            'email':     t.get('email'),
            'name':      t.get('name'),
            'has_creds': bool(c.get(cred_key)),
        }
    return jsonify({
        'google':  _svc('google',  'client_id'),
        'dropbox': _svc('dropbox', 'app_key'),
    })


@app.route('/api/cloud/verify')
def cloud_verify():
    """Actually attempt token refresh for each service; reports whether tokens are live."""
    creds  = _load_cloud_creds()
    tokens = _load_cloud_tokens()
    def _svc(name, cred_key, token_fn):
        c = creds.get(name, {})
        t = tokens.get(name, {})
        has_creds = bool(c.get(cred_key))
        has_token = bool(t.get('refresh_token'))
        live = token_fn() is not None if (has_creds and has_token) else False
        return {'live': live, 'has_creds': has_creds, 'has_token': has_token,
                'email': t.get('email')}
    return jsonify({
        'google':  _svc('google',  'client_id', _google_access_token),
        'dropbox': _svc('dropbox', 'app_key',   _dropbox_access_token),
    })


# ── Disconnect ─────────────────────────────────────────────────────────────────
@app.route('/api/cloud/disconnect', methods=['POST'])
def cloud_disconnect():
    service = (request.json or {}).get('service')
    tokens  = _load_cloud_tokens()
    tokens.pop(service, None)
    _save_cloud_tokens(tokens)
    return jsonify({'ok': True})


# ── Google OAuth ───────────────────────────────────────────────────────────────
@app.route('/api/oauth/google/start')
def google_oauth_start():
    creds = _load_cloud_creds().get('google', {})
    if not creds.get('client_id'):
        return jsonify({'error': 'Google credentials not configured'}), 400
    state             = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    _OAUTH_PENDING[state] = {'verifier': verifier}
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id':             creds['client_id'],
        'redirect_uri':          f'http://localhost:{PORT}/oauth/google/callback',
        'response_type':         'code',
        'scope':                 ('openid email profile '
                                  'https://www.googleapis.com/auth/drive'),
        'state':                 state,
        'code_challenge':        challenge,
        'code_challenge_method': 'S256',
        'access_type':           'offline',
        'prompt':                'consent select_account',
    })
    return jsonify({'url': url})


@app.route('/oauth/google/callback')
def google_oauth_callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    if error:
        return _oauth_close_page(f'Authorization failed: {error}', ok=False)
    pending = _OAUTH_PENDING.pop(state, None)
    if not pending:
        return _oauth_close_page('Invalid session. Please try again.', ok=False)
    creds = _load_cloud_creds().get('google', {})
    try:
        token_resp = _http('https://oauth2.googleapis.com/token', method='POST',
                           data=urllib.parse.urlencode({
                               'code':          code,
                               'client_id':     creds['client_id'],
                               'client_secret': creds['client_secret'],
                               'redirect_uri':  f'http://localhost:{PORT}/oauth/google/callback',
                               'grant_type':    'authorization_code',
                               'code_verifier': pending['verifier'],
                           }).encode())
        userinfo = _http('https://www.googleapis.com/oauth2/v3/userinfo',
                         headers={'Authorization': f'Bearer {token_resp["access_token"]}'})
        tokens = _load_cloud_tokens()
        tokens['google'] = {
            'access_token':  token_resp.get('access_token'),
            'refresh_token': token_resp.get('refresh_token'),
            'expires_at':    time.time() + token_resp.get('expires_in', 3600),
            'email':         userinfo.get('email'),
            'name':          userinfo.get('name'),
        }
        _save_cloud_tokens(tokens)
        return _oauth_close_page(f'Connected as {userinfo.get("email")}', ok=True)
    except Exception as exc:
        print(f'[cloud] Google OAuth error: {exc}', flush=True)
        return _oauth_close_page(f'Error: {exc}', ok=False)


# ── Dropbox OAuth ──────────────────────────────────────────────────────────────
@app.route('/api/oauth/dropbox/start')
def dropbox_oauth_start():
    creds = _load_cloud_creds().get('dropbox', {})
    if not creds.get('app_key'):
        return jsonify({'error': 'Dropbox credentials not configured'}), 400
    state               = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    _OAUTH_PENDING[state] = {'verifier': verifier}
    redirect_uri = f'http://localhost:{PORT}/oauth/dropbox/callback'
    print(f'[cloud] Dropbox redirect_uri = {redirect_uri}', flush=True)
    url = 'https://www.dropbox.com/oauth2/authorize?' + urllib.parse.urlencode({
        'client_id':             creds['app_key'],
        'redirect_uri':          redirect_uri,
        'response_type':         'code',
        'state':                 state,
        'code_challenge':        challenge,
        'code_challenge_method': 'S256',
        'token_access_type':     'offline',
        'scope': ('account_info.read '
                  'files.metadata.read '
                  'files.metadata.write '
                  'files.content.read '
                  'files.content.write'),
    })
    print(f'[cloud] Dropbox auth URL = {url}', flush=True)
    return jsonify({'url': url, 'redirect_uri': redirect_uri})


@app.route('/oauth/dropbox/callback')
def dropbox_oauth_callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error_description', request.args.get('error'))
    if error:
        return _oauth_close_page(f'Authorization failed: {error}', ok=False)
    pending = _OAUTH_PENDING.pop(state, None)
    if not pending:
        return _oauth_close_page('Invalid session.', ok=False)
    creds = _load_cloud_creds().get('dropbox', {})
    try:
        token_resp = _http('https://api.dropboxapi.com/oauth2/token', method='POST',
                           data=urllib.parse.urlencode({
                               'code':          code,
                               'grant_type':    'authorization_code',
                               'client_id':     creds['app_key'],
                               'redirect_uri':  f'http://localhost:{PORT}/oauth/dropbox/callback',
                               'code_verifier': pending['verifier'],
                           }).encode())
        account = _http('https://api.dropboxapi.com/2/users/get_current_account',
                        method='POST', data=b'null',
                        headers={
                            'Authorization': f'Bearer {token_resp["access_token"]}',
                            'Content-Type':  'application/json',
                        })
        tokens = _load_cloud_tokens()
        tokens['dropbox'] = {
            'access_token':  token_resp.get('access_token'),
            'refresh_token': token_resp.get('refresh_token'),
            'expires_at':    time.time() + token_resp.get('expires_in', 14400),
            'email':         account.get('email'),
            'name':          account.get('name', {}).get('display_name'),
        }
        _save_cloud_tokens(tokens)
        return _oauth_close_page(f'Connected as {account.get("email")}', ok=True)
    except Exception as exc:
        print(f'[cloud] Dropbox OAuth error: {exc}', flush=True)
        return _oauth_close_page(f'Error: {exc}', ok=False)


# ── List uploadable files for a deployment (4 categories) ─────────────────────
@app.route('/api/cloud/files/<dep>')
def cloud_list_files(dep):
    """Return files organised into 5 upload categories for a deployment."""

    if not _valid_deployment_name(dep):
        return jsonify({'error': 'Invalid deployment name'}), 400
    dep_dir     = _dep_dir(dep)
    adcp_dir    = _adcp_base_dir(dep, False)
    dat_flt_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'flight_data')
    dat_mrg_dir = os.path.join(BASE_DIR, 'dat_files', dep, 'merged_data')
    prof_dir    = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')
    traj_dir    = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'trajectory_science')

    # Include each per-run subdirectory so the cache invalidates when files
    # are added inside a run dir (adcp_dir's own mtime only updates on subdir creation).
    adcp_run_dirs = []
    if os.path.isdir(adcp_dir):
        for _entry in os.listdir(adcp_dir):
            _sub = os.path.join(adcp_dir, _entry)
            if os.path.isdir(_sub):
                adcp_run_dirs.append(_sub)

    newest = _dir_mtime(dat_flt_dir, dat_mrg_dir, prof_dir, traj_dir, dep_dir, adcp_dir,
                        *adcp_run_dirs)
    cached = _cloud_files_cache.get(dep)
    if cached and cached['mtime'] >= newest:
        return jsonify(cached['data'])

    def scan(dirs, exts, label_base=None):
        out = []
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for root, subdirs, fnames in os.walk(d):
                subdirs[:] = [s for s in subdirs if s != 'analysis']
                for fn in sorted(fnames):
                    if os.path.splitext(fn)[1].lower() in exts:
                        fp  = os.path.join(root, fn)
                        rel = os.path.relpath(fp, BASE_DIR)
                        lbl = os.path.relpath(fp, label_base or d)
                        out.append({'label': lbl, 'path': rel,
                                    'size': os.path.getsize(fp)})
        out.sort(key=lambda x: x['label'].lower())
        return out

    dat_exts = {'.dat', '.dbd', '.ebd', '.mbd', '.nbd', '.sbd', '.tbd',
                '.mlg', '.nlg', '.slg', '.tlg'}
    nc_ext   = {'.nc'}
    qc_exts  = {'.html', '.pdf', '.png', '.csv', '.json'}

    categories = {
        'dat_flight': {
            'label': 'Flight DAT Files',
            'files': scan(
                [os.path.join(BASE_DIR, 'dat_files', dep, 'flight_data')],
                dat_exts,
            ),
        },
        'dat_merged': {
            'label': 'Merged DAT Files',
            'files': scan(
                [os.path.join(BASE_DIR, 'dat_files', dep, 'merged_data')],
                dat_exts,
            ),
        },
        'profile': {
            'label': 'Profile NC',
            'files': scan(
                [os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')],
                nc_ext,
            ),
        },
        'traj': {
            'label': 'Trajectory NC',
            'files': scan(
                [os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep,
                              'trajectory_science')],
                nc_ext,
            ),
        },
        'qc': {
            'label': 'QC Reporting',
            'files': scan(
                [os.path.join(dep_dir, 'reports'),
                 os.path.join(dep_dir, 'figs')],
                qc_exts | {'.png', '.html', '.pdf'},
                label_base=dep_dir,
            ),
        },
        'qc_nc': {
            'label': 'QC Output NC',
            'files': scan(
                [os.path.join(dep_dir, 'data', 'out', 'nc')],
                nc_ext,
                label_base=dep_dir,
            ),
        },
        'adcp': {
            'label': 'AD2CP Files & Plots',
            'files': scan(
                [adcp_dir],
                {'.nc', '.png', '.npz', '.csv', '.html'},
                label_base=adcp_dir,
            ),
        },
    }
    data = {'categories': categories}
    _cloud_files_cache[dep] = {'mtime': newest, 'data': data}
    return jsonify(data)


# ── Cloud folder browser (for upload destination picker) ───────────────────────
@app.route('/api/cloud/folders/<service>')
def cloud_list_folders(service):
    parent = request.args.get('parent', '')
    try:
        if service == 'google':
            token = _google_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            pid   = parent or 'root'
            q_str = (f"'{pid}' in parents and "
                     f"mimeType='application/vnd.google-apps.folder' and trashed=false")
            url   = (f'https://www.googleapis.com/drive/v3/files'
                     f'?q={urllib.parse.quote(q_str)}'
                     f'&fields=files(id,name)&orderBy=name')
            resp    = _http(url, headers={'Authorization': f'Bearer {token}'})
            folders = [{'id': f['id'], 'name': f['name']}
                       for f in resp.get('files', [])]
            if parent:
                meta    = _http(
                    f'https://www.googleapis.com/drive/v3/files/{pid}?fields=id,name',
                    headers={'Authorization': f'Bearer {token}'})
                current = {'id': pid, 'name': meta.get('name', '')}
            else:
                current = {'id': 'root', 'name': 'My Drive'}
            return jsonify({'folders': folders, 'current': current})

        elif service == 'dropbox':
            token = _dropbox_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            path  = parent or ''
            resp  = _http('https://api.dropboxapi.com/2/files/list_folder',
                          method='POST',
                          data=json.dumps({'path': path}).encode(),
                          headers={'Authorization': f'Bearer {token}',
                                   'Content-Type': 'application/json'})
            folders = sorted(
                [{'id': e['path_lower'], 'name': e['name']}
                 for e in resp.get('entries', []) if e.get('.tag') == 'folder'],
                key=lambda x: x['name'].lower()
            )
            name = path.split('/')[-1] if path else 'Dropbox'
            return jsonify({'folders': folders, 'current': {'id': path, 'name': name}})

        return jsonify({'error': 'Unknown service'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/cloud/folders/<service>/create', methods=['POST'])
def cloud_create_folder(service):
    data   = request.json or {}
    name   = data.get('name', '').strip()
    parent = data.get('parent', '')
    if not name: return jsonify({'error': 'Name required'}), 400
    try:
        if service == 'google':
            token = _google_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            resp = _http(
                'https://www.googleapis.com/drive/v3/files?fields=id,name',
                method='POST',
                data=json.dumps({'name': name,
                                 'mimeType': 'application/vnd.google-apps.folder',
                                 'parents': [parent or 'root']}).encode(),
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type': 'application/json'})
            return jsonify({'id': resp.get('id'), 'name': resp.get('name')})

        elif service == 'dropbox':
            token = _dropbox_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            path = f'{parent}/{name}' if parent else f'/{name}'
            resp = _http('https://api.dropboxapi.com/2/files/create_folder_v2',
                         method='POST',
                         data=json.dumps({'path': path, 'autorename': True}).encode(),
                         headers={'Authorization': f'Bearer {token}',
                                  'Content-Type': 'application/json'})
            meta = resp.get('metadata', {})
            return jsonify({'id': meta.get('path_lower'), 'name': meta.get('name')})

        return jsonify({'error': 'Unknown service'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/cloud/folders/<service>/rename', methods=['POST'])
def cloud_rename_folder(service):
    data     = request.json or {}
    fid      = data.get('id', '')
    new_name = data.get('name', '').strip()
    if not fid or not new_name:
        return jsonify({'error': 'id and name required'}), 400
    try:
        if service == 'google':
            token = _google_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            resp = _http(
                f'https://www.googleapis.com/drive/v3/files/{fid}?fields=id,name',
                method='PATCH',
                data=json.dumps({'name': new_name}).encode(),
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type': 'application/json'})
            return jsonify({'id': resp.get('id'), 'name': resp.get('name')})

        elif service == 'dropbox':
            token = _dropbox_access_token()
            if not token: return jsonify({'error': 'Not connected'}), 401
            parts   = fid.rstrip('/').split('/')
            to_path = '/'.join(parts[:-1]) + '/' + new_name
            resp = _http('https://api.dropboxapi.com/2/files/move_v2',
                         method='POST',
                         data=json.dumps({'from_path': fid, 'to_path': to_path,
                                          'autorename': True}).encode(),
                         headers={'Authorization': f'Bearer {token}',
                                  'Content-Type': 'application/json'})
            meta = resp.get('metadata', {})
            return jsonify({'id': meta.get('path_lower'), 'name': meta.get('name')})

        return jsonify({'error': 'Unknown service'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Existing-file check (for resume mode) ──────────────────────────────────────
@app.route('/api/cloud/existing')
def cloud_existing():
    """Return all filenames (not folders) that already exist in a destination folder."""
    service = request.args.get('service')
    dest    = request.args.get('dest', '')

    try:
        if service == 'google':
            token = _google_access_token()
            if not token:
                return jsonify({'error': 'Not connected'}), 401
            parent_id  = dest or 'root'
            names      = []
            page_token = None
            while True:
                q      = f"'{parent_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
                params = (f'q={urllib.parse.quote(q)}'
                          f'&fields=nextPageToken,files(name)&pageSize=1000')
                if page_token:
                    params += f'&pageToken={urllib.parse.quote(page_token)}'
                r          = _http(f'https://www.googleapis.com/drive/v3/files?{params}',
                                   headers={'Authorization': f'Bearer {token}'})
                names.extend(f['name'] for f in r.get('files', []))
                page_token = r.get('nextPageToken')
                if not page_token:
                    break
            return jsonify({'filenames': names})

        elif service == 'dropbox':
            token = _dropbox_access_token()
            if not token:
                return jsonify({'error': 'Not connected'}), 401
            dbx_path = dest or ''
            names    = []
            r = _http('https://api.dropboxapi.com/2/files/list_folder',
                      method='POST',
                      data=json.dumps({'path': dbx_path, 'limit': 2000}).encode(),
                      headers={'Authorization': f'Bearer {token}',
                               'Content-Type':  'application/json'})
            names.extend(e['name'] for e in r.get('entries', []) if e['.tag'] == 'file')
            while r.get('has_more'):
                r = _http('https://api.dropboxapi.com/2/files/list_folder/continue',
                          method='POST',
                          data=json.dumps({'cursor': r['cursor']}).encode(),
                          headers={'Authorization': f'Bearer {token}',
                                   'Content-Type':  'application/json'})
                names.extend(e['name'] for e in r.get('entries', []) if e['.tag'] == 'file')
            return jsonify({'filenames': names})

        return jsonify({'error': f'Unknown service: {service}'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Upload ─────────────────────────────────────────────────────────────────────
@app.route('/api/cloud/upload', methods=['POST'])
def cloud_upload():
    import traceback as _tb
    data     = request.json or {}
    service  = data.get('service')
    items    = data.get('files', [])    # list of str | {path, subdir}
    dest     = data.get('dest', '')     # Google: folder ID  |  Dropbox: folder path
    conflict = data.get('conflict', 'overwrite')  # 'overwrite' | 'skip'

    folder_cache = {}   # Google: (parent_id, name)->id  |  Dropbox: path->True
    results = []
    for item in items:
        if isinstance(item, dict):
            rel    = item.get('path', '')
            subdir = item.get('subdir', '').strip('/\\')
        else:
            rel    = item
            subdir = ''

        fpath = os.path.realpath(os.path.join(BASE_DIR, rel))
        if not _is_within(fpath, BASE_DIR):
            results.append({'file': rel, 'error': 'Path not allowed'}); continue
        if not os.path.isfile(fpath):
            results.append({'file': rel, 'error': 'File not found'}); continue
        fname = os.path.basename(fpath)
        try:
            if service == 'google':
                token = _google_access_token()
                if not token:
                    results.append({'file': rel, 'error': 'Not connected'}); continue
                parent_id = dest or 'root'
                if subdir:
                    parent_id = _google_ensure_folder(token, subdir, parent_id, folder_cache)
                q_str    = (f"name='{fname.replace(chr(39), chr(92)+chr(39))}' "
                            f"and '{parent_id}' in parents and trashed=false")
                existing = _http(
                    f'https://www.googleapis.com/drive/v3/files'
                    f'?q={urllib.parse.quote(q_str)}&fields=files(id)',
                    headers={'Authorization': f'Bearer {token}'}
                ).get('files', [])
                resp = _google_upload(token, fpath, fname, parent_id, existing, conflict)
                if resp.get('skipped'):
                    results.append({'file': rel, 'skipped': True})
                else:
                    results.append({'file': rel, 'service': 'google', **resp})

            elif service == 'dropbox':
                token = _dropbox_access_token()
                if not token:
                    results.append({'file': rel, 'error': 'Not connected'}); continue
                if subdir:
                    folder_path = f'{dest}/{subdir}' if dest else f'/{subdir}'
                    _dropbox_ensure_folder(token, folder_path, folder_cache)
                    dbx_path = f'{folder_path}/{fname}'
                else:
                    dbx_path = f'{dest}/{fname}' if dest else f'/{fname}'
                resp = _dropbox_upload(token, fpath, dbx_path, conflict)
                if resp.get('skipped'):
                    results.append({'file': rel, 'skipped': True})
                else:
                    results.append({'file': rel, 'service': 'dropbox', **resp})

            else:
                results.append({'file': rel, 'error': f'Unknown service: {service}'})
        except Exception as exc:
            results.append({'file': rel, 'error': str(exc)})
            print(f'[cloud] upload error {rel}: {exc}', flush=True)
            print(_tb.format_exc(), flush=True)

    ok      = sum(1 for r in results if 'error' not in r and not r.get('skipped'))
    skipped = sum(1 for r in results if r.get('skipped'))
    failed  = sum(1 for r in results if 'error' in r)
    return jsonify({'ok': ok, 'skipped': skipped, 'failed': failed, 'results': results})


# ── AD2CP Processing ──────────────────────────────────────────────────────────
import threading  as _adcp_th
import io         as _adcp_io
import zipfile    as _adcp_zf

_adcp_live_logs   = {}   # (dep, stage, mode) -> [str]
_adcp_live_done   = {}   # (dep, stage, mode) -> bool
_adcp_live_error  = {}   # (dep, stage, mode) -> Exception | None  (set when done)
_adcp_live_result = {}   # (dep, stage, mode) -> dict              (merged into done event)
_adcp_stg_lock    = _adcp_th.Lock()

_ADCP_OUTPUTS = {
    'load_nc':          ['adcp_glider_combined.csv'],
    'gps_pairing':      ['gps_data.npz'],
    'write_glider_nc':  ['glider_ad2cp_datetime_timecoord.nc'],
    'process_shear':    ['shear_processed.nc'],
    'compute_currents': ['currents.nc', 'dac.npz'],
    'bias_correction':  ['currents_bias_corrected.nc', 'bias_diagnostic.png'],
    'generate_plots':   ['velocity_sections.png', 'speed_section.png', 'shear_sections.png',
                         'backscatter.png', 'correlation.png', 'beam_amplitude.png', 'snr.png',
                         'beam_quality.nc',
                         'dac_timeseries.png', 'dac_map_cartopy.png'],
    'tidal_filter':     ['currents_subtidal.nc', 'tidal_filter.png'],
    'diagnostics':      ['eke.png', 'transport.png', 'richardson_number.png'],
}
_ADCP_TEST_OUTPUTS = {
    'process_shear':   ['shear_processed.nc'],
    'generate_plots':  ['beam_diagnostics.png', 'sensor_timeseries.png'],
    'lab_diagnostics': ['lab_diagnostic_report.html'],
}
# Stages whose outputs depend on processing options → written to a run subdir
_ADCP_SHARED_STAGES = {'load_nc', 'gps_pairing', 'write_glider_nc'}
_ADCP_OPTION_STAGES = {'process_shear', 'compute_currents', 'bias_correction',
                       'generate_plots', 'tidal_filter', 'diagnostics'}


def _rf(run_dir, fname):
    """Return run_dir/runlabel_fname — makes output filenames globally unique."""
    lbl = os.path.basename(os.path.normpath(run_dir))
    return os.path.join(run_dir, lbl + '_' + fname)


def _adcp_run_label(opts):
    """Build a filesystem-safe subdirectory name from processing options."""
    xaxis = int(opts.get('xaxis', 1))
    dmin  = int(float(opts.get('depth_min', 10)))
    dmax  = int(float(opts.get('depth_max', 1000)))
    label = f'xaxis{xaxis}_d{dmin}-{dmax}'
    if opts.get('yaxis') is not None:
        label += f'_y{int(float(opts["yaxis"]))}'
    corr = float(opts.get('qc_corr', 80))
    amp  = float(opts.get('qc_amp',  80))
    vel  = float(opts.get('qc_vel',  0.8))
    snr  = float(opts.get('qc_snr',  3))
    if corr != 80 or amp != 80 or vel != 0.8 or snr != 3:
        label += f'_qc{corr}-{amp}-{vel}-{snr}'
    mount = opts.get('mounting', 'auto')
    if mount and mount != 'auto':
        label += f'_{mount}'
    p  = float(opts.get('pitch_offset',   0))
    r  = float(opts.get('roll_offset',    0))
    hd = float(opts.get('heading_offset', 0))
    if p != 0 or r != 0 or hd != 0:
        label += f'_off{p}p{r}r{hd}h'
    return label


def _adcp_base_dir(dep, is_test):
    if is_test:
        return os.path.join(BASE_DIR, 'ad2cp', 'tests', dep, 'output')
    return os.path.join(BASE_DIR, 'post_processing', dep, 'adcp')


def _adcp_run_dir(base_dir, opts):
    return os.path.join(base_dir, _adcp_run_label(opts))


def _adcp_input_dir(dep, is_test):
    if is_test:
        return os.path.join(BASE_DIR, 'ad2cp', 'tests', dep)
    return os.path.join(BASE_DIR, 'ad2cp', dep)


def _adcp_sse_run(key, fn, dep=None, adcp_stage=None, result=None):
    """Run fn(log_callback) in a thread; yield SSE events from its log output.
    Optional `result` dict is merged into the done event so callers can pass
    back extra data (e.g. actual_yaxis detected during processing)."""
    import time as _t
    log_lines    = []
    error_holder = [None]
    result       = result if result is not None else {}

    with _adcp_stg_lock:
        _adcp_live_logs[key]   = log_lines
        _adcp_live_done[key]   = False
        _adcp_live_error[key]  = None
        _adcp_live_result[key] = result  # same dict reference — mutations from fn() are visible

    def _thread():
        start_ts = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        try:
            fn(lambda msg: log_lines.append(str(msg)))
        except Exception as exc:
            import traceback as _tb
            log_lines.append(f'ERROR: {exc}')
            for ln in _tb.format_exc().splitlines():
                log_lines.append(ln)
            error_holder[0] = exc
        finally:
            _adcp_live_done[key]  = True
            _adcp_live_error[key] = error_holder[0]
            if dep and adcp_stage:
                _save_run_log(dep, f'adcp_{adcp_stage}', start_ts,
                              log_lines, error_holder[0] is None)

    yield ': stream-start\n\n'   # flush headers immediately so fast jobs still stream
    _adcp_th.Thread(target=_thread, daemon=True).start()

    sent = 0
    while True:
        while sent < len(log_lines):
            yield f"data: {json.dumps({'type': 'log', 'line': log_lines[sent]})}\n\n"
            sent += 1
        if _adcp_live_done.get(key):
            success = error_holder[0] is None
            yield f"data: {json.dumps({'type': 'done', 'success': success, **result})}\n\n"
            break
        _t.sleep(0.15)


def _adcp_sse_tail(key):
    """Attach to an already-running stage: replay its buffered log then stream live until done."""
    import time as _t
    yield ': stream-start\n\n'
    sent = 0
    while True:
        logs = _adcp_live_logs.get(key, [])
        while sent < len(logs):
            yield f"data: {json.dumps({'type': 'log', 'line': logs[sent]})}\n\n"
            sent += 1
        if _adcp_live_done.get(key):
            err = _adcp_live_error.get(key)
            res = _adcp_live_result.get(key, {})
            yield f"data: {json.dumps({'type': 'done', 'success': err is None, **res})}\n\n"
            break
        _t.sleep(0.15)


# ── Stage functions ────────────────────────────────────────────────────────────

def _adcp_load_nc(dep, out_dir, log):
    import glob as _glob
    import pandas as pd
    import xarray as xr

    nc_folder = os.path.join(BASE_DIR, 'gncutils_slocum_nc_output', dep, 'profile')
    nc_files  = sorted(_glob.glob(os.path.join(nc_folder, '*.nc')))
    log(f'Found {len(nc_files)} NC files in {nc_folder}')
    if not nc_files:
        raise RuntimeError(f'No NC files found in {nc_folder}')

    required = ['time', 'latitude', 'longitude', 'sci_water_pressure',
                'temperature', 'salinity', 'profile_id']
    rename   = {'sci_water_pressure': 'pressure', 'profile_id': 'profile_number'}

    all_dfs, skipped = [], []
    for f in nc_files:
        try:
            with _nc_lock:
                with xr.open_dataset(f, decode_times=False) as ds:
                    missing = [v for v in required if v not in ds.variables]
                    if missing:
                        skipped.append((os.path.basename(f), f'missing {missing}'))
                        continue
                    df = ds[required].to_dataframe().reset_index()
            df = df[[c for c in required if c in df.columns]].rename(columns=rename)
            all_dfs.append(df)
        except Exception as e:
            skipped.append((os.path.basename(f), str(e)))

    if not all_dfs:
        raise RuntimeError('No valid NC files could be loaded')

    combined = pd.concat(all_dfs, ignore_index=True)
    combined['time']     = pd.to_numeric(combined['time'], errors='coerce')
    combined['pressure'] = combined['pressure'] * 10  # dbar -> mbar

    unique = sorted(combined['profile_number'].dropna().unique())
    combined['profile_number'] = combined['profile_number'].map(
        {v: i + 1 for i, v in enumerate(unique)})
    combined = (combined.sort_values('time')
                .drop_duplicates(subset='time').reset_index(drop=True))
    combined['time']           = pd.to_datetime(combined['time'], unit='s', utc=True).dt.tz_localize(None)
    combined['profile_number'] = combined['profile_number'].astype('int32')

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'adcp_glider_combined.csv')
    combined.to_csv(out_path, index=False)

    log(f'Saved: {out_path}  ({len(combined):,} rows)')
    log(f'Profiles: {combined["profile_number"].nunique()}')
    log(f'Time: {combined["time"].min()} to {combined["time"].max()}')
    log(f'Pressure range: {combined["pressure"].min():.1f} - {combined["pressure"].max():.1f} mbar')
    if skipped:
        log(f'Skipped {len(skipped)} files')


def _adcp_gps_pairing(out_dir, log):
    import pandas as pd
    import numpy as np

    csv_path = os.path.join(out_dir, 'adcp_glider_combined.csv')
    df = pd.read_csv(csv_path, parse_dates=['time'])
    log(f'Loaded {len(df):,} rows from CSV')

    surface_df = df[df['pressure'] <= 20].copy()
    log(f'Near-surface rows (pressure ≤ 20 mbar): {len(surface_df)}')

    surf_first = (surface_df.groupby('profile_number').first().reset_index()
                  .dropna(subset=['latitude', 'longitude']))
    surf_last  = (surface_df.groupby('profile_number').last().reset_index()
                  .dropna(subset=['latitude', 'longitude']))

    def classify(grp):
        grp = grp.sort_values('time')
        mid = len(grp) // 2
        return 'dive' if grp['pressure'].iloc[mid:].mean() > grp['pressure'].iloc[:mid].mean() else 'climb'

    types  = df.groupby('profile_number').apply(classify)
    dives  = sorted(types[types == 'dive'].index)
    climbs = sorted(types[types == 'climb'].index)
    log(f'Dive profiles: {len(dives)}  Climb profiles: {len(climbs)}')

    pre_rows, post_rows = [], []
    for dp in dives:
        following = [c for c in climbs if c > dp]
        if not following:
            continue
        cp   = following[0]
        pre  = surf_first[surf_first['profile_number'] == dp]
        post = surf_last[surf_last['profile_number'] == cp]
        if not pre.empty and not post.empty:
            pre_rows.append(pre.iloc[0])
            post_rows.append(post.iloc[0])

    if not pre_rows:
        raise RuntimeError('No matching dive/climb pairs found')

    pre_df  = pd.DataFrame(pre_rows).reset_index(drop=True)
    post_df = pd.DataFrame(post_rows).reset_index(drop=True)
    log(f'Matched dive/climb pairs: {len(pre_df)}')

    def build_gps(df_):
        arr       = np.empty((len(df_), 3), dtype=object)
        arr[:, 0] = df_['time'].values.astype('datetime64[s]')
        arr[:, 1] = df_['longitude'].values.astype(float)
        arr[:, 2] = df_['latitude'].values.astype(float)
        return arr

    gps_predive  = build_gps(pre_df)
    gps_postdive = build_gps(post_df)

    gaps = (post_df['time'].values.astype('datetime64[s]').astype(float) -
            pre_df['time'].values.astype('datetime64[s]').astype(float))
    log(f'Dive duration — min: {gaps.min()/3600:.2f}h  mean: {gaps.mean()/3600:.2f}h  max: {gaps.max()/3600:.2f}h')

    out_path = os.path.join(out_dir, 'gps_data.npz')
    np.savez(out_path, gps_predive=gps_predive, gps_postdive=gps_postdive)
    log(f'Saved: {out_path}')


def _adcp_write_glider_nc(out_dir, log):
    import pandas as pd
    import xarray as xr

    csv_path = os.path.join(out_dir, 'adcp_glider_combined.csv')
    df = pd.read_csv(csv_path, parse_dates=['time']).set_index('time')
    ds = xr.Dataset.from_dataframe(df)

    out_path = os.path.join(out_dir, 'glider_ad2cp_datetime_timecoord.nc')
    ds.to_netcdf(out_path)
    log(f'Saved: {out_path}')
    log(f'Variables: {list(ds.data_vars)}')
    log(f'Time range: {df.index.min()} to {df.index.max()}')


def _adcp_gen_dummy_glider_nc(base_dir, adcp_path, log):
    """Generate synthetic glider NC from AD2CP time range (test mode only)."""
    import h5py
    import numpy as np
    import pandas as pd
    import xarray as xr

    log('Generating synthetic glider file from AD2CP time range...')
    with _nc_lock:
        with h5py.File(adcp_path, 'r') as hf:
            t_unix = hf['Data/Average/time'][:]

    t_min, t_max = float(t_unix.min()), float(t_unix.max())
    log(f'AD2CP time range: {pd.Timestamp(t_min, unit="s")} to {pd.Timestamp(t_max, unit="s")}')
    log(f'Total pings: {len(t_unix):,}')

    N     = 2000
    t_gd  = np.linspace(t_min, t_max, N)
    plen  = N // 50
    pnums = np.repeat(np.arange(1, 51), plen)[:N]
    phase = (np.arange(N) % plen) / plen
    press = np.abs(np.sin(phase * np.pi)) * 500.0

    times = pd.to_datetime(t_gd, unit='s', utc=True).tz_localize(None)
    df = pd.DataFrame({
        'latitude':       np.full(N, 18.5),
        'longitude':      np.full(N, -66.1),
        'pressure':       press,
        'temperature':    np.full(N, 20.0),
        'salinity':       np.full(N, 35.0),
        'profile_number': pnums.astype('int32'),
    }, index=times)
    df.index.name = 'time'

    out_path = os.path.join(base_dir, 'glider_ad2cp_datetime_timecoord.nc')
    xr.Dataset.from_dataframe(df).to_netcdf(out_path)
    log(f'Saved dummy glider NC: {out_path}')


def _adcp_process_shear(base_dir, run_dir, adcp_path, opts, log):
    import matplotlib
    matplotlib.use('Agg')
    import time as _t
    from gliderad2cp import process_shear, tools

    glider_nc = os.path.join(base_dir, 'glider_ad2cp_datetime_timecoord.nc')
    if not os.path.exists(glider_nc):
        raise RuntimeError('Glider NC not found — run Write Glider NetCDF first')

    qc_corr  = float(opts.get('qc_corr', 80))
    qc_amp   = float(opts.get('qc_amp',  80))
    qc_vel   = float(opts.get('qc_vel',  0.8))
    qc_snr   = float(opts.get('qc_snr',  3))
    mounting = opts.get('mounting', 'auto') or 'auto'
    p_off    = float(opts.get('pitch_offset',   0))
    r_off    = float(opts.get('roll_offset',    0))
    h_off    = float(opts.get('heading_offset', 0))

    options = tools.get_options(
        xaxis=int(opts.get('xaxis', 1)),
        yaxis=opts.get('yaxis') or None,
        shear_bias_regression_depth_slice=(
            float(opts.get('depth_min', 10)),
            float(opts.get('depth_max', 1000)),
        ),
        QC_correlation_threshold=qc_corr,
        QC_amplitude_threshold=qc_amp,
        QC_velocity_threshold=qc_vel,
        QC_SNR_threshold=qc_snr,
        ADCP_mounting_direction=mounting,
        pitch_offset=p_off,
        roll_offset=r_off,
        heading_offset=h_off,
    )
    log(f'AD2CP file:  {adcp_path}')
    log(f'Glider NC:   {glider_nc}')
    log(f'Options: xaxis={options["xaxis"]}, depth_slice=({opts.get("depth_min",10)},{opts.get("depth_max",1000)})')
    log(f'Mounting: {mounting}  Offsets: pitch={p_off}° roll={r_off}° heading={h_off}°')
    log(f'QC thresholds: corr≥{qc_corr}  amp≤{qc_amp}  vel≤{qc_vel} m/s  SNR≥{qc_snr} dB')

    t0      = _t.time()
    ds_adcp = process_shear.process(adcp_path, glider_nc, options=options)
    elapsed = _t.time() - t0

    os.makedirs(run_dir, exist_ok=True)
    out_path = _rf(run_dir, 'shear_processed.nc')
    ds_adcp.to_netcdf(out_path)
    log(f'Completed in {elapsed:.1f}s')
    log(f'Saved: {out_path}')
    log(f'Dimensions: {dict(ds_adcp.sizes)}')
    for v in ['Sh_E', 'Sh_N', 'Sh_U', 'E', 'N', 'U']:
        if v in ds_adcp:
            valid = int(ds_adcp[v].notnull().sum())
            total = ds_adcp[v].size
            log(f'  {v}: {valid}/{total} valid ({valid/total*100:.1f}%)')


def _adcp_compute_currents(base_dir, run_dir, opts, log):
    import numpy as np
    import xarray as xr
    import time as _t
    from gliderad2cp import process_currents, tools

    shear_path = _rf(run_dir, 'shear_processed.nc')
    gps_path   = os.path.join(base_dir, 'gps_data.npz')
    for p in [shear_path, gps_path]:
        if not os.path.exists(p):
            raise RuntimeError(f'Required file not found: {os.path.basename(p)}')

    log('Loading shear dataset...')
    with _nc_lock:
        with xr.open_dataset(shear_path) as ds_adcp:
            log(f'  Dimensions: {dict(ds_adcp.sizes)}')

            log('Loading GPS arrays...')
            gps          = np.load(gps_path, allow_pickle=True)
            gps_predive  = gps['gps_predive']
            gps_postdive = gps['gps_postdive']
            log(f'  GPS pairs: {len(gps_predive)}')

            options = tools.get_options(
                xaxis=int(opts.get('xaxis', 1)),
                yaxis=opts.get('yaxis') or None,
                shear_bias_regression_depth_slice=(
                    float(opts.get('depth_min', 10)),
                    float(opts.get('depth_max', 1000)),
                ),
            )
            log('Computing absolute currents...')
            t0 = _t.time()
            currents, DAC = process_currents.process(ds_adcp, gps_predive, gps_postdive, options=options)
            elapsed = _t.time() - t0
            log(f'Completed in {elapsed:.1f}s')

    out_nc  = _rf(run_dir, 'currents.nc')
    out_dac = _rf(run_dir, 'dac.npz')
    currents.to_netcdf(out_nc)
    np.savez(out_dac, dive_time=DAC['dive_time'], dac=DAC['dac'])
    log(f'Saved: {out_nc}')
    log(f'Saved: {out_dac}')
    n_valid = int(np.isfinite(DAC['dac']).all(axis=1).sum())
    log(f'DAC: {len(DAC["dive_time"])} dives, {n_valid} with valid vectors')

    # If yaxis was auto (None), detect actual depth bin size from the output
    # and rename the run directory to include it (e.g. xaxis3_d10-1000 → xaxis3_d10-1000_y2).
    actual_yaxis = None
    if opts.get('yaxis') is None and 'depth' in currents.coords:
        depth_vals = currents.coords['depth'].values
        if len(depth_vals) > 1:
            spacing = abs(float(depth_vals[1] - depth_vals[0]))
            actual_yaxis = int(round(spacing))
            old_label = os.path.basename(run_dir)
            if '_y' not in old_label:
                new_label   = f'{old_label}_y{actual_yaxis}'
                new_run_dir = os.path.join(os.path.dirname(run_dir), new_label)
                os.rename(run_dir, new_run_dir)
                log(f'Detected cell size: {actual_yaxis} m — renamed run directory:')
                log(f'  {old_label} → {new_label}')
                old_prefix = old_label + '_'
                new_prefix = new_label + '_'
                for fname in os.listdir(new_run_dir):
                    if fname.startswith(old_prefix):
                        os.rename(
                            os.path.join(new_run_dir, fname),
                            os.path.join(new_run_dir, new_prefix + fname[len(old_prefix):])
                        )
    return actual_yaxis


def _adcp_bias_correction(run_dir, opts, log):
    import xarray as xr
    import time as _t
    from gliderad2cp import process_bias, tools

    currents_path = _rf(run_dir, 'currents.nc')
    if not os.path.exists(currents_path):
        raise RuntimeError('currents.nc not found — run Compute Absolute Currents first')

    log('Loading currents dataset...')
    with _nc_lock:
        with xr.open_dataset(currents_path) as currents:
            log(f'  Dimensions: {dict(currents.sizes)}')

            options = tools.get_options(
                xaxis=int(opts.get('xaxis', 1)),
                yaxis=opts.get('yaxis') or None,
                shear_bias_regression_depth_slice=(
                    float(opts.get('depth_min', 10)),
                    float(opts.get('depth_max', 1000)),
                ),
                weight_shear_bias_regression=bool(opts.get('weight_bias', False)),
                velocity_dependent_shear_bias_correction=bool(opts.get('vel_dep_bias', False)),
            )
            log(f'Bias options: weight_regression={options["weight_shear_bias_regression"]}  '
                f'velocity_dependent={options["velocity_dependent_shear_bias_correction"]}')
            log('Applying bias correction...')
            t0             = _t.time()
            currents_bias  = process_bias.process(currents, options=options)
            elapsed        = _t.time() - t0
            log(f'Completed in {elapsed:.1f}s')

    out_path = _rf(run_dir, 'currents_bias_corrected.nc')
    currents_bias.to_netcdf(out_path)
    log(f'Saved: {out_path}')
    log(f'Variables: {list(currents_bias.data_vars)}')

    # Save bias diagnostic plot produced by the library
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        process_bias.visualise(currents_bias, options)
        diag_path = _rf(run_dir, 'bias_diagnostic.png')
        plt.savefig(diag_path, dpi=150, bbox_inches='tight')
        plt.close('all')
        log(f'Saved: {diag_path}')
    except Exception as exc:
        log(f'Bias diagnostic plot skipped ({exc})')


def _adcp_lab_diagnostics(run_dir, log):
    """Run pass/fail checks on a bench-test AD2CP file and write an HTML report."""
    import base64, io as _io, datetime as _dt
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr
    import pandas as pd

    shear_path = _rf(run_dir, 'shear_processed.nc')
    if not os.path.exists(shear_path):
        raise RuntimeError('shear_processed.nc not found — run Process Shear first')

    log('Loading shear_processed.nc...')
    with _nc_lock:
        with xr.open_dataset(shear_path) as ds:
            log(f'  Dimensions: {dict(ds.sizes)}')

            n_pings  = ds.sizes.get('time', 0)
            time_raw = ds.time.values
            try:
                time_pd = pd.to_datetime(time_raw)
            except Exception:
                time_pd = pd.to_datetime(time_raw, unit='s', errors='coerce')

            def _get(key):
                v = ds.get(key)
                return v.values.astype(float) if v is not None else None

            amp  = {i: _get(f'AmplitudeBeam{i}')   for i in range(1, 5)}
            cor  = {i: _get(f'CorrelationBeam{i}')  for i in range(1, 5)}
            heading  = _get('Heading')
            pitch    = _get('Pitch')
            roll     = _get('Roll')
            pressure = _get('Pressure')
            temp     = _get('WaterTemperature')
            battery  = _get('Battery')
            ensemble = ds['EnsembleCount'].values if 'EnsembleCount' in ds else None
            cell_size = float(np.nanmean(ds['CellSize'].values))   if 'CellSize'     in ds else None
            blanking  = float(np.nanmean(ds['Blanking'].values))   if 'Blanking'     in ds else None
            n_cells   = int(np.nanmax(ds['NumberofCells'].values)) if 'NumberofCells' in ds else None

    checks = {}

    # ── 1. Beam amplitude symmetry ─────────────────────────────────────────────
    amp_means = {}
    for i in range(1, 5):
        if amp[i] is not None:
            amp_means[i] = float(np.nanmean(amp[i]))
    if len(amp_means) == 4:
        diff = max(amp_means.values()) - min(amp_means.values())
        checks['beam_amp_symmetry'] = {
            'label': 'Beam Amplitude Symmetry',
            'pass':  diff < 3.0,
            'detail': (f'Max spread: {diff:.1f} dB  |  ' +
                       '  '.join(f'B{i}: {amp_means[i]:.1f}' for i in range(1,5))),
            'threshold': 'All 4 beams within 3 dB of each other',
        }
        log(f'Beam amplitude symmetry: spread {diff:.1f} dB → {"PASS" if diff < 3.0 else "FAIL"}')

    # ── 2. Beam correlation symmetry ──────────────────────────────────────────
    cor_means = {}
    for i in range(1, 5):
        if cor[i] is not None:
            cor_means[i] = float(np.nanmean(cor[i]))
    if len(cor_means) == 4:
        cor_diff = max(cor_means.values()) - min(cor_means.values())
        sym_ok   = cor_diff < 15
        checks['beam_cor_symmetry'] = {
            'label': 'Beam Correlation Symmetry',
            'pass':  sym_ok,
            'detail': (f'Max spread: {cor_diff:.1f}  |  ' +
                       '  '.join(f'B{i}: {cor_means[i]:.1f}' for i in range(1,5))),
            'threshold': 'All 4 beams within 15 counts of each other',
        }
        all_low = all(v < 50 for v in cor_means.values())
        checks['air_baseline'] = {
            'label': 'Air-Test Correlation Baseline',
            'pass':  all_low,
            'detail': ('All beams < 50 — expected noise floor for bench test' if all_low
                       else 'WARNING: High correlations detected — instrument may be in water'),
            'threshold': 'All beams < 50 counts (expected in air)',
        }
        log(f'Correlation symmetry: spread {cor_diff:.1f} → {"PASS" if sym_ok else "FAIL"}')

    # ── 3. Data completeness (ensemble counter gaps) ──────────────────────────
    if ensemble is not None:
        diffs = np.diff(ensemble.astype(float))
        med   = float(np.nanmedian(diffs))
        gaps  = int((diffs > med * 1.5).sum())
        checks['data_completeness'] = {
            'label': 'Data Completeness',
            'pass':  gaps == 0,
            'detail': 'No dropped pings detected' if gaps == 0 else f'{gaps} dropped ping(s) detected',
            'threshold': 'Zero dropped pings',
        }
        log(f'Data completeness: {gaps} gap(s) → {"PASS" if gaps == 0 else "FAIL"}')

    # ── 5. Orientation sensors ────────────────────────────────────────────────
    for name, arr, lo, hi in [('Heading', heading, 0, 360),
                               ('Pitch',   pitch,  -45, 45),
                               ('Roll',    roll,   -45, 45)]:
        if arr is not None:
            fin = arr[np.isfinite(arr)]
            if len(fin):
                in_range = bool(fin.min() >= lo and fin.max() <= hi)
                all_fin  = len(fin) == len(arr)
                checks[f'sensor_{name.lower()}'] = {
                    'label': f'{name} Sensor',
                    'pass':  in_range and all_fin,
                    'detail': (f'Range: {fin.min():.1f}° to {fin.max():.1f}°  |  '
                               f'{len(arr)-len(fin)} NaN value(s)'),
                    'threshold': f'All finite, within {lo}° – {hi}°',
                }
                log(f'{name}: {fin.min():.1f}–{fin.max():.1f}° → {"PASS" if in_range else "WARN"}')

    # ── 6. Pressure ───────────────────────────────────────────────────────────
    if pressure is not None:
        fin = pressure[np.isfinite(pressure)]
        if len(fin):
            ok = bool(fin.min() >= 0 and len(fin) == len(pressure))
            checks['pressure'] = {
                'label': 'Pressure Sensor',
                'pass':  ok,
                'detail': f'Range: {fin.min():.2f} – {fin.max():.2f} dbar  |  {len(pressure)-len(fin)} NaN',
                'threshold': 'All finite and non-negative',
            }
            log(f'Pressure: {fin.min():.2f}–{fin.max():.2f} dbar')

    # ── 7. Temperature ────────────────────────────────────────────────────────
    if temp is not None:
        fin = temp[np.isfinite(temp)]
        if len(fin):
            ok = bool(5 < fin.mean() < 50 and len(fin) == len(temp))
            checks['temperature'] = {
                'label': 'Temperature Sensor',
                'pass':  ok,
                'detail': f'Mean: {fin.mean():.1f}°C  |  Range: {fin.min():.1f} – {fin.max():.1f}°C',
                'threshold': '5 – 50°C (reasonable lab range)',
            }
            log(f'Temperature: {fin.mean():.1f}°C → {"PASS" if ok else "FAIL"}')

    # ── 8. Battery ────────────────────────────────────────────────────────────
    if battery is not None:
        fin = battery[np.isfinite(battery)]
        if len(fin):
            ok = bool(fin.min() > 10.0)
            checks['battery'] = {
                'label': 'Battery Voltage',
                'pass':  ok,
                'detail': f'Mean: {fin.mean():.2f} V  |  Min: {fin.min():.2f} V',
                'threshold': '> 10 V',
            }
            log(f'Battery: {fin.mean():.2f} V → {"PASS" if ok else "FAIL"}')

    n_pass  = sum(1 for c in checks.values() if c['pass'])
    n_total = len(checks)
    log(f'Checks: {n_pass}/{n_total} passed')

    # ── Plots ─────────────────────────────────────────────────────────────────
    log('Generating diagnostic plots...')
    colors = ['#e74c3c', '#3498db', '#2ecc71', '#f39c12']
    x_idx  = np.arange(n_pings)

    fig, axes = plt.subplots(1, 2, figsize=(16, 5))
    for i, col in enumerate(colors, start=1):
        if amp[i] is not None:
            mn = np.nanmean(amp[i], axis=1) if amp[i].ndim > 1 else amp[i]
            axes[0].plot(x_idx, mn, color=col, lw=0.9, alpha=0.85, label=f'Beam {i}')
        if cor[i] is not None:
            mn = np.nanmean(cor[i], axis=1) if cor[i].ndim > 1 else cor[i]
            axes[1].plot(x_idx, mn, color=col, lw=0.9, alpha=0.85, label=f'Beam {i}')
    axes[0].set_title('Amplitude per Beam'); axes[0].set_xlabel('Ping'); axes[0].set_ylabel('Amplitude (counts)'); axes[0].legend()
    axes[1].set_title('Correlation per Beam'); axes[1].set_xlabel('Ping'); axes[1].set_ylabel('Correlation (counts)')
    axes[1].axhline(50, color='orange', ls='--', lw=1.5, label='Validity threshold (50)')
    axes[1].set_ylim(0, 105); axes[1].legend()
    fig.suptitle('Beam Diagnostics', fontsize=13); plt.tight_layout()
    buf = _io.BytesIO(); plt.savefig(buf, format='png', dpi=130, bbox_inches='tight'); plt.close(fig)
    beam_img = base64.b64encode(buf.getvalue()).decode()

    sensor_panels = [
        ('Heading (°)',       heading),
        ('Pitch (°)',         pitch),
        ('Roll (°)',          roll),
        ('Pressure (dbar)',   pressure),
        ('Temperature (°C)',  temp),
        ('Battery (V)',       battery),
    ]
    sensor_panels = [(lbl, arr) for lbl, arr in sensor_panels if arr is not None]
    sensor_img = None
    if sensor_panels:
        cols_s = 2; rows_s = (len(sensor_panels) + 1) // 2
        fig, axes = plt.subplots(rows_s, cols_s, figsize=(16, 3.5 * rows_s))
        ax_flat = np.array(axes).flatten()
        for ax, (lbl, arr) in zip(ax_flat, sensor_panels):
            ax.plot(x_idx, arr, color='#3498db', lw=0.8, alpha=0.9)
            ax.set_title(lbl); ax.set_xlabel('Ping index')
        for ax in ax_flat[len(sensor_panels):]:
            ax.set_visible(False)
        fig.suptitle('Internal Sensors', fontsize=13); plt.tight_layout()
        buf2 = _io.BytesIO(); plt.savefig(buf2, format='png', dpi=130, bbox_inches='tight'); plt.close(fig)
        sensor_img = base64.b64encode(buf2.getvalue()).decode()

    # ── HTML report ───────────────────────────────────────────────────────────
    log('Building HTML report...')
    overall_ok  = n_pass == n_total
    summary_cls = 'overall-pass' if overall_ok else 'overall-fail'
    summary_txt = (f'ALL {n_total} CHECKS PASSED' if overall_ok
                   else f'{n_pass} / {n_total} CHECKS PASSED')

    rows_html = ''
    for c in checks.values():
        icon = '&#10004;' if c['pass'] else '&#10008;'
        cls  = 'pass' if c['pass'] else 'fail'
        rows_html += (f'<tr class="{cls}"><td class="icon">{icon}</td>'
                      f'<td class="name">{c["label"]}</td>'
                      f'<td class="detail">{c["detail"]}</td>'
                      f'<td class="thresh">{c["threshold"]}</td></tr>\n')

    cfg_rows = ''
    for lbl, val in [('Cell Size', f'{cell_size:.2f} m' if cell_size else 'N/A'),
                     ('Blanking Distance', f'{blanking:.2f} m' if blanking else 'N/A'),
                     ('Number of Cells', str(n_cells) if n_cells else 'N/A'),
                     ('Total Pings', f'{n_pings:,}'),
                     ('Start Time', str(time_pd[0])  if n_pings else 'N/A'),
                     ('End Time',   str(time_pd[-1]) if n_pings else 'N/A')]:
        cfg_rows += f'<tr><td>{lbl}</td><td>{val}</td></tr>\n'

    sensor_html = (f'<h2>Internal Sensors</h2>'
                   f'<img src="data:image/png;base64,{sensor_img}" style="max-width:100%">'
                   if sensor_img else '')

    html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AD2CP Lab Diagnostic Report</title>
<style>
body{{font-family:Arial,sans-serif;background:#0f1720;color:#c8dcea;margin:0;padding:24px}}
h1{{color:#7ecbf5;font-size:22px;margin-bottom:4px}}
.sub{{color:#6b8ba4;font-size:13px;margin-bottom:24px}}
.overall-pass{{background:#0d2e1a;border:1px solid #2e7d32;color:#4caf7d;padding:12px 20px;border-radius:6px;font-size:16px;font-weight:bold;margin-bottom:20px;display:inline-block}}
.overall-fail{{background:#2a1200;border:1px solid #7a3e00;color:#f0a85a;padding:12px 20px;border-radius:6px;font-size:16px;font-weight:bold;margin-bottom:20px;display:inline-block}}
.note{{background:#111d2a;border-left:3px solid #3498db;padding:10px 14px;font-size:12px;color:#9bbdd8;border-radius:0 4px 4px 0;margin-bottom:24px}}
h2{{color:#7ecbf5;font-size:15px;margin:28px 0 10px;border-bottom:1px solid #1e3a56;padding-bottom:6px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px}}
th{{background:#1a3045;color:#9bbdd8;text-align:left;padding:8px 12px;font-weight:600}}
td{{padding:7px 12px;border-bottom:1px solid #1a3045}}
tr.pass{{background:#0d1c14}}
tr.fail{{background:#1c1008}}
td.icon{{font-size:15px;width:28px}}
tr.pass td.icon{{color:#4caf7d}}
tr.fail td.icon{{color:#f0a85a}}
td.name{{font-weight:600;color:#c8dcea}}
td.detail{{color:#9bbdd8}}
td.thresh{{color:#6b8ba4;font-size:11px}}
.cfg-table td:first-child{{color:#9bbdd8;font-weight:600;width:200px}}
.cfg-table tr{{background:#121f2c}}
img{{border-radius:6px;margin-bottom:16px;max-width:100%;background:#fff}}
</style>
</head><body>
<h1>AD2CP Lab Diagnostic Report</h1>
<div class="sub">Generated: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp; {n_pings:,} pings</div>
<div class="{summary_cls}">{summary_txt}</div>
<div class="note">This is a <strong>bench / air test</strong>. Velocity data is always NaN in air — the checks
below assess instrument health and configuration, not acoustic performance in water.</div>
<h2>Diagnostic Checks</h2>
<table><tr><th></th><th>Check</th><th>Result</th><th>Threshold</th></tr>
{rows_html}</table>
<h2>Configuration</h2>
<table class="cfg-table"><tr><th>Parameter</th><th>Value</th></tr>{cfg_rows}</table>
<h2>Beam Diagnostics</h2>
<img src="data:image/png;base64,{beam_img}">
{sensor_html}
</body></html>'''

    out = _rf(run_dir, 'lab_diagnostic_report.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    log(f'Saved: {out}')
    ok_str = 'PASS' if overall_ok else 'FAIL'
    log(f'Overall result: {ok_str} ({n_pass}/{n_total} checks)')


def _adcp_generate_plots(base_dir, run_dir, is_test, opts, log):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import xarray as xr

    try:
        import cmocean.cm as cmo
        cmo_balance = cmo.balance
        cmo_speed   = cmo.speed
    except ImportError:
        cmo_balance = 'RdBu_r'
        cmo_speed   = 'viridis'
        log('cmocean not found — using fallback colormaps')

    if is_test:
        shear_path = _rf(run_dir, 'shear_processed.nc')
        if not os.path.exists(shear_path):
            raise RuntimeError('shear_processed.nc not found — run Process Shear first')

        with _nc_lock:
            with xr.open_dataset(shear_path) as ds:
                log(f'Loaded shear dataset: {dict(ds.sizes)}')
                n_pings = ds.sizes.get('time', 0)
                x_idx   = np.arange(n_pings)
                colors  = ['#e74c3c', '#3498db', '#2ecc71', '#f39c12']

                # Plot 1: Beam amplitude & correlation per beam
                fig, axes = plt.subplots(1, 2, figsize=(16, 5))
                for i, col in enumerate(colors, start=1):
                    av = ds.get(f'AmplitudeBeam{i}')
                    cv = ds.get(f'CorrelationBeam{i}')
                    if av is not None:
                        vals = av.values.astype(float)
                        mn   = np.nanmean(vals, axis=1) if vals.ndim > 1 else vals
                        axes[0].plot(x_idx, mn, color=col, lw=0.8, alpha=0.8, label=f'Beam {i}')
                    if cv is not None:
                        vals = cv.values.astype(float)
                        mn   = np.nanmean(vals, axis=1) if vals.ndim > 1 else vals
                        axes[1].plot(x_idx, mn, color=col, lw=0.8, alpha=0.8, label=f'Beam {i}')

                axes[0].set_title('Amplitude per Beam (counts)')
                axes[0].set_xlabel('Ping index'); axes[0].set_ylabel('Amplitude'); axes[0].legend()
                axes[1].set_title('Correlation per Beam')
                axes[1].set_xlabel('Ping index'); axes[1].set_ylabel('Correlation (counts)')
                axes[1].axhline(50, color='orange', ls='--', lw=1.5, label='Validity threshold')
                axes[1].set_ylim(0, 105); axes[1].legend()
                fig.suptitle('AD2CP Lab Test — Beam Diagnostics', fontsize=13)
                plt.tight_layout()
                p1 = _rf(run_dir, 'beam_diagnostics.png')
                plt.savefig(p1, dpi=150, bbox_inches='tight')
                plt.close(fig)
                log(f'Saved: {p1}')

                # Plot 2: Internal sensors
                sensor_pairs = [
                    ('Heading',          '°',    'Heading'),
                    ('Pitch',            '°',    'Pitch'),
                    ('Roll',             '°',    'Roll'),
                    ('Pressure',         'dbar', 'Pressure'),
                    ('WaterTemperature', '°C',   'Temperature'),
                    ('Battery',          'V',    'Battery'),
                ]
                avail = [(lbl, unit, ds[key].values.astype(float))
                         for key, unit, lbl in sensor_pairs if key in ds]
                if avail:
                    cols_s = 2; rows_s = (len(avail) + 1) // 2
                    fig, axes = plt.subplots(rows_s, cols_s, figsize=(16, 3.5 * rows_s))
                    ax_flat = np.array(axes).flatten()
                    for ax, (lbl, unit, arr) in zip(ax_flat, avail):
                        ax.plot(x_idx, arr, color='#3498db', lw=0.8, alpha=0.9)
                        ax.set_title(f'{lbl} ({unit})')
                        ax.set_xlabel('Ping index'); ax.set_ylabel(unit)
                    for ax in ax_flat[len(avail):]:
                        ax.set_visible(False)
                    fig.suptitle('AD2CP Lab Test — Internal Sensors', fontsize=13)
                    plt.tight_layout()
                    p2 = _rf(run_dir, 'sensor_timeseries.png')
                    plt.savefig(p2, dpi=150, bbox_inches='tight')
                    plt.close(fig)
                    log(f'Saved: {p2}')

    else:
        bias_path = _rf(run_dir, 'currents_bias_corrected.nc')
        gps_path  = os.path.join(base_dir, 'gps_data.npz')
        dac_path  = _rf(run_dir, 'dac.npz')
        for p in [bias_path, gps_path, dac_path]:
            if not os.path.exists(p):
                raise RuntimeError(f'Required file not found: {os.path.basename(p)}')

        with _nc_lock:
            with xr.open_dataset(bias_path) as currents_bias:
                log(f'Loaded bias-corrected currents: {dict(currents_bias.sizes)}')

                vel_E = currents_bias['velocity_E_DAC_reference_sb_corrected']
                vel_N = currents_bias['velocity_N_DAC_reference_sb_corrected']

                time_clean = (pd.Series(currents_bias.time.values)
                              .interpolate(method='linear').values)
                log(f'NaT remaining after interpolation: {pd.isnull(time_clean).sum()}')

                depth = currents_bias.depth.values

                # Depth axis limits — surface anchored at 0, max from data
                d_min = 0.0
                d_max = float(np.nanmax(depth))

                # Plot 1: E/N velocity sections
                fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True, sharey=True)
                for ax, var, label in zip(axes, [vel_E, vel_N],
                                          ['Eastward velocity (m/s)', 'Northward velocity (m/s)']):
                    p = ax.pcolormesh(time_clean, depth, var.values,
                                      cmap=cmo_balance, vmin=-0.5, vmax=0.5, shading='auto')
                    plt.colorbar(p, ax=ax, label=label, pad=0.01)
                    ax.set_ylabel('Depth (m)')
                axes[1].set_xlabel('Date')
                # Apply depth clipping and inversion ONCE on the shared axis
                axes[0].set_ylim(d_max, d_min)
                fig.suptitle('Bias-corrected absolute velocities', fontsize=13)
                plt.tight_layout()
                p1 = _rf(run_dir, 'velocity_sections.png')
                plt.savefig(p1, dpi=150)
                plt.close(fig)
                log(f'Saved: {p1}')

                # Plot 2: Speed
                speed = np.sqrt(vel_E ** 2 + vel_N ** 2)
                fig, ax = plt.subplots(figsize=(16, 4))
                p = ax.pcolormesh(time_clean, depth, speed.values,
                                  cmap=cmo_speed, vmin=0, vmax=0.5, shading='auto')
                plt.colorbar(p, ax=ax, label='Speed (m/s)', pad=0.01)
                ax.set_ylabel('Depth (m)')
                ax.set_xlabel('Date')
                ax.set_ylim(d_max, d_min)
                ax.set_title('Current speed')
                plt.tight_layout()
                p2 = _rf(run_dir, 'speed_section.png')
                plt.savefig(p2, dpi=150)
                plt.close(fig)
                log(f'Saved: {p2}')

                # Plot 3: Shear sections (Sh_E, Sh_N)
                # shear_processed.nc uses (time, gridded_bin); depth_offset coord = range from glider
                try:
                    shear_path = _rf(run_dir, 'shear_processed.nc')
                    with _nc_lock:
                        with xr.open_dataset(shear_path) as ds_sh:
                            if 'Sh_E' in ds_sh and 'Sh_N' in ds_sh:
                                sh_time = pd.Series(ds_sh.time.values).interpolate(method='linear').values
                                if 'depth_offset' in ds_sh.coords:
                                    # depth_offset is negative (upward-looking range); use abs() for readable axis
                                    sh_y      = np.abs(ds_sh['depth_offset'].values)
                                    sh_ylabel = 'Range from glider (m)'
                                else:
                                    sh_y      = np.arange(ds_sh['Sh_E'].shape[1])
                                    sh_ylabel = 'Bin index'
                                sh_E_v = ds_sh['Sh_E'].values
                                sh_N_v = ds_sh['Sh_N'].values
                                # clim at 95th percentile so the colorscale isn't dominated by noise spikes
                                sh_mag_all = np.sqrt(sh_E_v**2 + sh_N_v**2)
                                clim = float(np.nanpercentile(sh_mag_all, 95))
                                clim = max(clim, 0.005)   # floor so empty data still has a sensible scale
                                fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True, sharey=True)
                                for ax, var, label in zip(axes,
                                                          [sh_E_v, sh_N_v],
                                                          ['Eastward shear dU/dz (s⁻¹)', 'Northward shear dV/dz (s⁻¹)']):
                                    p = ax.pcolormesh(sh_time, sh_y, var.T,
                                                      cmap=cmo_balance, vmin=-clim, vmax=clim, shading='auto')
                                    plt.colorbar(p, ax=ax, label=label, pad=0.01)
                                    ax.set_ylabel(sh_ylabel)
                                    ax.invert_yaxis()
                                axes[1].set_xlabel('Date')
                                fig.suptitle(f'Vertical shear  (color limits ±{clim:.4f} s⁻¹, 95th pct)', fontsize=13)
                                plt.tight_layout()
                                p_sh = _rf(run_dir, 'shear_sections.png')
                                plt.savefig(p_sh, dpi=150)
                                plt.close(fig)
                                log(f'Saved: {p_sh}')
                            else:
                                log('Shear variables Sh_E/Sh_N not found in shear_processed.nc — skipping')
                except Exception as exc:
                    log(f'Shear section plot skipped ({exc})')

                # Plot 4: Backscatter / acoustic amplitude sections
                # Amplitude is on (time, bin); use mean of D1-D4 depth arrays as approximate y-axis
                try:
                    with _nc_lock:
                        with xr.open_dataset(_rf(run_dir, 'shear_processed.nc')) as ds_amp:
                            amp_vars = [f'AmplitudeBeam{b}' for b in range(1, 5)
                                        if f'AmplitudeBeam{b}' in ds_amp]
                            if amp_vars:
                                amp_time = pd.Series(ds_amp.time.values).interpolate(method='linear').values
                                # Use Amplitude Range (range from transducer) for y-axis
                                if 'Amplitude Range' in ds_amp:
                                    amp_y      = ds_amp['Amplitude Range'].values
                                    amp_ylabel = 'Range from transducer (m)'
                                else:
                                    amp_y      = np.arange(ds_amp[amp_vars[0]].shape[1])
                                    amp_ylabel = 'Bin index'
                                amp_mean = np.nanmean(
                                    np.stack([ds_amp[v].values for v in amp_vars], axis=0), axis=0
                                )
                                fig, ax = plt.subplots(figsize=(16, 5))
                                p = ax.pcolormesh(amp_time, amp_y, amp_mean.T,
                                                  cmap='inferno', shading='auto')
                                plt.colorbar(p, ax=ax, label='Mean acoustic amplitude (counts)', pad=0.01)
                                ax.set_ylabel(amp_ylabel)
                                ax.set_xlabel('Date')
                                ax.invert_yaxis()   # shallow at top, deep at bottom
                                ax.set_title(f'Acoustic backscatter (mean of {len(amp_vars)} beams)')
                                plt.tight_layout()
                                p_amp = _rf(run_dir, 'backscatter.png')
                                plt.savefig(p_amp, dpi=150)
                                plt.close(fig)
                                log(f'Saved: {p_amp}')
                            else:
                                log('AmplitudeBeam variables not found in shear_processed.nc — skipping')
                except Exception as exc:
                    log(f'Backscatter plot skipped ({exc})')

                # Plot 5: Correlation and amplitude heatmaps on absolute depth axis
                # Groups raw beam pings by profile_number, grids onto regular depth axis using D1-D4 mean depths
                try:
                    with _nc_lock:
                        with xr.open_dataset(_rf(run_dir, 'shear_processed.nc')) as ds_q:
                            corr_vars_q = [f'CorrelationBeam{b}' for b in range(1, 5) if f'CorrelationBeam{b}' in ds_q]
                            amp_vars_q  = [f'AmplitudeBeam{b}'   for b in range(1, 5) if f'AmplitudeBeam{b}'   in ds_q]
                            d_vars_q    = [v for v in ['D1', 'D2', 'D3', 'D4'] if v in ds_q]

                            if corr_vars_q and d_vars_q and 'profile_number' in ds_q:
                                # Per-profile time axis
                                profile_nums          = ds_q.profile_number.values.astype(int)
                                prof_labels, prof_inv = np.unique(profile_nums, return_inverse=True)
                                n_prof                = len(prof_labels)
                                t_float = pd.Series(ds_q.time.values).interpolate(method='linear').values.astype('float64')
                                t_sum   = np.bincount(prof_inv, weights=t_float, minlength=n_prof)
                                t_cnt   = np.bincount(prof_inv, minlength=n_prof)
                                q_time  = (t_sum / np.maximum(t_cnt, 1)).astype('datetime64[ns]')

                                # Mean absolute depth across D1-D4 per (ping, bin)
                                D_arr = np.nanmean(np.stack([ds_q[v].values for v in d_vars_q], axis=0), axis=0)

                                # Depth grid using yaxis parameter as step size (default 10 m).
                                # opts['yaxis'] is explicitly None on the wire in "auto" mode (not an
                                # absent key), so .get(..., 10) alone never falls back -- match the
                                # `or` idiom already used for this same field elsewhere in this file.
                                grid_step     = float(opts.get('yaxis') or 10)
                                dg            = np.arange(d_min, d_max + grid_step, grid_step)
                                n_depth       = max(len(dg) - 1, 1)
                                depth_centers = (dg[:-1] + dg[1:]) / 2

                                # Precompute flat indices once — reused for both plots
                                n_bins     = ds_q.sizes['bin']
                                depth_flat = D_arr.ravel()
                                prof_flat  = np.repeat(prof_inv, n_bins)
                                depth_idx  = np.searchsorted(dg[1:], depth_flat).clip(0, n_depth - 1)
                                in_range   = np.isfinite(depth_flat) & (depth_flat >= d_min) & (depth_flat <= d_max)
                                lin_base   = prof_flat * n_depth + depth_idx

                                def _pdgrid(var_arr):
                                    var_flat = var_arr.ravel()
                                    valid    = in_range & np.isfinite(var_flat)
                                    lin      = lin_base[valid]
                                    vsum     = np.bincount(lin, weights=var_flat[valid].astype(float), minlength=n_prof * n_depth)
                                    vcnt     = np.bincount(lin, minlength=n_prof * n_depth)
                                    return np.where(vcnt > 0, vsum / np.maximum(vcnt, 1), np.nan).reshape(n_prof, n_depth)

                                # Correlation heatmap
                                corr_data = np.nanmean(np.stack([ds_q[v].values for v in corr_vars_q], axis=0), axis=0)
                                corr_grid = _pdgrid(corr_data)
                                qc_thresh = float(opts.get('qc_corr', 80))
                                fig, ax   = plt.subplots(figsize=(16, 5))
                                p = ax.pcolormesh(q_time, depth_centers, corr_grid.T,
                                                  cmap='RdYlGn', vmin=0, vmax=100, shading='auto')
                                plt.colorbar(p, ax=ax, label=f'Mean beam correlation (%, {len(corr_vars_q)} beams)', pad=0.01)
                                ax.set_ylabel('Depth (m)')
                                ax.set_xlabel('Date')
                                ax.set_ylim(d_max, d_min)
                                ax.set_title(f'Beam correlation  (QC threshold: {qc_thresh:.0f}%  —  green = above threshold)')
                                plt.tight_layout()
                                p_corr = _rf(run_dir, 'correlation.png')
                                plt.savefig(p_corr, dpi=150)
                                plt.close(fig)
                                log(f'Saved: {p_corr}')

                                # Amplitude / SNR proxy heatmap
                                if amp_vars_q:
                                    amp_data = np.nanmean(np.stack([ds_q[v].values for v in amp_vars_q], axis=0), axis=0)
                                    amp_grid = _pdgrid(amp_data)
                                    fig, ax  = plt.subplots(figsize=(16, 5))
                                    p = ax.pcolormesh(q_time, depth_centers, amp_grid.T,
                                                      cmap='inferno', shading='auto')
                                    plt.colorbar(p, ax=ax,
                                                 label=f'Mean beam amplitude — SNR proxy (counts, {len(amp_vars_q)} beams)',
                                                 pad=0.01)
                                    ax.set_ylabel('Depth (m)')
                                    ax.set_xlabel('Date')
                                    ax.set_ylim(d_max, d_min)
                                    ax.set_title('Beam amplitude (SNR proxy)')
                                    plt.tight_layout()
                                    p_ba = _rf(run_dir, 'beam_amplitude.png')
                                    plt.savefig(p_ba, dpi=150)
                                    plt.close(fig)
                                    log(f'Saved: {p_ba}')

                                    # SNR heatmap — noise floor from 5th pct of last range bin (fully attenuated)
                                    noise_floor = float(np.nanmean(amp_data[:, -3:]))
                                    snr_grid    = None
                                    if noise_floor > 0:
                                        snr_data = 20.0 * np.log10(np.maximum(amp_data / noise_floor, 1e-9))
                                        snr_grid = _pdgrid(snr_data)
                                        fig, ax  = plt.subplots(figsize=(16, 5))
                                        p = ax.pcolormesh(q_time, depth_centers, snr_grid.T,
                                                          cmap='viridis', shading='auto')
                                        plt.colorbar(p, ax=ax,
                                                     label=f'SNR (dB, {len(amp_vars_q)} beams)', pad=0.01)
                                        ax.set_ylabel('Depth (m)')
                                        ax.set_xlabel('Date')
                                        ax.set_ylim(d_max, d_min)
                                        ax.set_title(
                                            f'Beam SNR  (noise floor: {noise_floor:.1f} counts '
                                            f'— mean of last 3 range bins)')
                                        plt.tight_layout()
                                        p_snr = _rf(run_dir, 'snr.png')
                                        plt.savefig(p_snr, dpi=150)
                                        plt.close(fig)
                                        log(f'Saved: {p_snr}')

                                    # Save depth-gridded beam quality data as NetCDF
                                    try:
                                        bq_coords = {
                                            'time':  xr.DataArray(q_time, dims=['profile'],
                                                         attrs={'long_name': 'Profile mean time'}),
                                            'depth': xr.DataArray(depth_centers, dims=['depth'],
                                                         attrs={'units': 'm', 'long_name': 'Depth bin centre',
                                                                'positive': 'down'}),
                                        }
                                        bq_vars = {
                                            'correlation': xr.DataArray(
                                                corr_grid, dims=['profile', 'depth'],
                                                attrs={'units': '%',
                                                       'long_name': f'Mean beam correlation ({len(corr_vars_q)} beams)'}),
                                            'amplitude': xr.DataArray(
                                                amp_grid, dims=['profile', 'depth'],
                                                attrs={'units': 'counts',
                                                       'long_name': f'Mean beam amplitude ({len(amp_vars_q)} beams)'}),
                                        }
                                        if snr_grid is not None:
                                            bq_vars['snr'] = xr.DataArray(
                                                snr_grid, dims=['profile', 'depth'],
                                                attrs={'units': 'dB',
                                                       'long_name': f'Beam SNR ({len(amp_vars_q)} beams)',
                                                       'noise_floor_counts': noise_floor,
                                                       'noise_floor_method':
                                                           'mean of last 3 range bins across all pings'})
                                        ds_bq = xr.Dataset(bq_vars, coords=bq_coords,
                                            attrs={'title': 'AD2CP beam quality metrics gridded to depth',
                                                   'depth_grid_step_m': grid_step,
                                                   'n_corr_beams': len(corr_vars_q),
                                                   'n_amp_beams':  len(amp_vars_q)})
                                        p_bq = _rf(run_dir, 'beam_quality.nc')
                                        ds_bq.to_netcdf(p_bq)
                                        ds_bq.close()
                                        log(f'Saved: {p_bq}')
                                    except Exception as exc:
                                        log(f'beam_quality.nc save skipped ({exc})')
                            else:
                                log('Beam quality heatmaps: CorrelationBeam/D1-D4/profile_number not found — skipping')
                except Exception as exc:
                    log(f'Beam quality heatmaps skipped ({exc})')

                # Plot 6: DAC time series
                try:
                    dac_data   = np.load(dac_path, allow_pickle=True)
                    dac_E_ts   = dac_data['dac'][:, 0].astype(float)
                    dac_N_ts   = dac_data['dac'][:, 1].astype(float)
                    dac_spd_ts = np.sqrt(dac_E_ts ** 2 + dac_N_ts ** 2)
                    dac_times  = pd.to_datetime(dac_data['dive_time'].astype('datetime64[ns]'))
                    valid_ts   = np.isfinite(dac_E_ts) & np.isfinite(dac_N_ts)
                    if valid_ts.sum() >= 2:
                        fig, axes = plt.subplots(2, 1, figsize=(14, 6), sharex=True)
                        axes[0].plot(dac_times[valid_ts], dac_E_ts[valid_ts], '-o',
                                     ms=3, lw=1.2, color='steelblue', label='U (east)')
                        axes[0].plot(dac_times[valid_ts], dac_N_ts[valid_ts], '-o',
                                     ms=3, lw=1.2, color='firebrick', label='V (north)')
                        axes[0].axhline(0, color='k', lw=0.5, ls='--')
                        axes[0].set_ylabel('DAC (m/s)')
                        axes[0].legend(fontsize=9)
                        axes[0].set_title('Depth-averaged current time series')
                        axes[1].plot(dac_times[valid_ts], dac_spd_ts[valid_ts], '-o',
                                     ms=3, lw=1.2, color='darkorchid', label='Speed')
                        axes[1].set_ylabel('Speed (m/s)')
                        axes[1].set_xlabel('Date')
                        axes[1].legend(fontsize=9)
                        plt.tight_layout()
                        p_dts = _rf(run_dir, 'dac_timeseries.png')
                        plt.savefig(p_dts, dpi=150)
                        plt.close(fig)
                        log(f'Saved: {p_dts}')
                    else:
                        log('Insufficient valid DAC vectors for time series — skipping')
                except Exception as exc:
                    log(f'DAC time series plot skipped ({exc})')

                # Plot 6: DAC map
                try:
                    import cartopy.crs as ccrs
                    import cartopy.feature as cfeature
                    from matplotlib.colors import Normalize
                    from matplotlib.cm import ScalarMappable

                    gps          = np.load(gps_path, allow_pickle=True)
                    gps_predive  = gps['gps_predive']
                    dac_data     = np.load(dac_path, allow_pickle=True)
                    dac_E        = dac_data['dac'][:, 0].astype(float)
                    dac_N        = dac_data['dac'][:, 1].astype(float)

                    dive_times_pd = pd.to_datetime(dac_data['dive_time'].astype('datetime64[ns]'))
                    pre_times_pd  = pd.to_datetime(gps_predive[:, 0].astype('datetime64[ns]'))

                    lons = np.full(len(dive_times_pd), np.nan)
                    lats = np.full(len(dive_times_pd), np.nan)
                    for i, dt in enumerate(dive_times_pd):
                        idx     = int(np.argmin(np.abs(pre_times_pd - dt)))
                        lons[i] = float(gps_predive[idx, 1])
                        lats[i] = float(gps_predive[idx, 2])

                    valid  = np.isfinite(dac_E) & np.isfinite(dac_N) & np.isfinite(lons) & np.isfinite(lats)
                    log(f'Valid DAC vectors: {valid.sum()} / {len(valid)}')

                    if valid.sum() >= 2:
                        N_sub    = max(1, len(np.where(valid)[0]) // 50)
                        idx_sub  = np.where(valid)[0][::N_sub]
                        lon_min  = lons[valid].min() - 0.5
                        lon_max  = lons[valid].max() + 0.5
                        lat_min  = lats[valid].min() - 0.5
                        lat_max  = lats[valid].max() + 0.5
                        dac_norm = Normalize(vmin=0, vmax=0.3)

                        fig, ax = plt.subplots(figsize=(10, 12),
                                               subplot_kw={'projection': ccrs.PlateCarree()})
                        ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
                        ax.add_feature(cfeature.OCEAN.with_scale('10m'),     color='lightsteelblue', alpha=0.4)
                        ax.add_feature(cfeature.LAND.with_scale('10m'),      color='wheat', zorder=2)
                        ax.add_feature(cfeature.COASTLINE.with_scale('10m'), linewidth=0.6, zorder=3)
                        gl = ax.gridlines(draw_labels=True, linewidth=0.4, color='grey', alpha=0.6, linestyle='--')
                        gl.top_labels   = False
                        gl.right_labels = False
                        ax.plot(lons[valid], lats[valid], 'k-', lw=0.8, alpha=0.4,
                                transform=ccrs.PlateCarree(), zorder=4)

                        sub_v = (np.isfinite(dac_E[idx_sub]) & np.isfinite(dac_N[idx_sub]) &
                                 np.isfinite(lons[idx_sub]) & np.isfinite(lats[idx_sub]))
                        q = ax.quiver(
                            lons[idx_sub][sub_v], lats[idx_sub][sub_v],
                            dac_E[idx_sub][sub_v], dac_N[idx_sub][sub_v],
                            dac_norm(np.sqrt(dac_E[idx_sub][sub_v] ** 2 + dac_N[idx_sub][sub_v] ** 2)),
                            cmap=cmo_speed, scale=4, width=0.003,
                            transform=ccrs.PlateCarree(), zorder=5,
                        )
                        sm = ScalarMappable(cmap=cmo_speed, norm=dac_norm)
                        sm.set_array([])
                        plt.colorbar(sm, ax=ax, orientation='vertical', pad=0.02,
                                     shrink=0.6, label='DAC speed (m/s)')
                        ax.quiverkey(q, 0.87, 0.97, 0.1, '0.1 m/s',
                                     labelpos='E', fontproperties={'size': 9})
                        ax.set_title('Depth-averaged currents (DAC)', fontsize=12)
                        plt.tight_layout()
                        p3 = _rf(run_dir, 'dac_map_cartopy.png')
                        plt.savefig(p3, dpi=200, bbox_inches='tight')
                        plt.close(fig)
                        log(f'Saved: {p3}')
                    else:
                        log('Insufficient valid DAC vectors for map plot — skipping')

                except Exception as exc:
                    log(f'DAC map skipped ({exc})')


def _adcp_tidal_filter(run_dir, opts, log):
    """40-hour low-pass Butterworth to separate sub-tidal from tidal signal."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr
    import pandas as pd
    from scipy import signal

    bias_path = _rf(run_dir, 'currents_bias_corrected.nc')
    if not os.path.exists(bias_path):
        raise RuntimeError('currents_bias_corrected.nc not found — run Bias Correction first')

    try:
        import cmocean.cm as cmo
        cmo_balance = cmo.balance
        cmo_amp     = cmo.amp
    except ImportError:
        cmo_balance = 'RdBu_r'
        cmo_amp     = 'hot_r'

    log('Loading bias-corrected currents...')
    with _nc_lock:
        with xr.open_dataset(bias_path) as currents:
            vel_E = currents['velocity_E_DAC_reference_sb_corrected'].values.copy()
            vel_N = currents['velocity_N_DAC_reference_sb_corrected'].values.copy()
            depth = currents.depth.values
            times = pd.to_datetime(currents.time.values)

            # Depth axis limits — surface anchored at 0, max from data
            d_max = float(np.nanmax(depth))

            # currents_bias_corrected.nc layout: (depth, profile_index)
            # depth axis = axis 0 (vel_E.shape[0]), profile/time axis = axis 1 (vel_E.shape[1])
            n_dep  = vel_E.shape[0]   # depth levels  (e.g. 501)
            n_prof = vel_E.shape[1]   # profiles/time (e.g. 355) — same length as time coord
            log(f'  Shape: {n_dep} depth levels × {n_prof} profiles')
            log(f'  depth coord length: {len(depth)}, time coord length: {len(times)}')

            # Estimate time spacing from actual profile times
            t_float  = times.astype(np.int64) / 1e9 / 3600   # hours since epoch
            dt_hours = float(np.nanmedian(np.diff(t_float)))
            log(f'  Median profile spacing: {dt_hours:.2f} h')

            CUTOFF_HOURS = 40.0
            nyq  = 1.0 / (2.0 * dt_hours)
            Wn   = (1.0 / CUTOFF_HOURS) / nyq

            if Wn >= 1.0:
                log(f'  Profile spacing too coarse for {CUTOFF_HOURS}-h filter — sub-tidal == full signal')
                u_sub = vel_E.copy()
                v_sub = vel_N.copy()
            else:
                order    = 4
                log(f'  Butterworth order={order}, cutoff={CUTOFF_HOURS} h, Wn={Wn:.4f}')
                b, a     = signal.butter(order, Wn, btype='low')
                u_sub    = np.full_like(vel_E, np.nan)
                v_sub    = np.full_like(vel_N, np.nan)
                min_samp = max(15, int(CUTOFF_HOURS / dt_hours) * 3)
                idx_all  = np.arange(n_prof)   # profile indices (length n_prof)

                # Filter along the profile/time axis (axis 1) for each depth level
                for di in range(n_dep):
                    uc = vel_E[di, :]   # time series at depth di, shape (n_prof,)
                    vc = vel_N[di, :]
                    ok = np.isfinite(uc) & np.isfinite(vc)
                    if ok.sum() < min_samp:
                        continue
                    idx_ok = idx_all[ok]
                    uf = np.interp(idx_all, idx_ok, uc[ok])
                    vf = np.interp(idx_all, idx_ok, vc[ok])
                    try:
                        uf = signal.filtfilt(b, a, uf)
                        vf = signal.filtfilt(b, a, vf)
                    except ValueError:
                        continue
                    u_sub[di, :] = np.where(ok, uf, np.nan)
                    v_sub[di, :] = np.where(ok, vf, np.nan)

                valid_dep = int(np.any(np.isfinite(u_sub), axis=1).sum())
                log(f'  Valid depth levels after filtering: {valid_dep}/{n_dep}')

            u_tidal = vel_E - u_sub
            v_tidal = vel_N - v_sub

            # Save NetCDF — same layout as input: (depth, profile_index)
            time_clean = pd.Series(currents.time.values).interpolate(method='linear').values
            ds_out = xr.Dataset({
                'velocity_E_subtidal': (['depth', 'profile_index'], u_sub),
                'velocity_N_subtidal': (['depth', 'profile_index'], v_sub),
                'velocity_E_tidal':    (['depth', 'profile_index'], u_tidal),
                'velocity_N_tidal':    (['depth', 'profile_index'], v_tidal),
            }, coords={'time': (['profile_index'], time_clean), 'depth': depth})
            ds_out.attrs['filter_cutoff_hours'] = CUTOFF_HOURS
            ds_out.attrs['filter_order']        = 4
            out_nc = _rf(run_dir, 'currents_subtidal.nc')
            ds_out.to_netcdf(out_nc)
            log(f'Saved: {out_nc}')

            # Plot: sub-tidal sections + tidal residual speed
            # pcolormesh(x=time, y=depth, C) expects C.shape=(n_dep, n_prof) — matches array layout
            fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True, sharey=True)
            for ax, data, label in zip(
                axes[:2],
                [u_sub, v_sub],
                ['Sub-tidal E velocity (m/s)', 'Sub-tidal N velocity (m/s)'],
            ):
                p = ax.pcolormesh(time_clean, depth, data,
                                  cmap=cmo_balance, vmin=-0.5, vmax=0.5, shading='auto')
                plt.colorbar(p, ax=ax, label=label, pad=0.01)
                ax.set_ylabel('Depth (m)')

            tidal_spd = np.sqrt(u_tidal**2 + v_tidal**2)
            p = axes[2].pcolormesh(time_clean, depth, tidal_spd,
                                   cmap=cmo_amp, vmin=0, vmax=0.2, shading='auto')
            plt.colorbar(p, ax=axes[2], label='Tidal signal speed (m/s)', pad=0.01)
            axes[2].set_ylabel('Depth (m)')
            axes[2].set_xlabel('Date')
            # Depth orientation — surface at 0, max from data
            axes[0].set_ylim(d_max, 0)
            fig.suptitle(f'Tidal filtering ({CUTOFF_HOURS}-h low-pass Butterworth)', fontsize=13)
            plt.tight_layout()
            out_png = _rf(run_dir, 'tidal_filter.png')
            plt.savefig(out_png, dpi=150)
            plt.close(fig)
            log(f'Saved: {out_png}')


def _adcp_diagnostics(base_dir, run_dir, opts, log):
    """Compute and plot EKE, depth-integrated transport, and Richardson number."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr
    import pandas as pd

    bias_path  = _rf(run_dir, 'currents_bias_corrected.nc')
    shear_path = _rf(run_dir, 'shear_processed.nc')
    for p in [bias_path, shear_path]:
        if not os.path.exists(p):
            raise RuntimeError(f'{os.path.basename(p)} not found — run Process Shear and Bias Correction first')

    try:
        import cmocean.cm as cmo
        cmo_amp = cmo.amp
    except ImportError:
        cmo_amp = 'hot_r'

    log('Loading bias-corrected currents...')
    with _nc_lock:
        with xr.open_dataset(bias_path) as currents:
            vel_E      = currents['velocity_E_DAC_reference_sb_corrected'].values
            vel_N      = currents['velocity_N_DAC_reference_sb_corrected'].values
            depth      = currents.depth.values
            time_clean = pd.Series(currents.time.values).interpolate(method='linear').values

            # Depth axis limits — surface anchored at 0, max from data
            d_max = float(np.nanmax(depth))

            # Layout: (depth=axis0, profile_index=axis1) — same as currents_bias_corrected.nc
            n_dep  = vel_E.shape[0]
            n_prof = vel_E.shape[1]
            log(f'  Shape: {n_dep} depth levels × {n_prof} profiles')

            # depth axis weights for integration (axis 0)
            dz = np.abs(np.gradient(depth))   # shape (n_dep,)

            # ── EKE ──────────────────────────────────────────────────────────────────────
            log('Computing EKE...')
            try:
                # Time-mean current at each depth (mean over profile axis=1)
                u_bar   = np.nanmean(vel_E, axis=1, keepdims=True)   # (n_dep, 1)
                v_bar   = np.nanmean(vel_N, axis=1, keepdims=True)
                EKE     = 0.5 * ((vel_E - u_bar)**2 + (vel_N - v_bar)**2)  # (n_dep, n_prof)
                # Depth-integrate: sum over axis 0, weighted by dz
                eke_int = np.nansum(EKE * dz[:, np.newaxis], axis=0)  # (n_prof,)

                fig, axes = plt.subplots(2, 1, figsize=(16, 8),
                                         gridspec_kw={'height_ratios': [3, 1]})
                # pcolormesh(x=time, y=depth, C) — C layout is (n_dep, n_prof) ✓
                p = axes[0].pcolormesh(time_clean, depth, EKE,
                                       cmap=cmo_amp, vmin=0, vmax=0.05, shading='auto')
                plt.colorbar(p, ax=axes[0], label='EKE (m²/s²)', pad=0.01)
                axes[0].set_ylabel('Depth (m)')
                axes[0].set_ylim(d_max, 0)
                axes[0].set_title('Eddy Kinetic Energy')
                axes[1].fill_between(time_clean, eke_int, color='firebrick', alpha=0.7)
                axes[1].set_ylabel('Depth-int. EKE (m³/s²)')
                axes[1].set_xlabel('Date')
                plt.tight_layout()
                p_eke = _rf(run_dir, 'eke.png')
                plt.savefig(p_eke, dpi=150)
                plt.close(fig)
                log(f'Saved: {p_eke}')
            except Exception as exc:
                log(f'EKE skipped ({exc})')

            # ── Transport ─────────────────────────────────────────────────────────────────
            log('Computing depth-integrated transport...')
            try:
                # Integrate over depth axis (axis 0), weighted by dz
                T_E   = np.nansum(vel_E * dz[:, np.newaxis], axis=0)  # (n_prof,)
                T_N   = np.nansum(vel_N * dz[:, np.newaxis], axis=0)
                T_mag = np.sqrt(T_E**2 + T_N**2)

                fig, axes = plt.subplots(2, 1, figsize=(14, 6), sharex=True)
                axes[0].plot(time_clean, T_E, '-o', ms=2, lw=1.2,
                             color='steelblue', label='E component')
                axes[0].plot(time_clean, T_N, '-o', ms=2, lw=1.2,
                             color='firebrick', label='N component')
                axes[0].axhline(0, color='k', lw=0.5, ls='--')
                axes[0].set_ylabel('Transport (m²/s)')
                axes[0].legend(fontsize=9)
                axes[0].set_title('Depth-integrated transport')
                axes[1].fill_between(time_clean, T_mag, color='darkorchid', alpha=0.7)
                axes[1].set_ylabel('Transport magnitude (m²/s)')
                axes[1].set_xlabel('Date')
                plt.tight_layout()
                p_tr = _rf(run_dir, 'transport.png')
                plt.savefig(p_tr, dpi=150)
                plt.close(fig)
                log(f'Saved: {p_tr}')
            except Exception as exc:
                log(f'Transport skipped ({exc})')


    # ── Richardson number ─────────────────────────────────────────────────────────
    log('Computing Richardson number (Ri = N² / shear²)...')
    try:
        import gsw
        with _nc_lock:
            with xr.open_dataset(shear_path) as ds_sh:

                need = ['glider_temperature', 'glider_salinity', 'Pressure',
                        'Depth', 'Latitude', 'Sh_E', 'Sh_N', 'bin_depth']
                missing = [v for v in need if v not in ds_sh and v not in ds_sh.coords]
                if missing:
                    log(f'  Missing variables {missing} in shear_processed.nc — skipping Ri')
                else:
                    T_raw    = ds_sh['glider_temperature'].values
                    S_raw    = ds_sh['glider_salinity'].values
                    p_raw    = ds_sh['Pressure'].values
                    lat_raw  = ds_sh['Latitude'].values
                    lon_raw  = ds_sh['Longitude'].values
                    prof_raw = np.round(ds_sh['profile_number'].values).astype(float)
                    Sh_E     = ds_sh['Sh_E'].values        # (time, gridded_bin)
                    Sh_N     = ds_sh['Sh_N'].values
                    bin_d    = ds_sh['bin_depth'].values   # (time, gridded_bin) — actual depths

                    dmin = 0.0
                    dmax = d_max  # from np.nanmax(depth) — surface anchored, full data range
                    dg   = np.arange(dmin, dmax + 1.0, 5.0)  # 5 m depth grid

                    unique_profs = np.unique(prof_raw[np.isfinite(prof_raw)])
                    log(f'  {len(unique_profs)} profiles, depth grid {dmin}–{dmax} m (5 m bins)')

                    ri_arr   = np.full((len(unique_profs), len(dg)), np.nan)
                    time_arr = np.empty(len(unique_profs), dtype='datetime64[ns]')
                    time_arr[:] = np.datetime64('NaT')

                    for pi, prof in enumerate(unique_profs):
                        t_mask = prof_raw == prof
                        if t_mask.sum() < 5:
                            continue

                        # Representative time for this profile
                        try:
                            time_arr[pi] = np.datetime64(
                                int(np.nanmean(ds_sh.time.values[t_mask].astype('int64'))), 'ns')
                        except Exception:
                            pass

                        # CTD profile: one (T, S, p) point per ADCP ping during this profile
                        ok_ctd = t_mask & np.isfinite(T_raw) & np.isfinite(S_raw) & np.isfinite(p_raw)
                        if ok_ctd.sum() < 10:
                            continue

                        T_p   = T_raw[ok_ctd]
                        S_p   = S_raw[ok_ctd]
                        p_p   = p_raw[ok_ctd]
                        lat_m = float(np.nanmean(lat_raw[ok_ctd]))
                        lon_m = float(np.nanmean(lon_raw[ok_ctd]))

                        # Sort strictly by pressure and remove duplicates
                        sidx  = np.argsort(p_p)
                        T_p, S_p, p_p = T_p[sidx], S_p[sidx], p_p[sidx]
                        _, uniq = np.unique(p_p, return_index=True)
                        T_p, S_p, p_p = T_p[uniq], S_p[uniq], p_p[uniq]
                        if len(p_p) < 4:
                            continue

                        # N² from gsw
                        try:
                            SA        = gsw.SA_from_SP(S_p, p_p, lon_m, lat_m)
                            CT        = gsw.CT_from_t(SA, T_p, p_p)
                            N2, p_mid = gsw.Nsquared(SA, CT, p_p, lat_m)
                            d_mid     = -gsw.z_from_p(p_mid, lat_m)   # positive-down
                            ok_n2     = np.isfinite(N2) & np.isfinite(d_mid) & (d_mid > 0)
                            if ok_n2.sum() < 3:
                                continue
                            N2_grid = np.interp(dg, d_mid[ok_n2], N2[ok_n2],
                                                left=np.nan, right=np.nan)
                        except Exception:
                            continue

                        # Shear² binned to depth grid (bin_depth gives true depths)
                        sh2  = (Sh_E[t_mask].ravel()**2 + Sh_N[t_mask].ravel()**2)
                        bd   = bin_d[t_mask].ravel()
                        ok_s = np.isfinite(sh2) & np.isfinite(bd) & (bd > 0)
                        if ok_s.sum() < 5:
                            continue
                        sh2_grid = np.array([
                            np.nanmean(sh2[ok_s & (bd >= d - 2.5) & (bd < d + 2.5)])
                            for d in dg
                        ])

                        with np.errstate(divide='ignore', invalid='ignore'):
                            ri = np.where(sh2_grid > 0, N2_grid / sh2_grid, np.nan)
                        ri[ri < 0]  = np.nan   # convective instability
                        ri[ri > 50] = 50.0     # cap display range
                        ri_arr[pi]  = ri

                    ok_p = np.any(np.isfinite(ri_arr), axis=1)
                    log(f'  Profiles with valid Ri: {ok_p.sum()}/{len(unique_profs)}')

                    if ok_p.sum() >= 2:
                        t_plot  = pd.Series(pd.to_datetime(time_arr[ok_p])).interpolate(method='linear').values
                        ri_plot = ri_arr[ok_p]

                        # Smooth Ri for display — raw per-profile values are too noisy to interpret
                        try:
                            from scipy.ndimage import uniform_filter, median_filter
                            ri_T = ri_plot.T   # (depth, time)
                            # Replace NaN with neutral stable value before filtering
                            ri_fill = np.where(np.isfinite(ri_T), ri_T, 1.0)
                            # Median filter to kill outlier spikes, then uniform for spatial coherence
                            # size=[depth_bins, time_bins]: 4 bins = 20 m, 15 profiles ≈ 2.5 h
                            ri_med    = median_filter(ri_fill, size=[4, 15])
                            ri_smooth = uniform_filter(ri_med,  size=[4, 15])
                            # Restore NaN mask where original had all-NaN
                            nan_mask  = ~np.isfinite(ri_T)
                            ri_disp   = np.where(nan_mask, np.nan, ri_smooth)
                        except Exception:
                            ri_disp = ri_plot.T

                        fig, ax = plt.subplots(figsize=(16, 6))
                        pcm = ax.pcolormesh(t_plot, dg, ri_disp,
                                            cmap='RdYlBu_r', vmin=0, vmax=1, shading='auto')
                        cb = plt.colorbar(pcm, ax=ax, label='Ri', pad=0.01)
                        cb.ax.axhline(0.25, color='white', linewidth=2, linestyle='--')

                        # Ri = 0.25 contour on smoothed data
                        try:
                            import matplotlib.patheffects as pe
                            cs = ax.contour(t_plot, dg, ri_smooth, levels=[0.25],
                                            colors='white', linewidths=2.5, linestyles='--')
                            for coll in cs.collections:
                                coll.set_path_effects([
                                    pe.Stroke(linewidth=5, foreground='black', alpha=0.7),
                                    pe.Normal(),
                                ])
                            ax.clabel(cs, fmt={0.25: 'Ri=0.25'}, fontsize=9,
                                      colors='white', inline=True)
                        except Exception:
                            pass
                        ax.set_ylabel('Depth (m)')
                        ax.set_xlabel('Date')
                        ax.invert_yaxis()
                        ax.set_ylim([dg.max(), dg.min()])
                        ax.set_title('Richardson number  —  white dashed: Ri = 0.25 shear instability threshold')
                        plt.tight_layout()
                        p_ri = _rf(run_dir, 'richardson_number.png')
                        plt.savefig(p_ri, dpi=150)
                        plt.close(fig)
                        log(f'Saved: {p_ri}')
                    else:
                        log('  Not enough valid profiles for Ri plot')

    except Exception as exc:
        log(f'Richardson number skipped ({exc})')


# ── AD2CP Routes ───────────────────────────────────────────────────────────────

@app.route('/api/adcp/import', methods=['POST'])
def adcp_import():
    """Copy .nc files into the ad2cp input directory for a dep/test."""
    body    = request.json or {}
    dep     = body.get('dep', '')
    is_test = bool(body.get('is_test', False))
    paths   = body.get('paths', [])

    if not dep:
        return jsonify({'error': 'dep required'}), 400

    dest_dir = _adcp_input_dir(dep, is_test)
    os.makedirs(dest_dir, exist_ok=True)

    results = []
    for src in paths:
        src  = os.path.normpath(src)
        name = os.path.basename(src)
        if not name.lower().endswith('.nc'):
            results.append({'path': src, 'ok': False, 'error': 'Only .nc files are accepted'})
            continue
        if not os.path.isfile(src):
            results.append({'path': src, 'ok': False, 'error': 'File not found'})
            continue
        dst = os.path.join(dest_dir, name)
        try:
            shutil.copy2(src, dst)
            results.append({'path': src, 'ok': True, 'name': name})
        except Exception as exc:
            results.append({'path': src, 'ok': False, 'error': str(exc)})

    return jsonify({'results': results})


@app.route('/api/adcp/files/<path:filename>', methods=['DELETE'])
def adcp_delete_file(filename):
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    fpath   = os.path.join(_adcp_input_dir(dep, is_test), os.path.basename(filename))
    if not os.path.isfile(fpath):
        return abort(404)
    try:
        os.remove(fpath)
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/adcp/files')
def adcp_files():
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    d       = _adcp_input_dir(dep, is_test)
    if not os.path.isdir(d):
        return jsonify({'files': []})
    files = sorted(f for f in os.listdir(d) if f.lower().endswith('.nc'))
    return jsonify({'files': files})


@app.route('/api/adcp/tests', methods=['GET'])
def adcp_list_tests():
    d = os.path.join(BASE_DIR, 'ad2cp', 'tests')
    if not os.path.isdir(d):
        return jsonify({'tests': []})
    tests = sorted(t for t in os.listdir(d) if os.path.isdir(os.path.join(d, t)))
    return jsonify({'tests': tests})


@app.route('/api/adcp/tests/<label>', methods=['DELETE'])
def adcp_delete_test(label):
    d = os.path.join(BASE_DIR, 'ad2cp', 'tests', label)
    if not _is_within(d, os.path.join(BASE_DIR, 'ad2cp', 'tests')):
        return abort(400)
    if not os.path.isdir(d):
        return abort(404)
    try:
        shutil.rmtree(d)
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/adcp/tests', methods=['POST'])
def adcp_create_test():
    label = (request.json or {}).get('label', '').strip()
    if not label:
        return jsonify({'error': 'label required'}), 400
    label = re.sub(r'[^A-Za-z0-9 _\-]', '_', label)
    os.makedirs(_adcp_input_dir(label, True), exist_ok=True)
    return jsonify({'label': label})


@app.route('/api/adcp/live-stages')
def adcp_live_stages():
    """Return which stages are currently running in memory for a given dep."""
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    mode    = 'test' if is_test else 'dep'
    running = []
    with _adcp_stg_lock:
        for (d, stage, m) in list(_adcp_live_logs.keys()):
            if d == dep and m == mode and not _adcp_live_done.get((d, stage, m), True):
                running.append(stage)
    return jsonify({'running': running})


@app.route('/api/adcp/status')
def adcp_status():
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    base    = _adcp_base_dir(dep, is_test)
    outputs = _ADCP_TEST_OUTPUTS if is_test else _ADCP_OUTPUTS

    def _file_info(path):
        exists = os.path.exists(path)
        return {'exists': exists, 'size': os.path.getsize(path) if exists else 0}

    # Shared stages (not option-dependent) — files live directly in base_dir
    shared_status = {}
    for stage in _ADCP_SHARED_STAGES:
        if stage not in outputs:
            continue
        files = outputs[stage]
        file_info = {f: _file_info(os.path.join(base, f)) for f in files}
        shared_status[stage] = {
            'done':  all(fi['exists'] for fi in file_info.values()),
            'files': file_info,
        }

    # Option stages — files live in per-run subdirs of base_dir
    opt_stages = [s for s in outputs if s in _ADCP_OPTION_STAGES]
    runs = {}
    if os.path.isdir(base):
        for entry in sorted(os.listdir(base)):
            sub = os.path.join(base, entry)
            if not os.path.isdir(sub):
                continue
            run_status = {}
            for stage in opt_stages:
                files = outputs[stage]
                lbl = os.path.basename(sub) + '_'
                file_info = {lbl + f: _file_info(os.path.join(sub, lbl + f)) for f in files}
                run_status[stage] = {
                    'done':  all(fi['exists'] for fi in file_info.values()),
                    'files': file_info,
                }
            runs[entry] = run_status

    return jsonify({'shared': shared_status, 'runs': runs, 'base_dir': base})


@app.route('/api/adcp/shear_stats')
def adcp_shear_stats():
    dep     = request.args.get('dep', '')
    run     = request.args.get('run', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    base    = _adcp_base_dir(dep, is_test)
    run_dir = os.path.join(base, run) if run else base
    path    = _rf(run_dir, 'shear_processed.nc') if run else ''

    # If the requested run's file doesn't exist, fall back to the most recent
    # prefixed shear_processed.nc from any run in this deployment so the
    # recommendation can appear before the current config has been processed.
    source_run = run
    if not os.path.exists(path) and os.path.isdir(base):
        candidates = []
        for entry in os.listdir(base):
            sub = os.path.join(base, entry)
            if not os.path.isdir(sub):
                continue
            p = os.path.join(sub, entry + '_shear_processed.nc')
            if os.path.exists(p):
                candidates.append((os.path.getmtime(p), entry, p))
        if candidates:
            candidates.sort(reverse=True)
            _, source_run, path = candidates[0]

    if not os.path.exists(path):
        return jsonify({'error': 'not found'}), 404

    try:
        import xarray as xr
        with _nc_lock:
            with xr.open_dataset(path) as ds:
                stats = {}
                for v in ['Sh_E', 'Sh_N']:
                    if v in ds:
                        valid = int(ds[v].notnull().sum())
                        total = int(ds[v].size)
                        stats[v] = {'valid': valid, 'total': total,
                                     'pct': round(100 * valid / total, 1) if total else 0.0}

                if not stats:
                    return jsonify({'error': 'no shear variables found'}), 422

                # Number of half-profiles (dive legs) in the shear dataset
                n_legs = int(ds.sizes.get('time', ds.sizes.get('profile', 0)))

                avg_pct = sum(s['pct'] for s in stats.values()) / len(stats)

                cols1 = n_legs
                cols2 = n_legs // 2
                cols3 = n_legs // 3

                action = (f'To apply: select "3 — Distance" from the X-axis dropdown in Options above, '
                          f'then re-run Process Shear. The shared stages (Load NC, GPS Pairing, Write Glider NC) '
                          f'do not need to re-run — only Process Shear, Compute Currents, and Bias Correction.')

                if avg_pct < 15:
                    rec_xaxis  = 3
                    rec_reason = (
                        f'Only {avg_pct:.1f}% of acoustic pings produced valid shear — typical of oligotrophic water '
                        f'with few biological scatterers. At this ping rate, individual dive legs carry sparse data '
                        f'and many depth bins within a single leg will be empty or noisy. '
                        f'Setting xaxis=3 groups 3 consecutive half-profiles into each output column '
                        f'({n_legs} dive legs → ~{cols3} output columns), pooling roughly 3× as many pings per depth bin. '
                        f'This dramatically improves depth coverage and reduces velocity noise, at the cost of coarser '
                        f'temporal and spatial resolution — each output column represents approximately 3 dive legs '
                        f'worth of glider track. Even at very low ping rates, xaxis=3 typically achieves >99% depth '
                        f'coverage from the surface to 500 m by aggregating enough valid returns across the averaged legs.'
                    )
                    rec_action = action.replace('"3 — Distance"', '"3 — Distance (every 3 legs, smoother)"')
                elif avg_pct < 35:
                    rec_xaxis  = 2
                    rec_reason = (
                        f'Valid ping rate of {avg_pct:.1f}% is moderate. Individual dive legs have enough data to be '
                        f'useful, but pairing each downcast with its following upcast (xaxis=2) reduces noise by '
                        f'averaging two legs together while preserving most of the temporal structure '
                        f'({n_legs} dive legs → ~{cols2} output columns). A complete dive pair covers both a descent '
                        f'and ascent, so the averaged shear profile samples the water column twice, helping to cancel '
                        f'out transient glider-induced errors. If temporal resolution is a priority, xaxis=1 is also '
                        f'viable ({cols1} output columns) but expect noisier profiles, particularly in the deepest bins '
                        f'where acoustic returns are weakest.'
                    )
                    rec_action = action.replace('"3 — Distance"', '"2 — Profile"')
                else:
                    rec_xaxis  = 1
                    rec_reason = (
                        f'Valid ping rate of {avg_pct:.1f}% is high — each individual dive leg produces well-populated '
                        f'shear profiles across most of the depth range. xaxis=1 keeps every half-profile as its own '
                        f'output column ({cols1} total), giving maximum temporal and spatial resolution. With this ping '
                        f'rate, per-leg profiles will be reliable and consistent. You may still see slightly noisier '
                        f'results in the deepest bins due to acoustic attenuation with range, but this is unlikely to '
                        f'require additional averaging. xaxis=2 or 3 remain valid choices if you prefer smoother output '
                        f'or are averaging over a larger spatial footprint for scientific reasons.'
                    )
                    rec_action = action.replace('"3 — Distance"', '"1 — Time"')

                return jsonify({'stats': stats, 'avg_pct': round(avg_pct, 1), 'n_legs': n_legs,
                                'rec_xaxis': rec_xaxis, 'rec_reason': rec_reason,
                                'rec_action': rec_action, 'source_run': source_run})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adcp/run/<stage>', methods=['POST'])
def adcp_run_stage(stage):
    body    = request.json or {}
    dep     = body.get('dep', '')
    is_test = bool(body.get('is_test', False))
    adcp_fn = body.get('adcp_file', '')
    opts    = body.get('options', {})

    if not dep:
        return jsonify({'error': 'dep required'}), 400

    base_dir  = _adcp_base_dir(dep, is_test)
    run_dir   = _adcp_run_dir(base_dir, opts)
    adcp_path = os.path.join(_adcp_input_dir(dep, is_test), adcp_fn) if adcp_fn else ''
    key       = (dep, stage, 'test' if is_test else 'dep')

    with _adcp_stg_lock:
        already_running = key in _adcp_live_logs and not _adcp_live_done.get(key)

    if already_running:
        # Stage is live on the server — reconnect as a new listener.
        # The tail generator replays the full buffered log then continues streaming.
        return Response(
            _adcp_sse_tail(key),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    valid_stages = list(_ADCP_TEST_OUTPUTS if is_test else _ADCP_OUTPUTS)
    if stage not in valid_stages:
        return jsonify({'error': f'Unknown stage: {stage}'}), 400

    run_result = {}

    def fn(log):
        os.makedirs(base_dir, exist_ok=True)
        if stage == 'load_nc':
            _adcp_load_nc(dep, base_dir, log)
        elif stage == 'gps_pairing':
            _adcp_gps_pairing(base_dir, log)
        elif stage == 'write_glider_nc':
            _adcp_write_glider_nc(base_dir, log)
        elif stage == 'process_shear':
            if is_test:
                _adcp_gen_dummy_glider_nc(base_dir, adcp_path, log)
            _adcp_process_shear(base_dir, run_dir, adcp_path, opts, log)
        elif stage == 'compute_currents':
            actual_yaxis = _adcp_compute_currents(base_dir, run_dir, opts, log)
            if actual_yaxis is not None:
                run_result['actual_yaxis'] = actual_yaxis
        elif stage == 'bias_correction':
            _adcp_bias_correction(run_dir, opts, log)
        elif stage == 'generate_plots':
            _adcp_generate_plots(base_dir, run_dir, is_test, opts, log)
        elif stage == 'lab_diagnostics':
            _adcp_lab_diagnostics(run_dir, log)
        elif stage == 'tidal_filter':
            _adcp_tidal_filter(run_dir, opts, log)
        elif stage == 'diagnostics':
            _adcp_diagnostics(base_dir, run_dir, opts, log)

    return Response(
        _adcp_sse_run(key, fn, dep=dep, adcp_stage=stage, result=run_result),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/adcp/download/<path:filename>')
def adcp_download(filename):
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    run     = request.args.get('run', '')
    base    = _adcp_base_dir(dep, is_test)

    def _ok(p):
        return _is_within(p, BASE_DIR) and os.path.isfile(p)

    if run:
        run_dir = os.path.join(base, run)
        fpath = os.path.join(run_dir, run + '_' + filename)
        if not _ok(fpath):
            fpath = os.path.join(run_dir, filename)
        if not _ok(fpath):
            fpath = os.path.join(base, filename)
    else:
        fpath = os.path.join(base, filename)
    if not _ok(fpath):
        return abort(404)
    return send_file(fpath, as_attachment=True, download_name=os.path.basename(fpath))


@app.route('/api/adcp/plot/<path:filename>')
def adcp_plot(filename):
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    run     = request.args.get('run', '')
    base    = _adcp_base_dir(dep, is_test)

    def _ok(p):
        return _is_within(p, BASE_DIR) and os.path.isfile(p)

    if run:
        run_dir = os.path.join(base, run)
        fpath = os.path.join(run_dir, run + '_' + filename)
        if not _ok(fpath):
            fpath = os.path.join(run_dir, filename)
        if not _ok(fpath):
            fpath = os.path.join(base, filename)
    else:
        fpath = os.path.join(base, filename)
    if not _ok(fpath):
        return abort(404)
    return send_file(fpath, mimetype='image/png')


@app.route('/api/adcp/export')
def adcp_export():
    dep     = request.args.get('dep', '')
    is_test = request.args.get('is_test', 'false').lower() == 'true'
    run     = request.args.get('run', '')
    base    = _adcp_base_dir(dep, is_test)
    if not os.path.isdir(base):
        return abort(404)

    buf = _adcp_io.BytesIO()
    with _adcp_zf.ZipFile(buf, 'w', _adcp_zf.ZIP_DEFLATED) as zf:
        if run:
            # Shared files from base_dir + option-specific files from the run subdir
            for fn in os.listdir(base):
                fp = os.path.join(base, fn)
                if os.path.isfile(fp):
                    zf.write(fp, fn)
            run_sub = os.path.join(base, run)
            if os.path.isdir(run_sub):
                for fn in os.listdir(run_sub):
                    fp = os.path.join(run_sub, fn)
                    if os.path.isfile(fp):
                        zf.write(fp, os.path.join(run, fn))
        else:
            # Export everything recursively
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    fp = os.path.join(root, fn)
                    zf.write(fp, os.path.relpath(fp, base))
    buf.seek(0)

    safe   = re.sub(r'[^A-Za-z0-9_\-]', '_', dep)
    suffix = f'_{re.sub(r"[^A-Za-z0-9_-]", "_", run)}' if run else ''
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=f'adcp_{safe}{suffix}.zip')


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f'[server] base_dir = {BASE_DIR}', flush=True)
    print(f'[server] listening on 127.0.0.1:{PORT}', flush=True)
    app.run(host='127.0.0.1', port=PORT, threaded=True, use_reloader=False)
