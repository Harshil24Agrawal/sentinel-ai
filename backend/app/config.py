"""Settings: every default from CLAUDE.md §11, overridable by environment or .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LOG_REGEX = (
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+(?P<level>[A-Za-z]+)\s+(?P<service>\S+)\s+"
    r"(?:latency_ms=(?P<latency_ms>\d+)\s+)?(?P<message>.*)$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Files
    LOG_PATH: str = "data/logs/app.log"
    DB_PATH: str = "data/sentinel.db"
    DRYRUN_PATH: str = "data/notifications_dryrun.jsonl"

    # Pipeline timing
    TICK_SECONDS: float = 1.0
    WINDOW_SECONDS: int = 60
    BASELINE_HISTORY_SAMPLES: int = 900
    WARMUP_SAMPLES: int = 30

    # Collector / parser
    COLLECTOR_FROM_START: bool = False
    COLLECTOR_POLL_SECONDS: float = 0.1
    LOG_REGEX: str = DEFAULT_LOG_REGEX

    # Detection thresholds
    MIN_VOLUME: int = 30
    OPEN_Z: float = 3.0
    RESOLVE_Z: float = 1.5
    ABS_FLOOR_ERROR: float = 0.02
    LATENCY_MIN_SAMPLES: int = 20
    LATENCY_MIN_RATIO: float = 1.5
    SILENCE_RATIO: float = 0.2
    # A silent service is released once its recent volume is back to this share of normal
    # (opens below 20%, closes at 50%: a ratio, because volume z is too noisy to release on).
    SILENCE_RESOLVE_RATIO: float = 0.5
    SURGE_RATIO: float = 5.0
    SURGE_MIN_COUNT: int = 10
    NEW_PATTERN_MIN_COUNT: int = 3

    # Silence looks at the last N seconds of the window. 60 (= WINDOW_SECONDS) is the spec's
    # literal "window volume < 20% of median" (~48s to fire); 20 fires in ~16s.
    SILENCE_LOOKBACK_SECONDS: int = 20
    MAX_PATTERNS_PER_SERVICE: int = 1000  # bounds first-seen/rate tracking per service

    # Severity (§6.9)
    SEVERITY_Z_MEDIUM: float = 5.0
    SEVERITY_Z_HIGH: float = 8.0
    SEVERITY_Z_CRITICAL: float = 12.0
    SEVERITY_LONG_ANOMALY_SECONDS: int = 120
    CRITICAL_ERROR_RATE: float = 0.25
    CRITICAL_SILENCE_SECONDS: int = 30

    # Extra probable-cause rules as (regex, hint) pairs, checked before the built-in ones.
    # JSON in the environment, e.g. HINT_RULES_EXTRA='[["cert|tls","Check TLS certificates."]]'
    HINT_RULES_EXTRA: list[tuple[str, str]] = []

    # Baseline floors (added to 1.4826 * MAD)
    ERROR_RATE_FLOOR: float = 0.005
    LATENCY_FLOOR_MS: float = 20.0
    VOLUME_FLOOR: float = 5.0

    # Incident lifecycle
    PERSIST_TICKS: int = 5
    RESOLVE_TICKS: int = 30
    COOLDOWN_SECONDS: int = 60
    CORRELATION_WINDOW_SECONDS: int = 180

    # Naive comparison detector
    NAIVE_ERROR_THRESHOLD: float = 0.05
    NAIVE_INTERVAL_SECONDS: int = 5

    # Redaction (extend via env as JSON, e.g. PHI_KEYS='["ssn","dob"]')
    PHI_KEYS: list[str] = [
        "ssn", "member_id", "patient_name", "name", "dob", "email", "phone", "address", "mrn",
    ]  # fmt: skip
    MEMBER_ID_PATTERN: str = r"\bMBR-?\d{6,}\b"

    # Notifier
    AWS_ENABLED: bool = False
    AWS_REGION: str = "us-east-1"
    AWS_PROFILE: str = ""  # named AWS CLI/SDK profile; empty uses the default credential chain
    CW_LOG_GROUP: str = "/sentinel-ai/incidents"
    CW_LOG_STREAM: str = "demo"
    SNS_TOPIC_ARN: str = ""
    DASHBOARD_URL: str = "http://localhost:8000"  # linked from SNS notification bodies
    DISPLAY_TIMEZONE: str = "Asia/Kolkata"  # human-facing times (SNS body); CloudWatch stays UTC

    # Optional LLM explainer
    LLM_PROVIDER: Literal["none", "anthropic", "openai", "azure_openai", "bedrock"] = "none"
    LLM_MODEL: str = ""
    LLM_TIMEOUT_SECONDS: float = 8.0
    LLM_MIN_INTERVAL_SECONDS: int = 60

    # API
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]  # Vite dev server
    HISTORY_SECONDS: int = 300  # metrics frames kept for GET /api/metrics/history
    LOG_LEVEL: str = "INFO"
    FRONTEND_DIR: str = ""  # default: <repo>/frontend, served at /

    # Live log feed: /api/logs/recent and the WebSocket "logs" message
    LOG_BUFFER_SIZE: int = 500
    LOG_INFO_PER_TICK: int = 10
    LOG_LINES_PER_MESSAGE: int = 50

    # Built-in synthetic traffic
    GENERATOR_ENABLED: bool = True
    GENERATOR_SEED: int | None = None  # set for reproducible demo traffic


@lru_cache
def get_settings() -> Settings:
    return Settings()
