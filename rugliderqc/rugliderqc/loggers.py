#!/usr/bin/env python

import os
import pwd
from datetime import datetime
import logging


def logfile_basename():
    logdir = '/home/glider/rugliderqc/logs'
    os.makedirs(logdir, exist_ok=True)

    user = pwd.getpwuid(os.getuid())[0]
    return os.path.join(logdir, f'{user}-glider_qc.log')



def logfile_deploymentname(deployment, dataset_type, cdm_data_type, mode):
    user = pwd.getpwuid(os.getuid())[0]
    # Use QC_RUN_TS from the environment when set — the pipeline stage runner exports it
    # so every QC script in the same run lands on the same per-run log file.
    # Fall back to a fresh timestamp when scripts are invoked manually outside the pipeline.
    run_ts = os.environ.get('QC_RUN_TS') or datetime.now().strftime('%Y%m%dT%H%M%S')
    logfilename = '-'.join([user, run_ts + '_' + deployment,
                            dataset_type, cdm_data_type, mode, 'qc']) + '.log'

    return logfilename


def setup_logger(name, loglevel, logfile):
    logger = logging.getLogger(name)

    # if the logger doesn't already exist, set it up
    if not logger.handlers:
        log_format = logging.Formatter('%(asctime)s%(module)s:%(levelname)s:%(message)s [line %(lineno)d]')
        handler = logging.FileHandler(logfile)
        handler.setFormatter(log_format)

        log_level = getattr(logging, loglevel)
        logger.setLevel(log_level)
        logger.addHandler(handler)

    return logger
