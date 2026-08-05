# Glider Processing Pipeline — Known Issues

Bugs and defects found while researching this codebase, not yet fixed (per standing instruction
to flag rather than fix without being asked). `CLAUDE.md` covers architecture and correct
current behavior; this file tracks things that are actually wrong — two with real
scientific-data-quality consequences. When something here gets fixed, **remove its entry**
rather than letting this file accumulate stale claims.

## Active bugs affecting real QC output

⚠️ **1. QC fallback config has been silently lapsed since 2021, not "might lapse again."**
`CHANGES_FROM_RUTGERS.md` claims `global_configs.yml`'s time window was extended from
`2021-12-31` to `2030-12-31`, but the live file (`qc/config/global_regional/global_configs.yml:6`,
**and** the Docker-baked copy at `rugliderqc/qc/config/global_regional/global_configs.yml:6`)
still ends `2021-12-31`. Only `usvi_configs.yml` and one of `mab_configs.yml`'s three contexts
actually got the 2030 extension. Since today's date is already past the lapsed date, **any
profile that doesn't fall inside a higher-priority regional polygon (`usvi`/`mab`) is currently
getting zero time-windowed global QC thresholds, silently.** Also worth knowing: `qc/config/`
(bind-mounted, actually read at runtime) and `rugliderqc/qc/config/` (baked into the Docker
image) are two independent, never-synced config trees with identically-named files — only the
bind-mounted copy is live, so a fix needs to land in `qc/config/global_regional/global_configs.yml`
specifically (and ideally the Docker-baked copy too, for consistency).
**Fix:** bump both copies' `ending:` to match `usvi_configs.yml`'s `2030-12-31`, or investigate
why the fix documented in `CHANGES_FROM_RUTGERS.md` never actually landed there.

⚠️ **2. Pressure gross-range QC test is unit-mismatched and effectively non-functional for
every RBR/UVI glider.** Confirmed against live data (`uvi_02-20260429T1420`). The manual
pressure gross-range test in `glider_qartod_qc.py` (~lines 258-282): the RBR
pressure-variable-detection patch (documented in `CLAUDE.md`) fixed a crash, but the threshold
itself was never rescaled. `pressure_rating = gsw.p_from_z(-depth_rating, ...)` returns **dbar**
(~1112 dbar for a 1000m-rated glider, `fail_span=[0, ~2224]`), compared directly against
`ds['sci_water_pressure'].values`, which is in **bar** (observed max ~70.5 bar over a 700m dive
in the same deployment). The fail threshold is ~30x higher than the sensor's real max reading —
this test can never flag SUSPECT/FAIL for an actual pressure-sensor malfunction on a UVI
deployment, and there's no redundant check: `qc/config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml`
has a `flat_line_test` for `sci_water_pressure` but no `gross_range_test` entry.
**Fix:** apply the same `÷10`-style rescale `CHANGES_FROM_RUTGERS.md` already documents applying
elsewhere in this file.

**Note:** all other claimed fixes in the full 636-line `CHANGES_FROM_RUTGERS.md` were
cross-checked against current code (both live and Docker-baked copies where duplicated) and
confirmed correct — bug #1 above is the only claimed-fixed-but-not-actually-fixed item found.
Safe to trust the rest of that file, but re-verify before relying on any single claim in it for
something consequential, the way both of these were found.

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
- **`add_derived_variables.py` hardcodes `dataset.pressure` unguarded** (~line 96, comment even
  says *"the units in the files are incorrect (bar)"*) — this script was **not** included in the
  `('pressure','sci_water_pressure')` detection patch that `glider_qartod_qc.py`/
  `ctd_hysteresis_test.py`/`time_shift.py` got. `calculate_ph()` is called unguarded whenever
  `sbe41n_ph_ref_voltage_shifted` is present (an actively configured calculation,
  `qc/config/sciencevar_processing.yml:12-13`, not hypothetical) and only catches `TypeError`,
  not `AttributeError`/`KeyError`. **On any RBR/UVI deployment with a pH sensor, this will crash
  Stage 8's `add_derived_variables` step with an uncaught `AttributeError`**, breaking the
  `&&`-chained Docker command. Worth fixing or at least testing before a pH sensor is deployed
  on a glider that uses this pipeline.

## gncutils issues

- **`gncutils/gncutils/validate.py:22` has a typo that makes validation a silent no-op:** it
  sets a local `validate = False` instead of `validated = False`, so the returned `validated`
  flag is never actually set to `False` — `validate_sensors()` always returns `True` regardless
  of missing sensors. Currently dormant (only caller, `dba_to_ngdac_profile_nc.py`, isn't
  invoked by `app/pipeline/stages.py`), but don't copy this pattern or re-wire that script
  without fixing it first.
- Stray `.bak` files in the repo (`gncutils/gncutils/ctd.py.bak`, `__init__.py.bak`,
  `qc/config/global_regional/usvi_configs.yml.bak`) — the `__init__.py.bak` diff shows a real
  scipy-compat fix (`scipy.signal.boxcar` → `scipy.signal.windows.boxcar`) that isn't applied in
  the live `__init__.py` — relevant if host scipy is ever upgraded and the boxcar import breaks.

## Server-side robustness gaps

- **`_dep_dir(deployment)` (server.py ~252-256) creates folders as a side effect of GET
  requests.** It derives year via `deployment.split('-')[1][:4]`, falling back to a
  **hardcoded `'2025'`** if the name has no `-`, then unconditionally `os.makedirs(...,
  exist_ok=True)`. A mistyped/unknown deployment name in any request — even a plain status
  GET — silently creates a stray folder under `deployments/2025/<name>/`, and that fallback
  year is now wrong (current year is 2026). The standalone PyQt5 UI's `dashboard.py` (~167-185)
  independently reimplements this exact same bug; several call sites in `pipeline_page.py`
  (5 places) and `report_viewer.py` (2 places) do the same year-split with **no fallback guard
  at all**, raising an uncaught `IndexError` on a hyphen-less deployment name.
- **Two file-ingestion endpoints have opposite overwrite behavior:**
  `/api/deployment/<dep>/import-raw` (~1346-1383) refuses to overwrite an existing file;
  `/api/cac-files/upload` (~1597-1610) unconditionally overwrites with no existence check.
- **`/api/deployment/<dep>/rename`** (~1464-1537) renames a deployment across **7 separate
  top-level folders** one `os.rename()` at a time, with **no transaction/rollback**. A
  mid-sequence failure leaves the deployment split across old/new names in different folders;
  the response only reports `renamed`/`skipped`/`errors` lists — nothing auto-reconciles.
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
  typed into the metadata editor risk silent mis-encoding or a crash.
- **`_current_sensor_sets()` (~1212-1238) silently defaults `trajectory_flight` to the full**
  `trajectory` sensor set if not explicitly configured in `sensor_sets.json` — flight-only
  trajectory NC files get the entire science sensor set unless someone deliberately narrows it.
- **`main.js` spawns Python via a hardcoded `'python'`**, not `python3` or a venv path
  (~line 18); also sets `webSecurity: false` on the BrowserWindow to allow `file://` iframes
  and localhost fetches.

## Standalone PyQt5 UI (`app/ui/`) issues

- **`metadata_editor.py` only ever edits one of three parallel sensor-defs copies**
  (`_load_tabs`, ~202-219, hardcoded to `science_profile/`). Each deployment actually has
  `science_profile/`, `science_trajectory/`, and `trajectory_flight/` copies feeding three
  different NetCDF-writer stages — editing through this page silently leaves the other two stale.

## Known perf issue (deferred by Travis)

- Navigating away from the AD2CP section mid-stage and back is laggy: `renderBelow(dep)`
  (`app.js`) clears the whole DOM, fires two parallel API fetches, rebuilds all stage
  cards, and reconnects SSE from scratch. Fix would be to show a cached last-known snapshot
  immediately and patch in fresh data — not yet implemented, explicitly deferred.
