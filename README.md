# SentinelAI

Real-time log anomaly detection and incident intelligence for healthcare software operations.
Built for the Acentra Health hackathon (Problem 1: Real-Time Log Anomaly Detector with Alert Feed).

**Detect early. Explain clearly. Show impact. Support human response.**


## Setup (Windows, PowerShell, conda env `sentinel`)

`conda run` fails when the user profile path contains a space, so call the env's interpreter
directly. Run everything below from the **repository root**:

```powershell
$py = "$env:USERPROFILE\Miniconda3\envs\sentinel\python.exe"
& $py -m pip install -e "backend[dev]"
```

## Run the server

```powershell
& $py -m uvicorn app.main:app --app-dir backend --port 8000
```

That starts the pipeline, the built-in synthetic traffic generator (`GENERATOR_ENABLED=true`),
and the API. The log file `data/logs/app.log` and the audit database `data/sentinel.db` are
created relative to the directory you run it from (hence: the repo root).

| | URL |
|---|---|
| Dashboard | http://localhost:8000/ |
| REST API (interactive docs) | http://localhost:8000/docs |
| WebSocket | `ws://localhost:8000/ws` |

The dashboard is served at http://localhost:8000/ (from `frontend/index.html`).

A service is judged only after about **90 seconds** of traffic (the baseline has to warm up), so
give it a minute and a half, then trigger a fault:

```powershell
curl.exe -X POST http://localhost:8000/api/simulate/db_degradation   # start an outage
curl.exe http://localhost:8000/api/incidents                         # one card per outage
curl.exe -X POST http://localhost:8000/api/simulate/recover          # then let it recover
curl.exe http://localhost:8000/api/stats                             # naive alerts vs Sentinel
```

Scenarios: `db_degradation`, `auth_outage`, `silent_service`, `noisy_blip`, `recover`.

## Tests

```powershell
& $py -m pytest backend                # default: fast (about 15 s), skips the slow simulations
& $py -m pytest backend -m "slow or not slow"   # FULL suite, including the slow ones (about 50 s)
& $py -m ruff check backend            # lint
& $py -m ruff format --check backend   # formatting
```

The slow tests (marked `@pytest.mark.slow`) run the generator's real scenarios for minutes of
simulated time at **realistic traffic volumes**, including the ten-minute end-to-end test
(generator -> log file -> collector -> pipeline -> API) and the PHI leak scan. Run the full
suite before every commit.

## Demo script

_TBD: Phase 8 (see `CLAUDE.md` §13)._

## Configuration

Every setting has a default and can be overridden by an environment variable or `.env`
(copy `.env.example`). The full table of defaults is in `backend/app/config.py` and `CLAUDE.md` §11.

_TBD: table of keys._

## Notifications and AWS

Every notification payload is appended, as one JSON line, to `data/notifications_dryrun.jsonl`
(payloads are redacted a second time on the way out, and this file is written in **every** mode
— live or dry-run — as a local backup audit trail). That is the default and needs no AWS.

CloudWatch Logs (state changes only — see below) and SNS (HIGH/CRITICAL opens and escalations,
and the resolve of a group that notified) are used **only** when `AWS_ENABLED=true` **and** the
startup self-check passes. On boot the notifier: (1) calls `sts:GetCallerIdentity` to verify
credentials, (2) creates the CloudWatch log group/stream if missing, (3) calls
`sns:GetTopicAttributes` on `SNS_TOPIC_ARN`. Each check is independent — CloudWatch failing does
not stop SNS from going live, and vice versa. Sending itself happens on a background worker with
retries, so a slow or failing AWS call never delays detection or the dashboard.

`GET /api/health` reports `aws: "live"|"dryrun"|"error"` (`"error"` means credentials themselves
failed) plus `aws_checks: {credentials, cloudwatch, sns}`, each `{ok, error}`. The dashboard's AWS
badge shows Live/Dry-run/Error and the reason on hover. `POST /api/aws/test` sends one
clearly-labeled test message to both CloudWatch and SNS and returns each result — use it to
verify connectivity without waiting for a real incident. The last 10 sends (any channel) are also
in `GET /api/stats` as `delivery_log`, shown as a table in the dashboard's Detection Quality panel.

```
AWS_ENABLED=true
AWS_REGION=us-east-1
AWS_PROFILE=sentinel        # empty uses the standard AWS credential chain instead
CW_LOG_GROUP=/sentinel-ai/incidents
CW_LOG_STREAM=demo
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:<account-id>:<topic>
DASHBOARD_URL=http://localhost:8000   # linked from SNS notification bodies
DISPLAY_TIMEZONE=Asia/Kolkata         # human-facing times in the SNS body; CloudWatch stays UTC
```

Never put access keys in `.env`; point `AWS_PROFILE` at a named profile (e.g. one set up with
`aws configure --profile sentinel`) instead, or leave it empty to use the environment/role chain.

### CloudWatch event schema

CloudWatch gets **state changes only**, not every tick an incident stays open: the per-tick
detail (patterns, samples, hint, the full timeline) lives in the local SQLite store instead
(`incidents`, `incident_events`), which is the audit trail of record. The six event types:

| `event` | fires on |
|---|---|
| `incident_opened` | a service's incident opens for the first time |
| `incident_reopened` | a resolved incident re-opens within `COOLDOWN_SECONDS` |
| `incident_escalated` | a service's current severity increases |
| `incident_resolved` | a service's incident resolves (hysteresis held for `RESOLVE_TICKS`) |
| `notification` | an SNS-eligible send went out (open/escalate at HIGH+, or a notified resolve) |
| `test` | `POST /api/aws/test` |

Every event is one JSON log message with this fixed set of top-level fields — a stable contract
to write Logs Insights queries against:

| field | meaning |
|---|---|
| `event` | one of the six values above |
| `incident_id` | the member (per-service) incident id |
| `group_id` | the correlation group id (one outage) |
| `service` | the service the event is about |
| `severity` | `LOW` \| `MEDIUM` \| `HIGH` \| `CRITICAL` |
| `origin` | the group's probable-origin service |
| `error_rate` | the triggering signal's current value (null if not an error-rate signal) |
| `baseline` | that signal's baseline median (null if none) |
| `z` | that signal's robust z-score (null if none) |
| `timestamp` | epoch seconds (float), UTC |

Example Logs Insights query — CRITICAL opens in the last day:
`fields @timestamp, service, origin, error_rate, baseline, z | filter event = "incident_opened" and severity = "CRITICAL" | sort @timestamp desc`

### Readable SNS notifications

Written for a phone screen. Subject (ASCII, under 100 chars) carries severity and title, so the
body does not repeat them: `[<SEVERITY>] <origin> <slowdown|errors|outage|anomaly> - <N> services
affected` (or `[RESOLVED] ...` on resolve). The title word (slowdown/errors/outage/anomaly) is
based on every signal type the incident has ever shown, not just this tick's, so it stays the
same word across open, escalate and resolve. Body sections: started-at (`DISPLAY_TIMEZONE`) /
still-active, CHECK FIRST (a shortened, imperative version of the hint — e.g. "Check the
database: health, latency, connection pool." — plus the top pattern as a readable quoted phrase),
WHAT'S WRONG (a plain-language multiplier, e.g. "Error rate 3x normal (7% vs 2%)" or "15x slower
than normal (185 ms vs 12 ms)" — no z-scores), AFFECTED (one service per line, origin marked),
WHAT HAPPENED (at most 4 deduplicated key moments from the origin's own timeline, with seconds so
escalations landing in the same minute are still distinguishable), then the dashboard link and
`Ref: <group id>` at the very bottom. A resolve notification is a single short summary line
instead: `RESOLVED: <title>. Lasted <duration>. Peak: <severity>. Affected: <services>. No action
needed.` Redacted a second time on the way out (defense in depth) and well under SNS's 256 KB
message limit.

Least-privilege IAM policy for the `sentinel-ai` user (replace the account id, region, group and
topic):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:us-east-1:<account-id>:log-group:/sentinel-ai/*"
    },
    {
      "Effect": "Allow",
      "Action": ["sns:Publish", "sns:GetTopicAttributes"],
      "Resource": "arn:aws:sns:us-east-1:<account-id>:<topic>"
    }
  ]
}
```

`sts:GetCallerIdentity` (used by the startup self-check) needs no resource-level permission —
every principal can call it.

## Design decisions

See [`docs/DESIGN.md`](docs/DESIGN.md).

## Notes on ambiguity

Where the spec was ambiguous, the choice is configurable. Recorded here as they arise:

- Python: the spec says 3.11; the `sentinel` env is 3.12. The code targets `>=3.11`.
- One incident card per correlation group (an outage), with each service's incident nested
  inside: `CLAUDE.md` §7.
- Silence is measured over the last 20 s (`SILENCE_LOOKBACK_SECONDS`); 60 gives the spec's
  literal "window volume < 20% of median", which takes about 48 s to fire.
- A silent service is released when its recent volume is back to 50% of normal
  (`SILENCE_RESOLVE_RATIO`), not on a z-score (see `docs/DESIGN.md`).
- `GET /api/health.aws` reports a third value, `"error"`, beyond the spec's `"live"|"dryrun"`:
  it means the AWS credentials themselves failed the startup self-check (as opposed to `"dryrun"`,
  which also covers AWS simply being disabled). `aws_checks` gives the per-check reason either way.
