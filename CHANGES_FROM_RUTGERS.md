# Changes from Rutgers / RUCOOL Vendored Repos

This document describes all modifications made to the original
[RUCOOL rugliderqc](https://github.com/rucool/rugliderqc) and
[RUCOOL gncutils](https://github.com/rucool/gncutils) scripts and configuration
files to support the UVI glider processing pipeline.

These repos are vendored directly (no submodules) because the code has been
modified and we do not want to receive upstream updates that would overwrite
our changes.

---

## Background

The original rugliderqc scripts were written for Slocum gliders equipped with
**Seabird CTDs**, which output a variable named `pressure` in units of **dbar**.

The UVI gliders use **RBR Legato³ CTDs**, which output a variable named
`sci_water_pressure` in units of **bar** (raw instrument units). Three pipeline
scripts that directly accessed the pressure variable would fail silently or crash
on RBR data without modification.

A secondary issue was Python version compatibility: the original scripts used
`dt.UTC`, a constant only available in Python ≥ 3.11, while the Docker
environment runs Python 3.9.

---

## Script Changes

### 1. `rugliderqc/scripts/glider_qartod_qc.py`

**Type:** Substantive — RBR CTD support + Python compatibility

**What changed:**
The manual pressure gross range test hardcoded `sensor = 'pressure'`. For RBR
gliders this variable does not exist and the test would crash.

```python
# ORIGINAL
sensor = 'pressure'
# convert the depth_rating in the file (meters) to dbar...
depth_rating = float("".join(filter(str.isdigit, ds.platform.depth_rating))) * 1.05
...
```

```python
# PIPELINE
# support both 'pressure' (standard) and 'sci_water_pressure' (gncutils default)
pressure_var = next((v for v in ('pressure', 'sci_water_pressure') if v in ds.data_vars), None)
if pressure_var is None:
    logging.warning('No pressure variable found in dataset (checked "pressure", '
                    '"sci_water_pressure"). Skipping pressure gross range test.')
else:
    sensor = pressure_var
```

**Also changed:** `dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`
for Python 3.9 compatibility (`dt.UTC` was added in Python 3.11).

**Follow-up fix (2026-08-10):** the RBR-support patch above fixed the crash but left a unit
mismatch behind — `pressure_rating` (the threshold) is computed in dbar via `gsw.p_from_z`, but
for `sensor == 'sci_water_pressure'` the comparison was still running against raw bar-unit
sensor values (RBR Legato³ pressure is bar, not dbar), making the threshold ~10x too loose. A
real pressure-sensor malfunction could never be flagged SUSPECT/FAIL on any RBR/UVI deployment.
`sensor == 'pressure'` (Seabird, already dbar) was and remains unaffected.

```python
# ORIGINAL (RBR-support patch above, unit bug not yet fixed)
flag_vals = qartod.gross_range_test(inp=ds[sensor].values, **cinfo)
```

```python
# PIPELINE
# sci_water_pressure (RBR) is in bar; pressure_rating above is in dbar (1 bar = 10 dbar).
# Convert to dbar before comparison so both sides of the test share units.
pressure_values = ds[sensor].values * 10 if sensor == 'sci_water_pressure' else ds[sensor].values
flag_vals = qartod.gross_range_test(inp=pressure_values, **cinfo)
```

---

### 2. `rugliderqc/scripts/ctd_hysteresis_test.py`

**Type:** Substantive — RBR CTD support + Python compatibility

**What changed:**
Two locations in the hysteresis test directly called `apply_qartod_qc(ds, 'pressure')`.
For RBR gliders this would produce a KeyError. Both were updated to detect
whichever pressure variable is present and normalise the name for downstream use.

```python
# ORIGINAL
pressure_copy = apply_qartod_qc(ds, 'pressure')
...
pressure_copy2 = apply_qartod_qc(ds2, 'pressure')
```

```python
# PIPELINE
pressure_var = next((v for v in ('pressure', 'sci_water_pressure') if v in ds.data_vars), None)
if pressure_var is None:
    logging.warning('No pressure variable found. Skipping hysteresis test.')
    continue
pressure_copy = apply_qartod_qc(ds, pressure_var)
pressure_copy.name = 'pressure'   # normalise name for downstream code
...
pressure_var2 = next((v for v in ('pressure', 'sci_water_pressure') if v in ds2.data_vars), None)
if pressure_var2 is None:
    logging.warning('No pressure variable found in ds2. Skipping.')
    continue
pressure_copy2 = apply_qartod_qc(ds2, pressure_var2)
pressure_copy2.name = 'pressure'
```

**Also changed:** `dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

---

### 3. `rugliderqc/scripts/time_shift.py`

**Type:** Substantive — RBR CTD support + Python compatibility

**What changed:**
The `identify_nans()` helper function directly accessed `dataset.pressure.values`,
causing an `AttributeError` on RBR datasets. The function was refactored to accept
an optional pre-resolved pressure DataArray and detect the variable name dynamically
when one is not supplied.

```python
# ORIGINAL
def identify_nans(dataset, varname):
    # identify where pressure is not nan
    press_non_nan_ind = np.where(np.invert(np.isnan(dataset.pressure.values)))[0]
    ...
    df = data_copy.to_dataframe().merge(ds.pressure.to_dataframe(), on='time')
    df_all = ds[testvar].to_dataframe().merge(ds.pressure.to_dataframe(), on='time')
    if ds.pressure.values[pressure_idx][0] > ds.pressure.values[pressure_idx][-1]:
```

```python
# PIPELINE
def identify_nans(dataset, varname, pressure_da=None):
    # identify where pressure is not nan; support both 'pressure' and 'sci_water_pressure'
    if pressure_da is None:
        pressure_varname = next(
            (v for v in ('pressure', 'sci_water_pressure') if v in dataset.data_vars), 'pressure')
        press_vals = dataset[pressure_varname].values
    else:
        press_vals = pressure_da.values
    ...
    # Caller resolves pressure variable once and passes it in:
    pressure_varname = next(
        (v for v in ('pressure', 'sci_water_pressure') if v in ds.data_vars), None)
    if pressure_varname is None:
        logging.warning('No pressure variable found in {:s}. Skipping.'.format(f))
        continue
    press_da = ds[pressure_varname].copy()
    press_da.name = 'pressure'
    data_idx, pressure_idx = identify_nans(ds, testvar, press_da)
    df = data_copy.to_dataframe().merge(press_da.to_dataframe(), on='time')
    df_all = ds[testvar].to_dataframe().merge(press_da.to_dataframe(), on='time')
    if press_da.values[pressure_idx][0] > press_da.values[pressure_idx][-1]:
```

**Also changed:** `dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

---

### 4. `rugliderqc/scripts/summarize_qartod_flags.py`

**Type:** Substantive — bug fix + Python compatibility

`dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

**What changed (2026-08-10):** the script's own output variable, `f'{sensor}_qartod_summary_flag'`,
contains the substring `_qartod_`, so its variable-selection filter matched its own prior output.
A second run against a `qc_queue` file that already carried a summary flag from an earlier pass
folded that stale value into `np.maximum(summary_flag, flag)` as if it were one of the individual
tests — since `max` is monotonic, a SUSPECT/FAIL flag could never improve on a later run even
after the underlying config/data issue was corrected.

```python
# ORIGINAL
qartod_vars = [x for x in ds.data_vars if np.logical_and('_qartod_' in x, '_qartod_climatology' not in x)]
```

```python
# PIPELINE
qartod_vars = [x for x in ds.data_vars
              if np.logical_and('_qartod_' in x, '_qartod_climatology' not in x)
              and not x.endswith('_qartod_summary_flag')]
```

Verified by simulating exactly this scenario: a test flag starts at FAIL, gets corrected to GOOD,
then the script runs a second time against the dataset (with the stale FAIL-valued summary flag
still present from the first run). The original logic stayed stuck at FAIL; the fixed logic
correctly reports GOOD.

---

### 5. `rugliderqc/scripts/add_derived_variables.py`

**Type:** Substantive — RBR CTD support + Python compatibility

`dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

**What changed (2026-08-10):** unlike `glider_qartod_qc.py`/`ctd_hysteresis_test.py`/
`time_shift.py`, this script was never included in the `('pressure', 'sci_water_pressure')`
detection patch. `calculate_ph()` accessed `dataset.pressure` (attribute-style) with no
exception handling at all — an uncaught `AttributeError` on any RBR/UVI deployment with a pH
sensor, breaking the `&&`-chained Stage 8 Docker command. `calculate_omega()` called
`apply_qc(dataset, 'pressure')` (dict-style) inside a `try/except KeyError` — didn't crash, but
silently always failed for RBR gliders, since a variable literally named `pressure` never
exists on one.

```python
# ORIGINAL
pressure = dataset.pressure  # pressure in dbar (the units in the files are incorrect (bar))
...
data_dict = dict(
    ...
    pressure=apply_qc(dataset, 'pressure').values,
    ...
)
```

```python
# PIPELINE
pressure_var = next((v for v in ('pressure', 'sci_water_pressure') if v in dataset.data_vars), None)
if pressure_var is None:
    log.error('No pressure variable found in dataset (checked "pressure", "sci_water_pressure"). '
              'Cannot calculate pH.')  # / 'Cannot calculate omega.' in calculate_omega()
    return  # (or `return []` in calculate_omega())
pressure = dataset[pressure_var]  # dbar
if pressure_var == 'sci_water_pressure':
    pressure = pressure * 10  # sci_water_pressure is in bar; convert to dbar
```

Verified against realistic-calibration synthetic data: `sci_water_pressure` (bar) now produces
pH/omega output numerically identical (~1e-11 max difference) to the equivalent `pressure`
(dbar) input, and neither function raises on a dataset with no pressure variable at all.

---

### 6. `rugliderqc/scripts/check_duplicate_timestamps.py`

**Type:** Bug fix — latent crash on datasets with duplicate timestamp pairs

**Root cause:**
The script builds a sorted list of all `.nc` files once, then iterates through
it by index comparing each file `i` against `i+1`. When it finds that file `i+1`'s
timestamps are a subset of file `i`'s, it renames `i+1` to `i+1.duplicate` on disk.
On the very next iteration, `i+1` becomes the new "current" file `f` — but it has
already been renamed, so `xr.open_dataset(f)` raises `FileNotFoundError`. The
`except OSError` handler caught this and set `status = 1`, causing `sys.exit(1)`,
which broke the `&&` pipeline chain and stopped all subsequent QC steps from running.

The bug was latent because it only triggers when a deployment actually has at least
one consecutive duplicate-timestamp pair. Deployments with no duplicates never hit
the rename path, so the bug was never observed during initial testing. The
`uvi_02-20260429T1420` deployment had 5 such pairs (from field events — aborted
dives, segment restarts) which surfaced the bug for the first time.

**What changed:**
Added an existence check at the top of the inner loop so that files already renamed
to `.duplicate` by a previous iteration are silently skipped rather than treated as
read errors. Also added `ds.close()` in the `OSError` and `IndexError` handlers to
prevent dataset handle leaks.

```python
# ORIGINAL — no guard; tries to open already-renamed file, sets status = 1
for i, f in enumerate(ncfiles):
    logging.debug(f'{f}')
    try:
        ds = xr.open_dataset(f, decode_times=False)
    except OSError as e:
        logging.error('Error reading file {:s} ({:})'.format(ncfiles[i], e))
        status = 1
        continue

    try:
        f2 = ncfiles[i + 1]
        ds2 = xr.open_dataset(f2, decode_times=False)
    except OSError as e:
        logging.error('Error reading file {:s} ({:})'.format(ncfiles[i + 1], e))
        status = 1
        continue
    except IndexError:
        continue
```

```python
# PIPELINE — skip already-renamed files silently; close ds on early exit
for i, f in enumerate(ncfiles):
    logging.debug(f'{f}')

    # File may have been renamed to .duplicate by a previous iteration — skip silently
    if not os.path.isfile(f):
        continue

    try:
        ds = xr.open_dataset(f, decode_times=False)
    except OSError as e:
        logging.error('Error reading file {:s} ({:})'.format(ncfiles[i], e))
        status = 1
        continue

    try:
        f2 = ncfiles[i + 1]
        # Next file may have been renamed to .duplicate by a previous iteration — skip silently
        if not os.path.isfile(f2):
            ds.close()
            continue
        ds2 = xr.open_dataset(f2, decode_times=False)
    except OSError as e:
        logging.error('Error reading file {:s} ({:})'.format(ncfiles[i + 1], e))
        ds.close()
        status = 1
        continue
    except IndexError:
        ds.close()
        continue
```

---

### 7. `rugliderqc/scripts/ctd_hysteresis_test.py` (additional fix beyond RBR changes)

**Type:** Bug fix — latent exit-code failure on deployments with no-science profiles

**Root cause:**
Three locations set `status = 1` for expected data conditions that are logged at
DEBUG level (indicating the author considered them normal operation):
- A test variable (`conductivity` or `temperature`) not found in the current file
- No data found for the test variable in the current file
- A test variable not found in the paired down/up profile file

Any deployment with profiles that lack science variables (e.g. pure navigation
profiles from aborted dives) triggers all three for every such file, causing
`sys.exit(1)` at the end and breaking the `&&` pipeline chain.  The script's
summary correctly reports these as "not evaluated profiles" — the data handling
is fine, only the exit code was wrong.

**What changed:**
Removed `status = 1` from the three DEBUG-level skip paths.  Legitimate errors
(OSError reading a file, missing config file, zero files found) still set
`status = 1` and fail correctly.

```python
# ORIGINAL — sets status=1 for expected missing-variable conditions
except KeyError:
    logging.debug('{:s} not found in file {:s})'.format(testvar, ncfiles[i]))
    status = 1
    continue

if len(data_idx) == 0:
    logging.debug('{:s} data not found in file {:s})'.format(testvar, ncfiles[i]))
    status = 1
    continue

except KeyError:  # paired file
    logging.debug('{:s} not found in file {:s})'.format(testvar, f2))
    status = 1
    ...

# PIPELINE — skip silently, no status change
except KeyError:
    logging.debug('{:s} not found in file {:s})'.format(testvar, ncfiles[i]))
    continue  # no status = 1

if len(data_idx) == 0:
    logging.debug('{:s} data not found in file {:s})'.format(testvar, ncfiles[i]))
    continue  # no status = 1

except KeyError:  # paired file
    logging.debug('{:s} not found in file {:s})'.format(testvar, f2))
    ...     # no status = 1
```

---

### 8. `rugliderqc/scripts/time_shift.py` (additional fix beyond RBR changes)

**Type:** Bug fix — latent exit-code failures on deployments with no-science profiles or no time-shift config

**Root cause:**
Three separate `status = 1` assignments had the same pattern as the `ctd_hysteresis_test.py` fix
(Section 7) — expected data conditions logged at DEBUG/WARNING level were incorrectly treated as
pipeline failures:

1. **No `time_shift.yml` config file** (line 213, WARNING level): Any deployment that doesn't need
   time shifting (no DO or pH sensors) will not have a `time_shift.yml`.  The original code set
   `status = 1` and continued, but since status was already 1, the script exited with code 1 and
   broke the pipeline chain.  A missing config file is a valid deployment state, not an error.

2. **`testvar` KeyError in current file** (DEBUG level): Same pattern as ctd_hysteresis_test.py —
   `testvar` (e.g. `oxygen_concentration`) not present in a no-science profile, logged at DEBUG
   level but setting `status = 1`.

3. **Empty `data_idx`** (DEBUG level): No non-NaN data for `testvar` in the current file, also
   logged at DEBUG but setting `status = 1`.

**What changed:**
- Removed `status = 1` from the missing-config-file case; it now logs the warning and skips the
  deployment via `continue` (same behaviour, no pipeline failure).
- Removed `status = 1` from the two DEBUG-level skip paths (KeyError and empty data_idx).
  Legitimate errors (OSError reading files, OSError reading source_file attribute) still set
  `status = 1` correctly.

```python
# ORIGINAL — sets status=1 for missing config file and expected missing-variable conditions
if not os.path.isfile(config_file):
    logging.warning('Time shifts not calculated because deployment config file '
                    'not specified: {:s}.'.format(config_file))
    status = 1
    continue

except KeyError:
    logging.debug('{:s} not found in file {:s})'.format(testvar, f))
    status = 1
    continue

if len(data_idx) == 0:
    logging.debug('{:s} data not found in file {:s})'.format(testvar, f))
    status = 1
    continue

# PIPELINE — skip silently where appropriate, no status change
if not os.path.isfile(config_file):
    logging.warning('Time shifts not calculated because deployment config file '
                    'not specified: {:s}.'.format(config_file))
    # No config file is expected for deployments without time-shiftable sensors
    continue  # no status = 1

except KeyError:
    # Variable absent in this file (e.g. a no-science profile) — skip silently
    logging.debug('{:s} not found in file {:s})'.format(testvar, f))
    continue  # no status = 1

if len(data_idx) == 0:
    # No data for this variable in this file — skip silently
    logging.debug('{:s} data not found in file {:s})'.format(testvar, f))
    continue  # no status = 1
```

---

### 9. `rugliderqc/scripts/check_science_variables.py`

**Type:** Bug fix — nosci files silently recreated after rename, propagating through all downstream scripts

**Root cause:**
The script renames files without science variables to `<filename>.nosci` (line 169), which is correct —
all downstream scripts glob `*.nc` and would skip `*.nc.nosci` files automatically. However, `ds.to_netcdf(f)`
at line 180 runs unconditionally for **all** files in the loop, both with and without science variables.
For a just-renamed nosci file, writing back to `f` (the original `.nc` path) **recreates** the file that
was just renamed. After `check_science_variables` finishes, both the `.nosci` and the `.nc` version exist
in `qc_queue`. Every downstream script then picks up the `.nc` version and processes it, producing:

- 24 `"No pressure variable found"` warnings in `glider_qartod_qc`
- 8 `"not evaluated"` profiles in `ctd_hysteresis_test`
- Silent skips (KeyError / empty data_idx) in `time_shift`
- Wasted processing time (~20 minutes across all scripts for 24 files)

The bug was present in the original Rutgers code. It only became visible at UVI because the
`uvi_02-20260429T1420` deployment had 24 pure-navigation (`*_dbd.nc`) profiles with no science data,
compared to none in the uvi_01 test deployment.

The `stages.py` entrypoint already accounts for leftover `.nosci` files with a clean-before-run
`rm -f qc_queue/*` step; the design was always that `.nosci` files would be excluded and cleaned
up on the next run. The rename just never worked end-to-end because of the unconditional write.

**What changed:**
Added `continue` after the nosci rename/log/count block so that `ds.to_netcdf(f)` is never reached
for files without science variables.

```python
# ORIGINAL — ds.to_netcdf(f) runs unconditionally, recreating the .nc file after the rename
if len(ds_sci_vars) == 0:
    os.rename(f, f'{f}.nosci')
    logging.info('Science variables not found in file: {:s}'.format(f))
    summary += 1
else:
    modified = check_zeros(ctd_vars, ds, modified, 'conductivity', 'temperature')
    modified = check_zeros(oxygen_vars, ds, modified, 'oxygen_concentration', 'optode_water_temperature')

ds.to_netcdf(f)   # <-- runs for BOTH branches; recreates .nc for nosci files

# PIPELINE — continue skips the write-back for nosci files
if len(ds_sci_vars) == 0:
    os.rename(f, f'{f}.nosci')
    logging.info('Science variables not found in file: {:s}'.format(f))
    summary += 1
    continue   # Skip ds.to_netcdf — writing back to f would recreate the .nc file
else:
    modified = check_zeros(ctd_vars, ds, modified, 'conductivity', 'temperature')
    modified = check_zeros(oxygen_vars, ds, modified, 'oxygen_concentration', 'optode_water_temperature')

ds.to_netcdf(f)   # only reached for files that have science variables
```

---

### 10. `rugliderqc/scripts/move_nc_files.py`

**Type:** Bug fix — discarded exit code masked partial/failed moves as success

**Root cause:**
The final move out of `qc_queue` used a shell `mv` via `subprocess.call(...)`, with the return
value discarded. A permission error, full disk, or locked file left `status` at `0` regardless,
and the log message reported the pre-move `glob()` count as if all of it had moved, not what
actually did. Any file stranded in `qc_queue` after a partial failure was silently deleted by
the *next* Stage 8 run's queue-wipe, forcing an invisible full reprocess with no error anywhere
in the chain.

```python
# ORIGINAL
subprocess.call("mv " + os.path.join(data_path, 'qc_queue', '*.nc') + " " + data_path, shell=True)
logging.info('Moved {:} netcdf files to: {:s}'.format(len(ncfiles), data_path))
```

```python
# PIPELINE
moved = 0
failed = []
for f in ncfiles:
    try:
        shutil.move(f, data_path)
        moved += 1
    except OSError as e:
        failed.append(os.path.basename(f))
        logging.error('Error moving {:s}: {:}'.format(f, e))

logging.info('Moved {:} of {:} netcdf files to: {:s}'.format(moved, len(ncfiles), data_path))
if failed:
    logging.error('Failed to move {:} file(s), left in qc_queue: {:}'.format(
        len(failed), ', '.join(failed)))
    status = 1
```

Verified with a real locked-file simulation on both platforms: on Linux (the actual Docker
production target), a same-filesystem `os.rename()` failure is atomic — confirmed zero
partial/duplicate files land in the destination on failure. On Windows specifically,
`shutil.move`'s fallback path (`copy_function` + `os.unlink`, triggered whenever `os.rename`
raises `OSError` for any reason, not just cross-device moves) can leave a duplicate if the copy
succeeds but the delete is blocked by a lock — a real `shutil.move` characteristic, but not one
that applies to this script's actual runtime environment.

---

### 11. `rugliderqc/scripts/glider_qartod_qc.py` (additional change beyond RBR/unit-fix changes)

**Type:** Refactor — extracted the regional streams-test loop into a reusable function

**Root cause / motivation:**
`glider_qartod_qc` runs before `add_derived_variables` in Stage 8 (it needs to run first so
`add_derived_variables`'s `apply_qc()` can mask bad CTD/voltage values using the individual
`_qartod_*` test flags this script produces). That means any regional QC config for a variable
that only exists *after* `add_derived_variables` runs — `pH`, via `usvi_configs.yml`'s `pH`
stream block — never gets applied by this script's own pass, since it skips any sensor not yet
present in the dataset. Rather than reorder the pipeline (which would break the CTD-masking
dependency) or duplicate ~150 lines of test logic into a new script, the existing inline loop
(`for sensor, config_info in c['streams'].items(): ...`) was extracted into a standalone
function, `run_regional_stream_tests(ds, c, times, logging, sensors=None)`, so a new script
(§12 below) can reuse it scoped to just `pH` after that variable exists.

```python
# ORIGINAL — inline in main()
times = cf.convert_epoch_ts(ds['time'])
for sensor, config_info in c['streams'].items():
    if sensor not in ds.data_vars:
        continue
    # ... ~140 lines of climatology/spike/rate_of_change/pressure test logic ...
```

```python
# PIPELINE — extracted to module level, call site becomes:
times = cf.convert_epoch_ts(ds['time'])
run_regional_stream_tests(ds, c, times, logging)
```

Mechanical extraction only — no logic changed. Verified byte-identical to the original: ran
both versions inside Docker against 5 real profile files from `uvi_02-20260429T1420`, diffed
all 170 resulting `_qartod_*` variables (values and attrs), zero differences.

**Found in the process, not fixed here:** `usvi_configs.yml`'s `oxygen_concentration`/
`oxygen_saturation` blocks use `gross_range_test`/`flat_line_test`, neither of which this loop's
`if/elif` chain has ever handled — an unmatched test type falls through using stale `qc_varname`/
`flag_vals` left over from whichever test ran immediately before it, silently overwriting a
neighboring sensor's QC variable with wrong metadata. Pre-existing in the original script,
confirmed still live against current config — see `KNOWN_ISSUES.md`.

---

### 12. `rugliderqc/scripts/derived_variable_regional_qc.py` (new script, no Rutgers equivalent)

**Type:** New script — gives `pH` the regional QC pass it never got (see §11 above for why)

Added to Stage 8's chain (`app/pipeline/stages.py`'s `qc_command()`) directly after
`add_derived_variables` and before `summarize_qartod_flags`:

```python
# PIPELINE — app/pipeline/stages.py, qc_command()
_qc('add_derived_variables',        'add_derived_variables',        '-m delayed -d profile '),
_qc('derived_variable_regional_qc', 'derived_variable_regional_qc', '-m delayed -d profile '),
_qc('summarize_qartod_flags',       'summarize_qartod_flags',       '-m delayed -d profile '),
```

Imports `build_global_regional_config()` and `run_regional_stream_tests()` from
`glider_qartod_qc.py` rather than duplicating them. Since no current fleet deployment has a pH
sensor, it's designed to be a near-free no-op on every real run today: it samples `file[0]` +
the last 10 files for `pH` before doing any real work (a full per-file check across all 840
files of a real deployment measured at ~63s — the sampled check is sub-second), with a new
`ph_regional_qc_check_all_files: true` `qctests.yml` key as an escape hatch for the rare case a
pH sensor gets turned on mid-mission outside the sampled files. `usvi_configs.yml`'s `pH` block
uses `gross_range_test`, which `run_regional_stream_tests()` doesn't implement (see §11) — calling
it with a single isolated sensor and no prior sensor to coincidentally borrow a stale value from
crashes outright (`UnboundLocalError`) rather than silently falling through, so `gross_range_test`
is implemented directly in this script instead of touching the shared function.

Verified end-to-end inside Docker (real, rebuilt image, real bind-mount paths matching how
`_qc()` actually invokes scripts): synthetic-pH-injected profile produces
`pH_qartod_gross_range_test`/`pH_qartod_spike_test` matching an independent manual
`qartod.gross_range_test()`/`qartod.spike_test()` calculation exactly; `summarize_qartod_flags`
picks up both and produces `pH_qartod_summary_flag` with zero changes needed to that script; a
real pH-less deployment's other 34 pre-existing `_qartod_*` variables are untouched by the
pH-only run; the full Stage 8 chain (10 scripts, including this one) runs cleanly end-to-end
against real fresh (pre-QC) data.

---

### 13. `rugliderqc/scripts/glider_qartod_qc.py` (additional fix beyond §11)

**Type:** Bug fix — unmatched regional test types silently corrupted a different sensor's QC metadata

**Root cause:**
`run_regional_stream_tests()` (added in §11) only implements `pressure_test`/`climatology_test`/
`spike_test`/`rate_of_change_test`. `usvi_configs.yml`'s `oxygen_concentration`/
`oxygen_saturation` blocks use `gross_range_test`/`flat_line_test` — unmatched by the `if/elif`
chain. `qc_varname`/`flag_vals` are only assigned inside a matched branch, so an unmatched test
type fell through to the variable-creation step using stale values left over from whichever
test ran immediately before it — for USVI's sensor order, `density`'s `rate_of_change_test`.
Reproduced against real data: the bug silently overwrote `density_qartod_rate_of_change_test`'s
*attrs* (not its flag values, which stayed density's own correct numbers) with metadata
describing `oxygen_saturation`'s `flat_line_test` instead.

```python
# ORIGINAL (also how §11's extraction first looked)
elif test == 'rate_of_change_test':
    qc_varname = f'{sensor}_qartod_rate_of_change_test'
    ...

# Define pressure/climatology/spike/rate of change QC variable attributes
attrs = cf.set_qartod_attrs(test, sensor, cinfo)
if not hasattr(ds[sensor], 'ancillary_variables'):
    ds[sensor].attrs['ancillary_variables'] = qc_varname
...
```

```python
# PIPELINE
elif test == 'rate_of_change_test':
    qc_varname = f'{sensor}_qartod_rate_of_change_test'
    ...

else:
    # gross_range_test/flat_line_test aren't implemented by this loop. Falling through
    # without this branch reused qc_varname/flag_vals from the last matched test and
    # silently overwrote that unrelated variable's attrs -- see KNOWN_ISSUES.md.
    logging.warning(
        '{:s}: regional test type "{:s}" is configured but not implemented by this '
        'loop -- skipping. This sensor still gets QC from its own per-instrument gross/'
        'flatline config if one exists.'.format(sensor, test))
    continue

# Define pressure/climatology/spike/rate of change QC variable attributes
attrs = cf.set_qartod_attrs(test, sensor, cinfo)
...
```

Deliberately scoped as a pure bug fix (Option A), not an implementation of the missing test
types (Option B) — oxygen already gets working QC from a separate, unrelated per-instrument
config (`qc/config/gross_flatline/optode_4831_gross_flatline.yml` for this fleet's sensor), and
implementing the region-specific bounds for real would overwrite those instrument-level results
with different numbers under the same variable names — a real change to oxygen's QC outcomes,
kept as a distinct future decision rather than bundled into this fix. See `KNOWN_ISSUES.md` for
the region-specific bounds that remain configured but unapplied.

Verified against real data: of 34 real `_qartod_*` variables in a test file, 33 are
byte-identical (values and attrs) to before this fix; the one exception
(`density_qartod_rate_of_change_test`) has identical values with only its attrs now corrected.
Confirmed 4 `WARNING` log lines appear per file (2 sensors × 2 unmatched test types). Re-verified
the pH regional-QC fix (§12) and the full Stage 8 chain are both unaffected.

---

### 14. `rugliderqc/scripts/time_shift.py` (additional fix — bar/dbar unit conversion)

**Type:** Bug fix — unconverted bar-unit pressure silently distorted both the minimum-profile-span
pre-check gates and the optimal-shift computation itself for RBR deployments.

**Root cause:**
`identify_nans()`'s pressure-variable detection (§3) supports both `pressure` (Seabird, dbar) and
`sci_water_pressure` (RBR, bar), but only fixed the *variable name lookup* — the actual VALUES
were passed through unconverted under a generic `pressure` label
(`press_da = ds[pressure_varname].copy(); press_da.name = 'pressure'`). Confirmed via the
original pre-patch code shown in §3 (`dataset.pressure.values`, `ds.pressure.to_dataframe()`,
etc. — accessed unconditionally, with no unit handling anywhere) that every downstream consumer
of this value — `calculate_pressure_range()`'s two `< 3` (dbar) minimum-span gates (lines ~370,
417) and `pressure_bins(df, interval=0.25)`'s bin width (line 128, meant as 0.25 dbar) — was
written and validated exclusively against genuine Seabird dbar data, and never touched when RBR
support was added. For this fleet's `sci_water_pressure` (bar), both effects compound: the ~3
dbar gate silently becomes a ~30 dbar gate, and the 0.25 dbar bin width silently becomes a ~2.5
dbar bin width — an order of magnitude coarser than intended, degrading the resolution of the
curve-matching search that the optimal-shift value is computed from.

**Verification before fixing, since this affects an actual computed scientific value, not just a
skip/proceed gate:**
1. Confirmed the bar/dbar premise two independent ways on real data
   (`uvi_01-20241026T1158`): `sci_water_pressure`'s own NC attrs declare `units: bar`; cross-checked
   against the independently-computed `depth` variable (meters) — for a profile with max
   `sci_water_pressure` = 20.5657, max `depth` = 204.32 m, ratio = 9.93 (expected ~10, since
   1 bar ≈ 10 dbar ≈ 10 m). Not circular — `depth` comes from a separate computation path.
2. Extracted real down/up cast pairs and reproduced `pressure_bins()` + the area-between-curves
   shift search exactly (same helper functions, same 0-59 s search range) outside the pipeline,
   comparing the current bar-unit-binned result against a correctly-dbar-scaled equivalent.
   **2 of 6 real pairs tested got a different optimal shift** (39s→41s, 40s→41s); the other 4
   matched (one of those hit the 59s search boundary either way, meaning it would have fallen
   back to the previous segment's shift regardless — see the `opt_shift == 0 or == max(seconds)`
   fallback logic already in this script). Small sample, so not a precise rate, but confirms the
   effect is real and occasionally changes the actual saved shift value, not just theoretical.
3. Verified the fix (converting at the source, below) reproduces the corrected 41s result exactly
   for both pairs that differed.

```python
# ORIGINAL (post-§3 RBR patch, pre-this-fix)
press_da = ds[pressure_varname].copy()
press_da.name = 'pressure'
```

```python
# PIPELINE
press_da = ds[pressure_varname].copy()
press_da.name = 'pressure'
if pressure_varname == 'sci_water_pressure':
    # sci_water_pressure (RBR) is in bar; convert to dbar here, once, at the
    # source, so every downstream consumer (the <3 dbar pressure-range gates
    # AND pressure_bins()'s 0.25 dbar binning interval) operates on
    # correctly-scaled values. 'pressure' (Seabird) is already dbar.
    press_da = press_da * 10
    press_da.name = 'pressure'
```

Converting once at the single source of `press_da` (rather than patching each downstream literal
individually) means both the two `< 3` gates and `pressure_bins()`'s `0.25` interval are correct
with no further changes — and this is the *only* place `press_da` is constructed
(`identify_nans()`'s own internal `pressure_varname` fallback at line 107 is unreachable dead
code — its only caller always passes a pre-resolved `pressure_da`, confirmed via grep — left
untouched).

**To revert:** remove the `if pressure_varname == 'sci_water_pressure': ...` block shown above;
`press_da` returns to being the raw, unconverted source value. `KNOWN_ISSUES.md` and this section
document the evidence trail if this needs re-evaluating.

---

### 15. `rugliderqc/scripts/add_derived_variables.py` (additional fix — implicit dependency
ordering)

**Type:** Bug fix — silent failure risk if the derived-variable config is ever reordered

**Root cause:**
The single pass over `proc_vars.items()` (from `qc/config/sciencevar_processing.yml`) is not just
an iteration order — it's an implicit dependency chain, confirmed against the real config file:

```
1. oxygen_concentration_shifted   -> oxygen_concentration_shifted_mgL   (DO -> mg/L, independent)
2. sbe41n_ph_ref_voltage_shifted  -> pH                                 (needs a raw sensor var)
3. pH                             -> total_alkalinity                   (needs entry 2's OUTPUT)
4. total_alkalinity               -> aragonite_saturation_state         (needs entry 3's OUTPUT)
```

Entries 3 and 4 need a variable that only exists because an *earlier* entry in the same loop pass
computed it and wrote it into `ds` a few lines below (`ds[vardict['nc_var_name']] = da`). This
only produced correct output because the YAML happens to list all four entries in exact
dependency order. Each precursor lookup was a bare `except KeyError: continue`, so reordering the
YAML, or inserting a new derived variable above `pH`'s entry, would have silently broken the
whole pH/TA/Omega chain with zero log output to explain why.

**What changed:** replaced the single pass with a retry loop that keeps re-attempting only the
still-unresolved entries across multiple passes, stopping when a full pass makes no further
progress. Deliberately *not* just adding a log line to the existing `except KeyError`: that
branch fires just as often for the correct, common case (a deployment with no pH sensor, so
`sbe41n_ph_ref_voltage_shifted` never exists) as it would for the real bug (a precursor that
exists but hasn't been computed *yet*) — logging every occurrence would drown the real signal in
noise. A multi-pass retry is correct for either case without needing to tell them apart: if a
full pass adds nothing new, every remaining entry's precursor is genuinely absent, not just
not-computed-yet.

```python
# ORIGINAL
for variable_name, vardict in proc_vars.items():
    # check if the variable required for additional processing is in the file
    try:
        ds[variable_name]
    except KeyError:
        continue
    # ... calculate, add to ds, run QC ...
```

```python
# PIPELINE
remaining_vars = dict(proc_vars)
made_progress = True
while remaining_vars and made_progress:
    made_progress = False
    still_remaining = {}
    for variable_name, vardict in remaining_vars.items():
        try:
            ds[variable_name]
        except KeyError:
            still_remaining[variable_name] = vardict
            continue
        made_progress = True
        # ... calculate, add to ds, run QC (unchanged) ...
    remaining_vars = still_remaining
```

**Verified three ways** with an isolated reproduction of the exact algorithm (`ioos_qc` isn't
available on the host Python environment, so the real script can't be run directly outside
Docker):
1. A deliberately reordered `proc_vars` (TA and Omega entries placed *before* the pH entry, the
   worst case this fix targets) resolves all four variables correctly across multiple passes —
   proves the fix genuinely fixes the ordering dependency, not just theoretically.
2. A genuinely missing precursor (no pH sensor — `sbe41n_ph_ref_voltage_shifted` absent from
   `ds`) terminates cleanly after exactly one pass, correctly leaving the entire downstream
   pH/TA/Omega chain unresolved — no infinite loop, no fabricated output.
3. The current, already-correctly-ordered real config produces an identical resolution order to
   the original single-pass behavior — confirmed no regression for the working case.

**To revert:** replace the `while`/retry block with the original single
`for variable_name, vardict in proc_vars.items():` pass shown above.

---

### Unchanged scripts

| Script | Status |
|---|---|
| `interpolate_depth.py` | Unchanged |
| `manual_qcflag.py` | Unchanged |
| `data_gap_check.py` | Unchanged |
| `ioos_qc_mods/qartod.py` | Unchanged (same patch as Rutgers original) |

---

## QC Configuration File Changes

### 1. `qc/config/global_regional/global_configs.yml`

**Two changes:**

**a) Time window extended:**
The original window ended `2021-12-31`, which would cause all post-2021 deployments
to fall back to no time-windowed config.

```yaml
# ORIGINAL
ending: 2021-12-31T23:59:59.999999

# PIPELINE
ending: 2030-12-31T23:59:59.999999
```

**b) `sci_water_pressure` stream added:**
The original file only had a `pressure` stream. For RBR gliders, `sci_water_pressure`
(in bar) is the pressure variable. Spike and rate-of-change thresholds were scaled
down by a factor of ~10 to account for bar vs dbar units.

```yaml
# ADDED
sci_water_pressure:
    qartod:
        pressure_test:
            suspect_threshold: 0
        spike_test:
            suspect_threshold: 0.02
            fail_threshold: 0.045
        rate_of_change_test:
            threshold: 0.045
```

---

### 2. `qc/config/global_regional/usvi_configs.yml`

**Two additions:**

**a) `sci_water_pressure` stream added** with bar-scaled thresholds:

```yaml
# ADDED
# sci_water_pressure: raw RBR pressure in bar (gncutils naming).
# Thresholds are pressure stream values scaled ÷10 for bar units.
sci_water_pressure:
  qartod:
    pressure_test:
      suspect_threshold: 0
    spike_test:
      suspect_threshold: 0.03
      fail_threshold: 0.05
    rate_of_change_test:
      threshold: 0.05
```

**b) `density` stream added** with USVI-appropriate climatology bounds:

```yaml
# ADDED
density:
  qartod:
    climatology_test:
      suspect_span: [1023, 1027]
      fail_span: [1020, 1030]
    spike_test:
      suspect_threshold: 0.025
      fail_threshold: 0.3
    rate_of_change_test:
      threshold: 0.5
```

---

### 3. `qc/config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml`

**`sci_water_pressure` flat_line_test added:**
The original file only had a `pressure` flat_line_test entry. `sci_water_pressure`
was added so flat-line detection runs on the RBR pressure variable.

```yaml
# ADDED
sci_water_pressure:
    qartod:
        flat_line_test:
            tolerance: 0.001
            suspect_threshold: 120
            fail_threshold: 300
```

> **Note:** The `pressure` entry remains in this file and will generate a harmless
> `pressure is not a variable in the xarray dataset, skipping` warning for RBR
> deployments. It can safely be removed for RBR-only pipelines.

---

### 4. `qc/config/deployment_specific/qctests.yml` (template)

**Default and documentation changes only:**

- Added inline comments explaining each test flag
- Changed template default `add_derived_variables` from `True` → `False`
  (derived variables require a separate config file; off by default is safer)

---

---

## gncutils Changes

### 1. `gncutils/gncutils/writers/BaseTrajectoryNetCDFWriter.py`

**Type:** Defensive — UTF-8 BOM tolerance

**What changed:**
Four `open()` calls that read JSON config files (`sensor_defs.json`,
`deployment.json`, `global_attributes.json`, `instruments.json`) used the
default system encoding. On Windows, files saved by tools like Notepad are
often written with a UTF-8 BOM (`\xef\xbb\xbf`). Python's default `json.load()`
treats the BOM as the first character and raises `"Expecting value: line 1
column 1 (char 0)"`, causing the NetCDF writer to silently skip the affected
config (logging an error but continuing with empty/default values).

All four reads were updated to use `encoding='utf-8-sig'`, which automatically
strips the BOM when present and is a no-op for files without one.

```python
# ORIGINAL (all four reads)
with open(self._sensor_defs_config_path, 'r') as fid:
    self._config_sensor_defs = json.load(fid)

with open(self._deployment_config_path, 'r') as fid:
    deployment_configs = json.load(fid)

with open(self._global_attributes_path, 'r') as fid:
    global_atts = json.load(fid)

with open(self._instruments_config_path, 'r') as fid:
    instrument_configs = json.load(fid)
```

```python
# PIPELINE (utf-8-sig on all four reads)
with open(self._sensor_defs_config_path, 'r', encoding='utf-8-sig') as fid:
    self._config_sensor_defs = json.load(fid)

with open(self._deployment_config_path, 'r', encoding='utf-8-sig') as fid:
    deployment_configs = json.load(fid)

with open(self._global_attributes_path, 'r', encoding='utf-8-sig') as fid:
    global_atts = json.load(fid)

with open(self._instruments_config_path, 'r', encoding='utf-8-sig') as fid:
    instrument_configs = json.load(fid)
```

**Lines changed:** `_load_sensor_defs()` (line 963) and `_read_configs()`
(lines 1064, 1072, 1081).

---

## Docker Changes

### `Dockerfile`

Anaconda's recent Miniconda releases require Terms of Service acceptance before
`conda env create` can run. Added two `RUN` lines after the Miniconda install step:

```dockerfile
# ADDED — Accept Anaconda ToS (required by recent Miniconda versions)
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main && \
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
```

Without these lines the Docker build fails at the `conda env create` step.

---

## Summary Table

| File | Change type | Reason |
|---|---|---|
| `scripts/glider_qartod_qc.py` | RBR support + Py3.9 compat + unit fix | `pressure` hardcoded; `dt.UTC` not in Py3.9; gross-range threshold compared bar values against a dbar span |
| `scripts/ctd_hysteresis_test.py` | RBR support + Py3.9 compat | `pressure` hardcoded; `dt.UTC` not in Py3.9 |
| `scripts/time_shift.py` | RBR support + Py3.9 compat | `pressure` hardcoded; `dt.UTC` not in Py3.9 |
| `scripts/summarize_qartod_flags.py` | Bug fix + Py3.9 compat | Own summary-flag output matched its own input filter, ratcheting flags to only get worse on a re-run; `dt.UTC` not in Py3.9 |
| `scripts/add_derived_variables.py` | RBR support + Py3.9 compat | `pressure` hardcoded (crashed calculate_ph, silently broke calculate_omega for RBR); `dt.UTC` not in Py3.9 |
| `scripts/check_duplicate_timestamps.py` | Bug fix | Latent crash when renamed `.duplicate` file re-opened as "current" in next iteration; set `status=1` and broke pipeline chain |
| `scripts/check_science_variables.py` | Bug fix | `ds.to_netcdf(f)` ran unconditionally, recreating the `.nc` file that was just renamed to `.nosci`; all 24 nosci profiles propagated through every downstream script |
| `scripts/move_nc_files.py` | Bug fix | Shell `mv`'s exit code was discarded; a partial/failed move looked like success and the log count didn't reflect what actually moved |
| `scripts/glider_qartod_qc.py` (again) | Refactor | Extracted regional streams-test loop into `run_regional_stream_tests()` so a new script could reuse it for `pH`; verified byte-identical |
| `scripts/derived_variable_regional_qc.py` | New script | `pH` never got regional QC since it doesn't exist until after `glider_qartod_qc` runs; near-free no-op today, no deployment has pH yet |
| `scripts/glider_qartod_qc.py` (again) | Bug fix | Unmatched regional test types (oxygen's `gross_range_test`/`flat_line_test`) silently corrupted `density`'s QC-variable metadata; now skipped with a warning instead |
| `config/global_regional/global_configs.yml` | RBR support + date range fix | Added `sci_water_pressure`; window ended 2021 |
| `config/global_regional/usvi_configs.yml` | RBR support + added density QC | Added `sci_water_pressure` and `density` streams |
| `config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml` | RBR support | Added `sci_water_pressure` flat_line_test |
| `config/deployment_specific/qctests.yml` | Documentation + default fix | Comments added; `add_derived_variables` default → False |
| `Dockerfile` | Build fix | Anaconda ToS acceptance required by recent Miniconda |
| **gncutils** | | |
| `gncutils/writers/BaseTrajectoryNetCDFWriter.py` | BOM tolerance | 4 JSON reads updated to `utf-8-sig`; BOM-prefixed files (e.g. from Notepad) were silently skipping config |
