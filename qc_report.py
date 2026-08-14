#!/usr/bin/env python
"""
Generate a styled HTML QC report for a processed glider deployment.

Usage (command line):
    python qc_report.py uvi_01-20250318T2008

Usage (VS Code / Jupyter):
    Edit the CONFIGURATION block at the bottom and run the file.

Report saved to:  deployments/<year>/<deployment>/reports/qc_report_<deployment>.html
"""

import argparse
import os
import glob
import sys
import datetime as dt
import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

import numpy as np
import pandas as pd
import xarray as xr

# ── Flag metadata ──────────────────────────────────────────────────────────────
FLAG_MEANING = {1: 'GOOD', 2: 'NOT_EVAL', 3: 'SUSPECT', 4: 'FAIL', 9: 'MISSING'}
FLAG_CLASS   = {1: 'good', 2: 'noteval',  3: 'suspect', 4: 'fail', 9: 'missing'}

# Short labels for QC test suffixes (full variable name minus sensor prefix)
TEST_LABELS = {
    'qartod_gross_range_test':    'Gross Range',
    'qartod_flat_line_test':      'Flat Line',
    'qartod_climatology_test':    'Climatology *',
    'qartod_spike_test':          'Spike',
    'qartod_rate_of_change_test': 'Rate of Change',
    'qartod_pressure_test':       'Pressure',
    'qartod_summary_flag':        'Summary &#9733;',
    'hysteresis_test':            'Hysteresis',
}

# (display_label, sensor_key, [full_qc_var_names])
QC_VARS_ORDER = [
    ('Temperature', 'temperature', [
        'temperature_qartod_gross_range_test',
        'temperature_qartod_flat_line_test',
        'temperature_qartod_climatology_test',
        'temperature_qartod_spike_test',
        'temperature_qartod_rate_of_change_test',
        'temperature_qartod_summary_flag',
        'temperature_hysteresis_test',
    ]),
    ('Conductivity', 'conductivity', [
        'conductivity_qartod_gross_range_test',
        'conductivity_qartod_flat_line_test',
        'conductivity_qartod_climatology_test',
        'conductivity_qartod_spike_test',
        'conductivity_qartod_rate_of_change_test',
        'conductivity_qartod_summary_flag',
        'conductivity_hysteresis_test',
    ]),
    ('Salinity', 'salinity', [
        'salinity_qartod_flat_line_test',
        'salinity_qartod_climatology_test',
        'salinity_qartod_spike_test',
        'salinity_qartod_rate_of_change_test',
        'salinity_qartod_summary_flag',
    ]),
    ('Density', 'density', [
        'density_qartod_flat_line_test',
        'density_qartod_climatology_test',
        'density_qartod_spike_test',
        'density_qartod_rate_of_change_test',
        'density_qartod_summary_flag',
    ]),
    ('Pressure (standard dbar)', 'pressure', [
        'pressure_qartod_gross_range_test',
        'pressure_qartod_flat_line_test',
        'pressure_qartod_pressure_test',
        'pressure_qartod_spike_test',
        'pressure_qartod_rate_of_change_test',
        'pressure_qartod_summary_flag',
    ]),
    ('Pressure — RBR sci_water_pressure (bar)', 'sci_water_pressure', [
        'sci_water_pressure_qartod_gross_range_test',
        'sci_water_pressure_qartod_flat_line_test',
        'sci_water_pressure_qartod_pressure_test',
        'sci_water_pressure_qartod_spike_test',
        'sci_water_pressure_qartod_rate_of_change_test',
        'sci_water_pressure_qartod_summary_flag',
    ]),
    ('Oxygen Concentration', 'oxygen_concentration', [
        'oxygen_concentration_qartod_gross_range_test',
        'oxygen_concentration_qartod_flat_line_test',
        'oxygen_concentration_qartod_summary_flag',
    ]),
    ('Oxygen Saturation', 'oxygen_saturation', [
        'oxygen_saturation_qartod_gross_range_test',
        'oxygen_saturation_qartod_flat_line_test',
        'oxygen_saturation_qartod_summary_flag',
    ]),
]

SCI_VARS = [
    ('temperature',           'degrees_C'),
    ('conductivity',          'S m⁻¹'),
    ('salinity',              'PSU'),
    ('density',               'kg m⁻³'),
    ('pressure',              'dbar'),
    ('sci_water_pressure',    'bar'),
    ('depth',                 'm'),
    ('depth_interpolated',    'm'),
    ('oxygen_concentration',  'µmol L⁻¹'),
    ('oxygen_saturation',     '%'),
    ('potential_temperature', '°C'),
]

CLIM_THRESHOLDS = {
    'temperature':  {'suspect_span': [22, 32],    'fail_span': [15, 35],    'units': '°C'},
    'conductivity': {'suspect_span': [3, 5.5],    'fail_span': [2, 6],      'units': 'S/m'},
    'salinity':     {'suspect_span': [33, 37.5],  'fail_span': [30, 40],    'units': 'PSU'},
    'density':      {'suspect_span': [1023, 1027],'fail_span': [1020, 1030],'units': 'kg/m³'},
}

# ── CSS ────────────────────────────────────────────────────────────────────────
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 14px;
    background: #f0f2f5;
    color: #2c3e50;
    line-height: 1.5;
}
.page { max-width: 1200px; margin: 0 auto; padding: 20px; }
header {
    background: linear-gradient(135deg, #1a252f 0%, #2c3e50 100%);
    color: #fff;
    padding: 28px 30px 20px;
    border-radius: 8px;
    margin-bottom: 20px;
}
header h1 { font-size: 22px; font-weight: 300; letter-spacing: 1px; color: #bdc3c7; }
header .subtitle { font-size: 24px; font-weight: 700; color: #3498db; margin: 4px 0 14px; font-family: monospace; }
.meta-bar { display: flex; gap: 10px; flex-wrap: wrap; }
.meta-bar span {
    background: rgba(255,255,255,0.1);
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    color: #bdc3c7;
}
.toc { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.toc a {
    color: #3498db; text-decoration: none;
    background: #eaf4fb; padding: 4px 14px;
    border-radius: 20px; font-size: 12px;
    border: 1px solid #d6eaf8;
}
.toc a:hover { background: #d6eaf8; }
section {
    background: #fff;
    padding: 24px;
    border-radius: 8px;
    margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
}
h2 {
    font-size: 17px; color: #2c3e50;
    border-left: 4px solid #3498db;
    padding-left: 12px; margin-bottom: 16px;
}
h3 {
    font-size: 14px; font-weight: 700; color: #34495e;
    margin: 20px 0 8px;
    padding-bottom: 5px;
    border-bottom: 2px solid #ecf0f1;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 6px 0 16px; }
th {
    background: #2c3e50; color: #ecf0f1;
    padding: 9px 14px; text-align: left;
    font-weight: 500; white-space: nowrap; font-size: 12px;
}
td { padding: 7px 14px; border-bottom: 1px solid #f4f4f4; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8fbff; }
.summary-row td { font-weight: 700; background: #f4f6f7 !important; border-top: 2px solid #dee2e6; }
td.good    { background: #d5f5e3; color: #1a7a40; font-weight: 700; text-align: center; }
td.suspect { background: #fdebd0; color: #b7510a; font-weight: 700; text-align: center; }
td.fail    { background: #fadbd8; color: #a93226; font-weight: 700; text-align: center; }
td.missing { background: #f4f6f7; color: #95a5a6; text-align: center; }
td.noteval { color: #bdc3c7; text-align: center; }
td.zero    { color: #ddd; text-align: center; }
td small   { display: block; font-size: 11px; font-weight: 400; opacity: 0.75; }
.legend {
    background: #f8f9fa; border: 1px solid #e8e8e8;
    border-radius: 6px; padding: 10px 16px; font-size: 12px;
    margin-bottom: 14px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
}
.legend b { color: #555; margin-right: 4px; }
.legend-item { display: flex; align-items: center; gap: 5px; }
.swatch { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.sw-good    { background: #2ecc71; }
.sw-suspect { background: #e67e22; }
.sw-fail    { background: #e74c3c; }
.sw-missing { background: #bdc3c7; }
.sw-noteval { background: #ecf0f1; border: 1px solid #ccc; }
.note {
    background: #eaf4fb; border-left: 4px solid #3498db;
    padding: 10px 16px; border-radius: 0 6px 6px 0;
    margin: 8px 0 12px; font-size: 13px;
}
.note.warn { background: #fef9e7; border-left-color: #e67e22; }
.pct-good    { color: #1a7a40; font-weight: 700; }
.pct-suspect { color: #b7510a; font-weight: 700; }
.pct-fail    { color: #a93226; font-weight: 700; }
.pct-missing { color: #95a5a6; }
.info-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px;
}
.info-card {
    background: #f8f9fa; border: 1px solid #e8e8e8;
    border-radius: 6px; padding: 12px 16px;
}
.info-card .label { font-size: 11px; color: #7f8c8d; text-transform: uppercase; letter-spacing: 0.5px; }
.info-card .value { font-size: 18px; font-weight: 700; color: #2c3e50; margin-top: 2px; }
.info-card .sub   { font-size: 11px; color: #95a5a6; margin-top: 2px; }
"""


# ── Helpers ────────────────────────────────────────────────────────────────────
def epoch_to_dt(epoch_val):
    return pd.to_datetime(float(epoch_val), unit='s', origin='unix')


def fmt_pct(n, total):
    if total == 0:
        return '0.0%'
    return f'{n / total * 100:.1f}%'


def flag_td(flag_key, count, total):
    """Colored <td> for a flag count + percentage."""
    if count == 0:
        return '<td class="zero">&mdash;</td>'
    cls = FLAG_CLASS[flag_key]
    pct = fmt_pct(count, total)
    return f'<td class="{cls}">{count:,}<small>{pct}</small></td>'


def test_label(var_name, sensor_key):
    """Convert full QC variable name to a short readable label."""
    suffix = var_name[len(sensor_key) + 1:]
    return TEST_LABELS.get(suffix, suffix)


def h(text):
    """Minimal HTML escape."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ── Single-pass data collection ───────────────────────────────────────────────
def collect_all(files):
    """
    One pass through all NetCDF files, collecting everything needed for the report.
    Returns: summary dict, var_ranges dict, qc_counts dict, clim_df DataFrame.
    """
    times, lats, lons = [], [], []
    total_obs   = 0
    nosci_count = 0
    depth_min   =  np.inf
    depth_max   = -np.inf

    var_min   = {}
    var_max   = {}
    var_count = {}
    var_units = {}

    qc_counts = {}

    clim_rows = []

    n = len(files)
    for i, f in enumerate(files, 1):
        if i % 200 == 0 or i == n:
            print(f'  [{i}/{n}] processed...')
        try:
            ds = xr.open_dataset(f, decode_times=False)
        except Exception as e:
            print(f'  Warning: could not open {os.path.basename(f)}: {e}', flush=True)
            continue

        try:
            # --- Deployment summary ---
            if 'profile_time' in ds:
                pt = float(ds['profile_time'].values.flat[0])
                if not np.isnan(pt):
                    times.append(epoch_to_dt(pt))
            if 'profile_lat' in ds:
                lats.append(float(ds['profile_lat'].values.flat[0]))
            if 'profile_lon' in ds:
                lons.append(float(ds['profile_lon'].values.flat[0]))
            if 'depth' in ds:
                d = ds['depth'].values.astype(float)
                vd = d[~np.isnan(d)]
                if len(vd):
                    depth_min = min(depth_min, float(vd.min()))
                    depth_max = max(depth_max, float(vd.max()))
            total_obs += int(ds.sizes.get('time', 0))
            if 'temperature' in ds and np.all(np.isnan(ds['temperature'].values.astype(float))):
                nosci_count += 1

            # --- Variable ranges (running min/max, no data accumulation) ---
            for var, default_units in SCI_VARS:
                if var not in ds.data_vars:
                    continue
                vals  = ds[var].values.astype(float)
                valid = vals[~np.isnan(vals)]
                if len(valid):
                    var_min[var]   = min(var_min.get(var,    np.inf), float(valid.min()))
                    var_max[var]   = max(var_max.get(var,  - np.inf), float(valid.max()))
                    var_count[var] = var_count.get(var, 0) + len(valid)
                if 'units' in ds[var].attrs:
                    var_units[var] = ds[var].attrs['units']
                elif var not in var_units:
                    var_units[var] = default_units

            # --- QC flag counts ---
            for _, sensor_key, qc_list in QC_VARS_ORDER:
                for v in qc_list:
                    if v not in ds.data_vars:
                        continue
                    vals = ds[v].values.astype(int).flatten()
                    if v not in qc_counts:
                        qc_counts[v] = {f: 0 for f in [1, 2, 3, 4, 9]}
                    for flag in [1, 2, 3, 4, 9]:
                        qc_counts[v][flag] += int(np.sum(vals == flag))

            # --- Climatology flagged observations ---
            for var, thr in CLIM_THRESHOLDS.items():
                flag_var = var + '_qartod_climatology_test'
                if var not in ds.data_vars or flag_var not in ds.data_vars:
                    continue
                vals  = ds[var].values.astype(float)
                flags = ds[flag_var].values.astype(int)
                depth = (ds['depth'].values.astype(float) if 'depth' in ds.data_vars
                         else np.full(len(vals), np.nan))
                mask  = (flags == 3) | (flags == 4)
                if not mask.any():
                    continue
                sus_lo, sus_hi   = thr['suspect_span']
                fail_lo, fail_hi = thr['fail_span']
                for idx in np.where(mask)[0]:
                    v  = float(vals[idx])
                    fl = int(flags[idx])
                    d  = float(depth[idx])
                    if fl == 4:
                        reason = f'&lt; fail_min ({fail_lo})' if v < fail_lo else f'&gt; fail_max ({fail_hi})'
                    else:
                        reason = f'&lt; suspect_min ({sus_lo})' if v < sus_lo else f'&gt; suspect_max ({sus_hi})'
                    clim_rows.append({'variable': var, 'depth_m': d, 'value': v,
                                      'flag': FLAG_MEANING.get(fl, str(fl)), 'reason': reason})
        except Exception as e:
            print(f'  Warning: error processing {os.path.basename(f)}: {e}', flush=True)
        finally:
            ds.close()

    summary = dict(
        times=times, lats=lats, lons=lons,
        total_obs=total_obs, nosci_count=nosci_count,
        depth_min=depth_min, depth_max=depth_max,
    )
    var_ranges = {
        var: {
            'min':   var_min[var],
            'max':   var_max[var],
            'count': var_count[var],
            'units': var_units.get(var, ''),
        }
        for var in var_min
    }
    clim_df = pd.DataFrame(clim_rows) if clim_rows else pd.DataFrame()
    return summary, var_ranges, qc_counts, clim_df


# ── HTML section builders ─────────────────────────────────────────────────────
def html_summary(n_files, s):
    times     = s['times']
    lats      = s['lats']
    lons      = s['lons']
    depth_min = s['depth_min']
    depth_max = s['depth_max']

    start_str = min(times).strftime('%Y-%m-%d %H:%M UTC') if times else 'N/A'
    end_str   = max(times).strftime('%Y-%m-%d %H:%M UTC') if times else 'N/A'
    days      = (max(times) - min(times)).days            if len(times) > 1 else 0
    lat_str   = f'{min(lats):.3f}–{max(lats):.3f} °N' if lats else 'N/A'
    lon_str   = f'{min(lons):.3f}–{max(lons):.3f} °E' if lons else 'N/A'
    dep_str   = (f'{max(0, depth_min):.1f}–{depth_max:.1f} m'
                 if depth_max > -np.inf else 'N/A')

    cards = [
        ('Total Profiles',     f'{n_files:,}',           'one per dive leg (NC file)'),
        ('Profiles missing science data', f'{s["nosci_count"]:,}', 'all NaN temperature'),
        ('Time observations',  f'{s["total_obs"]:,}',    ''),
        ('Start',              start_str,                 ''),
        ('End',                end_str,                   ''),
        ('Duration',           f'{days} days',            ''),
        ('Latitude range',     lat_str,                   ''),
        ('Longitude range',    lon_str,                   ''),
        ('Depth range',        dep_str,                   ''),
    ]

    html = '<div class="info-grid">\n'
    for label, value, sub in cards:
        html += (f'  <div class="info-card">'
                 f'<div class="label">{label}</div>'
                 f'<div class="value">{h(value)}</div>'
                 + (f'<div class="sub">{h(sub)}</div>' if sub else '')
                 + '</div>\n')
    html += '</div>\n'
    return html


def html_var_ranges(var_ranges):
    rows = ''
    for var, default_units in SCI_VARS:
        if var not in var_ranges:
            continue
        r = var_ranges[var]
        rows += (f'<tr><td><code>{h(var)}</code></td>'
                 f'<td>{h(r["units"])}</td>'
                 f'<td>{r["min"]:.4f}</td>'
                 f'<td>{r["max"]:.4f}</td>'
                 f'<td>{r["count"]:,}</td></tr>\n')
    return (
        '<table>\n'
        '<tr><th>Variable</th><th>Units</th><th>Min</th><th>Max</th><th>Non-NaN obs</th></tr>\n'
        + rows + '</table>\n'
    )


def html_qc_flags(qc_counts):
    legend = (
        '<div class="legend"><b>Flag colours:</b>'
        ' <span class="legend-item"><span class="swatch sw-good"></span>GOOD</span>'
        ' <span class="legend-item"><span class="swatch sw-suspect"></span>SUSPECT</span>'
        ' <span class="legend-item"><span class="swatch sw-fail"></span>FAIL</span>'
        ' <span class="legend-item"><span class="swatch sw-missing"></span>MISSING</span>'
        ' <span class="legend-item"><span class="swatch sw-noteval"></span>NOT_EVAL</span>'
        ' &nbsp;&nbsp;&mdash;&nbsp;&nbsp; counts show <em>n</em> &amp; % of total.'
        ' &nbsp;&nbsp;&#9733;&nbsp;= summary flag &nbsp; *&nbsp;= excluded from summary by design'
        '</div>\n'
    )
    html = legend
    for label, sensor_key, qc_list in QC_VARS_ORDER:
        present = [v for v in qc_list if v in qc_counts]
        if not present:
            continue
        html += f'<h3>{h(label)}</h3>\n'
        html += ('<table>\n'
                 '<tr><th>Test</th>'
                 '<th style="background:#1a7a40">GOOD</th>'
                 '<th style="background:#555">NOT_EVAL</th>'
                 '<th style="background:#b7510a">SUSPECT</th>'
                 '<th style="background:#a93226">FAIL</th>'
                 '<th style="background:#7f8c8d">MISSING</th>'
                 '</tr>\n')
        for v in present:
            c     = qc_counts[v]
            total = sum(c.values())
            label_txt = test_label(v, sensor_key)
            row_class = ' class="summary-row"' if 'summary_flag' in v else ''
            html += (f'<tr{row_class}>'
                     f'<td>{label_txt}</td>'
                     + flag_td(1, c[1], total)
                     + flag_td(2, c[2], total)
                     + flag_td(3, c[3], total)
                     + flag_td(4, c[4], total)
                     + flag_td(9, c[9], total)
                     + '</tr>\n')
        html += '</table>\n'
    return html


def html_climatology(clim_df):
    if clim_df.empty:
        return '<p><em>No climatology flags found.</em></p>\n'

    html = (
        '<p class="note">The climatology test is <strong>excluded from summary flags</strong> '
        'by design. This section shows what was flagged and why.</p>\n'
    )

    # 4.1 totals by variable
    html += '<h3>4.1 &nbsp; Total Flagged Observations</h3>\n'
    html += ('<table>\n'
             '<tr><th>Variable</th><th>SUSPECT</th><th>FAIL</th><th>Total</th></tr>\n')
    for var in CLIM_THRESHOLDS:
        sub = clim_df[clim_df.variable == var]
        if sub.empty:
            continue
        n_sus  = int((sub.flag == 'SUSPECT').sum())
        n_fail = int((sub.flag == 'FAIL').sum())
        sus_td  = f'<td class="suspect">{n_sus:,}</td>'  if n_sus  else '<td class="zero">&mdash;</td>'
        fail_td = f'<td class="fail">{n_fail:,}</td>'    if n_fail else '<td class="zero">&mdash;</td>'
        html += (f'<tr><td><code>{h(var)}</code></td>'
                 + sus_td + fail_td
                 + f'<td><strong>{len(sub):,}</strong></td></tr>\n')
    html += '</table>\n'

    # 4.2 reasons
    html += '<h3>4.2 &nbsp; Reasons for Flagging</h3>\n'
    html += ('<table>\n'
             '<tr><th>Variable</th><th>Flag</th><th>Reason</th><th>Count</th><th>Value range</th></tr>\n')
    for (var, flag, reason), grp in clim_df.groupby(['variable', 'flag', 'reason']):
        vmin  = round(grp.value.min(), 3)
        vmax  = round(grp.value.max(), 3)
        units = CLIM_THRESHOLDS[var]['units']
        cls   = 'suspect' if flag == 'SUSPECT' else 'fail'
        html += (f'<tr><td><code>{h(var)}</code></td>'
                 f'<td class="{cls}">{flag}</td>'
                 f'<td>{reason}</td>'
                 f'<td>{len(grp):,}</td>'
                 f'<td>{vmin}–{vmax} {h(units)}</td></tr>\n')
    html += '</table>\n'

    # 4.3 depth distribution
    html += '<h3>4.3 &nbsp; Depth Distribution of Flagged Observations</h3>\n'
    depth_bins   = [0, 50, 100, 200, 300, 400, 500]
    depth_labels = ['0–50 m', '50–100 m', '100–200 m',
                    '200–300 m', '300–400 m', '400–500 m']
    html += ('<table>\n<tr><th>Variable</th><th>Flag</th>'
             + ''.join(f'<th>{l}</th>' for l in depth_labels)
             + '</tr>\n')
    for var in CLIM_THRESHOLDS:
        for flag in ['SUSPECT', 'FAIL']:
            sub = clim_df[(clim_df.variable == var) & (clim_df.flag == flag)]
            if sub.empty:
                continue
            cut  = pd.cut(sub.depth_m, bins=depth_bins)
            cnts = cut.value_counts().sort_index()
            cls  = 'suspect' if flag == 'SUSPECT' else 'fail'
            cells = ''
            for interval in cnts.index:
                n = int(cnts[interval])
                cells += f'<td>{n:,}</td>' if n else '<td class="zero">&mdash;</td>'
            html += (f'<tr><td><code>{h(var)}</code></td>'
                     f'<td class="{cls}">{flag}</td>{cells}</tr>\n')
    html += '</table>\n'

    # 4.4 notes
    html += '<h3>4.4 &nbsp; Notes</h3>\n'

    t_fail = clim_df[(clim_df.variable == 'temperature') & (clim_df.flag == 'FAIL')]
    t_sus  = clim_df[(clim_df.variable == 'temperature') & (clim_df.flag == 'SUSPECT')]
    if not t_fail.empty or not t_sus.empty:
        c = CLIM_THRESHOLDS['temperature']
        html += (
            f'<div class="note">'
            f'<strong>Temperature:</strong> USVI climatology: suspect {c["suspect_span"]}°C, '
            f'fail {c["fail_span"]}°C. '
            f'Flagged observations are deep-water measurements where temperature naturally falls '
            f'below the surface climatology bounds — <em>this is physically correct data</em>.'
        )
        if not t_sus.empty:
            html += (f' Suspect: {t_sus.depth_m.min():.0f}–{t_sus.depth_m.max():.0f} m depth, '
                     f'{t_sus.value.min():.1f}–{t_sus.value.max():.1f}°C.')
        if not t_fail.empty:
            html += (f' Fail: {t_fail.depth_m.min():.0f}–{t_fail.depth_m.max():.0f} m depth, '
                     f'{t_fail.value.min():.1f}–{t_fail.value.max():.1f}°C.')
        html += '</div>\n'

    c_sus = clim_df[(clim_df.variable == 'conductivity') & (clim_df.flag == 'SUSPECT')
                    & (clim_df.reason.str.contains('suspect_max'))]
    if not c_sus.empty:
        c = CLIM_THRESHOLDS['conductivity']
        html += (
            f'<div class="note warn">'
            f'<strong>Conductivity:</strong> {len(c_sus):,} observations exceed the suspect_max '
            f'threshold of {c["suspect_span"][1]} S/m. Warm USVI surface water (~27–29°C, '
            f'~36–37 PSU) can reach 5.7–5.9 S/m, which exceeds this threshold. '
            f'<strong>The threshold may be set too low for peak tropical surface conditions '
            f'and should be reviewed.</strong>'
            f'</div>\n'
        )

    s_sus = clim_df[(clim_df.variable == 'salinity') & (clim_df.flag == 'SUSPECT')]
    if not s_sus.empty:
        c = CLIM_THRESHOLDS['salinity']
        html += (
            f'<div class="note">'
            f'<strong>Salinity:</strong> {len(s_sus):,} observations below suspect_min '
            f'({c["suspect_span"][0]} PSU). May indicate near-surface freshening or calibration '
            f'drift. Depth range: {s_sus.depth_m.min():.0f}–{s_sus.depth_m.max():.0f} m.'
            f'</div>\n'
        )

    d_sus = clim_df[clim_df.variable == 'density']
    if not d_sus.empty:
        c = CLIM_THRESHOLDS['density']
        html += (
            f'<div class="note">'
            f'<strong>Density:</strong> Observations outside suspect span {c["suspect_span"]} kg/m³ '
            f'are predominantly deep-water observations where density exceeds the surface climatology '
            f'bounds — physically correct.'
            f'</div>\n'
        )

    return html


def html_findings(qc_counts):
    # Collect summary flags only
    summary_vars = sorted(
        [v for v in qc_counts if '_qartod_summary_flag' in v]
    )
    if not summary_vars:
        return '<p><em>No summary flags found.</em></p>\n'

    html = (
        '<p style="margin-bottom:12px;color:#555;font-size:13px;">'
        'Based on <code>*_qartod_summary_flag</code> variables. '
        'Climatology test is excluded from these by design.</p>\n'
        '<table>\n'
        '<tr><th>Variable</th>'
        '<th style="background:#1a7a40">% Good</th>'
        '<th style="background:#b7510a">% Suspect</th>'
        '<th style="background:#a93226">% Fail</th>'
        '<th style="background:#7f8c8d">% Missing</th>'
        '</tr>\n'
    )

    for v in summary_vars:
        c     = qc_counts[v]
        total = sum(c.values())
        def pct(flag): return c[flag] / total * 100 if total else 0

        def pct_cell(flag, cls):
            val = pct(flag)
            if val == 0:
                return '<td class="zero">&mdash;</td>'
            return f'<td class="{cls}"><strong>{val:.1f}%</strong></td>'

        html += (
            f'<tr><td><code>{h(v)}</code></td>'
            + pct_cell(1, 'good')
            + pct_cell(3, 'suspect')
            + pct_cell(4, 'fail')
            + pct_cell(9, 'missing')
            + '</tr>\n'
        )
    html += '</table>\n'
    return html


# ── Assemble full HTML page ───────────────────────────────────────────────────
def build_html(deployment, mode, level, data_dir, now,
               n_files, summary, var_ranges, qc_counts, clim_df):

    toc = (
        '<div class="toc">'
        '<a href="#s1">1. Deployment Summary</a>'
        '<a href="#s2">2. Variable Ranges</a>'
        '<a href="#s3">3. QC Flag Distributions</a>'
        '<a href="#s4">4. Climatology Flags</a>'
        '<a href="#s5">5. Key Findings</a>'
        '</div>\n'
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QC Report &mdash; {h(deployment)}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="page">

<header>
  <h1>QC Report</h1>
  <div class="subtitle">{h(deployment)}</div>
  <div class="meta-bar">
    <span>Generated: {h(now)}</span>
    <span>Mode: {h(mode)}</span>
    <span>Level: {h(level)}</span>
    <span>Files: {n_files:,}</span>
  </div>
</header>

{toc}

<section id="s1">
  <h2>1. Deployment Summary</h2>
  {html_summary(n_files, summary)}
</section>

<section id="s2">
  <h2>2. Science Variable Ranges</h2>
  {html_var_ranges(var_ranges)}
</section>

<section id="s3">
  <h2>3. QC Flag Distributions</h2>
  {html_qc_flags(qc_counts)}
</section>

<section id="s4">
  <h2>4. Climatology Flag Analysis</h2>
  {html_climatology(clim_df)}
</section>

<section id="s5">
  <h2>5. Key Findings &mdash; Summary Flags</h2>
  {html_findings(qc_counts)}
</section>

</div>
</body>
</html>
"""
    return page


# ── Main ──────────────────────────────────────────────────────────────────────
def _in_jupyter():
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


def main(deployment=None, mode='delayed', level='sci', data_home=None):
    if not _in_jupyter() and deployment is None:
        parser = argparse.ArgumentParser(
            description='Generate styled HTML QC report for a glider deployment')
        parser.add_argument('deployment',
                            help='Deployment name, e.g. uvi_01-20250318T2008')
        parser.add_argument('--mode',      default='delayed', choices=['rt', 'delayed'])
        parser.add_argument('--level',     default='sci',     choices=['sci', 'ngdac'])
        parser.add_argument('--data_home', default=None,
                            help='Root directory (default: folder containing this script)')
        args       = parser.parse_args()
        deployment = args.deployment
        mode       = args.mode
        level      = args.level
        data_home  = args.data_home

    if deployment is None:
        raise ValueError("Pass a deployment name, e.g. main(deployment='uvi_01-20250318T2008')")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_home  = data_home or script_dir
    year = deployment.split('-')[1][:4]

    data_dir = os.path.join(data_home, 'deployments', year, deployment,
                            'data', 'out', 'nc', f'{level}-profile', mode)
    out_dir  = os.path.join(data_home, 'deployments', year, deployment, 'reports')
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isdir(data_dir):
        print(f'ERROR: data directory not found:\n  {data_dir}')
        return 1

    files = sorted(glob.glob(os.path.join(data_dir, '*.nc')))
    files = [f for f in files if 'qc_queue' not in f]
    print(f'Found {len(files)} NC files in {data_dir}')
    if not files:
        print('No files found.')
        return 1

    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    print('Loading data (single pass)...')
    summary, var_ranges, qc_counts, clim_df = collect_all(files)

    print('Building report...')
    html = build_html(deployment, mode, level, data_dir, now,
                      len(files), summary, var_ranges, qc_counts, clim_df)

    out_path = os.path.join(out_dir, f'qc_report_{deployment}.html')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(html)

    print(f'\nReport saved to: {out_path}')
    return 0


# ── CONFIGURATION — edit this block when running in VS Code or Jupyter ────────
DEPLOYMENT = 'uvi_01-20250318T2008'   # deployment name
MODE       = 'delayed'                 # 'rt' or 'delayed'
LEVEL      = 'sci'                     # 'sci' or 'ngdac'
DATA_HOME  = None                      # None = folder containing this script
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if _in_jupyter():
        main(deployment=DEPLOYMENT, mode=MODE, level=LEVEL, data_home=DATA_HOME)
    else:
        sys.exit(main())
