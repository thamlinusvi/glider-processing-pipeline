# To Do

- [ ] **Re-run Stage 4 (QARTOD QC) for `uvi_01-20241026T1158`** now that the `ctd_hysteresis_test.py`
  and `time_shift.py` bar/dbar fixes are baked into the rebuilt `glider-processing` Docker image.
  Takes ~147 minutes. Pre-fix QC output is already backed up at
  `qc_fix_comparison/uvi_01-20241026T1158_PRE_bar_dbar_fix/` (625 files, verified complete) for
  comparison — do not delete that backup before the comparison is done.
  - [ ] Once the run finishes, compare before/after: QARTOD flag counts, computed
    `oxygen_concentration_optimal_shift`/`oxygen_saturation_optimal_shift` values, and how many
    profiles' hysteresis test results changed, to quantify how far off the previously-processed
    data was. See `KNOWN_ISSUES.md` ("Other rugliderqc QC-script issues") and
    `CHANGES_FROM_RUTGERS.md` §14 for the fix details and the smaller-scale evidence already
    gathered before the fix.
