# Glider Processing Pipeline — Known Issues

Bugs and defects found while researching this codebase, not yet fixed (per standing instruction
to flag rather than fix without being asked). `CLAUDE.md` covers architecture and correct
current behavior; this file tracks things that are actually wrong — including several with real
scientific-data-quality consequences. When something here gets fixed, **remove its entry**
rather than letting this file accumulate stale claims.

## Preflight (Stage 3) could show DONE before its config changes were ever applied

**Fixed (2026-08-10):** `server.py`'s `run_stage()` used to mark Stage 3 `STATUS_DONE` the
instant the *analysis* subprocess exited, with no special-case for preflight's two-phase design
(analysis, then a separate `/preflight/apply` call to actually write config) — confirmed via real
deployment data that this had already happened at least once (`uvi_02-20260429T1420`'s last
preflight run proposed creating `ctd_hysteresis.yml` and an updated regional boundary, neither of
which was ever applied, yet the stage showed `done`). Fixed by adding a `NEEDS_REVIEW` status
(`app/pipeline/state.py`) that `run_stage()` now sets instead of `DONE` whenever the analysis
reports anything pending, verified against real log data and a live end-to-end run.

**Note (2026-08-13):** this repo used to also have a second, standalone PyQt5 desktop UI
(`app/main.py`/`app/ui/`/`app/workers/`) with the identical bug, never fixed there before the UI
was removed entirely as confirmed-unused and ~2 months stale relative to Electron — see git
history for that removal if relevant. The Electron fix above is unaffected; it was never
PyQt5-specific.

## Stage 8: re-running QC on an already-processed deployment failed to move any output files

**Fixed (2026-08-12): `move_nc_files.py` (the final step of Stage 8) failed to move any file
out of `qc_queue/` when re-running QC on a deployment that had already been through Stage 8
once.** Confirmed on real data (`uvi_01-20241026T1158`, 625 files): after editing
`usvi_configs.yml`'s conductivity bounds and re-running QC, the log showed
`Moved 0 of 625 netcdf files`, one `Error moving ... Destination path '...' already exists`
per file. Root cause: `move_nc_files.py:68` calls `shutil.move(f, data_path)` with `data_path`
as the destination *directory* — Python's `shutil.move()` deliberately refuses to overwrite in
that form when a same-named file already exists there, raising exactly this error, rather than
falling back to `os.rename`'s native overwrite behavior (which only applies when the destination
is a full file path, not a directory). Since Stage 8 always starts a fresh run by writing into
`qc_queue/` regardless of what's already in the final output directory, any second run against
the same deployment hit this on every single file — no clobber/overwrite handling existed at all.
Same underlying bug *class* as the Docker/Windows EPERM entry below (re-running against an
already-populated destination), different mechanism (a deliberate Python stdlib safety check,
not an OS permission error) and a different script. Nothing was lost — the freshly-computed files
sat safely in `qc_queue/` (nested under `delayed/`, i.e. `delayed/qc_queue/`) until manually moved
into place. **Fix:** remove the existing destination file (if any) before `shutil.move()`, mirroring
the same pattern used for the EPERM fix below. Verified two ways: (1) an isolated reproduction —
old content in the destination, new content in the queue, confirms the fixed logic overwrites
cleanly with no exception; (2) real data — after manually moving the stuck `qc_queue/` output into
`delayed/`, `conductivity_qartod_climatology_test`'s FAIL rate dropped from 52.1% (in the 15-85m
band that hits the old `[3,6]` bound) to 0.00% deployment-wide, confirming the new bounds are
correctly reflected in the moved output. **Caveat: this fix hasn't been exercised through an actual
Docker run yet** — `rugliderqc/` is baked into the `glider-processing` image at build time (unlike
`gncutils`, which is bind-mounted live), so it won't take effect until the image is rebuilt.

## Docker/Windows: overwriting existing pipeline output always failed EPERM

**Fixed (2026-08-11): re-running Stage 2a/4/5 against an already-processed deployment failed
`EPERM` on every single output file, silently producing zero (or garbage) NC output.**
`gncutils/gncutils/writers/slocum/ProfileNetCDFWriter.py` and `TrajectoryNetCDFWriter.py` both
write to a temp file, then `shutil.move(tmp_nc, out_nc_file)` to publish it — and every real
Stage 2a/4/5 invocation always passes `--clobber` (`app/pipeline/stages.py:115,129,153,177`), so
this overwrite-via-rename runs on every re-run against a deployment that's already been
processed once. Root cause, confirmed by direct reproduction against the real bind-mounted
output directory: this pipeline's Docker containers run as **non-root user `glider`**
(`Dockerfile:36-37`), and Docker Desktop's Windows bind mount reports every *pre-existing* file
as **owned by root (uid 0)** regardless of which container actually wrote it — a `rename()`-based
overwrite of a root-owned file by the non-root `glider` process is rejected with
`[Errno 1] Operation not permitted`, even though the mode bits report `0o777` (world-writable) and
`os.access(path, os.W_OK)` reports `True`. A **fresh** run (no file yet at the target path)
is unaffected — rename-to-create doesn't hit this check — so the bug only shows up on re-runs,
and hits every single file in the run, not just some. Confirmed via real-data reproduction: a
`shutil.move()` overwriting an existing real profile NC file failed every time with this exact
error; `os.remove()` on that same root-owned file succeeded immediately after (unlink is governed
by the containing directory's write permission, not the file's owner), and a subsequent move onto
the now-empty path succeeded cleanly. **Fix:** both writers now `os.remove(out_nc_file)` if it
exists immediately before `shutil.move()`, in both of `ProfileNetCDFWriter.py`'s two write paths
(`dbas_to_profile_nc`/`dba_obj_to_profile_nc`) and `TrajectoryNetCDFWriter.py`'s. Also fixed a
related latent bug found in the same `TrajectoryNetCDFWriter.py` except-block while there: its
error log line used `'{:s}: {:s}'.format(tmp_nc, e)` — formatting an exception object with an
explicit `s` presentation type raises `TypeError` (confirmed directly: exceptions don't implement
`__format__`), so *logging* the original error would itself have crashed instead of the intended
graceful `continue`; changed to `'{:s}: {:}'.format(...)`. Verified end-to-end against real data:
re-ran the actual Stage 2a Docker command against `uvi_01-20241026T1158` (669 profiles, fully
clobbering pre-existing output) — zero `EPERM` errors across the whole run (previously: every
single file failed). One important side effect for anyone reading this later: diagnosing this
required directly overwriting real files in this repo's `gncutils_slocum_nc_output/` to confirm
the root cause — one profile NC file was briefly left as placeholder test content mid-investigation,
then fully regenerated via the real Stage 2a command as part of verifying the fix; confirmed
restored (40 variables including `salinity`, matching its pre-corruption shape).
`gncutils/scripts/dba_to_ngdac_profile_nc.py` and `writers/navoceano/ProfileNetCDFWriter.py` have
the identical unfixed pattern but aren't invoked anywhere in this pipeline's stage commands
(confirmed via grep of `app/pipeline/stages.py`) — left as-is, not worth touching unused code.

## Active bugs affecting real QC output

⚠️ **`TA-salinity-regressions.yml` has no USVI region, so Total Alkalinity/Aragonite
Saturation State never compute for USVI, silently.** `qc/config/derived_variables/TA-salinity-regressions.yml`
defines exactly one region, `nyb` (lon `[-75,-71]`, lat `[37.5,41.5]` — Mid-Atlantic Bight),
nowhere near USVI (lon ≈ -65, lat ≈ 18). `calculate_ta()` (`add_derived_variables.py:176-219`)
falls through with empty results on no region match, with **no log line explaining why** — so
`total_alkalinity` never gets added, and `calculate_omega` (which needs it) can never run
either. **Fix:** add a USVI region block to `TA-salinity-regressions.yml`, and add explicit
logging to `calculate_ta()`'s no-region-matched fallthrough. The USVI region's own
temperature/salinity coefficients can't be derived from code — they need real in-situ
TA/salinity water-sample data, the same way the existing `nyb` block came from a published
Mid-Atlantic Bight study (Wright-Fairbanks et al 2020) — this is a science input, not a code fix.

**Fixed (2026-08-11): preflight's oxygen-variable detection sampled only the first profile NC
file, silently dropping oxygen time-lag QC config for duty-cycled optodes.**
`pre_qaqc_config_check.py`'s analysis mode used `find_profile_nc()`'s single file
(`sorted(glob.glob(...))[0]`, chronologically earliest) for both instrument-metadata detection
(fine — identical across profiles) and oxygen data-variable detection via `get_oxygen_vars()`
(not fine — data-variable *presence* is per-profile, not deployment-wide). Reproduced against
real data: `uvi_01-20241026T1158`'s first profile (`..._133012Z_dbd.nc`) has CTD-derived vars
(`salinity`/`density`/`potential_temperature`) but zero oxygen vars, even though the Aanderaa
optode is clearly sampling normally — 628 of the deployment's 669 profiles (94%) do have
`oxygen_concentration`/`oxygen_saturation`. `sensors['oxygen']` (from instrument metadata) still
correctly detected the optode, but `oxygen_vars` came back empty, and `oxygen_vars` is what
`build_time_shift_yml()` loops over to generate `time_shift.yml`'s per-variable entries — so a
deployment with plenty of real oxygen data would get a `time_shift.yml` with **no oxygen
time-lag-shift entries at all**, silently skipping that QC step in Stage 8, while printing a
misleading `WARNING: Oxygen instrument detected but no oxygen variables found in NC file.`
`qctests.yml` was unaffected (depends only on `sensors`, not `oxygen_vars`).
**Fix:** new `find_oxygen_vars(nc_dir, first_nc_path)` checks `first_nc_path` first (cheap, the
common case), then falls back to scanning the rest of the deployment's profile files in
timestamp order until one has oxygen vars, or all are exhausted (genuine no-data case).
Verified against real data: re-running preflight on `uvi_01-20241026T1158` now correctly reports
`oxygen_concentration`/`oxygen_saturation` found, no warning, and `time_shift.yml`'s generated
content includes both variables' 30-sample default-shift entries.

**Fixed (2026-08-11):** pH itself now gets its regional QC. `usvi_configs.yml`'s tightened pH
gross-range/spike bounds never fired because `glider_qartod_qc` runs before `add_derived_variables`
creates the `pH` variable, so the streams loop's `if sensor not in ds.data_vars: continue`
always skipped it — pH only ever got the much looser generic `[0,14]` gross-range check. Fixed
by adding `rugliderqc/scripts/derived_variable_regional_qc.py` to Stage 8's chain (after
`add_derived_variables`, before `summarize_qartod_flags`), which reuses `glider_qartod_qc.py`'s
regional-streams logic (extracted into `run_regional_stream_tests()`, verified byte-identical
to the original on real data across 170 QC variables) scoped to just `pH`. Cheap by design
since no current fleet deployment has a pH sensor: samples `file[0]` + the last 10 files before
doing any real work (~63s worst case per real-data timing, vs. checking every file), with a
`ph_regional_qc_check_all_files: true` `qctests.yml` escape hatch for the rare case pH gets
turned on mid-mission outside the sampled files. Verified end-to-end against synthetic pH data
(regional bounds match an independent manual calculation exactly; `summarize_qartod_flags`
correctly aggregates the new flags with zero changes needed) and against real pH-less
deployment data through the full Stage 8 chain (no regression). One implementation snag worth
noting: `usvi_configs.yml`'s pH block uses `gross_range_test`, which isn't one of the four test
types `run_regional_stream_tests()` handles — calling it with `sensors=['pH']` in isolation
crashed (`UnboundLocalError`) rather than silently falling through, which is what led to
discovering the bug entry directly below. `gross_range_test` is implemented directly in the new
script instead of the shared function, to avoid changing `glider_qartod_qc.py`'s own
already-verified behavior.

**Partially fixed (2026-08-10):** `add_derived_variables.py`'s `calculate_ph()`/`calculate_omega()`
both hardcoded `dataset.pressure`/`apply_qc(dataset, 'pressure')`, unguarded and never patched
for RBR's `sci_water_pressure` (see "Other rugliderqc QC-script issues" below) — `calculate_ph()`
would crash Stage 8 outright with an uncaught `AttributeError` on any RBR/UVI pH deployment, and
`calculate_omega()` would silently always return `[]` for one, via its own `KeyError` catch,
since it never finds a variable literally named `pressure`. Both now detect
`('pressure', 'sci_water_pressure')` and convert bar→dbar when needed, verified against real
calibration-shaped test data (numerically identical output to the dbar case, ~1e-11 max
difference).

**Fixed (2026-08-11): `glider_qartod_qc.py`'s regional streams loop no longer corrupts a
neighboring sensor's QC-variable metadata.** The streams loop (`run_regional_stream_tests()`)
only implements `pressure_test`/`climatology_test`/`spike_test`/`rate_of_change_test`, but
`usvi_configs.yml`'s `oxygen_concentration`/`oxygen_saturation` blocks use `gross_range_test`/
`flat_line_test`, and `pH`'s block uses `gross_range_test` — none matched by the `if/elif`
chain. Since `qc_varname`/`flag_vals` were only assigned inside a matched branch, an unmatched
test type fell straight through to the "create QC variable" step using stale values left over
from whatever test ran immediately before it in dict-iteration order — for USVI, that's
`density`'s `rate_of_change_test` (the sensor immediately preceding oxygen in
`usvi_configs.yml`'s `streams` order). Reproduced directly against real data, twice
independently (both the isolated extracted function and the real script via its actual `main()`
path): the bug overwrote `density_qartod_rate_of_change_test`'s **attrs** (not its flag values —
those stayed density's own, correct numbers throughout) with metadata describing
`oxygen_saturation`'s `flat_line_test` instead. Checked all three regional config files
(`global_configs.yml`, `mab_configs.yml`, `usvi_configs.yml`) via `ioos_qc.config.Config`
directly — confirmed this pattern is isolated to exactly 3 sensors, all in `usvi_configs.yml`:
`oxygen_concentration`, `oxygen_saturation`, and `pH` (the last already handled separately in
`derived_variable_regional_qc.py`, and unreachable in this primary pass since `pH` doesn't
exist as a variable when `glider_qartod_qc` runs).

**Note on an earlier claim in this entry (corrected):** an earlier version of this note guessed
the bug was "dormant only by timing" — that real stored files predated `usvi_configs.yml` gaining
oxygen entries. Checked properly and that explanation didn't hold up (the config commit
predated the real deployment's actual processing timestamp) — the real reason the historical
file looked clean couldn't be pinned down (file mtimes on this system may not reliably reflect
actual historical Docker-run times). What's certain, verified by direct repeated execution of
the real script: this fires on every current run, not just hypothetically.

**Fix applied (Option A — stop the corruption, don't change any QC output):** unmatched test
types now hit an explicit `else: continue` branch with a `WARNING` log line
(`glider_qartod_qc.py`, inside `run_regional_stream_tests()`), instead of falling through.
Verified against real data: `density_qartod_rate_of_change_test` now keeps correct attrs after
a run that includes oxygen; all 6 unaffected sensors (temperature, conductivity, salinity,
pressure, sci_water_pressure, density) produce byte-identical output (values *and* attrs) to
before this fix — of 34 real `_qartod_*` variables compared, exactly 1 differs
(`density_qartod_rate_of_change_test`'s attrs, now correct), 0 have different values anywhere.
Also verified the pH regional-QC fix above is unaffected, and the full Stage 8 chain still runs
cleanly end-to-end.

**Where the applied vs. never-applied oxygen bounds actually live**, for anyone tracing this
later:
- **Actually active today:** `qc/config/gross_flatline/optode_4831_gross_flatline.yml` — matched
  via `instrument_optode`'s `maker`/`model` attrs through `define_gross_flatline_config()`
  (this fleet's real sensor: Aanderaa Oxygen Optode 4831).
  `oxygen_concentration`: `suspect_span:[0,500]`/`fail_span:[0,1000]` (gross range),
  `tolerance:0.1`/`suspect:120`/`fail:300` (flat line). `oxygen_saturation`:
  `suspect_span:[0,150]`/`fail_span:[0,300]` (gross range). Not UVI-authored — absent from
  `CHANGES_FROM_RUTGERS.md`'s otherwise-exhaustive deviation list, and `optode_3830`/`optode_3835`/
  `optode_5013W`/`optode_5014W` share identical values with a `# specs copied from optode 3835`
  comment — this looks like vendored, accumulated-upstream, per-sensor-model config, not
  something built for this project.
- **Configured but never applied (Option B, still open, deliberately not done here):**
  `qc/config/global_regional/usvi_configs.yml`'s `oxygen_concentration`/`oxygen_saturation`/`pH`
  blocks. USVI's `oxygen_concentration` gross-range bounds specifically are
  `suspect_span:[100,300]`/`fail_span:[0,500]` — tighter than the generic instrument-level ones
  above. Actually implementing these would **overwrite** the instrument-level results (same
  variable names) with these tighter, region-specific bounds — a real change to oxygen's actual
  QC flag outcomes across the fleet, not just a bug fix. Deliberately deferred as its own
  decision, not bundled into this fix.

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

**Fixed (2026-08-13): `ctd_hysteresis_test.py`'s minimum-profile-span pre-check used an
unconverted bar-unit literal as if it were dbar.** Two identical gates (lines ~271-272 and
~340-346, one per profile in the down/up pair): `pressure_diff < 5` was meant to gate on ~5 dbar
(~5m) of profile depth change before bothering to run the test, but for this fleet's RBR CTDs
the source is `sci_water_pressure` in **bar**, not dbar — silently raising the effective gate to
~5 bar (~50 dbar, ~50m). Confirmed the root cause via `CHANGES_FROM_RUTGERS.md`'s own background
section: Seabird CTDs (upstream's original target) emit a raw glider variable literally named
`pressure`, already in dbar; RBR Legato³ CTDs (this fleet) emit `sci_water_pressure`, in bar —
this is a hardware/firmware fact, not a pipeline config choice, so which variable is present
tells you which CTD was actually flown. Verified real impact on `uvi_01-20241026T1158` (625
profiles): 3 were left NOT_EVALUATED by the old gate, 2 of which had a real span between 5-50
dbar and should have been tested — only 1 was genuinely too short either way. Real but modest
impact for this fleet's mostly-deep missions; would matter more for shallow/nearshore ones.
**Fix:** convert `pressure_diff`/`pressure_diff2` to dbar (`× 10`) when the selected pressure
variable is `sci_water_pressure`, before comparing against the literal `5` — mirrors the same
`if pressure_var == 'sci_water_pressure': ... * 10` pattern already used in
`add_derived_variables.py`'s `calculate_ph()`/`calculate_omega()` fix. Automatically correct for
a hypothetical future Seabird-equipped glider too, since `pressure` (dbar) and `sci_water_pressure`
(bar) are mutually exclusive in real data — whichever is present is left correctly unscaled or
scaled, respectively, with no user/config change needed either way. Verified against real data:
re-checked the gate with the fix applied — skips drop from 3 to 1 (only the genuinely-too-short
profile), and since this only loosens the gate, the 622 profiles that already passed the old
(stricter) gate are unaffected by construction.

**Fixed (2026-08-13): `time_shift.py` had the identical unconverted-bar-literal bug — and unlike
`ctd_hysteresis_test.py`, it also degraded the actual computed optimal-shift value, not just a
pre-check gate.** `calculate_pressure_range(...) < 3` (~lines 370,417) is meant to gate on ~3 dbar
(~3m); for this fleet's bar-unit `sci_water_pressure`, that silently became a ~30 dbar (~30m)
gate — same root cause as the `ctd_hysteresis_test.py` fix above. But `pressure_bins(df,
interval=0.25)` (line 128) — used to bin the pressure axis before computing the area-between-curves
that the lag search minimizes over — was *also* affected: its `0.25` (meant as 0.25 dbar, ~25cm
resolution) became ~2.5 dbar (~2.5m), a 10x coarser search resolution. This one changes an actual
saved scientific value (the optimal time-shift applied to correct oxygen/pH sensor lag), not just
whether a profile gets evaluated, so it got a higher bar of verification before fixing: confirmed
the bar/dbar premise two independent ways (NC attrs say `units: bar`; cross-checked against the
independently-computed `depth` variable, ratio ≈9.93 ≈ expected ~10), confirmed via the original
pre-patch code on record in `CHANGES_FROM_RUTGERS.md` §3 that `0.25`/`3` are verbatim
Seabird-dbar-authored literals never touched by the RBR patch, and empirically reproduced the real
area-minimization search against 6 real down/up cast pairs from `uvi_01-20241026T1158` outside the
pipeline — 2 of 6 got a different optimal shift under correct dbar-scaled binning (39s→41s,
40s→41s). **Fix:** convert `press_da` to dbar once, at its single source of creation
(`× 10` when `pressure_varname == 'sci_water_pressure'`), so both the gates and the binning
interval are correct with no further changes needed. Verified the real fixed code path reproduces
the corrected 41s result for both pairs that differed. Full before/after code and the complete
verification writeup are in `CHANGES_FROM_RUTGERS.md` §14.
- **`data_gap_check.py` is hardcoded to Rutgers' own ERDDAP and is silently dead for every UVI
  deployment.** `elink = f'http://slocum-data.marine.rutgers.edu/erddap/tabledap/{deployment}
  -profile-sci-rt'` (~line 19) will never resolve for a `uvi_*` deployment name — the script's
  broad `except Exception` just prints `"{deployment} not found on ERDDAP"` and exits 0 (no
  warning surfaced in the pipeline log). `app/pipeline/stages.py` (~line 263) wires it into
  Stage 8 as if it's a functioning step. Pure monitoring, doesn't set flags, so low severity —
  but Stage 8 currently gets zero value from this step for UVI.
- **Fixed (2026-08-13): `check_duplicate_timestamps.py`'s `ds`/`ds2` file handles weren't closed
  on the success path** — only the error branches (`OSError`/`IndexError`) called `.close()`;
  all three normal-outcome branches (ds2 is the duplicate, ds is the duplicate, neither is) fell
  through to the next loop iteration without closing either handle, which is the common case, not
  an edge case — it fired on essentially every iteration of a ~625-file loop. Worse than a simple
  leak: xarray doesn't close a file just because the Python reference is dropped — it goes through
  `CachingFileManager`'s global LRU cache (default cap 128 files), so handles only actually close
  on eviction, not on reassignment; a 625-file run comfortably exceeds that cap. There's also a
  concrete double-open at the loop boundary — the file that's `ds2` in iteration `i` gets reopened
  as `ds` in iteration `i+1` (line 89) *before* the old `ds2` reference is overwritten (line 102),
  so the same file is briefly open via two handles at once — the same class of risk this repo's
  own `_nc_lock`/`_TRAJ_WORKERS=1` precedent (`server.py`) already treats as unsafe for HDF5. No
  crash observed in practice (a real run's log shows this script completing cleanly), so this was
  a latent risk, not a confirmed failure. **Fix:** wrapped the three comparison branches (lines
  112-129) in a single `try/finally` that unconditionally closes both `ds` and `ds2`, so every
  path through the loop closes both handles exactly once. Docker image rebuilt and verified —
  `grep`'d the container's own copy of the script to confirm the fix is actually baked in.
**Fixed (2026-08-13): the derived-variable dependency cascade (DO→mg/L, pH voltage→pH, pH→TA,
TA→Omega) was implicit and unenforced.** `add_derived_variables.py`'s single pass
(`for variable_name, vardict in proc_vars.items():`) only worked because
`qc/config/sciencevar_processing.yml`'s 4 entries happen to be listed in exact dependency order —
confirmed via the real config: entry 2 (`sbe41n_ph_ref_voltage_shifted` → `pH`) must run before
entry 3 (`pH` → `total_alkalinity`) can find its precursor, which must run before entry 4
(`total_alkalinity` → `aragonite_saturation_state`) can find *its* precursor — each precursor
lookup wrapped in a bare `except KeyError: continue` with no log entry. Reordering the YAML or
inserting a new derived variable above an existing entry would have silently broken the whole
pH/TA/Omega chain with no error to indicate why. Deliberately not just a log-line fix: a
`KeyError` here is ambiguous between "genuinely missing precursor" (e.g. no pH sensor on this
deployment — correct, expected, should stay silent) and "precursor exists but hasn't been
computed yet due to ordering" (a real bug) — logging every KeyError would drown the common,
correct case in noise while trying to catch the rare one. **Fix:** replaced the single pass with
a retry loop that keeps re-attempting only the still-unresolved entries across multiple passes
until a full pass makes no further progress — correct for any config ordering, since a pass that
adds nothing new means every remaining entry's precursor is genuinely absent, not just
not-computed-yet. Verified three ways with an isolated reproduction of the exact algorithm
(`ioos_qc` isn't available on the host to run the real script directly): (1) a deliberately
reordered config (TA/Omega entries placed *before* the pH entry) resolves every variable
correctly across passes, proving the fix actually fixes the ordering dependency; (2) a genuinely
missing precursor (no pH sensor) terminates cleanly after 1 pass with the pH/TA/Omega chain
correctly left unresolved — no infinite loop, no fabricated output; (3) the current,
already-correctly-ordered config produces an identical resolution order to the original
single-pass behavior — no regression.
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

- **Fixed (2026-08-13): `gncutils/gncutils/validate.py:22` had a typo that made validation a
  silent no-op.** `validate_sensors()` set a local `validate = False` instead of
  `validated = False` on a missing-sensor warning, so the returned `validated` flag was never
  actually set to `False` — the function always returned `True` regardless of missing sensors,
  even though its sibling function two lines below (`validate_ngdac_var_names`) does the
  identical check correctly, which is what made this read as a copy/paste slip rather than
  intentional. **Confirmed safe to fix:** repo-wide grep found exactly one caller anywhere,
  `dba_to_ngdac_profile_nc.py:61` — and that script isn't invoked by anything in this pipeline
  (`app/pipeline/stages.py`, `server.py`, `start_app.bat`, `package.json` — zero references), so
  this had no effect on anything live regardless of import-ability (see the next entry for that
  script's own since-fixed brokenness). **Fix:** `validate = False` → `validated = False`.
  `gncutils` is bind-mounted live, no Docker rebuild needed for this to take effect.
- **Fixed (2026-08-13): `dba_to_ngdac_profile_nc.py` had two separate defects that made it
  unrunnable even though nothing in this pipeline calls it.** (1) Two broken imports:
  `from gncutils.ProfileNetCDFWriter import ProfileNetCDFWriter` (actual location:
  `gncutils.writers.slocum.ProfileNetCDFWriter`) and `from gncutils.readers.dba import
  create_llat_dba_reader` (no such module; the real one is `gncutils.readers.slocum`) — both
  raised `ModuleNotFoundError` at import time, confirmed against the correct paths the working
  sibling script `dba_to_profile_nc.py` actually uses. (2) A deeper bug the import fix alone
  wouldn't have surfaced: line 134's `calculate_density(ctd_data[:, 0], ctd_data[:, 5],
  ctd_data[:, 1], prac_sal, mean_lat, mean_lon)` passed 6 positional args into
  `ctd.py`'s 5-param `calculate_density(temperature, pressure, salinity, latitude, longitude)` —
  and not just an extra argument, the columns themselves were mismatched (passing `llat_time` as
  temperature and conductivity as pressure), unlike the correctly-mapped `calculate_practical_salinity`
  call one line above it. Would have raised `TypeError` immediately after a successful import.
  **Fix:** corrected both import paths, and changed the call to
  `calculate_density(ctd_data[:, 6], ctd_data[:, 1], prac_sal, mean_lat, mean_lon)` (temperature
  = `sci_water_temp` column, pressure = `llat_pressure` column). Spot-checked the rest of the
  script's `ProfileNetCDFWriter` method/attribute usage (`init_nc`, `open_nc`, `set_trajectory_id`,
  `set_container_variables`, `insert_var_data`, `finish_nc`, etc.) against the base/slocum writer
  classes — all present and used the same way the real, working `writers/slocum/ProfileNetCDFWriter.py`
  uses them internally. Still not invoked by anything in this pipeline — this fix makes the
  script *correct* if it's ever revived, not *wired in*.
- Stray `.bak` files in the repo (`gncutils/gncutils/ctd.py.bak`, `__init__.py.bak`,
  `qc/config/global_regional/usvi_configs.yml.bak`) — the `__init__.py.bak` diff shows a real
  scipy-compat fix (`scipy.signal.boxcar` → `scipy.signal.windows.boxcar`) that isn't applied in
  the live `__init__.py` — relevant if host scipy is ever upgraded and the boxcar import breaks.
  **Additionally confirmed:** `gncutils/gncutils/ctd.py:123`'s dead-code `derive_ctd_parameters`
  (unreferenced everywhere — every real caller imports the `readers/slocum.py` version instead)
  is not just unused but would crash if it ever were invoked: it does
  `sensors = dba["sensors"]; sensors.get(...)`, but `dba['sensors']` is always a **list** of
  dicts throughout this codebase, not a dict — lists have no `.get()`.
- **Fixed (2026-08-13): `pt0_from_t` (potential temperature) was called with Practical Salinity
  where GSW's function signature requires Absolute Salinity.** `gncutils/gncutils/readers/slocum.py:491`
  (primary CTD branch) and `:554` (alt-CTD branch): `p_temp = pt0_from_t(salt, t, p)`, where
  `salt` is `calculate_practical_salinity()`'s output (Practical Salinity, PSS-78), but GSW's
  `pt0_from_t(SA, t, p)` requires Absolute Salinity — confirmed against the installed `gsw`
  package's own docstring. `calculate_density`/`calculate_sound_speed` in the same file
  correctly call `SA_from_SP()` first; these two lines skipped that conversion. Real,
  reproducible defect (fired on every profile with full CTD coverage, output is written to
  profile NetCDF via the configured `potential_temperature` sensor def), but re-verified
  numerically as scientifically inert across a spread of realistic T/P/S combinations: max error
  ~1.2×10⁻⁴°C at 10°C/500 dbar/36 PSU, ~7×10⁻⁶°C at 25°C/50 dbar/35 PSU — roughly two orders of
  magnitude below the RBR Legato³'s stated ~0.002°C accuracy even at the worst-case depth this
  fleet operates at. **Fix:** `p_temp = pt0_from_t(SA_from_SP(salt, p, lon, lat), t, p)` in both
  branches, `SA_from_SP` added to the existing `from gsw import pt0_from_t` line. `gncutils` is
  bind-mounted live, no Docker rebuild needed for this to take effect.
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

- **Fixed (2026-08-13): `_dep_dir(deployment)` (server.py ~260-265) created folders as a side
  effect of GET requests, with a stale hardcoded year fallback.** It derives year via
  `deployment.split('-')[1][:4]`, falling back (pre-fix) to a hardcoded `'2025'` if the name has
  no `-`, then unconditionally `os.makedirs(..., exist_ok=True)`. Checked every call site rather
  than just the one already known: **six GET routes called `_dep_dir(dep)` with zero name
  validation beforehand** — `dep_state`, `stream_stage_logs`, `serve_report`, `list_plots`,
  `generate_map_image_route`, and `cloud_list_files` — so a mistyped/unknown deployment name in
  any of those, even a plain status check, silently created a stray folder under
  `deployments/2025/<name>/` (a year that isn't even current anymore — it's 2026).
  **Correction to an earlier version of this entry:** it also claimed `app/pipeline/stages.py`'s
  `_year()` and the `output_fn` lambdas (`preflight`/`qc`/`report`) crash with a raw 500 in the
  request thread on a hyphen-less name — re-checked and that's not accurate. `run_stage`, the
  only caller of `command_fn`/`output_fn`, already validates `_valid_deployment_name()` (a strict
  `name-YYYYMMDDTHHMM` regex) before touching either, so a malformed name gets a clean 400 well
  before `_year()`/`output_fn` are ever reached. `output_fn` also turned out to be genuinely dead
  code — nothing in the backend calls it at all, consistent with the separately-tracked "no
  server-side stage-prerequisite gating" item below. **Fix:** added a `_valid_deployment_name(dep)`
  guard (matching the pattern `run_stage` already used) to all six unguarded routes, returning a
  400 before `_dep_dir` is ever called; also changed `_dep_dir`'s hyphen-less fallback from the
  frozen `'2025'` literal to `datetime.datetime.now().year`, so it can't go stale again the same
  way — defensive only, since every route path now rejects a hyphen-less name before reaching it.
- **Two file-ingestion endpoints have opposite overwrite behavior:**
  `/api/deployment/<dep>/import-raw` (~1346-1383) refuses to overwrite an existing file;
  `/api/cac-files/upload` (~1597-1610) unconditionally overwrites with no existence check.
- **Fixed (2026-08-14): `/api/deployment/<dep>/rename`** (`rename_deployment`, ~1649-1725)
  renames a deployment across **8 separate top-level path joins** (`incoming_data`,
  `deployments/<year>`, `dat_files`, `gncutils_slocum_nc_output`, `post_processing`, `logs`,
  `ad2cp`, `glider_config_files_json/<glider>`) one `os.rename()` at a time. Two compounding
  gaps, confirmed by reading the code directly: (1) only the `incoming_data` pair got a
  pre-flight "does the destination already exist" check — the other 7 destinations were only
  discovered to conflict when `os.rename()` itself threw mid-loop, by which point earlier
  renames in the sequence were already committed; (2) a failure didn't stop the loop or roll
  anything back, so the deployment ended up genuinely scattered — not "everything after the
  failure point stays old," but exactly the one folder that failed staying old while everything
  else (both before *and* after it in the sequence) still landed on the new name. Since
  `incoming_data` runs first and always succeeds (it had its own pre-check and is rarely
  locked), the deployment would start showing up under the *new* name in listings while its
  actual pipeline state/QC output/logs stayed under the *old* name in whichever folder failed —
  looking like lost history rather than a mis-filed folder. Realistic trigger given this runs
  natively on Windows, not in Docker: a file inside one of these folders locked by another
  process (antivirus, an open Explorer window) at the moment of rename. **Fix, two parts:**
  (1) pre-flight-check all 8 destination paths before touching anything, failing cleanly with a
  409 naming the conflicting path if any already exists (catches the "stale name collision from
  an earlier partial failure" case for free, before any folder moves); (2) track each
  successful rename in this batch and roll all of them back, in reverse order, if any later one
  fails — so the deployment ends up either fully renamed or fully back on the old name, never
  split. If the rollback itself fails on one of the reversals (e.g. the same lock is still
  held), the response says so explicitly (`rollback_error`, distinct from the normal
  `rolled_back: true` case) rather than silently claiming success. Verified with a real Flask
  test-client run across all three paths: (1) a full 8-location rename succeeds and leaves
  nothing under the old name; (2) a destination collision seeded at a non-`incoming_data`
  location (simulating a stale leftover) is rejected with 409 before any folder moves, old
  deployment fully intact; (3) `os.rename` mocked to fail on the 3rd call (after 2 real
  successes) — response reports `rolled_back: true`, `renamed: []`, and the deployment is
  confirmed fully back under the old name in all 8 locations, nothing left under the new one.
- **Fixed (2026-08-14): disk cache staleness was mtime-based, not content-based**
  (`_load_disk_cache`/`_save_disk_cache`, ~212-247, backing `_surf_cache`/`_traj_report_cache`
  for the Map tab and trajectory report). Validity was `max(mtime across all trajectory NC
  files) >= stored value` — a pure number comparison, no hashing or content check. That silently
  assumes file content only ever changes together with a newer mtime, which is false for
  anything that replaces content while preserving the original timestamp: `shutil.copy2()`
  (already used elsewhere in this same codebase, e.g. `import_raw`), `robocopy`'s default
  timestamp-preserving flags, or a timestamp-preserving restore from backup. Realistic here
  specifically because this session's own workflow already involved exactly that pattern
  (`qc_fix_comparison/`-style backups) — anyone who restored trajectory NC files that way, then
  opened the Map tab, could have gotten silently stale data with zero indication anything was
  wrong. **Fix:** replaced the single `max(mtime)` float with `_dir_signature()`, a SHA-256 hash
  over `(filename, size, mtime_ns)` for every file in the directory (sorted for determinism) —
  still just `stat()` calls, no extra file reads, but now detects a same-mtime content swap.
  Cache comparison changed from `>=` (ordered float) to `==` (opaque signature) throughout: both
  disk-cache functions and the two in-memory caches (`_surf_cache`/`_traj_report_cache`, key
  renamed `mtime`→`sig`). Verified directly: wrote a file, computed its signature, replaced its
  content with different bytes, force-set the mtime back to the exact original value via
  `os.utime` (reproducing what `shutil.copy2`/`robocopy` do) — confirmed the old max-mtime
  approach would NOT have detected the change (mtime genuinely identical), while
  `_dir_signature()` correctly returns a different hash; also verified `_load_disk_cache`/
  `_save_disk_cache` round-trip correctly (matching sig hits with the right data, differing sig
  misses and forces recompute). Out of scope: the *separate* `_dir_mtime()`-based caches
  (`_nc_files_cache`, `_dat_files_cache`, `_raw_files_cache`, `_cloud_files_cache`, etc.) use a
  different, simpler mechanism and weren't part of this fix.
- **Fixed (2026-08-14): KMZ export had no disk-cache fallback, unlike its sibling report
  endpoints.** `download_kmz` (~2115-2134) only checked in-memory `_surf_cache`/
  `_traj_report_cache` and 404'd ("open the Map tab first") if cold, while
  `generate_map_image_route` and `flight_stats_report_html` both already retried via
  `_load_disk_cache(...)` when memory was empty. Net effect: after a server restart, KMZ export
  failed even when a valid disk cache existed that the other two endpoints would happily use.
  **Fix:** added the identical disk-cache-fallback pattern (reusing `_dir_signature()` from the
  content-based-cache fix above) when both in-memory caches are empty — tries `surfacings` first
  to match the function's existing surf-then-traj preference order, falling back to
  `trajectory_report` if that's what's actually on disk. Verified against real data: in a fresh
  process with both in-memory caches genuinely empty and no prior call to `/surfacings` in that
  process (simulating a real server restart), `/kmz` against a deployment with an existing disk
  cache from an earlier run now returns a real 73KB KMZ zip (200, valid `PK` zip signature)
  where it previously would have 404'd; confirmed no regression — a deployment with no cache
  anywhere still correctly 404s.
- **Investigated and ruled out as a bug (2026-08-14): `total_ah_used` (trajectory-report,
  `max(m_coulomb_amphr) - min(m_coulomb_amphr)` across a deployment's trajectory files) was
  flagged as not detecting a coulomb-counter reset (battery swap/reboot mid-deployment).**
  Confirmed with Travis this doesn't apply to how this fleet is actually operated: resets are
  operator-controlled and only ever happen between deployments, never mid-mission — and when a
  non-rechargeable battery is reused across deployments, its existing coulomb value is
  deliberately carried over rather than reset, specifically to keep the running amp-hour total
  correct for that physical battery. Independently corroborated against the real Slocum master
  sensor list (`masterdata_11_07.txt`): `m_coulomb_amphr` (`# integrated current, i.e. energy`)
  is a distinct sensor from `m_coulomb_amphr_total` (`# persistant amp-hours total`, a
  cross-deployment lifetime counter — not what this code reads), and the nearby
  `m_bms_battery_pack_anomaly` sensor's comment — "resets when coulomb starts" — confirms resets
  are tied to the *start* of a counting session (a deployment/battery-install boundary), not
  something that can happen mid-session. Given resets can't occur mid-deployment, `max - min`
  computed within one deployment's own data window is correct regardless of whether the battery
  started that deployment at zero (fresh) or at a carried-over nonzero value (reused) — a range
  calculation is invariant to its starting baseline. `total_ah_used` itself is confirmed to be a
  value computed entirely in this codebase (`server.py`), not a native glider telemetry
  variable. No fix needed.
- **Fixed (2026-08-14): `/api/deployment/<dep>/metadata` swallowed per-file JSON errors into
  `None`, and tracing the actual consequence turned up a real data-loss path, not just an
  ambiguous value.** `metadata()` (~1169-1181) used a bare `except Exception: files[rel] = None`.
  On the frontend, `renderMetaView`'s `JSON.stringify(jsonData, null, 2) || '{}'` (`app.js`) does
  NOT fall back to `'{}'` for `null` input — `JSON.stringify(null, ...)` returns the *string*
  `"null"`, which is truthy, so the editor pane silently displayed the literal text `null` as if
  it were the file's real content, with zero indication anything was wrong. Since a bare `null`
  is itself syntactically valid JSON, `save_metadata`'s own `json.loads()` validation would
  happily accept it — so a user who opened a corrupted file, saw `null`, and clicked Edit → Save
  without noticing would have **permanently overwritten the original (possibly trivially
  fixable) file content with a literal null**, with no warning at any point in that path. A
  smaller instance of the same ambiguity existed in `_metaCopyFromPrev` (~2845-2848), which
  treated "file doesn't exist in the source deployment" and "file exists but is corrupted" as
  the same case, showing a misleading "not found" message for the latter. **Fix, four parts:**
  (1) server now tracks parse failures in a separate `errors` dict (`{message, raw}` per file,
  the original raw text preserved) instead of mixing a `None` sentinel into `files`; (2) the
  frontend file list keeps a broken file visible (doesn't let it silently vanish just because it
  failed to parse) but flags it with a warning marker and tooltip; (3) selecting a flagged file
  goes straight into an editable view of its actual raw text — not a fake "null" — with a
  persistent banner naming the real parse error, reusing the existing raw-JSON editor/save flow
  (which already validates via `json.loads()` before writing) so a genuine fix saves cleanly and
  a still-broken save is still correctly rejected; (4) `_metaCopyFromPrev` now checks the new
  `errors` dict first and reports "has invalid JSON" distinctly from "not found". Verified
  directly against the real backend: a mix of one valid and one deliberately corrupted JSON file
  correctly split into `files`/`errors` with the raw broken text preserved byte-for-byte (not
  collapsed to `"null"`); a full repair round-trip (save corrected content over the broken file,
  re-fetch) correctly moved it back into `files` and out of `errors`.
- **Fixed (2026-08-14): JSON writes didn't follow the BOM-safe convention that JSON reads
  already use.** `save_metadata`, `save_sensor_sets` (server.py) and `build_sensor_defs.py`'s
  `write_json`/`sensor_sets.json` dump wrote with plain `open(path, 'w')`, no explicit
  `encoding='utf-8'` — on Windows this falls back to the locale codepage, risking silent
  mis-encoding or a crash for any non-ASCII character typed into the metadata editor. **Note
  while re-checking this:** `save_metadata` (server.py) turned out to already be fixed —
  `git log` shows `encoding='utf-8'` was added in commit `02758f4` ("Fix metadata editor:
  encoding crash on save...", 2026-08-11), six days after this entry was originally written, as
  a side effect of an unrelated crash fix; nobody circled back to mark it done here. The other
  three sites were still genuinely open: `save_sensor_sets` (server.py), and
  `build_sensor_defs.py`'s `write_json` (~181-183) and its own separate `sensor_sets.json` dump
  (~278-279) — worth noting these last two are two different write sites for the *same*
  physical file (`glider_config_files_json/sensor_sets.json`, confirmed both `_SENSOR_SETS_CFG`
  in server.py and `SENSOR_SETS_CONFIG` in build_sensor_defs.py resolve to it), so both needed
  the fix, not just one. Currently dormant in practice since none of today's live sensor-defs
  source JSONs contain non-ASCII characters, unlike `qc_report.py`'s unit labels
  (`'S m⁻¹'`, `'µmol L⁻¹'`) elsewhere in this codebase. **Fix:** added `encoding='utf-8'` to all
  three remaining write sites. Verified directly (not just by re-reading the diff): posted a
  sensor name containing µ/⁻¹/CJK characters through the real `/api/sensor-sets` endpoint and
  called `write_json()` directly with the same test data — both produced clean UTF-8 with no
  BOM, and the content round-tripped back to the exact original string on read.
- **`_current_sensor_sets()` (~1212-1238) silently defaults `trajectory_flight` to the full**
  `trajectory` sensor set if not explicitly configured in `sensor_sets.json` — flight-only
  trajectory NC files get the entire science sensor set unless someone deliberately narrows it.
- **`main.js` spawns Python via a hardcoded `'python'`**, not `python3` or a venv path
  (~line 18); also sets `webSecurity: false` on the BrowserWindow to allow `file://` iframes
  and localhost fetches.
- **Fixed (2026-08-14): no server-side stage-prerequisite gating.** `STAGE_INFO` (`app/pipeline/stages.py`,
  9 stages) defines `source_fn`/`output_fn` per stage, but nothing consumed them to gate
  execution — `run_stage()` only checked the stage exists, isn't `manual`, and isn't already
  running. A direct API call (or out-of-order UI click — no gating logic in `app.js` either)
  could run e.g. Stage 4 or 5 against an empty/stale input directory; whether that was caught
  depended entirely on the invoked script's own robustness, which is uneven (see
  `data_gap_check.py` above). **Fix:** added a `_dir_has_files()` helper (non-recursive — every
  `source_fn` directory across all 9 stages is confirmed flat, no nested subdirs) and a gate in
  `run_stage()`, right after the already-running check: if `info['source_fn'](BASE_DIR, dep)`
  doesn't exist or has zero files, reject with a 409 naming the stage and the exact missing
  path, before `command_fn` is ever built or any process is spawned. Scope note: this only
  checks "is there *any* file present," not "is it the *right kind* of file" — `dbd_to_dat` and
  `dbd_to_dat_flight` share the same `incoming_data/<dep>` source and are distinguished by
  science-vs-flight-only file content, which this gate doesn't (and isn't meant to) validate;
  the existing stage descriptions already leave that choice to the user's judgment. Also
  verified `fix_metadata`'s `source_fn == output_fn` case (it edits profile NC files in place)
  doesn't cause a false-positive block on re-runs — the check only cares whether the directory
  is non-empty, not which stage most recently wrote to it. Verified directly: every real
  prerequisite directory for an already-processed deployment (`uvi_01-20241026T1158`) correctly
  detected as populated for all of `dbd_to_dat`/`dat_to_profile_nc`/`fix_metadata`/`qc`/`report`;
  a real HTTP request against a valid-but-empty deployment name was rejected with 409 and a
  clear message, confirmed via `_procs` that nothing was ever spawned or tracked.
  **Related fix found while touching this function:** `run_stage()`'s pre-existing invalid-name,
  manual-stage, and already-running checks used bare `abort(400/409, 'message')`, which Flask
  renders as an HTML error page with no custom error handler registered — the frontend's
  `apiFetch()` only extracts an error message from a JSON body, so all three messages were
  silently dropped, leaving the user with a bare `"409 CONFLICT"` and no explanation. Converted
  all three to `jsonify({'error': ...}), <code>`, matching this file's dominant convention (and
  matching what the new gate above already uses). Verified directly: invalid-name and
  already-running both now return proper `{'error': '<real message>'}` bodies; the
  manual-stage branch is currently unreachable (no stage in `STAGE_INFO` is presently marked
  `manual: True`, that's dormant infrastructure for a not-yet-used feature) but is fixed the
  same way for whenever it is used.

## Frontend (`electron_app/renderer/app.js`) issues

- **Fixed (2026-08-14): cloud OAuth connect/switch polling and the Docker-wait banner weren't
  torn down on navigation.** `pollUntilConnected` (`app.js`, called from Switch Account and
  Connect) started a `setInterval` (2s × up to 45 tries) that only stopped via the card's own
  Cancel button or the try-cap — navigating away mid-poll left it running against a detached DOM
  node for up to 90s, and clicking Connect twice stacked duplicate intervals with no de-dup.
  Similarly, `_checkDockerBanner`'s `EventSource('/api/docker/wait')` (opened on every
  Pipeline-page visit) was only closed on its own `ready`/`timeout`/`onerror` — not on
  navigation, so a still-open one from a prior visit kept streaming until its own timeout. Both
  self-resolved and neither corrupted state, but confirmed this app has **no per-page "on leave"
  lifecycle hook at all** (checked `navigate()` — it just toggles CSS classes and calls the new
  page's renderer), so there was nowhere for cleanup to hook into. **Fix:** rather than build a
  generic lifecycle system for two known call sites, added two module-level trackers
  (`_activeCloudPoll`, `_activeDockerWait`) set when each starts and cleared when it naturally
  finishes, checked and torn down unconditionally at the top of `navigate()` regardless of
  which page is being left or entered. `pollUntilConnected` now also cancels any still-running
  previous poll before starting a new one, closing the double-click stacking gap in the same
  pass since the tracking variable was already there. Verified: `app.js` syntax-checks clean and
  the app starts/navigates without error with the change loaded (real API calls for
  state/nicknames/deployment data all succeeded on a fresh launch) — genuine interval-teardown
  timing wasn't independently exercised through the live GUI beyond that.
- **Fixed (2026-08-14): deployment rename deleted the wrong `state.depStates` cache entry.**
  The rename-save handler (`app.js`, ~6333-6359) reassigned `state.deployment` to the *new*
  name, then did `delete state.depStates[state.deployment]` — deleting the (empty) new-name
  entry instead of the actually-stale old-name one, which was left orphaned in the cache
  indefinitely. Confirmed as a genuine slip rather than intentional: the other two places this
  same cache gets invalidated elsewhere in the file both correctly delete using whichever name
  is actually relevant at that point — this was the one spot using an already-reassigned
  variable instead of capturing the old name first. Harmless in effect (nothing looks up the old
  name again after a rename), just a minor unbounded-growth contributor across a long
  rename-heavy session. **Fix:** capture `oldName = state.deployment` before the reassignment,
  delete using that instead. Verified with an isolated simulation of the exact object mutations
  both ways: the old logic really did leave the stale old-name entry behind exactly as
  described; the fixed version correctly removes it with no phantom entry created for the new
  name (which has nothing cached under it yet regardless).
- **Several `innerHTML` sinks are fed by server-derived strings instead of the codebase's
  normal safe-DOM `h()` helper**, inconsistent with the convention used almost everywhere else:
  `getStagePaths` output embedding `dep` raw (~776, low risk — deployment names are
  regex-locked at creation), `apiFetch` error messages (~1735, 1853, 1984, 2237 — server's raw
  `error` JSON field, unescaped), AD2CP recommendation text (~5436-5443), and a Leaflet popup
  built from parsed trajectory-NC GPS timestamps (~1937). None are demonstrated exploitable
  given current server-side validation, but worth normalizing if this app's threat model ever
  changes (e.g. multi-user or synced/shared deployment folders).

- **Fixed (2026-08-14): a running stage's live timer didn't survive a page rebuild, and could
  never resume once lost.** `buildStageCard()` only handled the timer display in two cases: an
  already-finished stage (`started_at` *and* `finished_at` both set) got a static final duration
  computed once; a currently-running stage (`started_at` set, `finished_at` not) had no matching
  branch at all, so `timingEl` was just left blank on rebuild. Confirmed on a real long-running
  Stage 8 (QARTOD QC) job: after the Pipeline page got rebuilt (navigating away and back is
  enough), the card correctly still showed `RUNNING` but the countdown sat blank/frozen for the
  rest of the ~2.5-hour run, even though the container was genuinely still making progress.
  **Turned out to be two genuinely different broken paths, not one, once actually traced through
  `buildStageCard()`'s existing reconnect logic** (the original fix idea above — "start a
  `setInterval` seeded from `started_at`" — was an oversimplification that only covers path 2):
  1. **Same-session navigate-away-and-back** (`activeRuns[sk]` truthy — this browser session
     started the run via `doRun()`). In-app page navigation doesn't reload the JS runtime, so
     `doRun()`'s original `setInterval` is genuinely still alive and ticking the whole time —
     the bug was that its rebuilt-card reconnect object (`stageEls[sk] = { log, badge, btnRun,
     btnAbort }`) omitted `timingEl`, and the interval looks up `stageEls[sk]?.timingEl` fresh
     every tick, so it silently found nothing to update. **Fix:** just add `timingEl` back into
     that object (plus an immediate seed from `stateEntry.started_at` so there's no blank flash
     before the next tick) — the *already-running* interval picks up the new DOM element with no
     new interval needed.
  2. **Cross-session reconnect** (real page reload, app/server restart, or a different tab —
     handled by the separate "⟳ Reconnect to logs" button / `attemptReconnect()` flow). Here
     there is no live JS execution left to reconnect to; the original `doRun()` call is long
     gone. **Fix:** a genuinely new `setInterval`, seeded from `stateEntry.started_at` (not
     `Date.now()`, so it reflects true elapsed time rather than time-since-reconnect), started
     right after the reconnect stream successfully opens and explicitly cleared in
     `applyFinishedState()` (normal completion) and the stream-error catch block (dropped
     connection) — both already-existing teardown points for this flow, so no new lifecycle
     hook was needed, just wiring the interval to them.
  Also seeded the timer immediately (not just on first tick) for the initial cross-session
  `RUNNING` render, before any reconnect click, so it shows real elapsed time right away instead
  of blank until the user clicks reconnect.

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

## Known perf issue (deferred by Travis)

- Navigating away from the AD2CP section mid-stage and back is laggy: `renderBelow(dep)`
  (`app.js`) clears the whole DOM, fires two parallel API fetches, rebuilds all stage
  cards, and reconnects SSE from scratch. Fix would be to show a cached last-known snapshot
  immediately and patch in fresh data — not yet implemented, explicitly deferred.

## Not yet in this repo — flagged proactively for planned pipeline integration

⚠️ **External MATLAB oxygen-lag-correction script (not currently part of this codebase, but
Travis is planning to port similar logic into the pipeline) silently corrupts the oxygen
correction for every up-cast profile.** Reviewed a standalone script (glider profile
CTD/oxygen post-processing: loads NC profiles, interpolates CTD + oxygen to common time
grids, optimum-lag-corrects the oxygen phase signal between paired down/up casts via
`AA_compute.m`, grids onto a pressure axis). The down/up pairing loop:
```matlab
for np = 1 : nI - 1
    df = np;   uf = np + 1;
    ...
    I69A(df).OXCor(d1:d2)  = oxdups(seg_d1:seg_d2);
    I69A(uf).OXCor(uout1:uout2) = oxdups(seg_u1:seg_u2);
end
```
pairs *every* consecutive profile index — (1,2), (2,3), (3,4), (4,5), … — not just the true
down+up pairs of a single dive cycle (1,2), (3,4), (5,6), … Since profiles alternate
down-cast/up-cast, half of these pairings — (2,3), (4,5), … — span the end of one dive's climb
and the start of the *next* dive's descent, which aren't part of the same physical cycle and
have no meaningful time-lag relationship. Because each profile is written to twice (once as
`uf` in iteration `np=k-1`, once as `df` in iteration `np=k`), and iterations run in order, the
later write always wins: traced the overwrite pattern explicitly — every **down-cast (odd)**
profile keeps its correct value (its `df`-role write always runs last), but every **up-cast
(even)** profile's final saved `OXCor` comes from the bogus pairing with the *following* dive's
down-cast, overwriting the correct value computed moments earlier from its true partner. This
isn't inert metadata — `OXCor` is the actual corrected oxygen concentration that gets gridded
into the final saved output (`OCm1`), so roughly half of all profiles (every up-cast) would
carry oxygen values computed at the wrong lag.
**Fix (for whenever this gets ported in):** step the pairing loop by 2 (`for np = 1:2:nI-1`)
so each profile is only ever paired once, with its own true down/up partner — assuming
down/up-cast alternation is confirmed to hold for the real profile-numbering scheme being used
(that assumption itself wasn't verified against real data, since this script isn't in this
repo yet).
Also flagged, lower severity: `U(j).MOtime` is set from `U(j).time` (the CTD/science computer
clock) with a commented-out line right above it showing the original intent —
`U(j).Otime` (the optode's own `oxy4_timestamp`) — meaning the oxygen samples are currently
time-aligned using the wrong clock; and the `* 10` pressure conversions
(`pressure = [...] * 10`) assume the source pressure is `sci_water_pressure` in **bar** (this
pipeline's RBR Legato³ CTD convention — see `CHANGES_FROM_RUTGERS.md`'s bar↔dbar discussion),
which would be silently wrong if ever fed a Seabird-CTD deployment's `pressure` (already dbar).
