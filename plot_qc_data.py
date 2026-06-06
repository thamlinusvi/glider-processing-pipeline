#!/usr/bin/env python
"""
Author: glider_processing_pipeline
Visualize QARTOD QC'd glider data.

Usage:
    python plot_qc_data.py uvi_01-20250318T2008
    python plot_qc_data.py uvi_01-20250318T2008 --mode delayed --nprofiles 20

Plots saved to:  <deployment>/figs/
"""

import argparse
import os
import glob
import warnings

import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import matplotlib.colors as mcolors
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

warnings.filterwarnings('ignore')

# ── QC flag styling ────────────────────────────────────────────────────────────
FLAG_COLOR = {1: '#2ecc71', 2: '#95a5a6', 3: '#f39c12', 4: '#e74c3c', 9: '#cccccc'}
FLAG_LABEL = {1: 'Good', 2: 'Not Evaluated', 3: 'Suspect', 4: 'Fail', 9: 'Missing'}
FLAG_ZORDER = {1: 1, 2: 2, 9: 0, 3: 3, 4: 4}
FLAG_VALUES = [1, 2, 3, 4, 9]


def qc_legend_elements():
    return [Patch(facecolor=FLAG_COLOR[f], label=FLAG_LABEL[f]) for f in [1, 3, 4, 2, 9]]


def flag_scatter(ax, x, y, flags, size=6, alpha=0.7):
    """Scatter plot coloured by QC flag value."""
    for f in FLAG_VALUES:
        mask = flags == f
        if mask.any():
            ax.scatter(x[mask], y[mask],
                       c=FLAG_COLOR[f], s=size, alpha=alpha,
                       zorder=FLAG_ZORDER[f], label=FLAG_LABEL[f])


def load_deployment(data_dir, max_files=None):
    """Load all NC files from a deployment directory into a list of datasets."""
    files = sorted(glob.glob(os.path.join(data_dir, '*.nc')))
    if max_files:
        # Spread evenly across the deployment
        idx = np.linspace(0, len(files) - 1, min(max_files, len(files))).astype(int)
        files = [files[i] for i in idx]
    datasets = []
    for f in files:
        try:
            ds = xr.open_dataset(f, decode_times=False)
            datasets.append(ds)
        except Exception as e:
            print(f'  Warning: could not open {os.path.basename(f)}: {e}')
    return datasets, files


def epoch_to_dt(epoch_seconds):
    """Convert epoch seconds to datetime64."""
    return pd.to_datetime(epoch_seconds, unit='s', origin='unix')


# ── Plot 1: Profile plots for sampled files ────────────────────────────────────
def plot_profiles(datasets, out_dir, nprofiles=20):
    """Individual profile plots: T, S, density, O2 vs depth coloured by QC summary flag."""
    print(f'  Plotting profiles ({len(datasets)} files)...')

    sci_pairs = [
        ('temperature',          'temperature_qartod_summary_flag',      'Temperature (°C)'),
        ('salinity',             'salinity_qartod_summary_flag',          'Salinity (PSU)'),
        ('density',              'density_qartod_summary_flag',           'Density (kg m⁻³)'),
        ('sci_water_pressure',   'sci_water_pressure_qartod_summary_flag','Pressure (bar)'),
        ('oxygen_concentration', 'oxygen_concentration_qartod_summary_flag', 'O₂ Conc. (µmol L⁻¹)'),
        ('oxygen_saturation',    'oxygen_saturation_qartod_summary_flag', 'O₂ Sat. (%)'),
    ]

    for ds in datasets:
        fname = str(ds.encoding.get('source', 'profile')).split(os.sep)[-1].replace('.nc', '')
        depth = ds['depth'].values if 'depth' in ds else ds.get('depth_interpolated', ds['sci_water_pressure']).values

        # Filter to panels that have data
        available = [(var, flag_var, label) for var, flag_var, label in sci_pairs
                     if var in ds.data_vars and np.any(~np.isnan(ds[var].values))]
        if not available:
            continue

        ncols = min(len(available), 3)
        nrows = int(np.ceil(len(available) / ncols))
        fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 8 * nrows), sharey=False)
        axes = np.array(axes).flatten()

        for ax, (var, flag_var, label) in zip(axes, available):
            x = ds[var].values.astype(float)
            d = depth.astype(float)
            flags = ds[flag_var].values.astype(int) if flag_var in ds.data_vars else np.ones_like(x, dtype=int)

            mask = ~np.isnan(x) & ~np.isnan(d)
            if mask.any():
                flag_scatter(ax, x[mask], d[mask], flags[mask])
            ax.invert_yaxis()
            ax.set_xlabel(label, fontsize=9)
            ax.set_ylabel('Depth (m)', fontsize=9)
            ax.grid(True, alpha=0.3)

        # Hide unused axes
        for ax in axes[len(available):]:
            ax.set_visible(False)

        axes[0].legend(handles=qc_legend_elements(), fontsize=7, loc='lower right')
        try:
            profile_time = epoch_to_dt(float(ds['profile_time'].values.flat[0]))
            title_time = profile_time.strftime('%Y-%m-%d %H:%M UTC')
        except Exception:
            title_time = fname
        fig.suptitle(f'Profiles  |  {title_time}', fontsize=11, y=1.01)
        plt.tight_layout()

        out_path = os.path.join(out_dir, 'profiles', fname + '_profiles.png')
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        plt.savefig(out_path, dpi=120, bbox_inches='tight')
        plt.close()

    print(f'    Saved to {os.path.join(out_dir, "profiles")}')


# ── Plot 2: Deployment-wide time series ───────────────────────────────────────
def plot_timeseries(all_datasets, out_dir):
    """Time series of key variables across the full deployment, coloured by QC summary flag."""
    print('  Building deployment time series...')

    sci_pairs = [
        ('temperature',          'temperature_qartod_summary_flag',          'Temperature (°C)'),
        ('salinity',             'salinity_qartod_summary_flag',              'Salinity (PSU)'),
        ('density',              'density_qartod_summary_flag',               'Density (kg m⁻³)'),
        ('sci_water_pressure',   'sci_water_pressure_qartod_summary_flag',    'Pressure (bar)'),
        ('oxygen_concentration', 'oxygen_concentration_qartod_summary_flag',  'O₂ Conc. (µmol L⁻¹)'),
        ('oxygen_saturation',    'oxygen_saturation_qartod_summary_flag',     'O₂ Sat. (%)'),
    ]

    # Collect data
    records = {k: {'time': [], 'val': [], 'flag': []} for _, k, _ in [(a, a, b) for a, _, b in sci_pairs]}
    ts_data = {var: {'time': [], 'val': [], 'flag': []} for var, _, _ in sci_pairs}

    for ds in all_datasets:
        try:
            t = epoch_to_dt(ds['time'].values.astype(float))
        except Exception:
            continue
        for var, flag_var, _ in sci_pairs:
            if var not in ds.data_vars:
                continue
            x = ds[var].values.astype(float)
            flags = ds[flag_var].values.astype(int) if flag_var in ds.data_vars else np.ones_like(x, dtype=int)
            mask = ~np.isnan(x)
            if mask.any():
                ts_data[var]['time'].extend(t[mask].tolist())
                ts_data[var]['val'].extend(x[mask].tolist())
                ts_data[var]['flag'].extend(flags[mask].tolist())

    available = [(var, flag_var, label) for var, flag_var, label in sci_pairs
                 if ts_data[var]['time']]
    if not available:
        print('    No data found for time series.')
        return

    fig, axes = plt.subplots(len(available), 1, figsize=(16, 3.5 * len(available)), sharex=True)
    if len(available) == 1:
        axes = [axes]

    for ax, (var, flag_var, label) in zip(axes, available):
        d = ts_data[var]
        t_arr = np.array(d['time'])
        v_arr = np.array(d['val'])
        f_arr = np.array(d['flag'])
        flag_scatter(ax, t_arr, v_arr, f_arr, size=3, alpha=0.6)
        ax.set_ylabel(label, fontsize=9)
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel('Time (UTC)', fontsize=9)
    axes[0].legend(handles=qc_legend_elements(), fontsize=7, ncol=5, loc='upper right')
    plt.suptitle('Deployment Time Series', fontsize=12)
    plt.tight_layout()

    out_path = os.path.join(out_dir, 'timeseries.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.close()
    print(f'    Saved: {out_path}')


# ── Plot 3: QC flag summary bar chart ─────────────────────────────────────────
def plot_qc_summary(all_datasets, out_dir):
    """Bar chart: % GOOD / SUSPECT / FAIL / MISSING per QC variable across the deployment."""
    print('  Building QC flag summary...')

    qc_vars_order = [
        'temperature_qartod_gross_range_test',
        'temperature_qartod_flat_line_test',
        'temperature_qartod_climatology_test',
        'temperature_qartod_spike_test',
        'temperature_qartod_rate_of_change_test',
        'temperature_qartod_summary_flag',
        'temperature_hysteresis_test',
        'conductivity_qartod_gross_range_test',
        'conductivity_qartod_flat_line_test',
        'conductivity_qartod_climatology_test',
        'conductivity_qartod_spike_test',
        'conductivity_qartod_rate_of_change_test',
        'conductivity_qartod_summary_flag',
        'conductivity_hysteresis_test',
        'salinity_qartod_flat_line_test',
        'salinity_qartod_climatology_test',
        'salinity_qartod_spike_test',
        'salinity_qartod_rate_of_change_test',
        'salinity_qartod_summary_flag',
        'density_qartod_flat_line_test',
        'density_qartod_climatology_test',
        'density_qartod_spike_test',
        'density_qartod_rate_of_change_test',
        'density_qartod_summary_flag',
        'sci_water_pressure_qartod_gross_range_test',
        'sci_water_pressure_qartod_flat_line_test',
        'sci_water_pressure_qartod_pressure_test',
        'sci_water_pressure_qartod_spike_test',
        'sci_water_pressure_qartod_rate_of_change_test',
        'sci_water_pressure_qartod_summary_flag',
        'oxygen_concentration_qartod_gross_range_test',
        'oxygen_concentration_qartod_flat_line_test',
        'oxygen_concentration_qartod_summary_flag',
        'oxygen_saturation_qartod_gross_range_test',
        'oxygen_saturation_qartod_flat_line_test',
        'oxygen_saturation_qartod_summary_flag',
    ]

    # Accumulate counts
    counts = {}
    for ds in all_datasets:
        for v in qc_vars_order:
            if v not in ds.data_vars:
                continue
            vals = ds[v].values.astype(int).flatten()
            if v not in counts:
                counts[v] = {f: 0 for f in FLAG_VALUES}
            for f in FLAG_VALUES:
                counts[v][f] += int(np.sum(vals == f))

    present = [v for v in qc_vars_order if v in counts]
    if not present:
        return

    # Build percentage arrays (exclude MISSING from the non-missing total)
    labels = [v.replace('_qartod_', '\n').replace('_test', '').replace('_', ' ') for v in present]
    n = len(present)

    fig, ax = plt.subplots(figsize=(max(12, n * 0.45), 7))
    x = np.arange(n)
    width = 0.7

    bottom = np.zeros(n)
    for flag in [1, 2, 3, 4]:  # stack in order, skip MISSING
        pct = np.array([counts[v][flag] / max(sum(counts[v].values()), 1) * 100 for v in present])
        ax.bar(x, pct, width, bottom=bottom, color=FLAG_COLOR[flag], label=FLAG_LABEL[flag])
        bottom += pct

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=60, ha='right', fontsize=7)
    ax.set_ylabel('% of all observations', fontsize=10)
    ax.set_title('QC Flag Distribution Across Deployment', fontsize=12)
    ax.legend(loc='upper right', fontsize=9)
    ax.set_ylim(0, 105)
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'qc_flag_summary.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.close()
    print(f'    Saved: {out_path}')


# ── Plot 4: T–S diagram ───────────────────────────────────────────────────────
def plot_ts_diagram(all_datasets, out_dir):
    """T–S diagram coloured by depth, with QC summary flag filter."""
    print('  Building T-S diagram...')

    temp_all, salt_all, depth_all, flag_all = [], [], [], []

    for ds in all_datasets:
        if 'temperature' not in ds.data_vars or 'salinity' not in ds.data_vars:
            continue
        t = ds['temperature'].values.astype(float)
        s = ds['salinity'].values.astype(float)
        d = ds['depth'].values.astype(float) if 'depth' in ds.data_vars else np.full_like(t, np.nan)
        flags = ds['temperature_qartod_summary_flag'].values.astype(int) \
            if 'temperature_qartod_summary_flag' in ds.data_vars else np.ones_like(t, dtype=int)

        mask = ~np.isnan(t) & ~np.isnan(s)
        temp_all.extend(t[mask].tolist())
        salt_all.extend(s[mask].tolist())
        depth_all.extend(d[mask].tolist())
        flag_all.extend(flags[mask].tolist())

    if not temp_all:
        return

    T = np.array(temp_all)
    S = np.array(salt_all)
    D = np.array(depth_all)
    F = np.array(flag_all)

    # Plot only good + suspect
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Left: coloured by depth
    sc = axes[0].scatter(S, T, c=D, cmap='viridis_r', s=2, alpha=0.5)
    plt.colorbar(sc, ax=axes[0], label='Depth (m)')
    axes[0].set_xlabel('Salinity (PSU)')
    axes[0].set_ylabel('Temperature (°C)')
    axes[0].set_title('T–S Diagram (coloured by depth)')
    axes[0].grid(True, alpha=0.3)

    # Right: coloured by QC flag
    flag_scatter(axes[1], S, T, F, size=2, alpha=0.5)
    axes[1].set_xlabel('Salinity (PSU)')
    axes[1].set_ylabel('Temperature (°C)')
    axes[1].set_title('T–S Diagram (coloured by QC flag)')
    axes[1].legend(handles=qc_legend_elements(), fontsize=8)
    axes[1].grid(True, alpha=0.3)

    plt.suptitle('T–S Diagram — Full Deployment', fontsize=12)
    plt.tight_layout()
    out_path = os.path.join(out_dir, 'ts_diagram.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.close()
    print(f'    Saved: {out_path}')


# ── Plot 5: Section (depth-time) plots ────────────────────────────────────────
def plot_sections(all_datasets, out_dir):
    """Depth–time section plots for T, S, O2."""
    print('  Building section plots...')

    section_vars = [
        ('temperature',          'temperature_qartod_summary_flag',         'Temperature (°C)',      'RdYlBu_r'),
        ('salinity',             'salinity_qartod_summary_flag',             'Salinity (PSU)',         'viridis'),
        ('oxygen_concentration', 'oxygen_concentration_qartod_summary_flag', 'O₂ Conc. (µmol L⁻¹)',  'plasma'),
    ]

    for var, flag_var, label, cmap in section_vars:
        time_all, depth_all, val_all, flag_all = [], [], [], []
        for ds in all_datasets:
            if var not in ds.data_vars:
                continue
            try:
                t = epoch_to_dt(ds['time'].values.astype(float))
            except Exception:
                continue
            v = ds[var].values.astype(float)
            d = ds['depth'].values.astype(float) if 'depth' in ds.data_vars else np.full_like(v, np.nan)
            flags = ds[flag_var].values.astype(int) if flag_var in ds.data_vars else np.ones_like(v, dtype=int)
            mask = ~np.isnan(v) & ~np.isnan(d)
            time_all.extend(t[mask].tolist())
            depth_all.extend(d[mask].tolist())
            val_all.extend(v[mask].tolist())
            flag_all.extend(flags[mask].tolist())

        if not time_all:
            continue

        T_arr = np.array(time_all)
        D_arr = np.array(depth_all)
        V_arr = np.array(val_all)
        F_arr = np.array(flag_all)

        # Show only GOOD data on the section
        good = F_arr == 1

        fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True)

        # Top: section scatter (good data only, coloured by value)
        if good.any():
            sc = axes[0].scatter(T_arr[good], D_arr[good], c=V_arr[good],
                                 cmap=cmap, s=2, alpha=0.7)
            plt.colorbar(sc, ax=axes[0], label=label)
        axes[0].invert_yaxis()
        axes[0].set_ylabel('Depth (m)')
        axes[0].set_title(f'{label} — Good data only')
        axes[0].grid(True, alpha=0.3)

        # Bottom: section coloured by QC flag
        flag_scatter(axes[1], T_arr, D_arr, F_arr, size=2, alpha=0.6)
        axes[1].invert_yaxis()
        axes[1].set_ylabel('Depth (m)')
        axes[1].set_xlabel('Time (UTC)')
        axes[1].set_title(f'{label} — QC flags')
        axes[1].legend(handles=qc_legend_elements(), fontsize=8, loc='lower right')
        axes[1].grid(True, alpha=0.3)

        plt.suptitle(f'Depth–Time Section: {label}', fontsize=12)
        plt.tight_layout()
        out_path = os.path.join(out_dir, f'section_{var}.png')
        plt.savefig(out_path, dpi=120, bbox_inches='tight')
        plt.close()
        print(f'    Saved: {out_path}')


# ── Main ───────────────────────────────────────────────────────────────────────
def _in_jupyter():
    """Return True when running inside a Jupyter kernel."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


def main(
    deployment=None,
    mode='delayed',
    level='sci',
    data_home=None,
    nprofiles=20,
    plots=None,
):
    """
    Can be called from the command line OR directly from a Jupyter notebook:

        from plot_qc_data import main
        main(deployment='uvi_01-20250318T2008')

    Or set the DEPLOYMENT variable below and run the cell.
    """
    if plots is None:
        plots = ['all']

    # ── Jupyter / interactive mode ────────────────────────────────────────────
    if _in_jupyter() or deployment is not None:
        # When called programmatically, 'deployment' is passed as a kwarg.
        # If still None here the caller forgot to set it.
        if deployment is None:
            raise ValueError(
                "Please pass a deployment name, e.g.:\n"
                "  main(deployment='uvi_01-20250318T2008')"
            )
    else:
        # ── Command-line mode ─────────────────────────────────────────────────
        parser = argparse.ArgumentParser(description="Plot QARTOD QC'd glider data")
        parser.add_argument('deployment', help='Deployment name, e.g. uvi_01-20250318T2008')
        parser.add_argument('--mode', default='delayed', choices=['rt', 'delayed'])
        parser.add_argument('--level', default='sci', choices=['sci', 'ngdac'])
        parser.add_argument('--data_home', default=None,
                            help='Path to GLIDER_DATA_HOME (default: directory of this script)')
        parser.add_argument('--nprofiles', type=int, default=20,
                            help='Number of profiles to sample for individual profile plots')
        parser.add_argument('--plots', nargs='+',
                            choices=['profiles', 'timeseries', 'qc_summary', 'ts', 'sections', 'all'],
                            default=['all'],
                            help='Which plots to generate')
        args = parser.parse_args()
        deployment = args.deployment
        mode       = args.mode
        level      = args.level
        data_home  = args.data_home
        nprofiles  = args.nprofiles
        plots      = args.plots

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_home = data_home or script_dir

    year = deployment.split('-')[1][:4]
    data_dir = os.path.join(data_home, 'deployments', year, deployment,
                            'data', 'out', 'nc',
                            f'{level}-profile', mode)
    out_dir = os.path.join(data_home, 'deployments', year, deployment, 'figs')
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isdir(data_dir):
        print(f'ERROR: data directory not found:\n  {data_dir}')
        return 1

    nc_files = sorted(glob.glob(os.path.join(data_dir, '*.nc')))
    # exclude any qc_queue subdirectory files
    nc_files = [f for f in nc_files if 'qc_queue' not in f]
    print(f'Found {len(nc_files)} NC files in {data_dir}')

    if not nc_files:
        print('No files found.')
        return 1

    do_all = 'all' in plots

    # Load profile sample for profile plots
    if do_all or 'profiles' in plots:
        print(f'\nLoading {nprofiles} profile samples...')
        profile_ds, _ = load_deployment(data_dir, max_files=nprofiles)
        plot_profiles(profile_ds, out_dir, nprofiles=nprofiles)
        for ds in profile_ds:
            ds.close()

    # Load all files for deployment-wide plots
    print(f'\nLoading all {len(nc_files)} files for deployment-wide plots...')
    all_ds, _ = load_deployment(data_dir)

    if do_all or 'timeseries' in plots:
        print()
        plot_timeseries(all_ds, out_dir)

    if do_all or 'qc_summary' in plots:
        print()
        plot_qc_summary(all_ds, out_dir)

    if do_all or 'ts' in plots:
        print()
        plot_ts_diagram(all_ds, out_dir)

    if do_all or 'sections' in plots:
        print()
        plot_sections(all_ds, out_dir)

    for ds in all_ds:
        ds.close()

    print(f'\nAll plots saved to: {out_dir}')
    return 0


# ── CONFIGURATION — edit this block when running in VS Code or Jupyter ────────
DEPLOYMENT = 'uvi_01-20250318T2008'   # deployment name
MODE       = 'delayed'                 # 'rt' or 'delayed'
LEVEL      = 'sci'                     # 'sci' or 'ngdac'
DATA_HOME  = None                      # None = folder containing this script
NPROFILES  = 20                        # number of individual profile plots
PLOTS      = ['all']                   # ['all'] or any combo of:
                                       #   'profiles', 'timeseries', 'qc_summary', 'ts', 'sections'
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if _in_jupyter():
        # VS Code interactive window / Jupyter notebook — use config block above
        main(deployment=DEPLOYMENT, mode=MODE, level=LEVEL,
             data_home=DATA_HOME, nprofiles=NPROFILES, plots=PLOTS)
    else:
        # Normal command-line run
        exit(main())
