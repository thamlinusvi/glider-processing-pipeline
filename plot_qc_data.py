#!/usr/bin/env python
"""
Author: glider_processing_pipeline
Visualize QARTOD QC'd glider data — interactive Plotly charts.

Usage:
    python plot_qc_data.py uvi_01-20250318T2008
    python plot_qc_data.py uvi_01-20250318T2008 --mode delayed --nprofiles 20

Plots saved to:  <deployment>/figs/   (*.html — open in browser or iframe)
"""

import argparse
import os
import glob
import shutil
import time
import warnings

import numpy as np
import pandas as pd
import xarray as xr
import plotly.graph_objects as go
from plotly.subplots import make_subplots

warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────────
# 'cdn'  → small files (~20 KB each), requires internet to render
# True   → fully self-contained (~3.5 MB each), works offline
PLOTLYJS = 'cdn'

# Maximum data points per variable in deployment-wide plots (timeseries, T-S, sections).
# Plotly serialises every point to JSON so uncapped plots balloon to hundreds of MB.
# 50 000 points ≈ 2–5 MB per HTML file and is more than enough visual resolution.
MAX_POINTS_PER_VAR = 50_000

# ── QC flag styling ────────────────────────────────────────────────────────────
FLAG_COLOR = {1: '#2ecc71', 2: '#95a5a6', 3: '#f39c12', 4: '#e74c3c', 9: '#cccccc'}
FLAG_LABEL = {1: 'Good', 2: 'Not Evaluated', 3: 'Suspect', 4: 'Fail', 9: 'Missing'}
FLAG_VALUES = [1, 2, 3, 4, 9]

SCI_PAIRS = [
    ('temperature',          'temperature_qartod_summary_flag',          'Temperature (°C)'),
    ('salinity',             'salinity_qartod_summary_flag',              'Salinity (PSU)'),
    ('density',              'density_qartod_summary_flag',               'Density (kg/m³)'),
    ('sci_water_pressure',   'sci_water_pressure_qartod_summary_flag',    'Pressure (bar)'),
    ('oxygen_concentration', 'oxygen_concentration_qartod_summary_flag',  'O₂ Conc. (µmol/L)'),
    ('oxygen_saturation',    'oxygen_saturation_qartod_summary_flag',     'O₂ Sat. (%)'),
]


# ── Progress helpers ───────────────────────────────────────────────────────────
def _step(msg):
    """Print a prominent step header and return the start time."""
    print(f'\n{"="*60}', flush=True)
    print(f'  {msg}', flush=True)
    print(f'{"="*60}', flush=True)
    return time.time()


def _done(t0, extra=''):
    elapsed = time.time() - t0
    suffix = f'  {extra}' if extra else ''
    print(f'  -> done in {elapsed:.1f}s{suffix}', flush=True)


def _progress(i, total, label='', every=50):
    if total == 0:
        return
    if i == 0 or (i + 1) % every == 0 or i + 1 == total:
        pct = (i + 1) / total * 100
        print(f'  [{i+1}/{total}  {pct:.0f}%] {label}', flush=True)


def _subsample(*arrays, max_n=MAX_POINTS_PER_VAR):
    """Return uniformly subsampled copies of arrays along axis-0.

    If len(arrays[0]) <= max_n the originals are returned unchanged.
    All arrays must have the same length.
    """
    n = len(arrays[0])
    if n <= max_n:
        return arrays
    idx = np.round(np.linspace(0, n - 1, max_n)).astype(int)
    return tuple(a[idx] for a in arrays)


# ── Plotly helpers ─────────────────────────────────────────────────────────────
def _flag_traces(x, y, flags, size=5, opacity=0.7, legend_added=None,
                 hover_x='%{x}', hover_y='%{y}', use_gl=True):
    """Return (list_of_traces, legend_added_set) with one trace per QC flag group."""
    if legend_added is None:
        legend_added = set()
    ScatterCls = go.Scattergl if use_gl else go.Scatter
    traces = []
    for f in FLAG_VALUES:
        mask = (flags == f)
        if not mask.any():
            continue
        show = f not in legend_added
        if show:
            legend_added.add(f)
        traces.append(ScatterCls(
            x=x[mask], y=y[mask],
            mode='markers',
            marker=dict(color=FLAG_COLOR[f], size=size, opacity=opacity),
            name=FLAG_LABEL[f],
            legendgroup=f'flag_{f}',
            showlegend=show,
            hovertemplate=(
                f'{hover_x}<br>{hover_y}'
                f'<br><b style="color:{FLAG_COLOR[f]}">{FLAG_LABEL[f]}</b>'
                '<extra></extra>'
            ),
        ))
    return traces, legend_added


def _save_html(fig, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.write_html(path, include_plotlyjs=PLOTLYJS)
    print(f'  Saved: {os.path.basename(path)}', flush=True)


# ── Data loading ───────────────────────────────────────────────────────────────
def load_deployment(data_dir, max_files=None):
    """Load NC files from a deployment directory into a list of xarray datasets."""
    files = sorted(glob.glob(os.path.join(data_dir, '*.nc')))
    if max_files:
        idx = np.linspace(0, len(files) - 1, min(max_files, len(files))).astype(int)
        files = [files[i] for i in idx]
    total = len(files)
    every = max(1, min(50, total // 10))
    datasets = []
    for i, f in enumerate(files):
        try:
            datasets.append(xr.open_dataset(f, decode_times=False))
        except Exception as e:
            print(f'  Warning: could not open {os.path.basename(f)}: {e}', flush=True)
        _progress(i, total, os.path.basename(f), every=every)
    return datasets, files


def epoch_to_dt(epoch_seconds):
    return pd.to_datetime(epoch_seconds, unit='s', origin='unix')


# ── Plot 1: Individual profile plots ──────────────────────────────────────────
def plot_profiles(datasets, out_dir, nprofiles=20, min_depth=10):
    """One interactive HTML per sampled profile: T, S, density, O2 vs depth.

    Args:
        min_depth: Skip profiles whose maximum depth (m) is below this threshold.
                   Useful to exclude surface checkout dives. Default 10 m.
    """
    total = len(datasets)
    print(f'  Plotting {total} individual profiles (min depth: {min_depth} m)...', flush=True)
    saved = 0
    skipped_shallow = 0

    for i, ds in enumerate(datasets):
        fname = str(ds.encoding.get('source', 'profile')).split(os.sep)[-1].replace('.nc', '')
        available = [
            (var, fv, lbl) for var, fv, lbl in SCI_PAIRS
            if var in ds.data_vars and np.any(~np.isnan(ds[var].values))
        ]
        if not available:
            print(f'  [{i+1}/{total}] {fname} - skipped (no data)', flush=True)
            continue

        depth = (
            ds['depth'].values if 'depth' in ds.data_vars
            else ds['sci_water_pressure'].values
        ).astype(float)

        # Skip shallow dives (e.g. surface checkout dives) below the depth threshold
        depth_max = float(np.nanmax(depth)) if depth.size else 0.0
        if depth_max < min_depth:
            skipped_shallow += 1
            print(f'  [{i+1}/{total}] {fname} - skipped (max depth {depth_max:.1f} m < {min_depth} m)', flush=True)
            continue

        ncols = min(len(available), 2)
        nrows = int(np.ceil(len(available) / ncols))

        fig = make_subplots(
            rows=nrows, cols=ncols,
            subplot_titles=[lbl for _, _, lbl in available],
            shared_yaxes=False,
            horizontal_spacing=0.14,
            vertical_spacing=0.12,
        )

        try:
            pt = epoch_to_dt(float(ds['profile_time'].values.flat[0]))
            title_str = pt.strftime('%Y-%m-%d %H:%M UTC')
        except Exception:
            title_str = fname

        legend_added = set()
        for j, (var, flag_var, label) in enumerate(available):
            row = j // ncols + 1
            col = j % ncols + 1

            x = ds[var].values.astype(float)
            flags = (
                ds[flag_var].values.astype(int)
                if flag_var in ds.data_vars
                else np.ones_like(x, dtype=int)
            )
            mask = ~np.isnan(x) & ~np.isnan(depth)
            if not mask.any():
                continue

            traces, legend_added = _flag_traces(
                x[mask], depth[mask], flags[mask],
                size=7, opacity=0.85,
                legend_added=legend_added,
                hover_x=f'{label}: %{{x:.3f}}',
                hover_y='Depth: %{y:.1f} m',
                use_gl=False,   # profiles are small, Scatter looks better
            )
            for tr in traces:
                fig.add_trace(tr, row=row, col=col)

            fig.update_xaxes(title_text=label, row=row, col=col, showgrid=True, gridcolor='#eee')
            fig.update_yaxes(
                title_text='Depth (m)',
                autorange='reversed',
                row=row, col=col,
                showgrid=True, gridcolor='#eee',
            )

        fig.update_layout(
            title=dict(text=f'<b>Profile — {title_str}</b>', font=dict(size=15)),
            height=440 * nrows,
            template='plotly_white',
            legend=dict(
                orientation='h', yanchor='bottom', y=1.02,
                xanchor='left', x=0, font=dict(size=13),
                itemsizing='constant',
            ),
            margin=dict(t=110, b=60, l=70, r=30),
            plot_bgcolor='white',
        )

        out_path = os.path.join(out_dir, 'profiles', fname + '_profiles.html')
        _save_html(fig, out_path)
        saved += 1
        print(f'  [{i+1}/{total}] saved: {fname}_profiles.html', flush=True)

    skipped_msg = f', {skipped_shallow} skipped (shallow)' if skipped_shallow else ''
    print(f'  Profiles done -{saved}/{total} saved{skipped_msg}', flush=True)


# ── Plot 2: Deployment-wide time series ───────────────────────────────────────
def plot_timeseries(all_datasets, out_dir):
    """Stacked interactive time series of all science variables, coloured by QC flag."""
    total = len(all_datasets)
    print(f'  Building deployment time series ({total} files)...', flush=True)
    every = max(1, min(100, total // 10))

    ts_data = {var: {'time': [], 'val': [], 'flag': []} for var, _, _ in SCI_PAIRS}

    for i, ds in enumerate(all_datasets):
        _progress(i, total, 'aggregating', every=every)
        try:
            t = epoch_to_dt(ds['time'].values.astype(float))
        except Exception:
            continue
        for var, flag_var, _ in SCI_PAIRS:
            if var not in ds.data_vars:
                continue
            x = ds[var].values.astype(float)
            flags = (
                ds[flag_var].values.astype(int)
                if flag_var in ds.data_vars
                else np.ones_like(x, dtype=int)
            )
            mask = ~np.isnan(x)
            if mask.any():
                ts_data[var]['time'].extend(t[mask].tolist())
                ts_data[var]['val'].extend(x[mask].tolist())
                ts_data[var]['flag'].extend(flags[mask].tolist())

    available = [(var, fv, lbl) for var, fv, lbl in SCI_PAIRS if ts_data[var]['time']]
    if not available:
        print('  No data found for time series.', flush=True)
        return

    n = len(available)
    print(f'  Rendering {n} panels...', flush=True)

    fig = make_subplots(
        rows=n, cols=1,
        shared_xaxes=True,
        subplot_titles=[lbl for _, _, lbl in available],
        vertical_spacing=0.04,
    )

    legend_added = set()
    for row, (var, _, label) in enumerate(available, 1):
        d = ts_data[var]
        t_arr  = np.array(d['time'])
        v_arr  = np.array(d['val'])
        f_arr  = np.array(d['flag'])
        n_raw  = len(t_arr)
        t_arr, v_arr, f_arr = _subsample(t_arr, v_arr, f_arr)
        if len(t_arr) < n_raw:
            print(f'  {var}: subsampled {n_raw:,} -> {len(t_arr):,} points', flush=True)
        traces, legend_added = _flag_traces(
            t_arr, v_arr, f_arr,
            size=3, opacity=0.6,
            legend_added=legend_added,
            hover_x='Time: %{x}',
            hover_y=f'{label}: %{{y:.3f}}',
        )
        for tr in traces:
            fig.add_trace(tr, row=row, col=1)
        fig.update_yaxes(title_text=label, row=row, col=1, showgrid=True, gridcolor='#eee')

    fig.update_xaxes(title_text='Time (UTC)', row=n, col=1)
    fig.update_layout(
        title=dict(text='<b>Deployment Time Series</b>', font=dict(size=18)),
        height=260 * n,
        template='plotly_white',
        legend=dict(
            orientation='h', yanchor='bottom', y=1.01,
            xanchor='left', x=0, font=dict(size=13),
            itemsizing='constant',
        ),
        margin=dict(t=120, b=60, l=90, r=40),
    )
    _save_html(fig, os.path.join(out_dir, 'timeseries.html'))


# ── Plot 3: QC flag summary ────────────────────────────────────────────────────
def plot_qc_summary(all_datasets, out_dir):
    """Stacked bar chart: % Good / Suspect / Fail / Not-Eval per QC variable."""
    total = len(all_datasets)
    print(f'  Building QC flag summary ({total} files)...', flush=True)
    every = max(1, min(100, total // 10))

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

    counts = {}
    for i, ds in enumerate(all_datasets):
        _progress(i, total, 'counting flags', every=every)
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
        print('  No QC flag variables found.', flush=True)
        return
    print(f'  Rendering {len(present)} QC variables...', flush=True)

    labels = [
        v.replace('_qartod_', ' / ')
         .replace('_test', '')
         .replace('_', ' ')
        for v in present
    ]
    totals_arr = [max(sum(counts[v].values()), 1) for v in present]

    fig = go.Figure()
    for flag in [1, 2, 3, 4]:
        pct = [counts[v][flag] / totals_arr[i] * 100 for i, v in enumerate(present)]
        raw = [counts[v][flag] for v in present]
        fig.add_trace(go.Bar(
            x=labels, y=pct,
            name=FLAG_LABEL[flag],
            marker_color=FLAG_COLOR[flag],
            customdata=raw,
            hovertemplate=(
                '<b>%{x}</b><br>'
                f'{FLAG_LABEL[flag]}: %{{y:.1f}}%  (%{{customdata:,}} obs)'
                '<extra></extra>'
            ),
        ))

    fig.update_layout(
        barmode='stack',
        title=dict(text='<b>QC Flag Distribution Across Deployment</b>', font=dict(size=18)),
        xaxis=dict(tickangle=-45, tickfont=dict(size=10), title='QC Variable'),
        yaxis=dict(title='% of all observations', range=[0, 105]),
        legend=dict(
            orientation='h', yanchor='bottom', y=1.02,
            xanchor='left', x=0, font=dict(size=13),
        ),
        template='plotly_white',
        height=620,
        margin=dict(t=100, b=220, l=70, r=40),
    )
    _save_html(fig, os.path.join(out_dir, 'qc_flag_summary.html'))


# ── Plot 4: T–S diagram ───────────────────────────────────────────────────────
def plot_ts_diagram(all_datasets, out_dir):
    """T–S scatter: depth-coloured (left) and QC-flag-coloured (right)."""
    total = len(all_datasets)
    print(f'  Building T-S diagram ({total} files)...', flush=True)
    every = max(1, min(100, total // 10))

    temp_all, salt_all, depth_all, flag_all = [], [], [], []

    for i, ds in enumerate(all_datasets):
        _progress(i, total, 'collecting T-S data', every=every)
        if 'temperature' not in ds.data_vars or 'salinity' not in ds.data_vars:
            continue
        t = ds['temperature'].values.astype(float)
        s = ds['salinity'].values.astype(float)
        d = (
            ds['depth'].values.astype(float)
            if 'depth' in ds.data_vars
            else np.full_like(t, np.nan)
        )
        flags = (
            ds['temperature_qartod_summary_flag'].values.astype(int)
            if 'temperature_qartod_summary_flag' in ds.data_vars
            else np.ones_like(t, dtype=int)
        )
        mask = ~np.isnan(t) & ~np.isnan(s)
        temp_all.extend(t[mask].tolist())
        salt_all.extend(s[mask].tolist())
        depth_all.extend(d[mask].tolist())
        flag_all.extend(flags[mask].tolist())

    if not temp_all:
        print('  No T-S data found.', flush=True)
        return

    T = np.array(temp_all)
    S = np.array(salt_all)
    D = np.array(depth_all)
    F = np.array(flag_all)
    n_raw = len(T)
    T, S, D, F = _subsample(T, S, D, F)
    if len(T) < n_raw:
        print(f'  T-S: subsampled {n_raw:,} -> {len(T):,} points', flush=True)
    print(f'  Rendering T-S diagram ({len(T):,} points)...', flush=True)

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=['Coloured by Depth', 'Coloured by QC Flag'],
        horizontal_spacing=0.14,
    )

    # Left: coloured by depth
    fig.add_trace(go.Scattergl(
        x=S, y=T,
        mode='markers',
        marker=dict(
            color=D, colorscale='Viridis_r', size=2, opacity=0.5,
            showscale=True,
            colorbar=dict(title='Depth (m)', x=0.44, thickness=14),
        ),
        showlegend=False,
        hovertemplate='S: %{x:.3f} PSU<br>T: %{y:.3f} °C<br>Depth: %{marker.color:.1f} m<extra></extra>',
    ), row=1, col=1)

    # Right: coloured by QC flag
    legend_added = set()
    for f in FLAG_VALUES:
        mask = F == f
        if not mask.any():
            continue
        fig.add_trace(go.Scattergl(
            x=S[mask], y=T[mask],
            mode='markers',
            marker=dict(color=FLAG_COLOR[f], size=2, opacity=0.5),
            name=FLAG_LABEL[f],
            legendgroup=f'flag_{f}',
            showlegend=True,
            hovertemplate=f'S: %{{x:.3f}} PSU<br>T: %{{y:.3f}} °C<br><b>{FLAG_LABEL[f]}</b><extra></extra>',
        ), row=1, col=2)

    fig.update_xaxes(title_text='Salinity (PSU)', showgrid=True, gridcolor='#eee')
    fig.update_yaxes(title_text='Temperature (°C)', showgrid=True, gridcolor='#eee')
    fig.update_layout(
        title=dict(text='<b>T–S Diagram — Full Deployment</b>', font=dict(size=18)),
        height=600,
        template='plotly_white',
        legend=dict(font=dict(size=13), itemsizing='constant'),
        margin=dict(t=80, b=60, l=80, r=40),
    )
    _save_html(fig, os.path.join(out_dir, 'ts_diagram.html'))


# ── Plot 5: Depth–time section plots ──────────────────────────────────────────
def plot_sections(all_datasets, out_dir):
    """Depth–time sections for T, S, O2: good-data coloured by value + all data by QC flag."""
    total = len(all_datasets)
    print(f'  Building section plots ({total} files)...', flush=True)
    every = max(1, min(100, total // 10))

    section_vars = [
        ('temperature',          'temperature_qartod_summary_flag',          'Temperature (°C)',   'RdYlBu', True),
        ('salinity',             'salinity_qartod_summary_flag',             'Salinity (PSU)',      'Viridis', False),
        ('oxygen_concentration', 'oxygen_concentration_qartod_summary_flag', 'O₂ Conc. (µmol/L)', 'Plasma',  False),
    ]

    for var, flag_var, label, colorscale, reversescale in section_vars:
        print(f'  Collecting data for {label}...', flush=True)
        time_all, depth_all, val_all, flag_all = [], [], [], []

        for i, ds in enumerate(all_datasets):
            _progress(i, total, var, every=every)
            if var not in ds.data_vars:
                continue
            try:
                t = epoch_to_dt(ds['time'].values.astype(float))
            except Exception:
                continue
            v = ds[var].values.astype(float)
            d = (
                ds['depth'].values.astype(float)
                if 'depth' in ds.data_vars
                else np.full_like(v, np.nan)
            )
            flags = (
                ds[flag_var].values.astype(int)
                if flag_var in ds.data_vars
                else np.ones_like(v, dtype=int)
            )
            mask = ~np.isnan(v) & ~np.isnan(d)
            time_all.extend(t[mask].tolist())
            depth_all.extend(d[mask].tolist())
            val_all.extend(v[mask].tolist())
            flag_all.extend(flags[mask].tolist())

        if not time_all:
            print(f'  No data for {label} - skipping.', flush=True)
            continue

        T_arr = np.array(time_all)
        D_arr = np.array(depth_all)
        V_arr = np.array(val_all)
        F_arr = np.array(flag_all)
        n_raw = len(V_arr)
        T_arr, D_arr, V_arr, F_arr = _subsample(T_arr, D_arr, V_arr, F_arr)
        if len(V_arr) < n_raw:
            print(f'  {label}: subsampled {n_raw:,} -> {len(V_arr):,} points', flush=True)
        print(f'  Rendering section for {label} ({len(V_arr):,} points)...', flush=True)

        good = F_arr == 1
        fig = make_subplots(
            rows=2, cols=1,
            subplot_titles=[
                f'{label} — Good data coloured by value',
                f'{label} — All data coloured by QC flag',
            ],
            shared_xaxes=True,
            vertical_spacing=0.08,
        )

        # Top panel: good data, coloured by measured value
        if good.any():
            fig.add_trace(go.Scattergl(
                x=T_arr[good], y=D_arr[good],
                mode='markers',
                marker=dict(
                    color=V_arr[good],
                    colorscale=colorscale,
                    reversescale=reversescale,
                    size=2, opacity=0.7,
                    showscale=True,
                    colorbar=dict(title=label, y=0.76, len=0.44, thickness=14),
                ),
                showlegend=False,
                hovertemplate=(
                    'Time: %{x}<br>Depth: %{y:.1f} m'
                    f'<br>{label}: %{{marker.color:.3f}}<extra></extra>'
                ),
            ), row=1, col=1)

        # Bottom panel: all data, coloured by QC flag
        legend_added = set()
        for f in FLAG_VALUES:
            mask = F_arr == f
            if not mask.any():
                continue
            show = f not in legend_added
            if show:
                legend_added.add(f)
            fig.add_trace(go.Scattergl(
                x=T_arr[mask], y=D_arr[mask],
                mode='markers',
                marker=dict(color=FLAG_COLOR[f], size=2, opacity=0.6),
                name=FLAG_LABEL[f],
                legendgroup=f'flag_{f}',
                showlegend=show,
                hovertemplate=(
                    f'Time: %{{x}}<br>Depth: %{{y:.1f}} m'
                    f'<br><b>{FLAG_LABEL[f]}</b><extra></extra>'
                ),
            ), row=2, col=1)

        fig.update_yaxes(title_text='Depth (m)', autorange='reversed',
                         showgrid=True, gridcolor='#eee', row=1, col=1)
        fig.update_yaxes(title_text='Depth (m)', autorange='reversed',
                         showgrid=True, gridcolor='#eee', row=2, col=1)
        fig.update_xaxes(title_text='Time (UTC)', row=2, col=1)
        fig.update_layout(
            title=dict(text=f'<b>Depth–Time Section: {label}</b>', font=dict(size=18)),
            height=900,
            template='plotly_white',
            legend=dict(
                orientation='h', yanchor='bottom', y=1.01,
                xanchor='left', x=0, font=dict(size=13),
                itemsizing='constant',
            ),
            margin=dict(t=100, b=60, l=80, r=40),
        )
        _save_html(fig, os.path.join(out_dir, f'section_{var}.html'))


# ── Main ───────────────────────────────────────────────────────────────────────
def _in_jupyter():
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
    min_depth=10,
    plots=None,
):
    """
    Can be called from the command line OR directly from a Jupyter notebook:

        from plot_qc_data import main
        main(deployment='uvi_01-20250318T2008')
    """
    if plots is None:
        plots = ['all']

    if _in_jupyter() or deployment is not None:
        if deployment is None:
            raise ValueError(
                "Please pass a deployment name, e.g.:\n"
                "  main(deployment='uvi_01-20250318T2008')"
            )
    else:
        parser = argparse.ArgumentParser(description="Plot QARTOD QC'd glider data")
        parser.add_argument('deployment', help='Deployment name, e.g. uvi_01-20250318T2008')
        parser.add_argument('--mode',     default='delayed', choices=['rt', 'delayed'])
        parser.add_argument('--level',    default='sci',     choices=['sci', 'ngdac'])
        parser.add_argument('--data_home', default=None)
        parser.add_argument('--nprofiles', type=int, default=20)
        parser.add_argument('--min_depth', type=float, default=10,
                            help='Skip profiles shallower than this depth in metres (default: 10)')
        parser.add_argument('--plots', nargs='+',
                            choices=['profiles', 'timeseries', 'qc_summary', 'ts', 'sections', 'all'],
                            default=['all'])
        args       = parser.parse_args()
        deployment = args.deployment
        mode       = args.mode
        level      = args.level
        data_home  = args.data_home
        nprofiles  = args.nprofiles
        min_depth  = args.min_depth
        plots      = args.plots

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_home  = data_home or script_dir

    year     = deployment.split('-')[1][:4]
    data_dir = os.path.join(data_home, 'deployments', year, deployment,
                            'data', 'out', 'nc', f'{level}-profile', mode)
    out_dir  = os.path.join(data_home, 'deployments', year, deployment, 'figs')

    # Clear existing plots so reruns don't accumulate stale files
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isdir(data_dir):
        print(f'ERROR: data directory not found:\n  {data_dir}', flush=True)
        return 1

    nc_files = [f for f in sorted(glob.glob(os.path.join(data_dir, '*.nc')))
                if 'qc_queue' not in f]
    print(f'Found {len(nc_files)} NC files in {data_dir}', flush=True)
    if not nc_files:
        print('No files found.', flush=True)
        return 1

    do_all    = 'all' in plots
    run_start = time.time()
    step      = 0

    # ── Profile plots ─────────────────────────────────────────────────────────
    if do_all or 'profiles' in plots:
        step += 1
        t0 = _step(f'Step {step}: loading {nprofiles} profile samples')
        profile_ds, _ = load_deployment(data_dir, max_files=nprofiles)
        _done(t0, f'{len(profile_ds)} files loaded')

        t0 = _step(f'Step {step}: individual profile plots')
        plot_profiles(profile_ds, out_dir, nprofiles=nprofiles, min_depth=min_depth)
        _done(t0)
        for ds in profile_ds:
            ds.close()

    # ── Load all files for deployment-wide plots ───────────────────────────────
    step += 1
    t0 = _step(f'Step {step}: loading all {len(nc_files)} files')
    all_ds, _ = load_deployment(data_dir)
    _done(t0, f'{len(all_ds)} files loaded')

    if do_all or 'timeseries' in plots:
        step += 1
        t0 = _step(f'Step {step}: time series plot')
        plot_timeseries(all_ds, out_dir)
        _done(t0)

    if do_all or 'qc_summary' in plots:
        step += 1
        t0 = _step(f'Step {step}: QC flag summary plot')
        plot_qc_summary(all_ds, out_dir)
        _done(t0)

    if do_all or 'ts' in plots:
        step += 1
        t0 = _step(f'Step {step}: T-S diagram')
        plot_ts_diagram(all_ds, out_dir)
        _done(t0)

    if do_all or 'sections' in plots:
        step += 1
        t0 = _step(f'Step {step}: depth-time section plots')
        plot_sections(all_ds, out_dir)
        _done(t0)

    for ds in all_ds:
        ds.close()

    total_elapsed = time.time() - run_start
    print(f'\n{"="*60}', flush=True)
    print(f'  All plots saved to: {out_dir}', flush=True)
    print(f'  Total time: {total_elapsed:.1f}s', flush=True)
    print(f'{"="*60}', flush=True)
    return 0


# ── CONFIGURATION — edit this block when running in VS Code or Jupyter ────────
DEPLOYMENT = 'uvi_01-20250318T2008'
MODE       = 'delayed'
LEVEL      = 'sci'
DATA_HOME  = None
NPROFILES  = 20
MIN_DEPTH  = 10      # metres — profiles shallower than this are skipped (removes checkout dives)
PLOTS      = ['all']
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if _in_jupyter():
        main(deployment=DEPLOYMENT, mode=MODE, level=LEVEL,
             data_home=DATA_HOME, nprofiles=NPROFILES, min_depth=MIN_DEPTH, plots=PLOTS)
    else:
        exit(main())
