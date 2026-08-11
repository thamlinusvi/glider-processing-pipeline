#!/usr/bin/env python

"""
Run region-aware QARTOD tests (climatology, spike, rate of change, pressure) against
derived science variables that don't exist yet when glider_qartod_qc.py makes its own
regional-streams pass.

Why this script exists: glider_qartod_qc.py runs before add_derived_variables.py in the
Stage 8 pipeline (glider_qartod_qc needs to have already produced the individual QC test
flags that add_derived_variables's apply_qc() depends on for masking bad CTD/voltage
values before computing pH). That means any regional QC config for a variable that only
exists *after* add_derived_variables runs -- currently just pH, via
qc/config/global_regional/usvi_configs.yml's 'pH' stream block -- never gets applied by
glider_qartod_qc's own pass, since it skips any sensor not yet present in the dataset.
This script re-runs just that regional-streams logic (reused, not duplicated, from
glider_qartod_qc.py), scoped to pH only, after pH has been computed.

Cheap no-op by design: as of this writing, no deployment in this fleet has a pH sensor,
so this script needs to stay effectively free on every real run. It samples a handful of
files (first + last 10) to check whether pH is present at all before doing any real work;
set ph_regional_qc_check_all_files: true in a deployment's qctests.yml to check every file
instead, for the rare case a pH sensor was turned on partway through a mission and might
not show up in the sampled files.
"""

import os
import sys
import glob
import argparse
import xarray as xr
from ioos_qc import qartod
from ioos_qc.utils import load_config_as_dict as loadconfig
import rugliderqc.common as cf
from rugliderqc.loggers import logfile_basename, setup_logger, logfile_deploymentname
from glider_qartod_qc import build_global_regional_config, run_regional_stream_tests

TARGET_SENSOR = 'pH'
SAMPLE_TAIL_COUNT = 10


def _sample_files(ncfiles):
    """First file + last SAMPLE_TAIL_COUNT files. Below that count, just returns everything."""
    if len(ncfiles) <= SAMPLE_TAIL_COUNT + 1:
        return list(ncfiles)
    return [ncfiles[0]] + ncfiles[-SAMPLE_TAIL_COUNT:]


def _file_has_sensor(f, sensor):
    with xr.open_dataset(f, decode_times=False) as ds:
        return sensor in ds.data_vars


def _run_gross_range_test(ds, sensor, cinfo):
    """
    Run a QARTOD gross_range_test and add the resulting QC variable to ds, mirroring
    glider_qartod_qc.py's own manual gross_range_test pattern (used there for the
    depth-rating pressure check). 'gross_range_test' isn't one of the four test types
    run_regional_stream_tests() handles (pressure/climatology/spike/rate_of_change) --
    usvi_configs.yml's pH block uses it, so it's implemented directly here rather than
    changing that shared, already-verified function's behavior for every other caller.
    """
    qc_varname = f'{sensor}_qartod_gross_range_test'
    flag_vals = qartod.gross_range_test(inp=ds[sensor].values, **cinfo)

    attrs = cf.set_qartod_attrs('gross_range_test', sensor, cinfo)
    if not hasattr(ds[sensor], 'ancillary_variables'):
        ds[sensor].attrs['ancillary_variables'] = qc_varname
    else:
        existing_av = ds[sensor].ancillary_variables.replace(',', ' ').strip()
        existing_list = existing_av.split()
        if qc_varname not in existing_list:
            ds[sensor].attrs['ancillary_variables'] = ' '.join(existing_list + [qc_varname])

    da = xr.DataArray(flag_vals.astype('int32'), coords=ds[sensor].coords, dims=ds[sensor].dims,
                      name=qc_varname, attrs=attrs)
    cf.set_encoding(da)
    ds[qc_varname] = da


def main(args):
    status = 0

    loglevel = args.loglevel.upper()
    cdm_data_type = args.cdm_data_type
    mode = args.mode
    dataset_type = args.level
    test = args.test

    logFile_base = logfile_basename()
    logging_base = setup_logger('logging_base', loglevel, logFile_base)

    data_home, deployments_root = cf.find_glider_deployments_rootdir(logging_base, test)
    if isinstance(deployments_root, str):

        qc_config_root = os.path.join(data_home, 'qc', 'config')
        if not os.path.isdir(qc_config_root):
            logging_base.warning('Invalid QC config root: {:s}'.format(qc_config_root))
            return 1

        for deployment in args.deployments:

            data_path, deployment_location = cf.find_glider_deployment_datapath(logging_base, deployment, deployments_root,
                                                                                dataset_type, cdm_data_type, mode)

            if not data_path:
                logging_base.error('{:s} data directory not found:'.format(deployment))
                continue

            if not os.path.isdir(os.path.join(deployment_location, 'proc-logs')):
                logging_base.error('{:s} deployment proc-logs directory not found:'.format(deployment))
                continue

            logfilename = logfile_deploymentname(deployment, dataset_type, cdm_data_type, mode)
            logFile = os.path.join(deployment_location, 'proc-logs', logfilename)
            logging = setup_logger('logging', loglevel, logFile)

            deployment_qc_config_root = os.path.join(deployment_location, 'config', 'qc')
            if not os.path.isdir(deployment_qc_config_root):
                logging.warning('Invalid deployment QC config root: {:s}'.format(deployment_qc_config_root))

            qctests_config_dict = {}
            qctests_config_file = os.path.join(deployment_qc_config_root, 'qctests.yml')
            if os.path.isfile(qctests_config_file):
                qctests_config_dict = loadconfig(qctests_config_file)
                if not qctests_config_dict.get('qartod', True):
                    logging.warning(
                        'Not running derived-variable regional QC because qartod test is turned off, check: {:s}'.format(
                            qctests_config_file))
                    continue

            check_all_files = qctests_config_dict.get('ph_regional_qc_check_all_files', False)

            ncfiles = sorted(glob.glob(os.path.join(data_path, 'qc_queue', '*.nc')))
            if len(ncfiles) == 0:
                logging.error(' 0 files found to QC: {:s}'.format(os.path.join(data_path, 'qc_queue')))
                status = 1
                continue

            if check_all_files:
                logging.info('ph_regional_qc_check_all_files is set -- checking every file for {:s}'.format(TARGET_SENSOR))
                found = any(_file_has_sensor(f, TARGET_SENSOR) for f in ncfiles)
            else:
                sample = _sample_files(ncfiles)
                found = any(_file_has_sensor(f, TARGET_SENSOR) for f in sample)

            if not found:
                logging.info(
                    '{:s} not found -- skipping derived-variable regional QC. If {:s} exists somewhere '
                    'outside the sampled files (first + last {:d}), set ph_regional_qc_check_all_files: true '
                    'in this deployment\'s qctests.yml to check every file.'.format(
                        TARGET_SENSOR, TARGET_SENSOR, SAMPLE_TAIL_COUNT))
                continue

            logging.info('{:s} found -- running regional QC: {:s}'.format(TARGET_SENSOR, os.path.join(data_path, 'qc_queue')))

            for f in ncfiles:
                try:
                    with xr.open_dataset(f, decode_times=False) as ds:
                        ds = ds.load()
                except OSError as e:
                    logging.error('Error reading file {:s} ({:})'.format(f, e))
                    status = 1
                    continue

                if TARGET_SENSOR not in ds.data_vars:
                    continue

                c = build_global_regional_config(ds, qc_config_root)
                times = cf.convert_epoch_ts(ds['time'])

                config_info = c['streams'].get(TARGET_SENSOR)
                if config_info is None:
                    logging.debug('{:s} not configured for region {:s} -- skipping {:s}'.format(
                        TARGET_SENSOR, c['region'], os.path.basename(f)))
                    ds.close()
                    continue

                # gross_range_test isn't one of the test types run_regional_stream_tests()
                # handles -- run it directly and remove it from the config before calling
                # that function (see _run_gross_range_test's docstring for why).
                gross_range_cinfo = config_info['qartod'].pop('gross_range_test', None)
                if gross_range_cinfo is not None:
                    _run_gross_range_test(ds, TARGET_SENSOR, gross_range_cinfo)

                if config_info['qartod']:
                    run_regional_stream_tests(ds, c, times, logging, sensors=[TARGET_SENSOR])

                ds.to_netcdf(f)
                ds.close()

            logging.info('Derived-variable regional QC finished')
    return status


if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser(description=main.__doc__,
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    arg_parser.add_argument('deployments',
                            nargs='+',
                            help='Glider deployment name(s) formatted as glider-YYYYmmddTHHMM')

    arg_parser.add_argument('-m', '--mode',
                            help='Deployment dataset status',
                            choices=['rt', 'delayed'],
                            default='rt')

    arg_parser.add_argument('--level',
                            choices=['sci', 'ngdac'],
                            default='sci',
                            help='Dataset type')

    arg_parser.add_argument('-d', '--cdm_data_type',
                            help='Dataset type',
                            choices=['profile'],
                            default='profile')

    arg_parser.add_argument('-l', '--loglevel',
                            help='Verbosity level',
                            type=str,
                            choices=['debug', 'info', 'warning', 'error', 'critical'],
                            default='info')

    arg_parser.add_argument('-test', '--test',
                            help='Point to the environment variable key GLIDER_DATA_HOME_TEST for testing.',
                            action='store_true')

    parsed_args = arg_parser.parse_args()

    sys.exit(main(parsed_args))
