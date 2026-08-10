# Glider Processing Pipeline — Known Issues

Bugs and defects found while researching this codebase, not yet fixed (per standing instruction
to flag rather than fix without being asked). `CLAUDE.md` covers architecture and correct
current behavior; this file tracks things that are actually wrong — including several with real
scientific-data-quality consequences. When something here gets fixed, **remove its entry**
rather than letting this file accumulate stale claims.

## Standalone PyQt5 UI: preflight apply mode is entirely unreachable

⚠️ **The standalone PyQt5 UI can never reach preflight's apply mode at all.**
`app/pipeline/stages.py`'s `preflight_command()` (183-195) invokes `pre_qaqc_config_check.py`
with only `[base_dir, deployment]` — never `--apply`/`--apply-file` (confirmed via grep: those
flags aren't referenced anywhere in `app/ui` or `app/workers`). The apply branch that actually
writes config files (`pre_qaqc_config_check.py` 427-482) is therefore unreachable from this UI —
Stage 3 here can only ever run in scan/report mode. `StageCard._on_finished`
(`pipeline_page.py:262`) still unconditionally sets `STATUS_DONE`/`STATUS_FAILED` on any
subprocess exit, the same premature-DONE pattern already fixed on the Electron side (see
below) — Electron's fix was not ported here, since porting it means first building an apply
flow that doesn't exist yet, a larger undertaking than the display-layer compatibility fix
below. Separately: when regional-boundary coverage fails, the analysis embeds a base64 map PNG
in the single `=== PREFLIGHT_JSON ===` log line, which `StageCard._append_log()` inserts as one
giant unbroken `insertHtml()` span — unreadable, and a real risk of degrading `QTextEdit`
responsiveness on a long line.
**Fix:** add `--apply`/`--apply-file` wiring to `preflight_command()`, build an Apply/Skip UI
comparable to the Electron confirm panel, and give `StageCard._on_finished` the same
`NEEDS_REVIEW`-aware status logic `run_stage()` now has (see below) instead of collapsing
straight to `DONE`.

**Fixed on the Electron side (2026-08-10):** `server.py`'s `run_stage()` used to mark Stage 3
`STATUS_DONE` the instant the *analysis* subprocess exited, with no special-case for preflight's
two-phase design (analysis, then a separate `/preflight/apply` call to actually write config) —
confirmed via real deployment data that this had already happened at least once
(`uvi_02-20260429T1420`'s last preflight run proposed creating `ctd_hysteresis.yml` and an
updated regional boundary, neither of which was ever applied, yet the stage showed `done`).
Fixed by adding a `NEEDS_REVIEW` status (`app/pipeline/state.py`) that `run_stage()` now sets
instead of `DONE` whenever the analysis reports anything pending, verified against real log
data and a live end-to-end run. As a required side effect of adding this status value to the
shared `pipeline_state.json` format, `app/ui/pipeline_page.py`'s `STATUS_LABEL`/
`STATUS_BADGE_STYLE` dicts (previously unguarded lookups) were also hardened against it so the
PyQt5 UI doesn't crash (`KeyError`) when displaying a deployment whose preflight status was set
by the Electron side — this is a display-layer compatibility fix only; PyQt5's own
`_on_finished` status-setting logic is unchanged, per the unresolved item above.

## Active bugs affecting real QC output

⚠️ **The pH → Total Alkalinity → Aragonite Saturation State derived-variable chain is dead
for USVI, silently.** Two independent gaps compound: (a) `usvi_configs.yml`'s tightened
regional `pH` gross-range bounds (`qc/config/global_regional/usvi_configs.yml:94-101`) never
fire, because `glider_qartod_qc` runs *before* `add_derived_variables` creates the `pH`
variable (Stage 8 order per `CLAUDE.md`), and `glider_qartod_qc.py:311` skips any config stream
not yet present in `ds.data_vars` — so `pH` is never there when this check runs, and the only
QC ever applied to it is the much looser `generic_pH_gross_flatline.yml` (fail_span `[0,14]`,
effectively the whole valid pH scale). (b) `qc/config/derived_variables/TA-salinity-regressions.yml`
defines exactly one region, `nyb` (lon `[-75,-71]`, lat `[37.5,41.5]` — Mid-Atlantic Bight),
nowhere near USVI (lon ≈ -65, lat ≈ 18). `calculate_ta()` (`add_derived_variables.py:176-219`)
falls through with empty results on no region match, with **no log line explaining why** — so
`total_alkalinity` never gets added, and `calculate_omega` (which needs it) can never run
either. **Fix:** add a USVI region block to `TA-salinity-regressions.yml`, and add explicit
logging to `calculate_ta()`'s no-region-matched fallthrough.

**Partially fixed (2026-08-10):** `add_derived_variables.py`'s `calculate_ph()`/`calculate_omega()`
both hardcoded `dataset.pressure`/`apply_qc(dataset, 'pressure')`, unguarded and never patched
for RBR's `sci_water_pressure` (see "Other rugliderqc QC-script issues" below) — `calculate_ph()`
would crash Stage 8 outright with an uncaught `AttributeError` on any RBR/UVI pH deployment, and
`calculate_omega()` would silently always return `[]` for one, via its own `KeyError` catch,
since it never finds a variable literally named `pressure`. Both now detect
`('pressure', 'sci_water_pressure')` and convert bar→dbar when needed, verified against real
calibration-shaped test data (numerically identical output to the dbar case, ~1e-11 max
difference). This removes one of the chain's contributing causes — (a) and (b) above are still
open.

**Note:** `CHANGES_FROM_RUTGERS.md` claims `global_configs.yml`'s time window was extended from
`2021-12-31` to `2030-12-31`, but the live file (`qc/config/global_regional/global_configs.yml:6`,
**and** the Docker-baked copy) still ends `2021-12-31` — that claim didn't actually land.
**Investigated and ruled out as a bug**: `build_global_regional_config()`
(`rugliderqc/scripts/glider_qartod_qc.py:28-80`) assigns the global config as the base
unconditionally (line 45, before any date check), then rewrites the chosen config's window
**year** to match each profile's own year (`.replace(year=profile_time.year)`, lines 77-78)
before it's ever used. Since the global window spans Jan 1–Dec 31, only month/day survive that
replace — the literal year in the YAML is inert and profiles never lose global QC coverage
over it. Don't re-flag this without re-checking that function first. All other claimed fixes in
the full 636-line `CHANGES_FROM_RUTGERS.md` were cross-checked against current code (both live
and Docker-baked copies where duplicated) and confirmed correct. Safe to trust the rest of that
file, but re-verify before relying on any single claim in it for something consequential, the
way the items above were checked.

## Other rugliderqc QC-script issues

- **`ctd_hysteresis_test.py`/`time_shift.py` minimum-profile-span pre-checks use unconverted
  bar-unit literals as if they were dbar.** `pressure_diff < 5` (hysteresis, ~lines 271-272) and
  `calculate_pressure_range(...) < 3` (time_shift, ~lines 370,417) are meant to gate on ~5m/~3m
  of profile depth change (dbar≈1m), but now run against bar-unit `sci_water_pressure`, silently
  raising the effective gate to ~51m/~30m before either test runs at all. Verified this mostly
  doesn't matter for deep missions (808/816 profiles in `uvi_02-20260429T1420` still clear the
  inflated threshold) — it would matter most for shallow/nearshore missions. The downstream
  FAIL/SUSPECT decision itself (`area = valid_polygons.area / pressure_range`) is scale-invariant
  and NOT affected — only the pre-check gate is.
- **`data_gap_check.py` is hardcoded to Rutgers' own ERDDAP and is silently dead for every UVI
  deployment.** `elink = f'http://slocum-data.marine.rutgers.edu/erddap/tabledap/{deployment}
  -profile-sci-rt'` (~line 19) will never resolve for a `uvi_*` deployment name — the script's
  broad `except Exception` just prints `"{deployment} not found on ERDDAP"` and exits 0 (no
  warning surfaced in the pipeline log). `app/pipeline/stages.py` (~line 263) wires it into
  Stage 8 as if it's a functioning step. Pure monitoring, doesn't set flags, so low severity —
  but Stage 8 currently gets zero value from this step for UVI.
- **`check_duplicate_timestamps.py`'s `ds`/`ds2` file handles aren't closed on the success
  path** — only the error branches (`OSError`/`IndexError`) call `.close()`. Given the
  documented HDF5-segfault-risk-with->1-reader concern elsewhere in this repo, accumulating
  unclosed handles across a long QC run is a minor pre-existing risk.
- **The derived-variable dependency cascade (DO→mg/L, pH voltage→pH, pH→TA, TA→Omega) is
  implicit and unenforced.** `add_derived_variables.py`'s single loop
  (`for variable_name, vardict in proc_vars.items():`, line 329) only works because
  `qc/config/sciencevar_processing.yml`'s keys happen to be in dependency order, and each step's
  precursor lookup is wrapped in a bare `except KeyError: continue` (lines 331-334) with **no
  log entry at all**. Reordering YAML entries or inserting a new derived variable above this
  block would silently break the whole pH/TA/Omega chain with no error to indicate why.
- **Docker-baked QC config copies under `rugliderqc/qc/config/` have materially diverged from
  the live `qc/config/` copies and are dead weight, but a documentation trap.**
  `rugliderqc/qc/config/global_regional/usvi_configs.yml` and `regional_boundaries.yml` differ
  substantially from the live versions (different climatology/gross-range spans, and a
  completely different USVI polygon — baked lat `17.5-18.6`/lon `-66..-64.5` vs live lat
  `11.9-18.7`/lon `-69.5..-64.5`). Confirmed currently inert: every rugliderqc invocation sets
  `GLIDER_DATA_HOME=/home/glider` (the bind-mounted live repo root), so `qc_config_root` always
  resolves to live `qc/config/`, never the baked path. The trap: `CLAUDE.md` documents that
  `rugliderqc/*.py` scripts genuinely do need a Docker rebuild to pick up edits, which invites
  the false assumption that `rugliderqc/qc/config/*.yml` works the same way — it doesn't, it's
  inert leftover from the original `COPY rugliderqc /opt/rugliderqc` build layer.

## gncutils issues

- **`gncutils/gncutils/validate.py:22` has a typo that makes validation a silent no-op:** it
  sets a local `validate = False` instead of `validated = False`, so the returned `validated`
  flag is never actually set to `False` — `validate_sensors()` always returns `True` regardless
  of missing sensors. Currently dormant (only caller, `dba_to_ngdac_profile_nc.py`, isn't
  invoked by `app/pipeline/stages.py`), but don't copy this pattern or re-wire that script
  without fixing it first. **Additionally confirmed:** `dba_to_ngdac_profile_nc.py` can't even
  be imported today — it references `gncutils.ProfileNetCDFWriter` (actual location:
  `gncutils.writers.slocum.ProfileNetCDFWriter`) and `gncutils.readers.dba` (no such module;
  only `readers/slocum.py` and `readers/navoceano.py` exist) — both raise `ModuleNotFoundError`
  at import time. Not newly broken, just confirms it's fully non-functional, not merely
  disconnected, if anyone considers reviving it.
- Stray `.bak` files in the repo (`gncutils/gncutils/ctd.py.bak`, `__init__.py.bak`,
  `qc/config/global_regional/usvi_configs.yml.bak`) — the `__init__.py.bak` diff shows a real
  scipy-compat fix (`scipy.signal.boxcar` → `scipy.signal.windows.boxcar`) that isn't applied in
  the live `__init__.py` — relevant if host scipy is ever upgraded and the boxcar import breaks.
  **Additionally confirmed:** `gncutils/gncutils/ctd.py:123`'s dead-code `derive_ctd_parameters`
  (unreferenced everywhere — every real caller imports the `readers/slocum.py` version instead)
  is not just unused but would crash if it ever were invoked: it does
  `sensors = dba["sensors"]; sensors.get(...)`, but `dba['sensors']` is always a **list** of
  dicts throughout this codebase, not a dict — lists have no `.get()`.
- **`pt0_from_t` (potential temperature) is called with Practical Salinity where GSW's function
  signature requires Absolute Salinity.** `gncutils/gncutils/readers/slocum.py:491` (primary CTD
  branch) and `:554` (alt-CTD branch): `p_temp = pt0_from_t(salt, t, p)`, where `salt` is
  `calculate_practical_salinity()`'s output (Practical Salinity, PSS-78), but GSW's
  `pt0_from_t(SA, t, p)` requires Absolute Salinity — confirmed against the installed `gsw`
  package's own docstring. `calculate_density`/`calculate_sound_speed` in the same file
  correctly call `SA_from_SP()` first; this one line skips that conversion. Real, reproducible
  defect (fires on every profile with full CTD coverage, output is written to profile NetCDF
  via the configured `potential_temperature` sensor def), but quantified numerically as
  scientifically inert: ~7×10⁻⁶°C error at 25°C/35 PSU/50 dbar, ~7×10⁻⁵°C at 500 dbar — roughly
  two orders of magnitude below the RBR Legato³'s stated ~0.002°C accuracy. Worth fixing for
  correctness; not a real-world data-quality concern at any depth this fleet operates at.
  **Fix:** `p_temp = pt0_from_t(gsw.SA_from_SP(salt, p, lon, lat), t, p)`.
- **The documented pressure bar→dbar "call-order landmine"** (see `CLAUDE.md`) was independently
  re-verified this session and confirmed accurate — `select_pressure_sensor` does convert to
  dbar (`readers/slocum.py:395,413`) before `create_llat_dba_reader` appends it, and every
  actual caller of `derive_ctd_parameters` (`dba_to_profile_nc.py` and its debug variants)
  follows the correct order with nothing in between that could disturb it. **Confirmed NOT
  currently triggered anywhere in the live pipeline** — this remains a landmine for a future
  refactor, not a live bug today. Also noted: the trajectory-writer path
  (`TrajectoryNetCDFWriter.dbas_to_trajectory_nc`, used by Stages 2b/2c) never calls
  `derive_ctd_parameters` at all — trajectory NC files carry raw pressure/CTD sensors only, no
  computed salinity/density/potential_temperature/sound_speed. Design fact, not a bug.

## Server-side robustness gaps

- **`_dep_dir(deployment)` (server.py ~252-256) creates folders as a side effect of GET
  requests.** It derives year via `deployment.split('-')[1][:4]`, falling back to a
  **hardcoded `'2025'`** if the name has no `-`, then unconditionally `os.makedirs(...,
  exist_ok=True)`. A mistyped/unknown deployment name in any request — even a plain status
  GET — silently creates a stray folder under `deployments/2025/<name>/`, and that fallback
  year is now wrong (current year is 2026). The standalone PyQt5 UI's `dashboard.py` (~167-185)
  independently reimplements this exact same bug; several call sites in `pipeline_page.py`
  (5 places) and `report_viewer.py` (2 places) do the same year-split with **no fallback guard
  at all**, raising an uncaught `IndexError` on a hyphen-less deployment name. **Two more call
  sites of the identical pattern confirmed in `app/pipeline/stages.py`:** `_year()` (~21-22,
  used inside `qc_command`) and the `output_fn` lambdas for `preflight`/`qc`/`report`
  (~423,432,441, `d.split('-')[1][:4]`) — for a hyphen-less name these raise synchronously
  inside the Flask request thread (`server.py:446`), surfacing as a raw 500 before the SSE
  generator's own try/except even starts, instead of a graceful failed-stage state.
- **Two file-ingestion endpoints have opposite overwrite behavior:**
  `/api/deployment/<dep>/import-raw` (~1346-1383) refuses to overwrite an existing file;
  `/api/cac-files/upload` (~1597-1610) unconditionally overwrites with no existence check.
- **`/api/deployment/<dep>/rename`** (`rename_deployment`, ~1487-1537) renames a deployment
  across **8 separate top-level path joins** (`incoming_data`, `deployments/<year>`,
  `dat_files`, `gncutils_slocum_nc_output`, `post_processing`, `logs`, `ad2cp`,
  `glider_config_files_json/<glider>`) one `os.rename()` at a time, with **no
  transaction/rollback**. A mid-sequence failure leaves the deployment split across old/new
  names in different folders; the response only reports `renamed`/`skipped`/`errors` lists —
  nothing auto-reconciles.
- **Disk cache staleness is mtime-based, not content-based** (`_load_disk_cache`/
  `_save_disk_cache`, ~209-234) — a file copied with timestamp preservation would leave the
  cache silently stale.
- **KMZ export has no disk-cache fallback, unlike its sibling report endpoints.**
  `download_kmz` (~1882-1901) only checks in-memory `_surf_cache`/`_traj_report_cache` and
  404s ("open the Map tab first") if cold. `generate_map_image_route` and
  `flight_stats_report_html` both retry via `_load_disk_cache(...)` when memory is empty. Net
  effect: after a server restart, KMZ export fails even when a valid disk cache exists that the
  other two endpoints would happily use.
- **`total_ah_used` (trajectory-report, ~1843-1845) is a naive `max - min` of
  `m_coulomb_amphr`** across all concatenated trajectory files — doesn't detect a coulomb-counter
  reset (battery swap, glider reboot mid-deployment), which would silently produce an implausible
  amp-hour figure.
- **`/api/deployment/<dep>/metadata` swallows per-file JSON errors into `None`** (~1054-1066,
  bare `except Exception: files[rel] = None`) — a corrupted config JSON is indistinguishable
  from a legitimately empty one in the response, with no error surfaced to the frontend.
- **JSON writes don't follow the BOM-safe convention that JSON reads do.** `save_metadata`,
  `save_sensor_sets` (server.py ~1092/1260) write with plain `open(path, 'w')`, no explicit
  `encoding='utf-8'` — on Windows this falls back to the locale codepage. Non-ASCII characters
  typed into the metadata editor risk silent mis-encoding or a crash. **Same pattern confirmed
  in `build_sensor_defs.py`** (`write_json` ~181-183, and the final `sensor_sets.json` dump
  ~278-279) — currently dormant there since the live sensor-defs source JSONs contain no
  non-ASCII characters, unlike `qc_report.py`'s unit labels (`'S m⁻¹'`, `'µmol L⁻¹'`) elsewhere
  in this codebase.
- **`_current_sensor_sets()` (~1212-1238) silently defaults `trajectory_flight` to the full**
  `trajectory` sensor set if not explicitly configured in `sensor_sets.json` — flight-only
  trajectory NC files get the entire science sensor set unless someone deliberately narrows it.
- **`main.js` spawns Python via a hardcoded `'python'`**, not `python3` or a venv path
  (~line 18); also sets `webSecurity: false` on the BrowserWindow to allow `file://` iframes
  and localhost fetches.
- **No server-side stage-prerequisite gating.** `STAGE_INFO` (`stages.py:338-447`) defines
  `source_fn`/`output_fn` per stage, but nothing consumes them to gate execution — `run_stage()`
  only checks the stage exists, isn't `manual`, and isn't already running. A direct API call (or
  out-of-order UI click — no gating logic found in `app.js` either) can run e.g. Stage 8 or 9
  against an empty/stale input directory; whether that's caught depends entirely on the invoked
  script's own robustness, which is uneven (see `data_gap_check.py` above).

## Frontend (`electron_app/renderer/app.js`) issues

- **Cloud OAuth connect/switch polling and the Docker-wait banner aren't torn down on
  navigation.** `pollUntilConnected` (~4247-4258, called from Switch Account ~4306-4325 and
  Connect ~4335-4363) starts a `setInterval` (2s × up to 45 tries) that only stops via the
  card's own Cancel button or the try-cap — navigating away mid-poll leaves it running against a
  detached DOM node for up to 90s, and clicking Connect twice stacks duplicate intervals with no
  de-dup. Similarly, `_checkDockerBanner`'s `EventSource('/api/docker/wait')` (~697-755, opened
  on every Pipeline-page visit) is only closed on its own `ready`/`timeout`/`onerror` — not on
  navigation, so a still-open one from a prior visit keeps streaming until its own timeout.
  Both self-resolve; neither corrupts state; both are real, verifiable leaks worth a
  navigate-time cleanup hook.
- **Deployment rename deletes the wrong `state.depStates` cache entry.** `app.js:6008-6012`
  reassigns `state.deployment` to the *new* name, then does
  `delete state.depStates[state.deployment]` — deleting the (empty) new-name entry instead of
  the actually-stale old-name one, which is left orphaned in the cache indefinitely. Harmless
  today (nothing looks up the old name again), but wrong logic and a minor unbounded-growth
  contributor across a long rename-heavy session.
- **Several `innerHTML` sinks are fed by server-derived strings instead of the codebase's
  normal safe-DOM `h()` helper**, inconsistent with the convention used almost everywhere else:
  `getStagePaths` output embedding `dep` raw (~776, low risk — deployment names are
  regex-locked at creation), `apiFetch` error messages (~1735, 1853, 1984, 2237 — server's raw
  `error` JSON field, unescaped), AD2CP recommendation text (~5436-5443), and a Leaflet popup
  built from parsed trajectory-NC GPS timestamps (~1937). None are demonstrated exploitable
  given current server-side validation, but worth normalizing if this app's threat model ever
  changes (e.g. multi-user or synced/shared deployment folders).

## Root utility script issues

- **`qc_report.py` has no exception handling around NC file opens — one bad file kills the
  entire report.** `collect_all()` (`qc_report.py:264-359`) has zero try/except around
  `xr.open_dataset` (line 288) or per-file processing; a single corrupted/unreadable NC file, or
  one with an empty `profile_time` array, throws uncaught and kills the script before any HTML
  is written. Its sibling `plot_qc_data.py`, chained right after it in the same Stage 9 command,
  *is* defensive about this (`load_deployment()` wraps the open in try/except and skips bad
  files) — the two scripts process the same directory with inconsistent robustness. A single bad
  file in a large deployment means Stage 9 produces no HTML QC report at all.
- **`qc_report.py`'s depth-distribution table silently truncates at 500m, undercounting deep
  flagged observations.** `html_climatology` (~522-543): `depth_bins = [0, 50, 100, 200, 300,
  400, 500]`. `pd.cut` drops out-of-range values as NaN rather than bucketing them — and this
  fleet documents ~700m dives (`uvi_02-20260429T1420`). The report's own explanatory text (4.4)
  says climatology flags occur mostly in "deep-water measurements," but the 4.3 per-depth-bin
  table has no bin past 500m, so most of the very observations being explained vanish from that
  table with no truncation notice. (Section 4.1's totals-by-variable table is computed
  independently and is unaffected.)
- **`plot_qc_data.py` has a depth-axis fallback that substitutes raw bar-unit pressure for
  meter-unit depth with no conversion.** `plot_profiles` (~172-175): if `depth` is missing,
  falls back to `ds['sci_water_pressure'].values` directly, still labeled `'Depth (m)'` for
  every subplot — the same bar-vs-dbar confusion class as the (now-fixed) pressure gross-range
  test, would compress the apparent depth axis ~10x. Every other depth fallback in this file (and in `qc_report.py`) uses
  a safe NaN fill instead, making this one function look like a copy/paste slip. Traced as
  currently dormant — `depth` (`llat_depth`) is part of the standard `PROFILE_SENSORS` set and
  appears to always be emitted — but a live landmine if that ever changes.
- **`fix_trajectoryprofile_metadata.py`'s auto-created `profile_id` fallback mis-dimensions the
  variable.** Lines 61-70: when no `cf_role=="profile_id"` variable exists anywhere *and* no
  `profile_id` variable exists at all, it falls back to
  `dim_size = ds.dims.get("profile", ds.dims.get("time", 1))`. Confirmed via grep that
  gncutils's writers never create a dimension literally named `"profile"` (profile files use
  `"time"` as their sole per-observation dimension) — so this fallback would create a **new**
  `"profile"` dimension sized to the time-series length and assign one sequential `profile_id`
  **per timestep** instead of one per profile, the opposite of correct CF-TrajectoryProfile
  semantics, directly undermining the script's own purpose. Traced as likely dormant today since
  `profile_id` is part of the standard sensor-defs set and normally already present.
- **`build_sensor_defs.py`'s profile-defs builder has no fallback to `sci_defs`, unlike its two
  siblings.** `build_profile_sensor_defs` (~186-190) looks up keys only in `sci_defs`, while
  `build_trajectory_sensor_defs`/`build_trajectory_flight_sensor_defs` (~193-217) correctly fall
  back to `sci_defs` when a key is missing from `base_defs`. Verified against the live JSON:
  every current `PROFILE_SENSORS` entry exists in `sci-sensor_defs.json`, so this produces zero
  missing keys today — but any future addition to `PROFILE_SENSORS` that only exists in
  `trajectory-sensor_defs.json` would be silently dropped from the master profile
  `sensor_defs.json`, reported only as a `missing` print in the `/api/build-sensor-defs`
  response body (easy to miss in the UI). Note: verified `copy_sensor_defs` (server.py
  ~1285-1303, the downstream fan-out step) reads exactly the folder/filenames
  `build_sensor_defs.py` writes — no mismatch there despite the documented inconsistent
  `'trajectory_flight'` naming, which is entirely on `copy_sensor_defs`'s destination side.

## Standalone PyQt5 UI (`app/ui/`) issues

- **`metadata_editor.py` only ever edits one of three parallel sensor-defs copies**
  (`_load_tabs`, ~202-219, hardcoded to `science_profile/`). Each deployment actually has
  `science_profile/`, `science_trajectory/`, and `trajectory_flight/` copies feeding three
  different NetCDF-writer stages — editing through this page silently leaves the other two stale.
- **Shared singleton `StageCard` widgets can misattribute a running stage's status/log to
  whichever deployment happens to be selected when its worker finishes — a cross-deployment
  data-mixup risk.** `PipelinePage._build()` (`pipeline_page.py` ~308-316) creates one
  `StageCard` per stage key, **shared across every deployment**, not recreated per deployment.
  `MainWindow._on_deployment_changed()` (`main_window.py` 176-185) calls
  `self._pipeline.set_deployment(dep)` on every deployment-combo change unconditionally —
  including on any card whose worker is still running. `StageCard._on_finished` (259-267)
  recomputes `dep_dir` from `self.deployment` **at signal-fire time**, not launch time. Concrete
  scenario: start Stage 4 (QC, can run minutes) on deployment A, switch to deployment B in the
  sidebar (its card resets to B's on-disk state, hiding that A's worker is still live), click Run
  on B's now-idle-looking card — two concurrent workers now share one `self._worker` reference
  (second overwrites the first, so Abort can no longer reach A's run), log output interleaves,
  and whichever `finished` signal fires last writes its DONE/FAILED status into whichever
  deployment happens to be selected *at that moment* — not necessarily either A or B.
- **`PipelineWorker` replaces rather than merges the subprocess environment for all native
  stages.** `pipeline_worker.py:33`: `subprocess.Popen(..., env=self.env, ...)` — Python
  semantics mean a non-`None` `env` replaces the parent environment entirely. `stages.py`
  returns partial env dicts for the three natively-run stages (`fix_metadata_command`,
  `preflight_command`: `PYTHONUTF8`/`PYTHONIOENCODING` only; `report_command`: three keys) —
  none include `PATH`, `SYSTEMROOT`, `USERPROFILE`, `PYTHONPATH`, etc. Compare
  `electron_app/python/server.py:478`, which explicitly does `env={**os.environ, **(env or {})}`
  for the identical command-tuple contract — strong evidence the merge is the intended pattern
  and this worker never picked it up. On Windows, a stripped environment is a classic source of
  `ImportError: DLL load failed` and broken path/temp-dir resolution for these three stages.
- **Reports → Plots tab is 100% non-functional against the current pipeline.**
  `plot_qc_data.py` now exclusively produces interactive Plotly `.html` output (confirmed via
  grep — `fig.write_html`, no `savefig`/PNG anywhere, its own docstring says
  `figs/ (*.html — open in browser or iframe)`). `report_viewer.py`'s `_load_plots()`
  (146-182) globs exclusively for `*.png` and renders via `QPixmap` — since zero PNGs are ever
  produced, this tab always shows "No PNG files found," even for a fully successful, fully
  reported deployment. (The separate QC Report HTML tab correctly targets `qc_report.py`'s
  actual `.html` output and still works.)
- **No QThread teardown on window close.** No `closeEvent`/`aboutToQuit`/thread-wait logic
  exists anywhere in `main_window.py`, `main.py`, or `pipeline_page.py`. Closing the app while
  any `StageCard`'s `PipelineWorker` is mid-run destroys the still-running `QThread` without an
  orderly `abort()`/`wait()` — the standard "QThread: Destroyed while thread is still running"
  Qt warning/crash. Worse operationally: `PipelineWorker.run()` never reaches its
  `proc.terminate()` call in this path (only the explicit `_abort()` path does that), so a
  headless `docker run --rm ...` container is left running with no UI reference and no way to
  reattach or abort it after restart — the same orphaned-container problem already fixed on the
  Electron side (`main.js`/`server.py`'s `shutdown_cleanup`), but via a different code path here
  that hasn't been ported.
- **Abort is only polled between subprocess output lines.** `PipelineWorker.run()`'s abort check
  (~line 37) happens only inside the `for raw_line in proc.stdout` loop — for a stage that goes
  quiet for a while (e.g. Stage 1/1b before any output, or a Docker image pull), clicking Abort
  won't take effect until the process next writes a line. Not a hang, but a real UX gap on a
  stage that otherwise looks stuck.
- **Misleading dead-end UI text when `QtWebEngineWidgets` isn't installed.**
  `report_viewer.py:86-92`'s fallback label reads "Click the button below to open the report in
  your browser" — no such button is ever added; the corresponding `else` branch in
  `_load_report()` (142-144) is literally a `pass`. On any host without that optional Qt module,
  the QC Report tab becomes a permanently non-functional placeholder pointing at a control that
  doesn't exist.
- **`dashboard.py`'s `STAGE_LABELS` only maps 5 of the current 9 pipeline stages** (~16-22) —
  `dbd_to_dat_flight`, `dat_to_trajectory_flight_nc`, `fix_metadata`, `preflight` fall back to
  raw snake_case badge text. Cosmetic, but another data point that this UI wasn't updated when
  the stage list grew to 9, consistent with the staleness `CLAUDE.md` already documents.

## Known perf issue (deferred by Travis)

- Navigating away from the AD2CP section mid-stage and back is laggy: `renderBelow(dep)`
  (`app.js`) clears the whole DOM, fires two parallel API fetches, rebuilds all stage
  cards, and reconnects SSE from scratch. Fix would be to show a cached last-known snapshot
  immediately and patch in fresh data — not yet implemented, explicitly deferred.
