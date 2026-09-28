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
| REST API (interactive docs) | http://localhost:8000/docs |
| WebSocket | `ws://localhost:8000/ws` |

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

## AWS least-privilege IAM policy

_TBD: Phase 6. Until then every notification is a dry run (recorded, nothing is sent)._

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
