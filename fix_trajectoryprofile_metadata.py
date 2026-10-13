import os
import sys
import shutil
import warnings
import xarray as xr
import numpy as np

# Suppress harmless xarray RuntimeWarnings about all-NaN time slices
# (happens when a profile has no valid timestamps — the data is still usable)
warnings.filterwarnings('ignore', category=RuntimeWarning, module='xarray')

# Detect the best available NetCDF engine (same logic as fix_cdm_profile_variables.py).
# netCDF4's C library has a numpy ABI mismatch on this server; h5netcdf avoids it.
_ENGINE = None
try:
    import netCDF4  # noqa: F401
    _ENGINE = 'netCDF4'
except (ImportError, ValueError):
    pass

if _ENGINE is None:
    try:
        import h5netcdf  # noqa: F401
        _ENGINE = 'h5netcdf'
    except (ImportError, ValueError):
        pass

# None → xarray picks its own default (last resort)


def fix_trajectoryprofile_metadata_ignore_encoding(directory):
    # Remove any stale .tmp files left by a previous crashed run
    for f in os.listdir(directory):
        if f.endswith(".nc.tmp"):
            stale = os.path.join(directory, f)
            os.remove(stale)
            print(f"  🧹 Removed stale temp file: {f}")

    errors = 0
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".nc"):
            continue

        path = os.path.join(directory, fname)
        print(f"🔍 Checking: {fname}")
        tmp_path = path + ".tmp"
        try:
            kwargs = dict(decode_times=False)
            if _ENGINE:
                kwargs['engine'] = _ENGINE
            # decode_times=False: keep time as raw numeric values so xarray
            # never tries to compare NoneType with datetime when re-encoding.
            # We only modify global attributes here — time data is untouched.
            ds = xr.open_dataset(path, **kwargs)
            modified = False

            # Set required global attributes
            if ds.attrs.get("cdm_data_type") != "TrajectoryProfile":
                ds.attrs["cdm_data_type"] = "TrajectoryProfile"
                print("  ✅ Set global attribute: cdm_data_type = TrajectoryProfile")
                modified = True

            if ds.attrs.get("featureType") != "TrajectoryProfile":
                ds.attrs["featureType"] = "TrajectoryProfile"
                print("  ✅ Set global attribute: featureType = TrajectoryProfile")
                modified = True

            if ds.attrs.get("cdm_trajectory_variables") != "trajectory":
                ds.attrs["cdm_trajectory_variables"] = "trajectory"
                print("  ✅ Set global attribute: cdm_trajectory_variables = trajectory")
                modified = True

            # Ensure trajectory variable has cf_role
            if "trajectory" in ds.variables:
                if ds["trajectory"].attrs.get("cf_role") != "trajectory_id":
                    ds["trajectory"].attrs["cf_role"] = "trajectory_id"
                    print("  ✅ Set cf_role = trajectory_id on trajectory variable")
                    modified = True
            else:
                print("  ⚠️ WARNING: 'trajectory' variable not found")

            # Ensure profile_id variable with cf_role exists
            if not any(v.attrs.get("cf_role") == "profile_id" for v in ds.variables.values()):
                if "profile_id" not in ds:
                    pid = None
                    if "profile_time" in ds.variables:
                        pt = float(ds["profile_time"].values.flat[0])
                        if not np.isnan(pt):
                            pid = int(pt)
                    if pid is None:
                        pid = 1
                    ds["profile_id"] = ((), np.int32(pid))
                ds["profile_id"].attrs["cf_role"] = "profile_id"
                ds["profile_id"].attrs["long_name"] = "Profile ID"
                ds["profile_id"].attrs["comment"] = "Sequential profile number within the trajectory"
                print("  ✅ Added or updated profile_id with cf_role = profile_id")
                modified = True

            # Save changes atomically: load into memory, close source, write tmp, rename
            if modified:
                try:
                    ds.load()   # pull all data into memory
                    ds.close()  # release HDF5 file handle before writing
                    write_kwargs = dict(mode="w")
                    if _ENGINE:
                        write_kwargs['engine'] = _ENGINE
                    ds.to_netcdf(tmp_path, **write_kwargs)
                    shutil.move(tmp_path, path)
                    print("  💾 Changes saved")
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    raise
            else:
                ds.close()
                print("  ✅ No changes needed")

        except Exception as e:
            print(f"  ❌ Error with {fname}: {e}")
            errors += 1

    if errors:
        print(f"\n✖ Finished with {errors} error(s).")
        sys.exit(1)
    else:
        print("\n✔ All files processed successfully.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python fix_trajectoryprofile_metadata.py /path/to/nc/files")
        sys.exit(1)
    else:
        fix_trajectoryprofile_metadata_ignore_encoding(sys.argv[1])
