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

- [ ] **Get `AA_compute.m` from Travis, then resume the Python port of
  `C:\Users\900094088\Downloads\glider_processing_improved.m`** (oxygen phase-lag correction —
  see `KNOWN_ISSUES.md`'s "Not yet in this repo" MATLAB entry for the original diagnosis, and its
  `n+1`→`n+2` down/up-cast pairing fix, already confirmed with Travis). Blocked: the script's
  actual corrected-oxygen output (`OCm1`, the whole point of the script) comes entirely from
  `AA_compute(phsBest, OT, salinity, pressure)` (line 346) — the Aanderaa optode phase-to-
  concentration conversion, run on the lag-shifted phase signal. The NC file's existing
  `oxygen_concentration` variable is only used earlier for quality-filtering, never as the
  output — traced this directly, confirmed there's no way around needing the real formula. Can't
  reconstruct it from general Aanderaa-optode knowledge either: it needs this sensor's own
  foil-batch calibration coefficients, which live inside `AA_compute.m`, not derivable from the
  sensor model name. Everything else in the script (NC loading, time-grid interpolation, lag
  search, `gsw` calls) has a direct, already-identified Python equivalent and can be ported
  without this file — only the actual oxygen-recompute step is blocked.
