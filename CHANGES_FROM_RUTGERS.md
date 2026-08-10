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
| `config/global_regional/global_configs.yml` | RBR support + date range fix | Added `sci_water_pressure`; window ended 2021 |
| `config/global_regional/usvi_configs.yml` | RBR support + added density QC | Added `sci_water_pressure` and `density` streams |
| `config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml` | RBR support | Added `sci_water_pressure` flat_line_test |
| `config/deployment_specific/qctests.yml` | Documentation + default fix | Comments added; `add_derived_variables` default → False |
| `Dockerfile` | Build fix | Anaconda ToS acceptance required by recent Miniconda |
| **gncutils** | | |
| `gncutils/writers/BaseTrajectoryNetCDFWriter.py` | BOM tolerance | 4 JSON reads updated to `utf-8-sig`; BOM-prefixed files (e.g. from Notepad) were silently skipping config |
