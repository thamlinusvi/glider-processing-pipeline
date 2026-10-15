# Glider Processing Pipeline — Project Guide

Electron desktop app wrapping a Python processing pipeline for Slocum glider data
(gncutils/rugliderqc-based) plus a dedicated AD2CP (Nortek ADCP) processing tab.

> Repo name is `glider-processing-pipeline` (hyphens) on GitHub; the local folder is
> `glider_processing_pipeline` (underscores) — don't assume they match when scripting.

> **Known bugs live in `KNOWN_ISSUES.md`, not here.** This file is architecture/reference — how
> the pipeline is built and how to work in it correctly. `KNOWN_ISSUES.md` is the punch list of
> things that are actually wrong, including two with real scientific-data-quality consequences.
> Check both before touching related code.

## Maintaining this file

This file is held to a high bar — treat it as a living document, not a one-time snapshot:
- **Verify every addition against the actual current code** (file:line) before writing it in.
  Never copy from memory, assumption, or a stale doc without checking it against the real
  source first.
- **Match the existing depth.** A one-line mention of a feature name is not enough — document
  the actual formula, sign convention, race condition, or data-shape trap, the way the rest of
  this file does. If something looks routine on the surface, read the code before assuming it is.
- **New bugs go in `KNOWN_ISSUES.md`, not here** — keep this file's content stable architecture
  reference. Flag prominently (like the ⚠️ callouts in that file) and tell the user directly, in
  the same message, not just in a commit. Do not fix without being asked to. When an issue there
  gets fixed, remove it from that file rather than letting it accumulate stale entries.
- **Prefer digging one level deeper over listing more surface-level facts.** Quality over
  quantity.
- **This file can go stale.** Code changes; line numbers drift. Spot-check anything load-bearing
  before trusting it at face value, and correct or remove claims that no longer hold.

## Stack (verify, don't assume)

- **Shell:** Electron main process at `electron_app/main.js`. On launch it spawns
  `electron_app/python/server.py` as a child process (`python server.py <BASE_DIR> <PORT>`,
  **port 5757**) and polls `/api/ping` until it's up before showing the window.
- **Backend:** `electron_app/python/server.py` (~6000 lines) — a **Flask app**
  (`app.run(..., threaded=True, ...)`), NOT just a generic HTTP API layer. It wraps
  `app/pipeline/stages.py` + `app/pipeline/state.py` **unmodified** (its own docstring says
  so) — those two files, not server.py, are the actual pipeline backbone. Host Python needs
  numpy/xarray/netCDF4-or-h5py/matplotlib/pandas/contextily/pyproj/PyYAML — there's no
  `requirements.txt` anywhere; `electron_app/package.json`'s `install-python` script only does
  `pip install flask`, so most deps must already be present in the host's `python`/conda env
  (failures surface lazily, the first time a feature that needs them is used, not at startup).
- **Frontend:** `electron_app/renderer/app.js` (~6000 lines, vanilla JS, no framework) +
  `index.html` + `styles.css` (~3600 lines).
- **Launch:** `start_app.bat` → `cd electron_app && npm start` (Electron app only, the only
  frontend in this repo — a second, separate PyQt5 desktop UI existed at `app/main.py`/`app/ui/`/
  `app/workers/` but was removed 2026-08-13, confirmed unused, ~2 months stale relative to
  Electron, and never launched by `start_app.bat`; see git history / commit message for that
  removal if you need the full reasoning).
- **Docker required for most of the pipeline:** the `glider-processing` image (name only,
  `app/pipeline/stages.py` ~line 14) is not built automatically anywhere in this repo — build
  it manually (`docker build -t glider-processing .` from repo root) before running stages.

## Layout

- `electron_app/python/server.py` — all HTTP endpoints; AD2CP-specific logic lives in
  functions prefixed `_adcp_*` (grep for these — file is large and line numbers drift).
- `electron_app/renderer/app.js` — all frontend logic (map, stage cards, SSE stage
  progress, cloud upload UI, AD2CP export panel).
- `app/pipeline/stages.py` — the 9 pipeline stages: per-stage input/output dirs and the
  actual Docker/native command to run (`STAGE_INFO` dict, ~338-447). **Edit here**, not
  server.py, when changing what a stage does.
- `app/pipeline/state.py` — stage status persistence (`pipeline_state.json` per deployment;
  `not_run`/`running`/`done`/`failed`) and the fixed 9-stage list (~line 9).
- `app/pre_qaqc_config_check.py` — drives the Stage 3 "preflight" sensor-detection step.
- `qc_report.py`, `plot_qc_data.py`, `fix_trajectoryprofile_metadata.py`,
  `build_sensor_defs.py` (repo root) — called by specific stages (see Main pipeline below)
  *and* independently CLI-runnable. `check_nc_attrs.py` is the exception: **not called by
  anything**, a standalone debug script with a hardcoded deployment path baked in — don't
  expect it to work against the current deployment without editing the source first.
- `CHANGES_FROM_RUTGERS.md` (repo root, ~636 lines) — **the single most important
  undocumented-elsewhere file in this repo.** Explains every deviation from upstream
  `rugliderqc`/`gncutils`, including the pressure-unit convention below and several pipeline-
  breaking bugs that were already found and fixed once. Read this before touching QC scripts.
  Fully cross-checked against current code — see `KNOWN_ISSUES.md` for the one claim that
  doesn't actually hold.
- `deployments/`, `dat_files/`, `incoming_data/`, `gncutils_slocum_nc_output/`,
  `post_processing/`, `logs/` — per-deployment working data, named `<glider>-<timestamp>`
  (e.g. `uvi_01-20260114T1932`).
- `ad2cp/` — AD2CP raw/processed data; `ad2cp/tests/` holds AD2CP lab-test-mode data.
- `master_cac/` — Slocum `.cac`/`.ccc` calibration cache files.
- `config/cloud/` — real (not `.example`) `credentials.json` (Google client_id/secret, Dropbox
  app_key) and `tokens.json` (OAuth tokens) for cloud upload — gitignored, never commit or
  repeat their values.
- `qc/config/` — QARTOD QC thresholds, subfolders `deployment_specific/`, `derived_variables/`,
  `global_regional/`, `gross_flatline/`. **`deployment_specific/*.yml` are templates, not live
  config** (headed `# Template — copy to deployments/<year>/<glider-timestamp>/config/qc/...`)
  — there's no automation in `stages.py` that performs this copy, it's a manual per-deployment
  step. Default `qctests.yml` template has `add_derived_variables: False` — pH/TA/oxygen derived
  variables are opt-in per deployment, not automatic. Region matching
  (`glider_qartod_qc.py`'s `build_global_regional_config()`) is filename-convention-based
  (`<region>_configs.yml` must match `regional_boundaries.yml`'s `region:` key) — a mismatch
  fails silently, falling through to no regional config, not an error.
- `gncutils/`, `rugliderqc/` — vendored/adapted third-party processing packages, invoked as
  Docker subprocess scripts, never `import`ed (see below). Each has its own thin upstream
  README — the real project-specific documentation is `CHANGES_FROM_RUTGERS.md`, not these.
- `slocum/` — vendored Slocum binary tools (`dbd2asc`, `dba_merge`, `processDbds.sh`) used by
  Stages 1a/1b. Was once accidentally committed as a git submodule/gitlink and had to be
  re-added as regular files (commit `5e12ffd`) — don't reintroduce that mistake if re-vendoring.

## Main pipeline (non-AD2CP) — the primary workflow

A normal deployment runs 9 fixed stages (`app/pipeline/state.py` ~line 9), science and
flight tracks partly in parallel:

1. **`dbd_to_dat`** — DBD+EBD → `dat_files/<dep>/merged_data/` (Docker).
2. **`dbd_to_dat_flight`** — DBD-only → `dat_files/<dep>/flight_data/` (Docker); copies only
   `.dbd` files to a temp dir first so the merge script never sees EBD files.
3. **`dat_to_profile_nc`** — profile NetCDF, used for QC (Docker).
4. **`dat_to_trajectory_nc`** — science trajectory NC, used for map/flight-stats (Docker).
5. **`dat_to_trajectory_flight_nc`** — flight-only trajectory NC when no EBD/science data
   exists (Docker).
6. **`fix_metadata`** — runs `fix_trajectoryprofile_metadata.py` **natively** (no Docker) on
   the profile NC, fixing `cdm_data_type`/`featureType`/`cf_role` attrs so QC doesn't reject
   the files.
7. **`preflight`** — runs `app/pre_qaqc_config_check.py` **natively**; detects
   CTD/oxygen/pH sensors from NC metadata and streams a `=== PREFLIGHT_JSON ===` marker line
   consumed by `app.js`'s `doRun` (~1043-1082) to show a confirm/apply panel, applied via
   `POST /api/deployment/<dep>/preflight/apply`.
8. **`qc`** — the full rugliderqc QARTOD pipeline, **in Docker** (see below for exact order).
9. **`report`** — chains `qc_report.py` then `plot_qc_data.py` **natively** in one subprocess,
   `min_depth`/`nprofiles` passed from the UI.

**Docker vs native matters for debugging:** stages 1-5 and 8 run in Docker
(`_docker_run`, `app/pipeline/stages.py` ~25-32, image `glider-processing`, bind-mounts
`<base_dir>:/home/glider`); stages 6, 7, 9 run natively on the host via `sys.executable`.
A Stage 6/7/9 failure is a host-Python-environment problem, not a Docker problem, and vice versa.

**Status tracking is unified here but NOT for AD2CP:** the main pipeline persists status to
`<deployment>/pipeline_state.json` via `app/pipeline/state.py`. AD2CP has no equivalent file —
`/api/adcp/status` derives "done" purely from `os.path.exists()` on expected output files, so
there's no AD2CP "failed" state distinct from "not run".

### gncutils / rugliderqc — how they're actually invoked

Neither is ever `import`ed by server.py — both run as **subprocess scripts inside the Docker
container**, but with different edit-to-effect latency:
- **gncutils is bind-mounted live from the host** (`Dockerfile` ~40-42,
  `PYTHONPATH=/home/glider/gncutils:/opt/rugliderqc`) — editing `gncutils/*.py` takes effect
  on the next container run, **no rebuild needed**. Called as
  `python /home/glider/gncutils/scripts/dba_to_profile_nc.py` / `dba_to_trajectory_nc.py`.
  The actual salinity/density computation is `derive_ctd_parameters` in
  `gncutils/gncutils/readers/slocum.py:456-590` (imported by `dba_to_profile_nc.py`) — a
  same-named function in `gncutils/gncutils/ctd.py:123-150` is dead code, nothing imports it;
  grep the `readers/slocum.py` version.
- **rugliderqc is baked into the image at build time** (`COPY rugliderqc /opt/rugliderqc` +
  `pip install /opt/rugliderqc`) — editing `rugliderqc/*.py` requires a **Docker rebuild** or
  the change is silently ignored. Each QC script is invoked individually with
  `GLIDER_DATA_HOME=/home/glider`, chained with `&&` in one docker run.

**Stage 8's actual script order deviates from rugliderqc's own `run_glider_qc.py` wrapper**
(which is never invoked): `check_science_variables` → `interpolate_depth` →
`check_duplicate_timestamps` → `glider_qartod_qc` → `ctd_hysteresis_test` → `time_shift` →
`data_gap_check` (not in rugliderqc's own README) → `add_derived_variables` →
`summarize_qartod_flags` → `move_nc_files` (`app/pipeline/stages.py` ~257-266). All share
`-m delayed -d profile` flags except `data_gap_check`.

**Unit convention — the important one:** UVI gliders use RBR Legato³ CTDs outputting
`sci_water_pressure` in **bar**, not Seabird's `pressure` in **dbar** that upstream rugliderqc
hardcodes. `glider_qartod_qc.py`, `ctd_hysteresis_test.py`, `time_shift.py` were patched to
detect whichever of `('pressure', 'sci_water_pressure')` is present in `ds.data_vars` instead
of assuming `pressure`. QC config YAMLs (`qc/config/global_regional/*.yml`,
`qc/config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml`) have separate
`sci_water_pressure` threshold blocks scaled **÷10** from the `pressure` ones. This same bar↔dbar
distinction is the root cause of two active bugs — see `KNOWN_ISSUES.md`: the pressure
gross-range test threshold was never rescaled, and the hysteresis/time_shift minimum-span
pre-checks use unconverted literals.

**The pressure bar→dbar conversion depends on call order, not a guard inside the formula
itself.** `derive_ctd_parameters`'s primary branch uses `llat_pressure`, already ×10'd by
`select_pressure_sensor` (`readers/slocum.py:395,413`) and appended to `dba['data']` by
`create_llat_dba_reader` *before* `derive_ctd_parameters` runs. Calling `derive_ctd_parameters`
on a `dba` that skipped that setup step, or reordering the append, silently reintroduces a 10x
pressure error into GSW's salinity/density calls — a landmine to know about if this code path
is ever refactored.

**Known historical pipeline-breaking bugs already fixed** (full detail in
`CHANGES_FROM_RUTGERS.md`, cross-checked against live code) — relevant if rugliderqc/gncutils
are ever re-vendored/upgraded:
- `check_duplicate_timestamps.py` raised `FileNotFoundError` on a rename-mid-loop race,
  breaking the whole `&&`-chained Stage 8 — fixed with an `isfile` guard before reopening.
- `check_science_variables.py` recreated `.nosci`-renamed files via an unconditional
  `to_netcdf()`, causing every downstream script to reprocess ghost files — fixed with a
  `continue` after the rename branch.
- `ctd_hysteresis_test.py` / `time_shift.py` returned `status=1` on expected
  DEBUG/WARNING conditions (no-science profiles, missing optional config), breaking the
  pipeline chain — fixed by removing `status=1` from those specific branches only.
- `gncutils`'s `BaseTrajectoryNetCDFWriter.py` JSON config reads needed `encoding='utf-8-sig'`
  because Windows-edited JSON files carry a BOM that `json.load()` chokes on — the original
  code caught the error and silently continued with empty/default config. server.py's JSON
  *reads* already follow this; JSON *writes* do not (see `KNOWN_ISSUES.md`).
- `dt.UTC` (Python ≥3.11-only) → `dt.datetime.now(dt.timezone.utc)` for Python 3.9 compat,
  across 5 scripts — confirmed zero remaining `dt.UTC` usages anywhere in `rugliderqc/scripts/`.

## AD2CP pipeline

- Stage outputs are declared in two dicts in `server.py`: `_ADCP_OUTPUTS` (normal mode)
  and `_ADCP_TEST_OUTPUTS` (lab test mode, `is_test=True` — different, smaller plot set:
  `beam_diagnostics.png`, `sensor_timeseries.png`). Both keyed by stage name, e.g.
  `'generate_plots'`.
- `_adcp_base_dir(dep, is_test)` / `_adcp_input_dir(dep, is_test)` branch the working
  directory when `is_test` is set — this is how lab-test runs stay isolated from real
  deployment data.
- Plot depth axes: most depth-section plots use `d_min = 0.0` (surface anchor) and
  `d_max = float(np.nanmax(depth))` from data — this applies inside `_adcp_generate_plots`,
  `_adcp_tidal_filter`, and `_adcp_diagnostics` (EKE + Richardson number). `depth_min`/
  `depth_max` opts control the shear-bias regression slice **only**, not plot display limits.
- Beam quality: `correlation.png`, `beam_amplitude.png`, `snr.png` (absolute depth axis).
  SNR: `noise_floor = np.nanmean(amp_data[:, -3:])` (mean of last 3 range bins, already
  4-beam averaged), then `20*log10(amplitude/noise_floor)`. There is no preserved hardware
  noise floor in `shear_processed.nc` — last-bins estimate is the standard approach here.
  `beam_quality.nc` (depth-gridded correlation/amplitude/snr) is saved alongside the plots.
- Plot cache-busting: `buildAdcpExportPanel` (`app.js`) appends `&_t=${Date.now()}` to plot
  image URLs so the browser re-fetches PNGs after each stage re-run. That same function wires
  the `beam_quality.nc` (and other stage-output) download buttons via its `runDataMap` /
  `sharedDataMap` objects — download buttons only appear once `statusMap[stageId]?.done`.
- **Plot inventory** (name → source `.nc` → axis convention):
  - `velocity_sections.png`, `speed_section.png` — E/N velocity & speed, from
    `currents_bias_corrected.nc`, 0→full depth.
  - `shear_sections.png` — vertical shear, from `shear_processed.nc`, range-from-glider axis.
  - `backscatter.png` — acoustic backscatter/amplitude, from `shear_processed.nc`, range-from-transducer axis.
  - `correlation.png`, `beam_amplitude.png`, `snr.png` — beam quality, from `shear_processed.nc`, absolute depth axis.
  - `bias_diagnostic.png` — from the `bias_correction` stage.
  - `tidal_filter.png` — sub-tidal E/N + tidal speed, from `currents_bias_corrected.nc`, 0→full depth.
  - `eke.png`, `transport.png`, `richardson_number.png` — diagnostics, from `currents_bias_corrected.nc`/`shear_processed.nc`, 0→full depth.
  - `dac_timeseries.png`, `dac_map_cartopy.png` — depth-averaged current plots (`generate_plots` stage).
  - Lab-test mode (`is_test`) only produces a reduced set: `beam_diagnostics.png`,
    `sensor_timeseries.png` from `generate_plots`, plus `process_shear` → `shear_processed.nc`.
  - Full authoritative lists live in `reportStagePlots` and `stagePlots` in `app.js`, and
    `_ADCP_OUTPUTS`/`_ADCP_TEST_OUTPUTS` in `server.py` — grep these, this list drifts.
- Navigating away from the AD2CP section mid-stage and back is laggy (known perf issue,
  deferred — see `KNOWN_ISSUES.md`).

## AD2CP hardware notes (empirical, not derivable from code)

- This deployment's 1 MHz AD2CP has 15 × 1 m range bins (max ~15.2 m range). Real signal is
  only present in bin 1 (~1.2 m); bins 2–15 are flat noise floor (~31.4 counts). SNR is ~4 dB
  in bin 1 and ~0 dB everywhere else — **this is expected sensor behavior, not a bug** in the
  SNR/beam-quality plots or calculation.

## Cloud upload panel

Not AD2CP-specific — uploads span **7 categories**: `dat_flight`, `dat_merged`, `profile`,
`traj`, `qc`, `qc_nc`, `adcp` (`/api/cloud/files/<dep>`, server.py ~3614-3720). Two services,
both PKCE OAuth2: **Google Drive** and **Dropbox** (server.py ~3482-3610). OAuth redirect is a
Flask route on the same embedded port-5757 server (`/oauth/{google,dropbox}/callback`); the
auth URL opens in the system browser via `shell.openExternal` through Electron's `open-url`
IPC handler (`main.js` ~line 82), not an in-app webview. `/api/cloud/upload`
(server.py ~3902-3974) path-containment-checks every file against `BASE_DIR` before touching
it, supports per-item `subdir` for nested destination folders, and an `overwrite`/`skip`
conflict mode.

Grouped run-folder view (originally added for AD2CP, applies to any category with subdirs):
- `buildCategorySection` (`app.js`) groups files by first path segment when
  `hasSubdirs` is true — each run folder becomes a collapsible row with its own select-all
  checkbox; `folderExpandState` (a `Map`) persists expand/collapse state across search re-renders.
- Grouping splits on `/[\\/]/` (both separators) because Windows `os.path.relpath` returns
  backslashes — don't switch this to a plain `indexOf('/')`.
- Checkbox click handling: the `e.target === cb` branch skips `e.preventDefault()` so the
  native checkbox toggle isn't cancelled; row-text clicks still use `preventDefault` +
  manual toggle. Keep these two paths separate if touching this handler.

Sensor-defs sync is a 3-step, manual process with an inconsistent naming convention:
`build_sensor_defs()` (server.py ~1266-1283) runs `build_sensor_defs.py` natively to produce
one master set; `copy_sensor_defs` (~1285-1303) is a *separate* required step that fans it into
per-deployment folders, mapping `'profile'→'science_profile'`, `'trajectory'→'science_trajectory'`,
but `'trajectory_flight'→'trajectory_flight'` (no `science_` prefix — don't assume the pattern
is uniform when extending this).

**HDF5/netCDF access is deliberately serialized:** all netCDF4/HDF5 file opens go through a
dedicated `_nc_lock` with `_TRAJ_WORKERS` hardcoded to `1` (server.py ~82-89) — comment says
HDF5 can segfault with >1 concurrent reader. `_procs`/`_live_logs`/`_live_done` are protected
by a separate `_lock` (~50-58). Other caches (`_traj_report_cache`, `_nc_files_cache`,
`_cloud_files_cache`, etc.) are **not** lock-protected — fine for simple dict ops under the
GIL today, but don't add read-modify-write logic to them without adding locking.

**NC reads fall back from netCDF4 to xarray automatically** (commit `9aa0bcb`,
`_nc_read_surf`/`_nc_read_traj`) if `netCDF4` isn't importable — intentional resilience, but
there's no user-visible indication of which (slower) path is active.

## Git

- Repo: https://github.com/thamlinusvi/glider-processing-pipeline.git · branch: **master**.

## Working style (Travis' standing preferences)

- **Impact-check before applying** any non-trivial fix/refactor: state per-item whether it
  changes existing behavior before applying it, and wait for the OK.
- **Verify before changing:** grep every consumer of a shared file/field before editing it.
- **Verify correctness before "done":** trace the logic through before declaring a fix
  done — pipeline runs take hours, so a broken change is expensive to discover late.

## Next work item

- AD2CP lab testing feature: test mode (`is_test`, `ad2cp/tests/`) exists; likely needs
  dedicated UI/workflow improvements. Start from `_adcp_base_dir`, `_ADCP_TEST_OUTPUTS`,
  `adcp_create_test`, and the test tab in the frontend.
- In-app glider config file generator: UI flow that generates the `glider_config_files_json/`
  files (`global_attributes.json`, `instruments.json`, `sensor_defs.json`, etc.) for a new
  deployment by (a) pulling metadata from an ERDDAP endpoint the user points to and (b) using
  recent deployment configs as templates. Currently done manually / via Claude outside the app.
