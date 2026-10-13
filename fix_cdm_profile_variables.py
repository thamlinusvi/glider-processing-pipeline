"""
Add/fix cdm_profile_variables = "profile_id" on all profile NetCDF files
for a deployment, across every directory where the pipeline writes them.

Usage:
    python fix_cdm_profile_variables.py <base_data_dir> <deployment_name>

Example (server):
    python fix_cdm_profile_variables.py "E:\Glider_Lab\GliderDataProcessing" "uvi_02-20260429T1420"

Searches:
    gncutils_slocum_nc_output/<deployment>/profile/           (Stage 3 output, pre-QC)
    deployments/<year>/<deployment>/data/out/nc/sci-profile/delayed/          (post-QC final)
    deployments/<year>/<deployment>/data/out/nc/sci-profile/delayed/qc_queue/ (in-progress QC)

Only modifies files whose cdm_data_type is "TrajectoryProfile".
Trajectory files (cdm_data_type="Trajectory") are left untouched.
Skips files that already have cdm_profile_variables = "profile_id".
"""

import sys
import os
import glob

_BACKEND = None

try:
    import netCDF4 as nc
    _BACKEND = 'netCDF4'
except (ImportError, ValueError):
    pass

if _BACKEND is None:
    try:
        import xarray as xr
        import h5netcdf  # noqa: F401 — verify engine is available
        _BACKEND = 'h5netcdf'
    except (ImportError, ValueError):
        pass

if _BACKEND is None:
    try:
        import xarray as xr
        _BACKEND = 'xarray'
    except ImportError:
        pass

if _BACKEND is None:
    print("ERROR: no working NetCDF backend found (tried netCDF4, h5netcdf, xarray).")
    sys.exit(1)


TARGET_ATTR  = 'cdm_profile_variables'
TARGET_VALUE = 'profile_id'
REQUIRED_CDM = 'TrajectoryProfile'


def fix_file_netcdf4(path):
    try:
        with nc.Dataset(path, 'r+') as ds:
            cdm = getattr(ds, 'cdm_data_type', None)
            if cdm != REQUIRED_CDM:
                return 'skipped', f'cdm_data_type={cdm!r}'
            current = getattr(ds, TARGET_ATTR, None)
            if current == TARGET_VALUE:
                return 'skipped', 'already correct'
            ds.setncattr(TARGET_ATTR, TARGET_VALUE)
            return 'fixed', f'was {current!r}'
    except Exception as e:
        return 'error', str(e)


def fix_file_xarray(path, engine=None):
    tmp = path + '.tmp_fix'
    try:
        import xarray as xr
        kwargs = dict(decode_times=False)
        if engine:
            kwargs['engine'] = engine
        ds = xr.open_dataset(path, **kwargs)
        cdm = ds.attrs.get('cdm_data_type')
        if cdm != REQUIRED_CDM:
            ds.close()
            return 'skipped', f'cdm_data_type={cdm!r}'
        current = ds.attrs.get(TARGET_ATTR)
        if current == TARGET_VALUE:
            ds.close()
            return 'skipped', 'already correct'
        ds.load()   # pull all data into memory
        ds.close()  # release source file handle before writing
        ds.attrs[TARGET_ATTR] = TARGET_VALUE
        write_kwargs = {}
        if engine:
            write_kwargs['engine'] = engine
        ds.to_netcdf(tmp, **write_kwargs)
        os.replace(tmp, path)
        return 'fixed', f'was {current!r}'
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return 'error', str(e)


def fix_file(path):
    if _BACKEND == 'netCDF4':
        return fix_file_netcdf4(path)
    if _BACKEND == 'h5netcdf':
        return fix_file_xarray(path, engine='h5netcdf')
    return fix_file_xarray(path)


def _year(deployment):
    return deployment.split('-')[1][:4]


def collect_nc_files(base_dir, deployment):
    year = _year(deployment)

    search_dirs = [
        # Stage 3 profile NC output (pre-QC)
        os.path.join(base_dir, 'gncutils_slocum_nc_output', deployment, 'profile'),
        # Post-QC final output (move_nc_files promotes from qc_queue → delayed/)
        os.path.join(base_dir, 'deployments', year, deployment,
                     'data', 'out', 'nc', 'sci-profile', 'delayed'),
        # In-progress QC staging area (in case QC is mid-run or was interrupted)
        os.path.join(base_dir, 'deployments', year, deployment,
                     'data', 'out', 'nc', 'sci-profile', 'delayed', 'qc_queue'),
    ]

    found = []
    for d in search_dirs:
        if not os.path.isdir(d):
            print(f"  (not found)  {d}")
            continue
        matches = glob.glob(os.path.join(d, '**', '*.nc'), recursive=True)
        matches = [f for f in matches
                   if not f.endswith('.tmp_fix') and not f.endswith('.nosci')]
        print(f"  {len(matches):4d} files  {d}")
        found.extend(matches)

    # deduplicate in case qc_queue is inside delayed/ (it is — avoid double-counting)
    return sorted(set(found))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    base_dir   = sys.argv[1]
    deployment = sys.argv[2]

    if not os.path.isdir(base_dir):
        print(f"ERROR: base data directory not found: {base_dir}")
        sys.exit(1)

    print(f"Backend    : {_BACKEND}")
    print(f"Base dir   : {base_dir}")
    print(f"Deployment : {deployment}")
    print(f"Searching:")
    nc_files = collect_nc_files(base_dir, deployment)
    print()

    if not nc_files:
        print("No .nc files found. Check the deployment name and that pipeline stages have run.")
        sys.exit(0)

    print(f"Total .nc files : {len(nc_files)}")
    print(f"Fixing          : {TARGET_ATTR} = \"{TARGET_VALUE}\" where cdm_data_type=\"{REQUIRED_CDM}\"")
    print()

    fixed  = []
    errors = []

    for path in nc_files:
        rel = os.path.relpath(path, base_dir)
        action, reason = fix_file(path)
        if action == 'fixed':
            fixed.append(rel)
            print(f"  FIXED   {rel}  ({reason})")
        elif action == 'error':
            errors.append((rel, reason))
            print(f"  ERROR   {rel}  {reason}")

    print()
    skipped = len(nc_files) - len(fixed) - len(errors)
    print(f"Summary: {len(fixed)} fixed, {skipped} skipped (already correct or not TrajectoryProfile), {len(errors)} errors")
    if errors:
        print("\nErrors:")
        for rel, reason in errors:
            print(f"  {rel}: {reason}")
        sys.exit(1)


if __name__ == '__main__':
    main()
