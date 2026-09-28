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
    SURGE_RATIO: float = 5.0
    SURGE_MIN_COUNT: int = 10
    NEW_PATTERN_MIN_COUNT: int = 3

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
    CW_LOG_GROUP: str = "/sentinel-ai/incidents"
    CW_LOG_STREAM: str = "demo"
    SNS_TOPIC_ARN: str = ""

    # Optional LLM explainer
    LLM_PROVIDER: Literal["none", "anthropic", "openai", "azure_openai", "bedrock"] = "none"
    LLM_MODEL: str = ""
    LLM_TIMEOUT_SECONDS: float = 8.0
    LLM_MIN_INTERVAL_SECONDS: int = 60

    # Built-in synthetic traffic
    GENERATOR_ENABLED: bool = True
    GENERATOR_SEED: int | None = None  # set for reproducible demo traffic


@lru_cache
def get_settings() -> Settings:
    return Settings()
