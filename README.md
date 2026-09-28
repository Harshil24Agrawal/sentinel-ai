# SentinelAI

Real-time log anomaly detection and incident intelligence for healthcare software operations.
Built for the Acentra Health hackathon (Problem 1: Real-Time Log Anomaly Detector with Alert Feed).

**Detect early. Explain clearly. Show impact. Support human response.**

> Status: Phase 0 scaffold. Sections marked _TBD_ are filled in by later phases (see `CLAUDE.md` §14).

## Setup (Windows, PowerShell, conda env `sentinel`)

`conda run` fails when the user profile path contains a space, so call the env's interpreter directly:

```powershell
$py = "$env:USERPROFILE\Miniconda3\envs\sentinel\python.exe"
& $py -m pip install -e "backend[dev]"
& $py -m pytest backend          # run the tests
& $py -m ruff check backend      # lint
```

Run the server from the repo root so `data/` resolves correctly (_TBD: Phase 4_):
`& $py -m uvicorn app.main:app --app-dir backend`

## Demo script

_TBD: Phase 8 (see `CLAUDE.md` §13)._

## Configuration

Every setting has a default and can be overridden by an environment variable or `.env`
(copy `.env.example`). The full table of defaults is in `backend/app/config.py` and `CLAUDE.md` §11.

_TBD: table of keys._

## AWS least-privilege IAM policy

_TBD: Phase 6._

## Design decisions

See [`docs/DESIGN.md`](docs/DESIGN.md).

## Notes on ambiguity

Where the spec was ambiguous, the choice is configurable. Recorded here as they arise:

- Python: the spec says 3.11; the `sentinel` env is 3.12. The code targets `>=3.11`.
