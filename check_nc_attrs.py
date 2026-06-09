import xarray as xr, os, glob, warnings
warnings.filterwarnings("ignore")

prof_dir = r"gncutils_slocum_nc_output\uvi_02-20260429T1420\profile"
files = sorted(glob.glob(os.path.join(prof_dir, "*.nc")))
sample = [files[0], files[len(files)//2], files[-1]]

ok = 0
missing = []

for f in files:
    ds = xr.open_dataset(f, decode_times=False)
    has_cdm  = ds.attrs.get("cdm_data_type") == "TrajectoryProfile"
    has_feat = ds.attrs.get("featureType") == "TrajectoryProfile"
    has_traj = ds.attrs.get("cdm_trajectory_variables") == "trajectory"
    has_traj_cf = "trajectory" in ds and ds["trajectory"].attrs.get("cf_role") == "trajectory_id"
    has_pid  = "profile_id" in ds and ds["profile_id"].attrs.get("cf_role") == "profile_id"
    ds.close()
    if has_cdm and has_feat and has_traj and has_traj_cf and has_pid:
        ok += 1
    else:
        missing.append((os.path.basename(f), has_cdm, has_feat, has_traj, has_traj_cf, has_pid))

print(f"Total files : {len(files)}")
print(f"Fully valid : {ok}")
print(f"Issues      : {len(missing)}")
if missing:
    print("\nFiles with missing attrs (cdm, feat, traj_attr, traj_cf, pid_cf):")
    for name, *flags in missing[:20]:
        print(f"  {name}  {flags}")

print("\n--- Sample detail (first / middle / last) ---")
for f in sample:
    ds = xr.open_dataset(f, decode_times=False)
    print(f"\n{os.path.basename(f)}")
    print(f"  cdm_data_type       : {ds.attrs.get('cdm_data_type', 'MISSING')}")
    print(f"  featureType         : {ds.attrs.get('featureType', 'MISSING')}")
    print(f"  cdm_traj_variables  : {ds.attrs.get('cdm_trajectory_variables', 'MISSING')}")
    traj_cf = ds["trajectory"].attrs.get("cf_role", "MISSING") if "trajectory" in ds else "NO trajectory var"
    print(f"  trajectory.cf_role  : {traj_cf}")
    pid_cf  = ds["profile_id"].attrs.get("cf_role", "MISSING") if "profile_id" in ds else "NO profile_id var"
    print(f"  profile_id.cf_role  : {pid_cf}")
    print(f"  dims                : {dict(ds.dims)}")
    ds.close()
