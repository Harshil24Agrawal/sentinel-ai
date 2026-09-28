"""Load the synthetic service catalogue (services.yaml). Also the dependency graph source."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_SERVICES_PATH = Path(__file__).with_name("services.yaml")


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    rps: float
    base_error: float
    latency_mean_ms: float
    latency_std_ms: float
    depends_on: tuple[str, ...] = field(default_factory=tuple)


def load_services(path: str | Path | None = None) -> dict[str, ServiceSpec]:
    data = yaml.safe_load(Path(path or DEFAULT_SERVICES_PATH).read_text(encoding="utf-8"))
    specs: dict[str, ServiceSpec] = {}
    for name, cfg in data["services"].items():
        mean, std = cfg["latency_ms"]
        specs[name] = ServiceSpec(
            name=name,
            rps=float(cfg["rps"]),
            base_error=float(cfg["base_error"]),
            latency_mean_ms=float(mean),
            latency_std_ms=float(std),
            depends_on=tuple(cfg.get("depends_on") or ()),
        )
    unknown = {d for s in specs.values() for d in s.depends_on} - specs.keys()
    if unknown:
        raise ValueError(f"services.yaml: unknown dependencies {sorted(unknown)}")
    return specs
