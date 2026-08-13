# Glider Processing Pipeline — Future Work (Needs Input)

Items that can't be resolved by editing code alone — either they need a real-world value/dataset
that doesn't exist anywhere in this repo, or a decision only Travis can make. Distinct from
`KNOWN_ISSUES.md`, which tracks defects that *are* fixable purely in code; each entry here links
back to its `KNOWN_ISSUES.md` section for the full technical detail rather than repeating it.

## 1. USVI Total Alkalinity regression coefficients — needs real water-sample data

**Needs:** in-situ TA/salinity bottle-sample data for the USVI region, to derive
temperature/salinity regression coefficients in the same form as the existing `nyb` (Mid-Atlantic
Bight) block in `qc/config/derived_variables/TA-salinity-regressions.yml` — that block was sourced
from a published study (Wright-Fairbanks et al 2020) for the MAB region specifically; USVI has no
equivalent study/dataset backing it yet.

**Why it's blocked:** `TA-salinity-regressions.yml` currently defines exactly one region (`nyb`),
nowhere near USVI (lon ≈ -65, lat ≈ 18). `calculate_ta()` silently returns empty results when no
region matches, so `total_alkalinity` — and `calculate_omega` (Aragonite Saturation State), which
depends on it — never compute for any USVI deployment.

**Once the data exists:** add a USVI region block to `TA-salinity-regressions.yml`. (Separately,
`calculate_ta()`'s no-region-matched fallthrough should also get a log line so this doesn't fail
silently for the *next* region gap — that part isn't blocked on data and could be done now.)

See `KNOWN_ISSUES.md` → "Active bugs affecting real QC output" for the code-level detail.

## 2. Whether/when to apply USVI's tighter oxygen QC bounds — policy decision

**Needs:** a decision from Travis on timing/scope, not new data — the bounds themselves already
exist.

**What's pending:** `usvi_configs.yml` already defines tighter, region-specific gross-range/
flat-line bounds for `oxygen_concentration`/`oxygen_saturation`, but they're not wired to actually
run — `run_regional_stream_tests()` doesn't implement those two test types yet, so oxygen QC
currently runs on the looser generic per-instrument bounds instead
(`qc/config/gross_flatline/optode_4831_gross_flatline.yml`).

**Why this isn't just a bug fix:** implementing it would overwrite the current oxygen QC flag
outcomes fleet-wide, across every USVI deployment, not just one — a real change to QC results, not
a correctness fix. Needs a deliberate go-ahead before implementing.

See `KNOWN_ISSUES.md` → "Active bugs affecting real QC output" → "Configured but never applied
(Option B)" for full detail.

## 3. MATLAB oxygen lag-correction code — must fix the profile-pairing bug before porting in

**Needs:** confirmation that this fix goes in *during* the port, not after — flagged here so it
isn't forgotten once the port is underway.

**The bug:** the down/up pairing loop pairs every consecutive profile index (`(1,2), (2,3), (3,4),
…`) instead of stepping by 2 through true down/up dive pairs (`(1,2), (3,4), (5,6), …`). Each
profile gets written to twice, and the later write always wins — so every up-cast profile's final
saved oxygen correction ends up computed from a bogus pairing with the *next* dive's down-cast
instead of its own true partner. Confirmed empirically with an isolated Python reproduction of the
exact loop logic: 3 of 8 profiles came out wrong, in a clean run with zero rejected profiles (i.e.
this isn't a rejection/edge-case-only bug — it fires on a perfectly normal run).

**Fix when porting:** step the pairing loop by 2 (`for np = 1:2:nI-1` in the original MATLAB) —
but first confirm the down/up-cast alternation assumption actually holds for whatever
profile-numbering scheme the real data uses; that assumption was never verified against real data
since this script isn't in this repo yet.

**Also worth fixing during the same port** (lower severity, same script): `MOtime` is set from the
CTD/science-computer clock instead of the optode's own `oxy4_timestamp` (a commented-out line
shows this was the original intent); and the `×10` pressure conversion assumes bar-unit
`sci_water_pressure` (this pipeline's RBR Legato³ convention) — would silently be wrong if ever fed
a different CTD's pressure that's already in dbar.

See `KNOWN_ISSUES.md` → "Not yet in this repo — flagged proactively for planned pipeline
integration" for the full walkthrough and the Python reproduction.
