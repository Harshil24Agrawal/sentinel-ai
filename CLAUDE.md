# SentinelAI — Build Specification (CLAUDE.md)

> Real-time log anomaly detection and incident intelligence for healthcare software operations.
> Built for the Acentra Health hackathon (Problem 1: Real-Time Log Anomaly Detector with Alert Feed).
>
> **Tagline:** Detect early. Explain clearly. Show impact. Support human response.

This file is the single source of truth for Claude Code. Read it fully before writing code.
Build in the phase order in §14. Do not start a phase until the previous phase's tests pass.

---

## 1. Context (why this exists)

- **Organizer:** Acentra Health (spelled **Acentra**, never "Accentra"). Formed in 2023 by the merger of CNSI and Kepro. Builds Medicaid claims, provider and clinical systems (evoBrix, Atrezzo) for US state and federal agencies. Processes over a billion claims and encounters a year. Has a Chennai engineering office.
- **Acentra's AI stance:** human-in-the-loop and explainable (MedScribe drafts, nurses decide; the SAMA alliance builds AI governance on the NIST AI RMF). HIPAA compliance is non-negotiable.
- **User:** the on-call engineer (SRE) who must learn *what* is broken, *how badly*, *what is affected*, *how it developed*, and *where to look first*, within seconds, without alert spam.
- **Hard constraints:** 3-hour build, then a pitch with a live demo. Synthetic data only.

### Problem statement minimum requirements (all mandatory)
1. Monitor a continuously growing log file
2. Rolling error rates using a sliding window
3. Baseline for normal behavior
4. Detect deviations from the baseline
5. Severity levels for anomalies
6. Real-time frontend (WebSockets or polling)
7. Display alerts as they are generated
8. Push alerts to AWS CloudWatch Logs or SNS

### Non-goals
No real patient data. No clinical recommendations. No automatic production changes. No autonomous agents. No Kubernetes, distributed tracing, RAG or vector DBs. No supervised ML (there are no labels). No TF-IDF (pattern templating does the job explainably).

---

## 2. Design principles (every choice must be defensible)

1. **Explainable over clever.** Every alert shows the numbers that caused it. Statistical detectors are primary; ML is a second opinion only; the LLM only explains.
2. **One outage produces one incident.** Alert fatigue is the central problem. Group, deduplicate, apply hysteresis, and notify only on escalation.
3. **PHI never leaves the process unredacted.** Redaction happens before storage, display, LLM, or AWS.
4. **Local first.** AWS and the LLM are optional at runtime. Their failure never blocks detection or the dashboard.
5. **Everything configurable.** Mentor answers must become config changes, not code changes.
6. **Deterministic and testable.** All time comes from an injectable `Clock`. Tests simulate minutes of traffic in milliseconds.

---

## 3. Architecture

```
generator (asyncio task, driven by /api/simulate) ──writes──> data/logs/app.log
                                                                   │
[1] collector   tail file; partial lines; rotation/truncation ─────┘
[2] processor   parse → redact PHI → pattern template (dynamic tokens masked)
[3] aggregator  per-service 1s buckets; 60s sliding window (lines, errors, warns, latencies, pattern counts)
[4] detectors   robust baseline (median/MAD, frozen during incidents)
                error_rate · latency_p95 · silence · new_pattern · pattern_surge
                (+ optional IsolationForest second opinion, never a trigger)
[5] incidents   persistence · dual threshold · min volume · hysteresis · cooldown/reopen
                severity · correlation groups (dependency graph) · probable cause · hints
[6] explainer   rule-based (always) + LLM (optional) from a REDACTED evidence packet
[7] outputs     WebSocket → React dashboard · notifier queue → CloudWatch Logs (all) + SNS (HIGH/CRITICAL)
                SQLite store (incidents, events, notifications = local audit trail)
[8] evaluation  naive threshold detector in parallel · detection latency per injected fault
```

Single Python process, asyncio. The pipeline runs a tick every 1 second (`TICK_SECONDS`).

---

## 4. Repository layout

```
sentinel-ai/
├── CLAUDE.md
├── README.md                  # setup, run, demo script, design decisions
├── docs/DESIGN.md             # the "why" behind each decision (pitch backup)
├── .env.example
├── docker-compose.yml         # stretch
├── backend/
│   ├── pyproject.toml         # or requirements.txt
│   ├── app/
│   │   ├── main.py            # FastAPI app, lifespan starts tasks
│   │   ├── config.py          # pydantic-settings, all defaults from §11
│   │   ├── clock.py           # Clock protocol, SystemClock, FakeClock
│   │   ├── models.py          # pydantic models (LogEvent, Incident, etc.)
│   │   ├── collector.py
│   │   ├── parser.py
│   │   ├── redaction.py
│   │   ├── patterns.py
│   │   ├── aggregator.py
│   │   ├── baseline.py
│   │   ├── detectors.py
│   │   ├── severity.py
│   │   ├── incidents.py
│   │   ├── correlation.py
│   │   ├── hints.py
│   │   ├── whatif.py          # stretch
│   │   ├── ml_second_opinion.py  # stretch
│   │   ├── naive.py
│   │   ├── store.py           # SQLite
│   │   ├── pipeline.py        # wires stages, tick loop
│   │   ├── explainer/{base.py, rule_based.py, llm.py}
│   │   ├── notifier/{dispatcher.py, cloudwatch.py, sns.py, dryrun.py}
│   │   └── api/{routes.py, ws.py}
│   ├── generator/{generator.py, scenarios.py, services.yaml}
│   └── tests/
└── frontend/                  # Vite + React + Recharts
    └── src/{App.jsx, api.js, ws.js, components/...}
```

---

## 5. Log format and synthetic services

### Primary line format (configurable regex in `config.LOG_REGEX`)
```
<ISO8601 UTC timestamp> <LEVEL> <service> [latency_ms=<int>] <message>
2026-09-28T10:00:01.123Z INFO  appointment-service latency_ms=112 Appointment request 88213 completed
2026-09-28T10:00:02.004Z ERROR appointment-service latency_ms=3004 Database timeout while saving appointment 88214
```
- LEVEL ∈ DEBUG, INFO, WARN/WARNING, ERROR, CRITICAL/FATAL. Error = ERROR|CRITICAL|FATAL.
- Also accept JSON lines (`{"ts":..,"level":..,"service":..,"latency_ms":..,"msg":..}`) automatically.
- Lines that fail to parse: count as `unparsed`, keep the raw line redacted, service = `unknown`. Never crash.

### Synthetic services and dependencies (`generator/services.yaml`, also used for blast radius)
```yaml
services:
  patient-portal:       {rps: 20, base_error: 0.02, latency_ms: [80, 20],  depends_on: [auth-service, appointment-service]}
  auth-service:         {rps: 15, base_error: 0.04, latency_ms: [40, 10],  depends_on: [database]}
  appointment-service:  {rps: 12, base_error: 0.02, latency_ms: [110, 25], depends_on: [database, notification-service]}
  claims-service:       {rps: 25, base_error: 0.015,latency_ms: [150, 40], depends_on: [database, provider-service]}
  provider-service:     {rps: 8,  base_error: 0.01, latency_ms: [90, 20],  depends_on: [database]}
  notification-service: {rps: 6,  base_error: 0.01, latency_ms: [60, 15],  depends_on: []}
  database:             {rps: 30, base_error: 0.005,latency_ms: [8, 3],    depends_on: []}
```
Normal messages per service are realistic and varied (IDs, numbers). About 1 in 200 normal lines includes PHI-like fields
(`member_id=MBR1234567`, `ssn=123-45-6789`, `email=jane@example.com`, `patient_name="Jane Doe"`) so redaction is visibly exercised.

### Fault scenarios (`generator/scenarios.py`), triggered via `POST /api/simulate/{name}`
| name | behavior | expected detection |
|---|---|---|
| `db_degradation` | database latency ×20 over 15s; then database emits new `Connection pool exhausted after <NUM> ms`; appointment/claims emit `Database timeout while saving ...` rising to ~30% errors; notification errors rise 20s later | CRITICAL group, origin = database, NEW + surging patterns, latency anomaly |
| `auth_outage` | auth-service 401/403 errors to ~40%, new pattern `Token validation failed: signing key <HEX> not found`; patient-portal errors follow | HIGH/CRITICAL, hint = credentials/config |
| `silent_service` | notification-service stops logging entirely | SILENCE incident |
| `noisy_blip` | one service spikes to 25% errors for 3 seconds only | **no incident** (proves persistence) |
| `recover` | all services return to normal | incidents move to RESOLVED after hysteresis |

The generator records `fault_injected_at[service]` for detection-latency measurement. It runs in the same process as an asyncio task,
but it must write to the file; the collector must read from the file. Never short-circuit in memory.

---

## 6. Module specifications

### 6.1 `clock.py`
`Clock` protocol with `now() -> float` (epoch seconds). `SystemClock`, and `FakeClock` with `advance(seconds)`. Every module takes a clock.

### 6.2 `collector.py`
- Opens the file, seeks to end on start (config `COLLECTOR_FROM_START=false`; tests may use true).
- Polls every 100 ms (portable, no inotify dependency). Reads available bytes, splits on `\n`, buffers the trailing partial line until complete.
- Detects truncation (size < offset) → reset offset to 0. Detects rotation (inode changed or file replaced) → reopen and read the new file from start.
- File missing → wait and retry, log a warning once.
- Emits raw lines to an `asyncio.Queue`.

### 6.3 `parser.py`
Regex from config with JSON fallback → `LogEvent{ts, level, is_error, is_warn, service, latency_ms|None, message, raw}`.
If the timestamp is missing or unparsable, use clock time.

### 6.4 `redaction.py` (runs before anything is stored, templated, displayed, or sent)
- Key-based: values of keys `ssn, member_id, patient_name, name, dob, email, phone, address, mrn` (key=value, key="value", JSON) → `[REDACTED]`.
- Pattern-based: SSN `\b\d{3}-\d{2}-\d{4}\b`, email, US phone numbers, member IDs `\bMBR-?\d{6,}\b` (configurable list).
- Must NOT redact ordinary request IDs like `appointment 88214`, latencies, or HTTP codes.
- Returns `(text, redaction_count)`. Keep a global counter for the dashboard ("PHI fields redacted: N").

### 6.5 `patterns.py`
Mask dynamic tokens in this order: UUID → `<UUID>`, email → `<EMAIL>`, IPv4 → `<IP>`, hex ≥ 8 chars → `<HEX>`, numbers → `<NUM>`.
Collapse whitespace. Pattern ID = short stable hash of the template. `[REDACTED]` stays literal so templates are stable.
`drain3` is an optional drop-in replacement behind the same interface.

### 6.6 `aggregator.py`
Per service, a ring of 1-second buckets covering `WINDOW_SECONDS` (default 60). Each bucket: lines, errors, warns, latency list (capped),
pattern counts (error/warn patterns only). Window stats are maintained incrementally (add new bucket, subtract expired), O(1) per tick:
`volume, error_rate, p95_latency, pattern_counts, distinct_error_patterns`.
Also keep `first_seen[pattern_id]` and long-run pattern rates for the surge detector.

### 6.7 `baseline.py` — robust, per service, per metric
- Each tick, when the service is NOT in an open incident, append the current window value to a history deque (`BASELINE_HISTORY_SAMPLES`, default 900 = 15 min).
- **Freeze:** do not append while the service has an open incident (prevents learning an outage as the new normal).
- `median`, `mad`, `robust_z(x) = (x - median) / (1.4826 * mad + floor)`. Floors: error rate 0.005, latency 20 ms, volume 5 lines.
- Warm-up: a service is `ready` when the first window has filled and it has at least `WARMUP_SAMPLES` (default 30) history samples.

### 6.8 `detectors.py` — returns `Signal{type, service, value, baseline_median, z, detail}` per tick
| detector | fires when (all conditions) |
|---|---|
| `error_rate` | ready; volume ≥ `MIN_VOLUME` (30); z ≥ `OPEN_Z` (3); rate − median ≥ `ABS_FLOOR_ERROR` (0.02) |
| `latency` | ready; ≥ 20 latency samples in window; z ≥ `OPEN_Z`; p95 ≥ 1.5 × median |
| `silence` | ready; baseline median volume ≥ `MIN_VOLUME`; window volume < 20% of median |
| `new_pattern` | ready; error/warn pattern first seen after warm-up; count in window ≥ 3 |
| `pattern_surge` | ready; (window_count + 1) / (expected_count + 1) ≥ 5 and window_count ≥ 10 |

`new_pattern` and `pattern_surge` feed probable cause and raise confidence; on their own they open at most a LOW incident.

### 6.9 `severity.py`
Base on the largest z among active signals: 3–5 LOW, 5–8 MEDIUM, 8–12 HIGH, ≥ 12 CRITICAL. Then:
- +1 level if the anomaly has lasted > 120s.
- CRITICAL if window error rate ≥ 25%, or silence has lasted ≥ 30s.
- +1 level if the correlation group contains ≥ 2 anomalous services.
- Capped at CRITICAL. An incident's recorded severity is the max observed (current severity is also shown).

### 6.10 `incidents.py` — the alert-fatigue core
States: `OPEN → RESOLVED` (with `current_severity`, `max_severity`, `escalations[]`).
- **Persistence:** a service must have a qualifying signal for `PERSIST_TICKS` (5) consecutive ticks before an incident opens.
- **Grouping:** at most one open incident per service; new signals update it.
- **Hysteresis:** resolve only when every detector's z < `RESOLVE_Z` (1.5) and the absolute floor is not exceeded, for `RESOLVE_TICKS` (30) consecutive ticks.
- **Cooldown/reopen:** if the same service anomalies again within `COOLDOWN_SECONDS` (60) of resolving, reopen the same incident.
- **Notifications:** only on (a) open at HIGH+, (b) escalation into HIGH or CRITICAL, (c) resolve of an incident that notified. Never repeat for the same severity.
- Every state change is written to the store and to CloudWatch (via the notifier queue).
- **The card the user sees is per correlation group** (§7); the per-service incident is a member of it.
- The incident card holds: what (metric now vs baseline), how bad (z, duration, failed ops), affected services, timeline (every severity transition with timestamp), probable cause, hint, 3 redacted sample lines, explanation, optional what-if and ML score.

### 6.11 `correlation.py`
When an incident opens on service S, join an existing group if another group member opened within `CORRELATION_WINDOW_SECONDS` (180) and is connected to S in the dependency graph (either direction).
- **Probable origin:** among the group, the most upstream service (a dependency of the others), tie-broken by earliest deviation.
- **Blast radius:** all services that transitively depend on the origin. Mark each as `impacted` (has an open incident) or `at_risk`.
- **Incident story:** the merged, time-ordered timeline across the group.

### 6.12 `hints.py` — probable cause
Rank the group's error/warn patterns: NEW patterns first, then by surge ratio. Show the top 3 with counts and "normally X/min".
Keyword rules → hint text (config-extensible):
- `timeout|pool exhausted|deadlock|connection refused` → "Database or downstream dependency: check DB health, latency and connection pool."
- `401|403|unauthorized|token|signing key|certificate` → "Authentication/credentials: check keys, certificates, recent config changes."
- `OutOfMemory|heap|disk full|no space` → "Resource exhaustion: check memory/disk on the host."
- `5\d\d|unavailable|circuit` → "Downstream service unavailable: check the dependency's health."
- silence → "Service stopped logging: check whether the process is alive and the log pipeline is intact."
Always label as **probable cause**, never root cause.

### 6.13 `explainer/`
- `rule_based.py` (always on): deterministic plain-English summary from the incident card and hints.
- `llm.py` (optional, `LLM_PROVIDER=none|anthropic|openai|azure_openai|bedrock`, `LLM_MODEL`, API key from env):
  - Called once when a group reaches MEDIUM+, and again on escalation to CRITICAL. Rate limit: 1 call per group per 60s.
  - Input: the **evidence packet** only (service metrics vs baseline, triggered detectors, top patterns, 3 redacted samples, timeline, dependency info). Run redaction again on the packet as defense in depth.
  - System prompt: use only the evidence; if it is insufficient, say so; no clinical content; output JSON only.
  - Output schema (validated with pydantic): `{is_incident: bool, summary: str, probable_cause: str, affected_services: [str], recommended_checks: [str], confidence: "low"|"medium"|"high"}`.
  - Timeout 8s. On timeout, error, or invalid JSON → use the rule-based explanation and log why. The UI labels LLM text "AI-generated — verify".

### 6.14 `notifier/`
- `dispatcher.py`: an `asyncio.Queue`; a worker drains it so AWS latency never blocks the pipeline. Retries with exponential backoff (3 attempts). Batches CloudWatch events.
- `cloudwatch.py`: `create_log_group`/`create_log_stream` if missing (ignore AlreadyExists), then `put_log_events` with JSON incident events (all state changes).
- `sns.py`: `publish` for HIGH/CRITICAL opens/escalations and resolves of notified incidents. Short subject; body = compact incident card.
- `dryrun.py`: when `AWS_ENABLED=false` or credentials are missing, append the exact payloads to `data/notifications_dryrun.jsonl` and show them in the UI.
- The IAM policy in the README must be least-privilege: `logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents, sns:Publish` on the specific resources.

### 6.15 `naive.py` — the comparison baseline
Every 5 seconds, for each service: if window error rate > 5%, count one "alert". No grouping, no hysteresis. Exposed via `/api/stats`
as `naive_alerts` vs `sentinel_notifications` and `sentinel_incidents`.

### 6.16 `store.py` — SQLite (stdlib `sqlite3`)
Tables: `incidents`, `incident_events` (every transition, with a JSON snapshot), `notifications` (channel, status, payload), `frames` (for replay).

### 6.17 Stretch modules
- `whatif.py`: least-squares fit on the last 60s of the error rate; if slope > 0 and R² ≥ 0.6, project +180s (clamped 0–100%) and list at-risk services from the blast radius. The UI label is "Projection if the trend continues — not a prediction."
- `ml_second_opinion.py`: sklearn `IsolationForest` per service trained on warm-up feature vectors `[error_rate, volume, p95_latency, distinct_error_patterns]`. Shows a score in the card only. **Never opens or escalates an incident.**
- Replay: store 1 frame per second per group member from 60s before open until resolve; `GET /api/replay/{incident_id}` returns the frames.

---

## 7. API and WebSocket contract (the frontend and backend build against this in parallel)

**One card per outage.** The unit the on-call engineer sees is the **correlation group** (§6.11): one outage is one incident card, however many services it touches. Each service inside it keeps its own state machine (§6.10) and appears nested in the card as a *member incident*. Every `id` below is a **group id**; a member incident id is also accepted by `GET /incidents/{id}` and resolves to its group. Severities are serialized as their names: `"LOW" | "MEDIUM" | "HIGH" | "CRITICAL"`. Timestamps are epoch seconds (float).

REST (JSON, prefix `/api`):
- `GET /health` → `{status, uptime_s, aws: "live"|"dryrun", llm: provider|"none"}`
- `GET /services` → `[{name, status: "healthy"|"warming_up"|"LOW"|"MEDIUM"|"HIGH"|"CRITICAL"|"silent", volume, error_rate, p95_latency, baseline: {error_rate_median, error_rate_band: [lo, hi], p95_median, ready}, depends_on}]`
- `GET /incidents?status=open|resolved|all` (default `all`) → `[GroupSummary]`, sorted by priority (open first, then severity, NEW-pattern incidents above surge-only, then recency)
- `GET /incidents/{id}` → `GroupCard` (404 if unknown)
- `POST /simulate/{scenario}` → `{ok, scenario, started_at}` (404 unknown scenario; 409 if the generator is disabled)
- `POST /config/sensitivity` `{open_z: 2..5}` → `{ok, open_z}` (422 outside the range)
- `GET /stats` → `{naive_alerts, sentinel_incidents, sentinel_notifications, detection_latency_s: {scenario: seconds}, phi_redacted}` (`sentinel_incidents` counts groups)
- `GET /metrics/history?seconds=300` → `[metrics frame]` (the last N seconds of the WebSocket `metrics` messages, oldest first; lets a freshly loaded page draw its chart)
- `GET /logs/recent?service=&level=&limit=200` → `[LogLine]`, the live log tail, **newest last**. Reads a ring buffer of the last `LOG_BUFFER_SIZE` (500) parsed, **redacted** events. `service` is an exact match; `level` is an exact match on one level or a comma-separated list (`ERROR,CRITICAL`; `WARNING` is accepted for `WARN`, `FATAL` for `CRITICAL`); both are optional. The filters apply first, then the last `limit` (1..500) remain. An unknown `level` or an out-of-range `limit` is a 422.
- `GET /replay/{id}` → `{frames: [{t, services: {...}}]}` (stretch)

`LogLine` = `{ts, level: "DEBUG"|"INFO"|"WARN"|"ERROR"|"CRITICAL", service, message, pattern_id|null}` (`pattern_id` is set for ERROR/WARN lines only; lines that match no known format arrive as `service: "unknown"`, `INFO`).

`GroupSummary`:
```
{id, state: "OPEN"|"RESOLVED", current_severity, max_severity, opened_at, resolved_at|null,
 title, origin, affected_services: [str], has_new_pattern, notified,
 incidents: [MemberIncident]}
```
`MemberIncident` (one per service in the group, ordered by `opened_at`):
```
{id, service, state, current_severity, max_severity, opened_at, resolved_at|null, duration_s,
 what, failed_ops, peak_z, has_new_pattern, signals: [Signal], escalations: [{t, from_severity, to_severity}],
 timeline: [{t, service, event, severity|null, detail}]}
```
`GroupCard` = `GroupSummary` plus:
```
{what, duration_s, failed_ops, peak_z,
 hint,                                   // the PROBABLE cause, never "root cause"
 patterns: [{pattern_id, template, count, expected_per_min, is_new, surge_ratio, service}],   // top 3
 samples: [str],                         // up to 3 redacted lines
 story: [TimelineEntry],                 // every member's timeline merged, time-ordered
 blast_radius: {origin, nodes: [{service, status: "impacted"|"at_risk"|"healthy"}]},
 explanation: {is_incident, summary, probable_cause, affected_services, recommended_checks,
               confidence: "low"|"medium"|"high", source: "rule_based"|"llm", label},
 what_if: {...}|null, ml_score: float|null}     // stretch, null until implemented
```
`Signal` = `{type: "error_rate"|"latency"|"silence"|"new_pattern"|"pattern_surge", service, value, baseline_median, z, detail, pattern_id|null}`.

WebSocket `/ws`: each message is `{"type": ..., "data": ...}`:
- `metrics` every tick: `{t, services: {name: {volume, error_rate, p95_latency, band: [lo, hi], status}}}` (`band` is the normal error-rate range)
- `incident_opened` | `incident_updated` | `incident_resolved`: `GroupSummary`. `opened` is sent when a group appears (or a resolved group reopens), `updated` when its severity, members, origin or evidence change, `resolved` when its last member resolves.
- `notification`: `{channel: "sns"|"cloudwatch"|"dryrun", incident_id (the group id), severity, ok}`
- `logs` once per tick (sent even when nothing arrived): `{lines: [LogLine], sampled_out: N, capped: M}`, in arrival order. Every ERROR/CRITICAL and WARN line of the tick is included, plus a sample of at most `LOG_INFO_PER_TICK` (10) INFO/DEBUG lines, evenly spaced through the tick; `sampled_out` counts the INFO/DEBUG lines **not** sent. A message never holds more than `LOG_LINES_PER_MESSAGE` (50) lines: when the tick has more, ERROR lines are kept before WARN before INFO (newest first within a level), and `capped` counts the ERROR/WARN lines dropped for that reason (`0` in normal operation; the ring buffer behind `/logs/recent` still has them). `capped` is additive to the original `{lines, sampled_out}` shape. Lines are redacted before they are buffered, so a `logs` message never carries PHI.

The frontend falls back to polling `/api/services` and `/api/incidents` every 2s if the WebSocket disconnects, and shows a connection indicator.

---

## 8. Frontend (Vite + React + Recharts)

Single page, dark operations-center style, responsive.
1. **Header:** overall health (worst open severity), connection indicator, AWS live/dry-run badge, PHI-redacted counter.
2. **Service grid:** one tile per service with status color, error rate, p95, volume. Click to select.
3. **Live chart:** the selected service's error rate over the last 5 min, with a shaded **normal band** (median ± bounds) as a Recharts range `Area`. Sensitivity slider (2–5) calls `/config/sensitivity`.
4. **Incident feed:** priority-sorted cards that appear live, color by severity, with a NEW badge for new patterns.
5. **Incident detail panel:** WHAT / HOW BAD / AFFECTED / TIMELINE (the story) / PROBABLE CAUSE (patterns table) / HINT / SAMPLE (redacted) / EXPLANATION (labeled AI or rule-based) / WHAT-IF.
6. **Blast radius:** an SVG graph of the dependency map with fixed positions; nodes colored impacted/at-risk/healthy; the origin outlined.
7. **Simulation panel:** buttons for each scenario plus Recover.
8. **Stats panel:** naive alerts vs Sentinel incidents and notifications; detection latency.

---

## 9. Security, privacy and quality standard ("Acentra standard")

- Python 3.11, full type hints, pydantic models, `ruff` clean.
- Structured JSON logging for the app's own logs; never log raw unredacted lines.
- Secrets only from the environment (`.env`, git-ignored); provide `.env.example`. No credentials in code, tests, or the frontend.
- Least-privilege IAM documented. The app works with no AWS and no LLM configured.
- Graceful startup/shutdown (FastAPI lifespan; cancel tasks; flush the notifier queue).
- Pipeline errors are caught per stage and counted; one bad line never stops monitoring.
- `README.md`: setup in ≤ 5 commands, demo script, configuration table, IAM policy, design decisions. `docs/DESIGN.md`: the reasoning (§12).

---

## 10. Tests (pytest; all use FakeClock; the suite must run in < 10s)

- **collector:** appended lines are read; a partial line is held until its newline; truncation resets; rotation reopens; a missing file is waited for.
- **redaction:** SSN, email, phone, member ID and key=value fields are redacted; `appointment 88214`, `latency_ms=120`, `HTTP 503` are NOT.
- **patterns:** `Database timeout while saving appointment 88214` and `... 90431` produce the same pattern ID.
- **baseline:** median/MAD correct; history does not grow while frozen; `ready` only after warm-up.
- **detectors:** a sustained spike opens an incident within 10 simulated seconds; `noisy_blip` (3s) opens none; a low-volume service (< MIN_VOLUME) opens none; silence is detected; a new pattern is flagged only after warm-up.
- **incidents:** a full `db_degradation` run gives exactly 1 correlation group and ≤ 3 SNS notifications; a rate oscillating around the open threshold does not flap; an anomaly within the cooldown reopens the same incident.
- **correlation:** database + appointment + notification anomalies group together with origin = database; blast radius is correct.
- **notifier:** SNS only for HIGH+; an AWS exception triggers retry and never blocks the pipeline; dry-run writes the payloads.
- **explainer:** invalid LLM JSON or a timeout falls back to rule-based; the evidence packet contains no PHI.
- **end-to-end:** generator → file → collector → dashboard state with FakeClock over 10 simulated minutes; `stats.naive_alerts` > `stats.sentinel_notifications`.

---

## 11. Configuration defaults (`config.py`, all overridable by env)

| key | default | meaning |
|---|---|---|
| LOG_PATH | data/logs/app.log | file to monitor |
| TICK_SECONDS | 1 | pipeline tick |
| WINDOW_SECONDS | 60 | sliding window |
| BASELINE_HISTORY_SAMPLES | 900 | baseline length (15 min) |
| WARMUP_SAMPLES | 30 | samples before alerting |
| MIN_VOLUME | 30 | lines per window required (≈ 0.5 lines/s) |
| OPEN_Z / RESOLVE_Z | 3.0 / 1.5 | hysteresis thresholds |
| PERSIST_TICKS / RESOLVE_TICKS | 5 / 30 | persistence / recovery hold |
| ABS_FLOOR_ERROR | 0.02 | minimum absolute error-rate increase |
| COOLDOWN_SECONDS | 60 | reopen window |
| CORRELATION_WINDOW_SECONDS | 180 | grouping window |
| AWS_ENABLED / AWS_REGION | false / us-east-1 | notifier mode |
| CW_LOG_GROUP / CW_LOG_STREAM | /sentinel-ai/incidents / demo | CloudWatch target |
| SNS_TOPIC_ARN | (empty) | SNS target |
| LLM_PROVIDER / LLM_MODEL | none / (empty) | optional explainer |
| GENERATOR_ENABLED | true | built-in synthetic traffic |
| LOG_BUFFER_SIZE | 500 | redacted log events kept for `/api/logs/recent` |
| LOG_INFO_PER_TICK | 10 | INFO/DEBUG lines sampled into each WebSocket `logs` message |
| LOG_LINES_PER_MESSAGE | 50 | hard cap on lines in one `logs` message |

---

## 12. Design decisions to state in docs/DESIGN.md and the pitch

1. **Statistics over trained ML:** no labeled data; instant cold start; explainable numbers; O(1) updates. (For comparison, CloudWatch's log anomaly detector trains on two weeks of history first.)
2. **Median/MAD over mean/std:** outliers cannot drag the baseline (Datadog's basic algorithm uses rolling quantiles for the same reason).
3. **Baseline freeze during incidents:** otherwise the outage becomes the new normal and the alert silently closes.
4. **Hysteresis (thermostat analogy):** different open and close thresholds prevent flapping.
5. **Minimum volume and absolute floor:** tiny samples and statistically "large" but harmless changes don't page anyone.
6. **Patterns and dynamic tokens:** the same approach as CloudWatch and Datadog; enables NEW and surge detection and probable cause.
7. **No seasonality:** it needs weeks of history; the detector interface allows a seasonal model later.
8. **Two-tier AI:** statistics decide *when*; the LLM only explains *what*, from redacted evidence, with a deterministic fallback. Human-in-the-loop, like MedScribe.
9. **Redaction before storage, LLM and AWS:** in healthcare, logs leak PHI.
10. **Isolation Forest as a second opinion only:** it catches multi-signal oddities but isn't explainable, so it never pages anyone by itself.

---

## 13. Demo script (the tests should prove every step works)

1. Healthy system: tiles green, error rates inside their bands, 0 incidents.
2. Press **Simulate Database Degradation**: the database latency tile turns amber; then appointment and claims turn red; escalation LOW → HIGH → CRITICAL; one SNS message arrives on a phone.
3. Open the incident: origin = database, blast radius lit up, NEW pattern `Connection pool exhausted after <NUM> ms`, hint = check DB and connection pool, AI explanation labeled.
4. Stats: "Naive detector: 47 alerts. SentinelAI: 1 incident, 2 notifications. Detected in N seconds."
5. Press **noisy blip**: nothing pages (persistence).
6. Press **Recover**: incidents hold, then RESOLVED after hysteresis.
7. Press **silent service**: silence detected.
8. Open CloudWatch (or the dry-run file): incident records show `[REDACTED]` where PHI was.

---

## 14. Build phases (paste each prompt into Claude Code in order)

**Phase 0 — Scaffold (10 min)**
> Read CLAUDE.md. Create the repository layout in §4, config.py with the §11 defaults, clock.py, and models.py. Set up pytest and ruff. Add .env.example and a README skeleton. Run the (empty) tests.

**Phase 1 — Ingestion (25 min)**
> Implement generator (services.yaml, all §5 scenarios, writing to LOG_PATH), collector, parser, redaction, and patterns per §5–6.5. Write the §10 tests for these modules. Run them until they pass.

**Phase 2 — Detection (30 min)**
> Implement aggregator, baseline, detectors, and severity per §6.6–6.9, all using the Clock. Write the §10 tests including noisy_blip and low volume. Run them until they pass.

**Phase 3 — Incidents (30 min)**
> Implement incidents, correlation, hints, naive, and store per §6.10–6.12, 6.15, 6.16. Write the §10 incident and correlation tests, including the db_degradation end-to-end test with FakeClock. Run them until they pass.

**Phase 4 — API (15 min)**
> Implement pipeline.py, main.py, REST routes and WebSocket exactly per the §7 contract. Start the server and verify with curl and a WebSocket client that metrics stream and a simulated scenario produces incidents.

**Phase 5 — Frontend (parallel session, starting after Phase 0)**
> Read CLAUDE.md §7–8. Build the Vite + React dashboard against the contract, with a mock WebSocket server for development until the backend is ready. Then connect to the real backend.

**Phase 6 — AWS (15 min)**
> Implement the notifier per §6.14 with dry-run as default. Test retry and non-blocking behavior. Document the least-privilege IAM policy in the README. Verify live against the configured topic and log group.

**Phase 7 — Stretch (only if Phases 0–6 are green)**
> In order: the LLM explainer with fallback (§6.13), then what-if, then replay, then the Isolation Forest second opinion (§6.17). Each with tests.

**Phase 8 — Finish (15 min)**
> Complete the README and docs/DESIGN.md (§12). Run the full demo script (§13) end to end and fix only real bugs. Add docker-compose if time allows.

**Rules for Claude Code:** keep modules small and single-purpose; never skip tests to save time; never send unredacted text to the LLM, AWS, store, or UI; if a requirement is ambiguous, choose the configurable option and note it in the README.
