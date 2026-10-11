"""
Stage definitions for the UVI Glider Pipeline.

Each stage either:
  - has a 'command_fn' returning (cmd_list, cwd, env) for the PipelineWorker, OR
  - has 'manual': True with a 'manual_note' shown in the UI.

Docker image: glider-processing
Bind mount:   <base_dir> → /home/glider
"""
import os
import sys

DOCKER_IMAGE = 'glider-processing'

# Repo root — scripts like qc_report.py live here, not in the data base_dir
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _glider(deployment):
    return deployment.split('-')[0]          # 'uvi_01-20250318T2008' → 'uvi_01'


def _year(deployment):
    return deployment.split('-')[1][:4]      # '20250318T2008' → '2025'


def _docker_run(base_dir, inner_cmd):
    """Wrap inner_cmd (a bash -c string) in a docker run call.

    Mounts the repo dir as /home/glider so app code (slocum/, gncutils/,
    qc/, master_cac/) is present, then overlays data subdirectories from
    base_dir on top so deployment data appears at the expected paths.
    This supports having the app code and data in separate directories.
    When base_dir == _REPO_DIR (single-directory installs) the overlays
    are redundant no-ops, so backward compatibility is preserved.
    """
    data_dirs = [
        'incoming_data', 'dat_files', 'deployments',
        'gncutils_slocum_nc_output', 'logs',
    ]
    cmd = ['docker', 'run', '--rm', '-v', f'{_REPO_DIR}:/home/glider']
    for d in data_dirs:
        cmd += ['-v', f'{os.path.join(base_dir, d)}:/home/glider/{d}']
    cmd += [DOCKER_IMAGE, 'bash', '-c', inner_cmd]
    return cmd


# ── Stage 1a: DBD + EBD → merged_data ────────────────────────────────────────
def dbd_to_dat_command(base_dir, deployment):
    """
    Convert paired DBD + EBD Slocum binary files to merged ASCII DAT files.
    Output always goes to dat_files/<dep>/merged_data/.
    Run this stage when science (EBD) files are present.

    Any "dinkum compressed" files (.dcd/.ecd/.mcd/.ncd/.scd/.tcd/.ncg/.mcg -- the
    real-time/acoustic-modem transmission format) are decompressed in place first, via
    slocum/bin2ascii/decompress_dbds.sh, so processDbds.sh (which only recognizes
    dbd/ebd/mbd/nbd/sbd/tbd) actually has something to convert. Safe to re-run: the
    decompress step skips a target that already exists, and never touches the
    compressed originals.
    """
    src    = f'/home/glider/incoming_data/{deployment}'
    dest   = f'/home/glider/dat_files/{deployment}/merged_data'
    exes   = '/home/glider/slocum/linux-bin'
    cac    = '/home/glider/master_cac'
    script = '/home/glider/slocum/bin2ascii/processDbds.sh'
    decomp = '/home/glider/slocum/bin2ascii/decompress_dbds.sh'

    inner = ' && '.join([
        f'chmod +x {exes}/dbd2asc {exes}/dba_merge {exes}/dba_sensor_filter {exes}/dba2_orig_matlab {exes}/compexp || true',
        f'sed -i "s/\\r//" {script} {decomp}',
        f'find {src} -maxdepth 1 \\( -iname "*.dcd" -o -iname "*.ecd" -o -iname "*.mcd" -o '
        f'-iname "*.ncd" -o -iname "*.scd" -o -iname "*.tcd" -o -iname "*.ncg" -o -iname "*.mcg" \\) '
        f'-print0 | xargs -0r bash {decomp} -e {exes}',
        f'mkdir -p {dest}',
        f'cp -r {cac} /tmp/slocum_cac',
        f'bash {script} -c /tmp/slocum_cac -e {exes} {src} {dest}',
        f'cp /tmp/slocum_cac/*.cac {cac}/ 2>/dev/null || true',
    ])
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 1b: DBD only → flight_data ─────────────────────────────────────────
def dbd_to_dat_flight_command(base_dir, deployment):
    """
    Convert DBD-only Slocum binary files to flight ASCII DAT files.
    Output always goes to dat_files/<dep>/flight_data/.
    Only DBD files are passed to the script — EBD files are ignored so
    this stage can run alongside Stage 1a on the same deployment.

    Flight-side "dinkum compressed" files (.dcd/.mcd/.scd) are decompressed in place
    first, via slocum/bin2ascii/decompress_dbds.sh, so the .dbd copy step below (and
    processDbds.sh, which only recognizes dbd/mbd/sbd) actually has something to
    convert. Science-side compressed extensions (.ecd/.ncd/.tcd) are deliberately not
    touched here, matching this stage's own EBD-ignored scoping.
    """
    src    = f'/home/glider/incoming_data/{deployment}'
    dest   = f'/home/glider/dat_files/{deployment}/flight_data'
    exes   = '/home/glider/slocum/linux-bin'
    cac    = '/home/glider/master_cac'
    script = '/home/glider/slocum/bin2ascii/processDbds.sh'
    decomp = '/home/glider/slocum/bin2ascii/decompress_dbds.sh'
    tmp    = f'/tmp/dbd_flight_{deployment}'

    # Copy only .dbd files into a temp dir so the script never sees EBD files
    inner = ' && '.join([
        f'mkdir -p {tmp}',
        f'chmod +x {exes}/compexp || true',
        f'sed -i "s/\\r//" {script} {decomp}',
        f'find {src} -maxdepth 1 \\( -iname "*.dcd" -o -iname "*.mcd" -o -iname "*.scd" \\) '
        f'-print0 | xargs -0r bash {decomp} -e {exes}',
        f'find {src} -maxdepth 1 -iname "*.dbd" -exec cp {{}} {tmp}/ \\;',
        f'chmod +x {exes}/dbd2asc {exes}/dba_merge {exes}/dba_sensor_filter {exes}/dba2_orig_matlab || true',
        f'mkdir -p {dest}',
        f'cp -r {cac} /tmp/slocum_cac',
        f'bash {script} -c /tmp/slocum_cac -e {exes} {tmp} {dest}',
        f'cp /tmp/slocum_cac/*.cac {cac}/ 2>/dev/null || true',
        f'rm -rf {tmp}',
    ])
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 2a: DAT → Profile NetCDF ────────────────────────────────────────────
def dat_to_profile_nc_command(base_dir, deployment):
    """
    python /home/glider/gncutils/scripts/dba_to_profile_nc.py
      <config_dir>
      <dat_files>/*.dat
      --output_path <output_dir>
      --science --clobber --loglevel info
    """
    glider     = _glider(deployment)
    config_dir = (f'/home/glider/glider_config_files_json/{glider}/'
                  f'{deployment}/science_profile')
    dat_glob   = f'/home/glider/dat_files/{deployment}/merged_data/*.dat'
    out_dir    = f'/home/glider/gncutils_slocum_nc_output/{deployment}/profile'

    inner = (
        f'mkdir -p {out_dir} && '
        f'python /home/glider/gncutils/scripts/dba_to_profile_nc.py '
        f'{config_dir} '
        f'{dat_glob} '
        f'--output_path {out_dir} '
        f'--science --clobber --loglevel info'
    )
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 2b: DAT → Trajectory NC (Science) ───────────────────────────────────
def dat_to_trajectory_nc_command(base_dir, deployment):
    """
    Build a science trajectory NetCDF from the merged (DBD+EBD) DAT files.
    Reads from merged_data/ so the trajectory includes science-sensor columns
    as defined in the deployment's science_trajectory/sensor_defs.json.
    """
    glider     = _glider(deployment)
    config_dir = (f'/home/glider/glider_config_files_json/'
                  f'{glider}/{deployment}/science_trajectory')
    dat_glob   = f'/home/glider/dat_files/{deployment}/merged_data/*.dat'
    out_dir    = f'/home/glider/gncutils_slocum_nc_output/{deployment}/trajectory_science'

    inner = (
        f'mkdir -p {out_dir} && '
        f'python /home/glider/gncutils/scripts/dba_to_trajectory_nc.py '
        f'{config_dir} '
        f'{dat_glob} '
        f'--output_path {out_dir} '
        f'--clobber --loglevel info'
    )
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 2c: DAT → Trajectory NC (Flight / DBD-only) ────────────────────────
def dat_to_trajectory_flight_nc_command(base_dir, deployment):
    """
    Build a flight-only trajectory NetCDF from the flight_data/ DAT files.
    Used when only DBD (no EBD science) files were collected.
    Config lives in trajectory_flight/ and sensor_defs selects flight sensors.
    """
    glider     = _glider(deployment)
    config_dir = (f'/home/glider/glider_config_files_json/'
                  f'{glider}/{deployment}/trajectory_flight')
    dat_glob   = f'/home/glider/dat_files/{deployment}/flight_data/*.dat'
    out_dir    = f'/home/glider/gncutils_slocum_nc_output/{deployment}/trajectory_flight'

    inner = (
        f'mkdir -p {out_dir} && '
        f'python /home/glider/gncutils/scripts/dba_to_trajectory_nc.py '
        f'{config_dir} '
        f'{dat_glob} '
        f'--output_path {out_dir} '
        f'--clobber --loglevel info'
    )
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 2d: Fix CF Metadata ─────────────────────────────────────────────────
def fix_metadata_command(base_dir, deployment):
    """
    Run fix_trajectoryprofile_metadata.py natively (no Docker) on the profile
    NC files generated by Stage 2a (Profile NC).  Fixes cdm_data_type, featureType,
    cdm_trajectory_variables, cf_role attributes so QC doesn't reject them.

    Input/output:  gncutils_slocum_nc_output/<deployment>/profile/  (in-place)
    """
    nc_dir = os.path.join(base_dir, 'gncutils_slocum_nc_output', deployment, 'profile')
    script = os.path.join(base_dir, 'fix_trajectoryprofile_metadata.py')
    return (
        [sys.executable, script, nc_dir],
        base_dir,
        # PYTHONUTF8=1  → UTF-8 mode for all I/O + string decoding (fixes charmap errors
        #                  when xarray reads non-ASCII NetCDF string attributes on Windows)
        # PYTHONIOENCODING → ensures emoji in print() reach the log stream correctly
        {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
    )


# ── Stage 3: Pre QAQC Config File Check ──────────────────────────────────────
def preflight_command(base_dir, deployment):
    """
    Read instrument metadata from a profile NC file and determine what
    QC config files (time_shift.yml, qctests.yml) are needed.  Outputs a
    PREFLIGHT_JSON line for the Electron UI to display a confirmation panel.
    The user reviews and clicks Apply — the UI POSTs to /api/preflight/apply.
    """
    script = os.path.join(base_dir, 'app', 'pre_qaqc_config_check.py')
    return (
        [sys.executable, script, base_dir, deployment],
        base_dir,
        {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
    )


# ── Stage 4: QARTOD QC ────────────────────────────────────────────────────────
def qc_command(base_dir, deployment):
    """
    Run the full rugliderqc QARTOD pipeline inside Docker.

    The rugliderqc scripts resolve paths via GLIDER_DATA_HOME env var and the
    deployment name (positional arg), NOT via -p flags.  They expect NC files
    to be staged in:
      $GLIDER_DATA_HOME/deployments/<year>/<dep>/data/out/nc/sci-profile/delayed/qc_queue/

    QC config is read from:
      $GLIDER_DATA_HOME/qc/config/

    move_nc_files.py promotes QC-passed files from qc_queue/ → delayed/,
    which is the final output the Report stage reads.
    """
    year      = _year(deployment)
    nc_in     = f'/home/glider/gncutils_slocum_nc_output/{deployment}/profile'
    dep_root  = f'/home/glider/deployments/{year}/{deployment}'
    nc_out    = f'{dep_root}/data/out/nc/sci-profile/delayed'
    qc_queue  = f'{nc_out}/qc_queue'
    proc_logs = f'{dep_root}/proc-logs'

    # Helper: build a rugliderqc script invocation with GLIDER_DATA_HOME set.
    # All scripts share the same -m delayed -d profile positional-arg interface
    # except data_gap_check.py which has no -m/-d flags.
    def _qc(name, script, extra=''):
        return (
            f'echo ">>> {name}" && '
            f'GLIDER_DATA_HOME=/home/glider '
            f'python /home/glider/rugliderqc/scripts/{script}.py '
            f'{extra}{deployment}'
        )

    # The rugliderqc scripts write to per-run log files in proc-logs/, not stdout.
    # QC_RUN_TS is exported so every script in this run writes to the same file.
    # A polling loop streams new lines from the current run's log file to stdout.
    # The setup step also writes a startup banner to the proc-log for context when viewed later.
    log_glob = f'{proc_logs}/*{deployment}*qc.log'
    setup = (
        f'mkdir -p {qc_queue} {proc_logs} && '
        # QC_RUN_TS (machine-readable, used in the log filename) is exported so all QC scripts
        # in this run write to the same per-run log file.  RUN_TS (human-readable) is used in
        # the start banner echoed to the live UI stream.
        f'QC_RUN_TS="$(date \'+%Y%m%dT%H%M%S\')" && export QC_RUN_TS && '
        f'RUN_TS="$(date \'+%Y-%m-%d %H:%M:%S\')" && '
        # Write a startup banner to the new per-run proc-log.  This creates the file immediately
        # so the log-discovery loop below finds it without waiting for the first QC script.
        f'printf "\\n=== Stage 4 QC started at $RUN_TS ===\\n" '
        f'  >> "{proc_logs}/glider-${{QC_RUN_TS}}_{deployment}-sci-profile-delayed-qc.log" && '
        # Clean qc_queue before starting so stale artifacts (.duplicate, .nosci, etc.)
        # from any previous partial run don't interfere with the fresh pipeline pass.
        f'rm -f {qc_queue}/* 2>/dev/null; '
        f'cp {nc_in}/*.nc {qc_queue}/ && '
        # Echo the start banner to stdout so it appears in the live UI stream
        f'echo "=== Stage 4 QC started at $RUN_TS ===" && '
        f'echo "Staged $(ls {qc_queue}/*.nc | wc -l) files — starting QC pipeline"'
    )
    pipeline = ' && \\\n  '.join([
        _qc('check_science_variables',    'check_science_variables',    '-m delayed -d profile '),
        _qc('interpolate_depth',          'interpolate_depth',          '-m delayed -d profile '),
        _qc('check_duplicate_timestamps', 'check_duplicate_timestamps', '-m delayed -d profile '),
        _qc('glider_qartod_qc',           'glider_qartod_qc',           '-m delayed -d profile '),
        _qc('ctd_hysteresis_test',        'ctd_hysteresis_test',        '-m delayed -d profile '),
        _qc('time_shift',                 'time_shift',                 '-m delayed -d profile '),
        _qc('data_gap_check',             'data_gap_check',             ''),
        _qc('add_derived_variables',      'add_derived_variables',      '-m delayed -d profile '),
        _qc('derived_variable_regional_qc', 'derived_variable_regional_qc', '-m delayed -d profile '),
        _qc('summarize_qartod_flags',     'summarize_qartod_flags',     '-m delayed -d profile '),
        _qc('move_nc_files',              'move_nc_files',              '-m delayed -d profile '),
        f'echo "=== QC complete — $(ls {nc_out}/*.nc 2>/dev/null | wc -l) files in delayed/ ==="',
    ])
    # Stream the rugliderqc proc-log to stdout using a polling loop.
    # tail -f does NOT work reliably on Windows bind-mounted volumes in Docker
    # because inotify events don't propagate from the host filesystem — tail
    # outputs existing content then freezes.  Polling wc -l / tail -n +N avoids
    # inotify entirely and works correctly on any filesystem.
    inner = (
        f'{setup} && '
        # Run the full QC pipeline in the background so we can poll alongside it
        f'( {pipeline} ) & PIPE_PID=$! ; '
        # Wait up to 30 s for the first QC script to create its log file
        f'LOG_FILE="" ; '
        f'for I in $(seq 1 30); do '
        # Filter by QC_RUN_TS so we find this run's file, not a leftover from a previous run
        f'  LOG_FILE=$(ls {log_glob} 2>/dev/null | grep "$QC_RUN_TS" | head -1) ; '
        f'  [ -n "$LOG_FILE" ] && break ; '
        f'  sleep 1 ; '
        f'done ; '
        # Skip the startup banner already shown via echo on stdout — stream only QC script output
        f'[ -n "$LOG_FILE" ] '
        f'  && LAST_LINE=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0) '
        f'  || LAST_LINE=0 ; '
        # Poll every second while the pipeline is still running
        f'while kill -0 $PIPE_PID 2>/dev/null ; do '
        f'  if [ -n "$LOG_FILE" ]; then '
        f'    LINES=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0) ; '
        f'    if [ "$LINES" -gt "$LAST_LINE" ]; then '
        f'      tail -n +"$((LAST_LINE+1))" "$LOG_FILE" ; '
        f'      LAST_LINE=$LINES ; '
        f'    fi ; '
        f'  fi ; '
        f'  sleep 1 ; '
        f'done ; '
        # Final flush — print any lines written between the last poll and exit
        f'if [ -n "$LOG_FILE" ]; then '
        f'  LINES=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0) ; '
        f'  [ "$LINES" -gt "$LAST_LINE" ] && tail -n +"$((LAST_LINE+1))" "$LOG_FILE" ; '
        f'fi ; '
        f'wait $PIPE_PID ; RC=$? ; '
        f'exit $RC'
    )
    return _docker_run(base_dir, inner), base_dir, None


# ── Stage 5: Report + Plots ───────────────────────────────────────────────────
def report_command(base_dir, deployment, min_depth=10, nprofiles=20):
    """Generate HTML QC report then all plots using native Python.

    Args:
        min_depth:  Profiles shallower than this (metres) are excluded from profile
                    plots — removes surface checkout dives.  Default: 10 m.
        nprofiles:  Number of evenly-spaced profiles to sample for per-profile plots.
                    Default: 20.
    """
    report_script = os.path.join(_REPO_DIR, 'qc_report.py')
    plot_script   = os.path.join(_REPO_DIR, 'plot_qc_data.py')
    # Chain both scripts in one subprocess call so the server streams both outputs.
    # -u on each inner call forces unbuffered stdout so progress lines appear immediately.
    # Exit 1 if either script fails.
    runner = (
        'import subprocess, sys; '
        f'r1 = subprocess.run([sys.executable, "-u", {repr(report_script)}, {repr(deployment)}, "--data_home", {repr(base_dir)}]); '
        f'r2 = subprocess.run([sys.executable, "-u", {repr(plot_script)}, {repr(deployment)}, '
        f'"--data_home", {repr(base_dir)}, "--nprofiles", str({nprofiles}), "--min_depth", str({min_depth})]); '
        'sys.exit(0 if r1.returncode == 0 and r2.returncode == 0 else 1)'
    )
    return [sys.executable, '-u', '-c', runner], _REPO_DIR, {'PYTHONUNBUFFERED': '1', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}


# ── Stage metadata (for UI) ───────────────────────────────────────────────────
STAGE_INFO = {
    'dbd_to_dat': {
        'label':       'Stage 1a — DBD + EBD → DAT (Science)',
        'description': (
            'Convert paired DBD + EBD Slocum binary files to merged ASCII DAT files '
            'using the vendored slocum dbd2asc and dba_merge tools inside Docker.  '
            'Output goes to dat_files/<dep>/merged_data/.  '
            'Run this stage when science (EBD) files are present.'
        ),
        'manual':      False,
        'command_fn':  dbd_to_dat_command,
        'source_fn':   lambda b, d: os.path.join(b, 'incoming_data', d),
        'output_fn':   lambda b, d: os.path.join(b, 'dat_files', d, 'merged_data'),
    },
    'dbd_to_dat_flight': {
        'label':       'Stage 1b — DBD → DAT (Flight)',
        'description': (
            'Convert DBD-only Slocum binary files to flight ASCII DAT files '
            'using the vendored slocum dbd2asc and dba_merge tools inside Docker.  '
            'Output goes to dat_files/<dep>/flight_data/.  '
            'Run this stage when only DBD (no science EBD) files are present.'
        ),
        'manual':      False,
        'command_fn':  dbd_to_dat_flight_command,
        'source_fn':   lambda b, d: os.path.join(b, 'incoming_data', d),
        'output_fn':   lambda b, d: os.path.join(b, 'dat_files', d, 'flight_data'),
    },
    'dat_to_profile_nc': {
        'label':       'Stage 2a — DAT → Profile NetCDF',
        'description': ('Run dba_to_profile_nc.py to produce science profile NetCDF files '
                        'from merged DAT data.'),
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'dat_files', d, 'merged_data'),
        'output_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'profile'),
        'command_fn':  dat_to_profile_nc_command,
    },
    'dat_to_trajectory_nc': {
        'label':       'Stage 2b — DAT → Trajectory NC (Science)',
        'description': (
            'Run dba_to_trajectory_nc.py on merged_data/ (DBD+EBD) to produce a '
            'science trajectory NetCDF.  '
            'sensor_defs in science_trajectory/ controls which variables are written.'
        ),
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'dat_files', d, 'merged_data'),
        'output_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'trajectory_science'),
        'command_fn':  dat_to_trajectory_nc_command,
    },
    'dat_to_trajectory_flight_nc': {
        'label':       'Stage 2c — DAT → Trajectory NC (Flight)',
        'description': (
            'Run dba_to_trajectory_nc.py on flight_data/ (DBD-only) to produce a '
            'flight trajectory NetCDF.  '
            'Use this stage when no science (EBD) files were collected.  '
            'sensor_defs in trajectory_flight/ controls which variables are written.'
        ),
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'dat_files', d, 'flight_data'),
        'output_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'trajectory_flight'),
        'command_fn':  dat_to_trajectory_flight_nc_command,
    },
    'fix_metadata': {
        'label':       'Stage 2d — Fix CF Metadata',
        'description': (
            'Run fix_trajectoryprofile_metadata.py on the profile NetCDF files.\n'
            'Sets cdm_data_type, featureType, cdm_trajectory_variables, '
            'trajectory cf_role, and profile_id cf_role so files conform to '
            'CF/ACDD conventions before QC.'
        ),
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'profile'),
        'output_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'profile'),
        'command_fn':  fix_metadata_command,
    },
    'preflight': {
        'label':       'Stage 3 — Pre QAQC Config File Check',
        'description': (
            'Reads instrument metadata from a profile NC file to detect sensors '
            '(CTD type, oxygen optode, pH) and determine which QC config files '
            'are needed for this deployment.  Shows a preview of files to be '
            'created or updated and asks for confirmation before writing anything.'
        ),
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'profile'),
        'output_fn':   lambda b, d: os.path.join(
            b, 'deployments', d.split('-')[1][:4], d, 'config', 'qc'),
        'command_fn':  preflight_command,
    },
    'qc': {
        'label':       'Stage 4 — QARTOD QC',
        'description': 'Run the full rugliderqc QARTOD quality-control pipeline in Docker.',
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(b, 'gncutils_slocum_nc_output', d, 'profile'),
        'output_fn':   lambda b, d: os.path.join(
            b, 'deployments', d.split('-')[1][:4], d,
            'data', 'out', 'nc', 'sci-profile', 'delayed'),
        'command_fn':  qc_command,
    },
    'report': {
        'label':       'Stage 5 — Report + Plots',
        'description': 'Generate the HTML QC report and all data visualisation plots.',
        'manual':      False,
        'source_fn':   lambda b, d: os.path.join(
            b, 'deployments', d.split('-')[1][:4], d,
            'data', 'out', 'nc', 'sci-profile', 'delayed'),
        'output_fn':   lambda b, d: os.path.join(
            b, 'deployments', d.split('-')[1][:4], d, 'reports'),
        'command_fn':  report_command,
    },
}
