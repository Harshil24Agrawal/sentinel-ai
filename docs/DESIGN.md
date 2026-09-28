# SentinelAI — Design decisions

The reasoning behind each choice (CLAUDE.md §12). The ten headline decisions are filled in
during Phase 8; the notes below are recorded as they are made.

## Headline decisions

1. Statistics over trained ML
2. Median/MAD over mean/std
3. Baseline freeze during incidents
4. Hysteresis
5. Minimum volume and absolute floor
6. Patterns and dynamic tokens
7. No seasonality
8. Two-tier AI (statistics decide when; the LLM only explains)
9. Redaction before storage, LLM and AWS
10. Isolation Forest as a second opinion only

## Implementation notes and known limits

### Cost model: the window is O(1), the baseline is O(n log n) with n = 900

- **Sliding window (aggregator).** Each tick seals one 1-second bucket per service and
  adds/subtracts whole buckets from running totals (lines, errors, warns, pattern counts, and
  a sorted latency list for p95). Window statistics cost the same whether the window is 60
  seconds or 60 minutes.
- **Baseline.** Median and MAD are recomputed from a fixed-size history of at most
  `BASELINE_HISTORY_SAMPLES` = 900 values, lazily, once per appended sample. That is an
  O(n log n) sort with n capped at 900 (about 50 µs), per metric, per service, per tick.
  It is bounded, does not grow with uptime, and is negligible next to log parsing; it is
  *not* O(1). The MAD makes an incremental update awkward (every deviation changes when the
  median moves), and 900 is small enough that simplicity wins.

### Warm-up takes about 90 seconds

A service is judged only when it is `ready`: its first 60-second window has filled AND
`WARMUP_SAMPLES` = 30 baseline samples exist. Samples are taken only from a *full* window
(a half-filled window has a biased volume), so the first sample arrives at second 60 and the
30th at second ~90. Nothing alerts before that. Both numbers are configuration.

### Why a 3-second blip never pages anyone: window smoothing plus persistence

Two independent filters, and it matters which one does the work:

1. **Window smoothing.** The error rate is measured over a 60-second window. A 3-second
   blip at 25% errors moves the window by only about `0.24 × 3/60 ≈ 1.2` points, whatever the
   service's volume. That is below the absolute floor (`ABS_FLOOR_ERROR` = 2 points) and below
   z = 3, so the detectors emit **no signal at all**. Measured on the generator: peak z < 3.
2. **Persistence.** A bigger spike does register, and then it stays in the window for 60
   seconds, so it produces signals for far longer than one tick. Persistence
   (`PERSIST_TICKS` = 5 consecutive ticks with a signal) therefore filters *flickering* signals,
   i.e. a detector hovering around its threshold, not blips as such. It is tested directly,
   with a signal that flickers above and below `OPEN_Z` in runs of 1-4 ticks.

Consequence for detection latency: a step to 30% errors needs about 6 seconds for the
window to reach z >= 3, plus 5 persistence ticks, so an incident opens about 11 seconds
after a 30% step (about 9 seconds after a 50% step).

### Slow, sub-threshold drift is absorbed (known limit)

The baseline keeps learning whenever a service has no open incident. A change that stays
below the alert thresholds, or that arrives slowly enough, is gradually treated as the new
normal: for example a permanent drop to 50% of the usual volume is absorbed after a couple of
minutes, and an error rate creeping up by a fraction of a point per hour never alerts.
This is the price of a self-adjusting baseline with no labelled data; the freeze during
incidents only protects against *alerting* outages.

**Future fix:** keep a second, long-term reference baseline (for example, a daily median
that is updated much more slowly and is never frozen or adapted by short-term samples) and
raise a low-severity "drifted from long-term normal" signal when the two diverge.

### Silence detection lookback

The spec's rule ("window volume below 20% of the median") needs the 60-second window to
drain by 80% before it fires, about 48 seconds after a service goes quiet.
`SILENCE_LOOKBACK_SECONDS` (default **20**) applies the same rule to the most recent 20
seconds instead, which fires in about 16 seconds. Set it to 60 for the literal spec rule.
