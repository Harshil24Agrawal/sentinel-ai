# SentinelAI

Real-time log anomaly detection and incident intelligence for healthcare software operations.
Built for the Acentra Health hackathon (Problem 1: Real-Time Log Anomaly Detector with Alert Feed).

**Detect early. Explain clearly. Show impact. Support human response.**

> Status: backend complete through Phase 4 (ingestion, detection, incidents, REST + WebSocket API).
> Sections marked _TBD_ are filled in by later phases (see `CLAUDE.md` §14).

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
(payloads are redacted a second time on the way out). That is the default and needs no AWS.

CloudWatch Logs (every incident state change) and SNS (HIGH/CRITICAL opens and escalations, and
the resolve of a group that notified) are used **only** when `AWS_ENABLED=true` **and** AWS
credentials are found (environment variables, a profile or a role); otherwise it stays a dry
run and logs a warning. Sending happens on a background worker with retries, so a slow or failing
AWS call never delays detection or the dashboard. `/api/health` reports `aws: "live"|"dryrun"`.

```
AWS_ENABLED=true
AWS_REGION=us-east-1
CW_LOG_GROUP=/sentinel-ai/incidents
CW_LOG_STREAM=demo
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:<account-id>:<topic>
```

Least-privilege IAM policy (replace the account id, region, group and topic):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": [
        "arn:aws:logs:us-east-1:<account-id>:log-group:/sentinel-ai/incidents",
        "arn:aws:logs:us-east-1:<account-id>:log-group:/sentinel-ai/incidents:log-stream:*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-east-1:<account-id>:<topic>"
    }
  ]
}
```

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
