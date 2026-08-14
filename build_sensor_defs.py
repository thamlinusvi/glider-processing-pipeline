"""
build_sensor_defs.py

Generate trimmed sensor_defs.json files for a specific mission from the master
raw JSON sources.  Run this OUTSIDE Docker (on the host) with updated paths.

Usage:
    python build_sensor_defs.py

Outputs:
    glider_config_files_json/updated_sensor_defs/profile/sensor_defs.json
    glider_config_files_json/updated_sensor_defs/trajectory/sensor_defs.json
    glider_config_files_json/updated_sensor_defs/trajectory_flight/sensor_defs.json

Then copy them into deployment config dirs via "Copy Sensor Defs" in the app, or manually:
    glider_config_files_json/<glider>/<mission>/science_profile/sensor_defs.json
    glider_config_files_json/<glider>/<mission>/science_trajectory/sensor_defs.json
    glider_config_files_json/<glider>/<mission>/trajectory_flight/sensor_defs.json
"""

import os
import json

# -----------------------------
# Paths  (update as needed)
# -----------------------------
SENSOR_DEF_DIR = os.path.join(os.path.dirname(__file__),
                               "glider_config_files_json", "raw_json")

# Optional JSON config that overrides the hardcoded sensor sets below.
# Written automatically after each build so the app can read/edit it.
SENSOR_SETS_CONFIG = os.path.join(os.path.dirname(__file__),
                                   "glider_config_files_json", "sensor_sets.json")

BASE_SENSOR_DEFS = os.path.join(SENSOR_DEF_DIR, "trajectory-sensor_defs.json")
SCI_SENSOR_DEFS  = os.path.join(SENSOR_DEF_DIR, "sci-sensor_defs.json")

OUTPUT_PROFILE            = os.path.join(os.path.dirname(__file__),
                                          "glider_config_files_json",
                                          "updated_sensor_defs", "profile")
OUTPUT_TRAJECTORY         = os.path.join(os.path.dirname(__file__),
                                          "glider_config_files_json",
                                          "updated_sensor_defs", "trajectory")
OUTPUT_TRAJECTORY_FLIGHT  = os.path.join(os.path.dirname(__file__),
                                          "glider_config_files_json",
                                          "updated_sensor_defs", "trajectory_flight")

os.makedirs(OUTPUT_PROFILE,           exist_ok=True)
os.makedirs(OUTPUT_TRAJECTORY,        exist_ok=True)
os.makedirs(OUTPUT_TRAJECTORY_FLIGHT, exist_ok=True)

# -----------------------------
# Variable sets
# -----------------------------
PROFILE_SENSORS = set([
    "crs",
    "density",
    "llat_depth",
    "llat_latitude",
    "llat_longitude",
    "llat_time",
    "m_altitude",
    "m_pitch",
    "m_roll",
    "m_science_clothesline_lag",
    "m_water_depth",
    "platform",
    "potential_temperature",
    "profile_id",
    "profile_lat",
    "profile_lon",
    "profile_time",
    "salinity",
    "sci_m_present_time",
    "sci_oxy4_c1rph",
    "sci_oxy4_c2rph",
    "sci_oxy4_calphase",
    "sci_oxy4_is_installed",
    "sci_oxy4_oxygen",
    "sci_oxy4_rawtemp",
    "sci_oxy4_saturation",
    "sci_oxy4_tcphase",
    "sci_oxy4_temp",
    "sci_oxy4_timestamp",
    "sci_software_ver",
    "sci_water_cond",
    "sci_water_pressure",
    "sci_water_temp",
    "sound_speed",
    "m_final_water_vx",
    "m_final_water_vy",
])

TRAJECTORY_SENSORS = set([
    "c_fin",
    "c_heading",
    "c_oxy4_num_fields_to_send",
    "c_oxy4_on",
    "c_pitch",
    "c_roll",
    "c_wpt_lat",
    "c_wpt_lon",
    "crs",
    "llat_depth",
    "llat_latitude",
    "llat_longitude",
    "llat_time",
    "m_altitude",
    "m_appear_to_be_at_surface",
    "m_coulomb_amphr",
    "m_depth",
    "m_fin",
    "m_final_water_vx",
    "m_final_water_vy",
    "m_gps_full_status",
    "m_gps_lat",
    "m_gps_lon",
    "m_gps_status",
    "m_heading",
    "m_iridium_call_num",
    "m_iridium_redials",
    "m_iridium_signal_strength",
    "m_lat",
    "m_lon",
    "m_pitch",
    "m_present_time",
    "m_pressure",
    "m_roll",
    "m_science_clothesline_lag",
    "m_vacuum",
    "m_water_depth",
    "m_water_vx",
    "m_water_vy",
    "platform",
    "sci_m_present_time",
    "sci_oxy4_c1amp",
    "sci_oxy4_c1rph",
    "sci_oxy4_c2rph",
    "sci_oxy4_calphase",
    "sci_oxy4_is_installed",
    "sci_oxy4_oxygen",
    "sci_oxy4_rawtemp",
    "sci_oxy4_saturation",
    "sci_oxy4_tcphase",
    "sci_oxy4_temp",
    "sci_oxy4_timestamp",
    "sci_software_ver",
    "sci_water_cond",
    "sci_water_pressure",
    "sci_water_temp",
    "u_oxy4_slow_surface_mode",
    "x_software_ver",
    "m_battery",
    # Comment out if no ADCP on this deployment:
    #"sci_ad2cp_bottom_track_signal",
    #"sci_ad2cp_file_state",
    #"sci_ad2cp_is_installed",
    #"sci_ad2cp_run_state",
    #"sci_ad2cp_surface_state",
])

# Flight-only trajectory sensors (DBD-only deployments — no science data).
# These mirror TRAJECTORY_SENSORS but exclude sci_* variables.
TRAJECTORY_FLIGHT_SENSORS = set([
    s for s in TRAJECTORY_SENSORS if not s.startswith('sci_')
])

# -----------------------------
# Helpers
# -----------------------------
def load_json_dict(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing required file: {path}")
    with open(path, "r", encoding='utf-8-sig') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON must be a dict: {path}")
    return data


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def build_profile_sensor_defs(base_defs, sci_defs):
    needed = PROFILE_SENSORS
    out = {}
    for k in needed:
        if k in base_defs:
            out[k] = base_defs[k]
        elif k in sci_defs:
            out[k] = sci_defs[k]
    missing = needed - set(base_defs.keys()) - set(sci_defs.keys())
    out = {k: out[k] for k in sorted(out.keys())}
    return out, missing


def build_trajectory_sensor_defs(base_defs, sci_defs):
    needed = TRAJECTORY_SENSORS
    out = {}
    for k in needed:
        if k in base_defs:
            out[k] = base_defs[k]
        elif k in sci_defs:
            out[k] = sci_defs[k]
    pulled_from_sci = (needed - set(base_defs.keys())) & set(sci_defs.keys())
    still_missing   = needed - set(base_defs.keys()) - set(sci_defs.keys())
    out = {k: out[k] for k in sorted(out.keys())}
    return out, pulled_from_sci, still_missing


def build_trajectory_flight_sensor_defs(base_defs, sci_defs):
    needed = TRAJECTORY_FLIGHT_SENSORS
    out = {}
    for k in needed:
        if k in base_defs:
            out[k] = base_defs[k]
        elif k in sci_defs:
            out[k] = sci_defs[k]
    missing = needed - set(base_defs.keys()) - set(sci_defs.keys())
    out = {k: out[k] for k in sorted(out.keys())}
    return out, missing


def main():
    # Override hardcoded sets if a saved config exists
    global PROFILE_SENSORS, TRAJECTORY_SENSORS, TRAJECTORY_FLIGHT_SENSORS
    if os.path.isfile(SENSOR_SETS_CONFIG):
        try:
            with open(SENSOR_SETS_CONFIG, encoding='utf-8-sig') as f:
                cfg = json.load(f)
            if 'profile' in cfg:
                PROFILE_SENSORS = set(cfg['profile'])
            if 'trajectory' in cfg:
                TRAJECTORY_SENSORS = set(cfg['trajectory'])
            # trajectory_flight defaults to trajectory minus sci_* if not set
            TRAJECTORY_FLIGHT_SENSORS = set(cfg.get(
                'trajectory_flight',
                [s for s in TRAJECTORY_SENSORS if not s.startswith('sci_')]
            ))
        except Exception as e:
            print(f"  Warning: could not load sensor_sets.json ({e}); using defaults")

    base_defs = load_json_dict(BASE_SENSOR_DEFS)
    sci_defs  = load_json_dict(SCI_SENSOR_DEFS)

    # ---- Profile ----
    profile_out, profile_missing = build_profile_sensor_defs(base_defs, sci_defs)
    profile_path = os.path.join(OUTPUT_PROFILE, "sensor_defs.json")
    write_json(profile_path, profile_out)
    print(f"Wrote PROFILE sensor_defs.json         -> {profile_path}  ({len(profile_out)} keys)")
    if profile_missing:
        print(f"  Missing from both trajectory- and sci-sensor_defs.json: {sorted(profile_missing)}")

    print()

    # ---- Trajectory (Science) ----
    traj_out, pulled_from_sci, traj_missing = build_trajectory_sensor_defs(base_defs, sci_defs)
    traj_path = os.path.join(OUTPUT_TRAJECTORY, "sensor_defs.json")
    write_json(traj_path, traj_out)
    print(f"Wrote TRAJECTORY (science) sensor_defs -> {traj_path}  ({len(traj_out)} keys)")
    if pulled_from_sci:
        print(f"  Pulled from sci_sensor_defs.json: {sorted(pulled_from_sci)}")
    if traj_missing:
        print(f"  Still missing (not in either file): {sorted(traj_missing)}")

    print()

    # ---- Trajectory (Flight) ----
    traj_flt_out, traj_flt_missing = build_trajectory_flight_sensor_defs(base_defs, sci_defs)
    traj_flt_path = os.path.join(OUTPUT_TRAJECTORY_FLIGHT, "sensor_defs.json")
    write_json(traj_flt_path, traj_flt_out)
    print(f"Wrote TRAJECTORY (flight) sensor_defs  -> {traj_flt_path}  ({len(traj_flt_out)} keys)")
    if traj_flt_missing:
        print(f"  Still missing (not in either file): {sorted(traj_flt_missing)}")

    # Persist current sensor sets so the app can read/edit them
    cfg_data = {
        'profile':           sorted(PROFILE_SENSORS),
        'trajectory':        sorted(TRAJECTORY_SENSORS),
        'trajectory_flight': sorted(TRAJECTORY_FLIGHT_SENSORS),
    }
    with open(SENSOR_SETS_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg_data, f, indent=2)


if __name__ == "__main__":
    main()
