# Changes from Rutgers rugliderqc Scripts

This document describes all modifications made to the original
[RUCOOL rugliderqc](https://github.com/rucool/rugliderqc) scripts and QC
configuration files to support the UVI glider processing pipeline.

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

**Type:** Minor — Python compatibility only

`dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

---

### 5. `rugliderqc/scripts/add_derived_variables.py`

**Type:** Minor — Python compatibility only

`dt.datetime.now(dt.UTC)` → `dt.datetime.now(dt.timezone.utc)`

---

### Unchanged scripts

| Script | Status |
|---|---|
| `check_science_variables.py` | Unchanged |
| `interpolate_depth.py` | Unchanged |
| `move_nc_files.py` | Unchanged |
| `check_duplicate_timestamps.py` | Unchanged |
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
| `scripts/glider_qartod_qc.py` | RBR support + Py3.9 compat | `pressure` hardcoded; `dt.UTC` not in Py3.9 |
| `scripts/ctd_hysteresis_test.py` | RBR support + Py3.9 compat | `pressure` hardcoded; `dt.UTC` not in Py3.9 |
| `scripts/time_shift.py` | RBR support + Py3.9 compat | `pressure` hardcoded; `dt.UTC` not in Py3.9 |
| `scripts/summarize_qartod_flags.py` | Py3.9 compat | `dt.UTC` not in Py3.9 |
| `scripts/add_derived_variables.py` | Py3.9 compat | `dt.UTC` not in Py3.9 |
| `config/global_regional/global_configs.yml` | RBR support + date range fix | Added `sci_water_pressure`; window ended 2021 |
| `config/global_regional/usvi_configs.yml` | RBR support + added density QC | Added `sci_water_pressure` and `density` streams |
| `config/gross_flatline/rbr_legato3_ctd_gross_flatline.yml` | RBR support | Added `sci_water_pressure` flat_line_test |
| `config/deployment_specific/qctests.yml` | Documentation + default fix | Comments added; `add_derived_variables` default → False |
| `Dockerfile` | Build fix | Anaconda ToS acceptance required by recent Miniconda |
