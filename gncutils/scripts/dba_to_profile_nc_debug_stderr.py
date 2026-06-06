#!/usr/bin/env python

import os
import sys
import logging
import argparse
import tempfile
import shutil
from gncutils.writers.slocum.ProfileNetCDFWriter import ProfileNetCDFWriter as SlocumProfileNetCDFWriter
from gncutils.readers.slocum import create_llat_dba_reader, derive_ctd_parameters
from gncutils.yo import build_yo, find_profiles
from gncutils.constants import NETCDF_FORMATS

def main(args):
    """Parse one or more Slocum glider ascii dba files and write CF-compliant Profile NetCDF files"""

    # Set up logger
    log_level = getattr(logging, args.loglevel.upper())
    log_format = '%(asctime)s:%(module)s:%(levelname)s:%(message)s [line %(lineno)d]'
    logging.basicConfig(format=log_format, level=log_level)

    config_path = args.config_path
    output_path = args.output_path or os.path.realpath(os.curdir)
    dba_files = args.dba_files
    start_profile_id = args.start_profile_id
    clobber = args.clobber
    comp_level = args.compression
    nc_format = args.nc_format
    ngdac_extensions = args.ngdac
    debug = args.debug

    if debug:
        logging.info('Debug mode: No files will be processed')

    if not os.path.isdir(config_path):
        logging.error(f'Invalid configuration directory: {config_path}')
        return 1

    if not os.path.isdir(output_path):
        logging.error(f'Invalid output_path: {output_path}')
        return 1

    if not dba_files:
        logging.error('No Slocum dba files specified')
        return 1

    ncw = SlocumProfileNetCDFWriter(
        config_path,
        comp_level=comp_level,
        nc_format=nc_format,
        profile_id=start_profile_id,
        clobber=clobber
    )

    if not args.clobber:
        logging.info('Keeping existing NetCDF files')

    output_nc_files = []

    if not args.science:
        logging.info('Writing unprocessed NetCDF files')
        if debug:
            sys.stdout.write(f'{ncw}\n')
            return 0
        output_nc_files = ncw.dbas_to_profile_nc(dba_files, output_path, ngdac_extensions=ngdac_extensions)
    else:
        logging.info('Writing science NetCDF files')
        if debug:
            sys.stdout.write(f'{ncw}\n')
            return 0

        tmp_dir = tempfile.mkdtemp()
        logging.debug(f'Temporary NetCDF directory: {tmp_dir}')

        for dba_file in dba_files:
            try:
                logging.info(f'Processing dba: {dba_file}')
                dba = create_llat_dba_reader(dba_file)
                if dba is None:
                    continue

                yo = build_yo(dba)
                if yo is None:
                    continue

                try:
                    profile_times = find_profiles(yo)
                except ValueError as e:
                    logging.error(f'{dba_file}: {e}')
                    continue

                logging.info(f'Indexed {len(profile_times)} profiles')
                if len(profile_times) == 0:
                    continue

                # === BEGIN INPUT VAR CHECK ===
                print("Pre-checking required input vars for CTD computation:", file=sys.stderr)
                for var in ["sci_water_temp", "sci_water_cond", "sci_water_pressure"]:
                    if var in dba["data"]:
                        print(f"✅ {var} is present in dba['data']", file=sys.stderr)
                    else:
                        print(f"❌ {var} is missing in dba['data']", file=sys.stderr)
                # === END INPUT VAR CHECK ===

                dba = derive_ctd_parameters(dba)

                # === BEGIN DEBUG BLOCK ===
                print("Derived variables available in sensors:", file=sys.stderr)
                for var in ["salinity", "density", "sound_speed", "potential_temperature"]:
                    if var in dba["sensors"]:
                        print(f"✅ {var} defined in sensors", file=sys.stderr)
                    else:
                        print(f"❌ {var} missing in sensors", file=sys.stderr)

                print("Derived variables available in data:", file=sys.stderr)
                for var in ["salinity", "density", "sound_speed", "potential_temperature"]:
                    if var in dba["data"]:
                        values = dba["data"][var]
                        if hasattr(values, "__len__") and len(values) and not all(
                            [v is None or (hasattr(v, 'isnan') and v.isnan()) for v in values]):
                            print(f"✅ {var} contains data", file=sys.stderr)
                        else:
                            print(f"⚠️ {var} present but all values are NaN", file=sys.stderr)
                    else:
                        print(f"❌ {var} missing from data", file=sys.stderr)
                # === END DEBUG BLOCK ===

                ncw.update_data_file_sensor_defs(dba['sensors'], override=False)
                nc_files = ncw.dba_obj_to_profile_nc(dba, output_path, tmp_dir=tmp_dir, ngdac_extensions=ngdac_extensions)
                if nc_files:
                    output_nc_files += nc_files

            except Exception as e:
                logging.exception(f"Unhandled error while processing {dba_file}")

        logging.info(f'{len(output_nc_files)} NetCDF files written: {output_path}')

        try:
            logging.debug(f'Removing temporary directory: {tmp_dir}')
            shutil.rmtree(tmp_dir)
        except OSError as e:
            logging.error(e)

    for output_nc_file in output_nc_files:
        os.chmod(output_nc_file, 0o664)
        sys.stdout.write(f'{output_nc_file}\n')

    return 0


if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser(description=main.__doc__,
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    arg_parser.add_argument('config_path', help='Location of deployment configuration files')
    arg_parser.add_argument('dba_files', help='Source ASCII dba files to process', nargs='+')
    arg_parser.add_argument('-s', '--science', action='store_true',
                            help='Calculate derived scientific parameters and add to NetCDF if sensor definitions exist')
    arg_parser.add_argument('--ngdac', action='store_true',
                            help='Name output files using IOOS NGDAC naming conventions')
    arg_parser.add_argument('-p', '--start_profile_id', type=int, default=0,
                            help='Starting profile ID (default: mean profile unix timestamp)')
    arg_parser.add_argument('-o', '--output_path', help='Directory to write NetCDF files to (must exist)')
    arg_parser.add_argument('-c', '--clobber', action='store_true', help='Overwrite existing NetCDF files')
    arg_parser.add_argument('-f', '--format', dest='nc_format',
                            choices=NETCDF_FORMATS, default='NETCDF4_CLASSIC',
                            help='NetCDF format')
    arg_parser.add_argument('--compression', choices=range(10), default=1,
                            help='NetCDF4 compression level (0–9)')
    arg_parser.add_argument('-x', '--debug', action='store_true',
                            help='Dry run: initialize NetCDF writer, but skip file processing')
    arg_parser.add_argument('-l', '--loglevel', type=str,
                            choices=['debug', 'info', 'warning', 'error', 'critical'],
                            default='info', help='Logging verbosity')

    parsed_args = arg_parser.parse_args()
    sys.exit(main(parsed_args))
