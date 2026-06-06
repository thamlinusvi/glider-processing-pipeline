import os
import shutil
import xarray as xr
import numpy as np

def fix_trajectoryprofile_metadata_ignore_encoding(directory):
    for fname in os.listdir(directory):
        if not fname.endswith(".nc"):
            continue

        path = os.path.join(directory, fname)
        print(f"🔍 Checking: {fname}")
        try:
            ds = xr.open_dataset(path)
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
                    # Attempt to use the length of the 'profile' dimension or fall back to time
                    dim_size = ds.dims.get("profile", ds.dims.get("time", 1))
                    ds["profile_id"] = ("profile", np.arange(1, dim_size + 1, dtype=np.int32))
                ds["profile_id"].attrs["cf_role"] = "profile_id"
                ds["profile_id"].attrs["long_name"] = "Profile ID"
                ds["profile_id"].attrs["comment"] = "Sequential profile number within the trajectory"
                print("  ✅ Added or updated profile_id with cf_role = profile_id")
                modified = True

            # Save changes
            if modified:
                tmp_path = path + ".tmp"
                ds.to_netcdf(tmp_path, mode="w")
                ds.close()
                shutil.move(tmp_path, path)
                print("  💾 Changes saved")
            else:
                ds.close()
                print("  ✅ No changes needed")

        except Exception as e:
            print(f"  ❌ Error with {fname}: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python fix_trajectoryprofile_metadata_ignore_encoding.py /path/to/nc/files")
    else:
        fix_trajectoryprofile_metadata_ignore_encoding(sys.argv[1])
