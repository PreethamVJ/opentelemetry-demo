from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from oncall.clients.backends import docker, prom_query
from oncall.config import settings
from oncall.contracts import Incident
from oncall.guard import is_banned_service

SERVER_KINDS = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
ERROR_RATE = 0.05
ERROR_RATIO = 2.0
P95_MS = 1000.0
P95_RATIO = 3.0
MIN_REQUESTS = 20
WINDOW_MINUTES = 2
BAD_STATES = {"exited", "dead", "restarting", "paused"}


def _by_service(expr: str) -> dict[str, float]:
    values = {r["metric"]["service_name"]: float(r["value"][1]) for r in prom_query(expr)}
    return {name: value for name, value in values.items() if value == value}


def sample_metrics(window: int = WINDOW_MINUTES) -> dict[str, dict[str, float]]:
    calls = f"traces_span_metrics_calls_total{{{SERVER_KINDS}}}"
    failed = f'traces_span_metrics_calls_total{{{SERVER_KINDS},status_code="STATUS_CODE_ERROR"}}'
    bucket = f"traces_span_metrics_duration_milliseconds_bucket{{{SERVER_KINDS}}}"
    p95 = f"histogram_quantile(0.95, sum by (le, service_name)(rate({bucket}[{window}m])))"
    base = f"histogram_quantile(0.95, sum by (le, service_name)(rate({bucket}[10m] offset {window}m)))"
    requests = _by_service(f"sum by (service_name)(increase({calls}[{window}m]))")
    errors = _by_service(f"sum by (service_name)(increase({failed}[{window}m]))")
    latency, baseline = _by_service(p95), _by_service(base)
    prev_requests = _by_service(f"sum by (service_name)(increase({calls}[10m] offset {window}m))")
    prev_errors = _by_service(f"sum by (service_name)(increase({failed}[10m] offset {window}m))")
    return {
        name: {
            "requests": requests[name],
            "errors": errors.get(name, 0.0),
            "baseline_error_rate": prev_errors.get(name, 0.0) / prev_requests[name] if prev_requests.get(name, 0.0) >= MIN_REQUESTS else 0.0,
            "p95_ms": latency.get(name, 0.0),
            "baseline_p95_ms": baseline.get(name, 0.0),
        }
        for name in requests
    }


def sample_containers() -> dict[str, str]:
    out = docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}")
    states = {}
    for line in out.splitlines():
        name, _, state = line.partition("\t")
        if name:
            states[name] = state.strip().lower()
    return states


def find_breaches(metrics: dict[str, dict[str, float]], containers: dict[str, str], seen_running: set[str]) -> list[dict[str, Any]]:
    breaches: list[dict[str, Any]] = []
    for name, m in metrics.items():
        if is_banned_service(name) or m["requests"] < MIN_REQUESTS:
            continue
        rate = m["errors"] / m["requests"]
        if rate >= ERROR_RATE and rate >= ERROR_RATIO * m.get("baseline_error_rate", 0.0):
            breaches.append({"kind": "error_rate", "service": name, "value": round(rate, 3), "threshold": ERROR_RATE})
        slow = m["p95_ms"] >= P95_MS and m["baseline_p95_ms"] > 0 and m["p95_ms"] >= P95_RATIO * m["baseline_p95_ms"]
        if slow:
            breaches.append({"kind": "latency", "service": name, "value": round(m["p95_ms"]), "threshold": P95_MS})
    for name, state in containers.items():
        if state == "running":
            seen_running.add(name)
        elif name in seen_running and state in BAD_STATES and not is_banned_service(name):
            breaches.append({"kind": "container", "service": name, "value": state, "threshold": "running"})
    return breaches


def describe(breaches: list[dict[str, Any]]) -> str:
    parts = []
    for b in sorted(breaches, key=lambda b: b["kind"] != "error_rate")[:4]:
        if b["kind"] == "error_rate":
            parts.append(f"{b['service']} error rate {b['value']:.0%} (threshold {b['threshold']:.0%})")
        elif b["kind"] == "latency":
            parts.append(f"{b['service']} p95 latency {b['value']}ms (threshold {b['threshold']:.0f}ms)")
        else:
            parts.append(f"{b['service']} container is {b['value']}")
    return "; ".join(parts)


def make_incident(breaches: list[dict[str, Any]], window: int = WINDOW_MINUTES) -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        id=f"INC-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
        symptom=describe(breaches),
        started_at=now.isoformat(),
        window=f"{window}m",
        source="poller",
    )


def save_incident(incident: Incident, breaches: list[dict[str, Any]], runs_dir: str) -> Path:
    path = Path(runs_dir) / "incidents" / f"{incident.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"incident": incident.model_dump(), "breaches": breaches}, indent=2), encoding="utf-8")
    return path


def investigate_incident(incident: Incident) -> None:
    from oncall.agent.run import investigate, new_run_id
    from oncall.events import EventLog
    from oncall.ledger import EvidenceLedger
    from oncall.llm import LLMClient
    from oncall.schema import to_submission

    events = EventLog(new_run_id(), settings.RUNS_DIR)
    events.emit("alert", incident.model_dump())
    diagnosis, _ = investigate(incident.symptom, LLMClient(), EvidenceLedger(), events)
    diagnosis.incident_id = incident.id
    path = Path(settings.RUNS_DIR) / "incidents" / f"{incident.id}.diagnosis.json"
    path.write_text(json.dumps(to_submission(diagnosis), indent=2), encoding="utf-8")


class Detector:
    def __init__(self, on_incident: Callable[[Incident], None] = investigate_incident, interval: float = 15.0,
                 confirm: int = 2, cooldown: float = 600.0, runs_dir: str | None = None) -> None:
        self.on_incident = on_incident
        self.interval = interval
        self.confirm = confirm
        self.cooldown = cooldown
        self.runs_dir = runs_dir or settings.RUNS_DIR
        self.seen_running: set[str] = set()
        self.streak = 0
        self.quiet_until = 0.0
        self.stop = threading.Event()

    def poll_once(self, now: float | None = None) -> Incident | None:
        now = time.monotonic() if now is None else now
        breaches = find_breaches(sample_metrics(), sample_containers(), self.seen_running)
        self.streak = self.streak + 1 if breaches else 0
        if self.streak < self.confirm or now < self.quiet_until:
            return None
        incident = make_incident(breaches)
        save_incident(incident, breaches, self.runs_dir)
        self.quiet_until = now + self.cooldown
        self.streak = 0
        self.on_incident(incident)
        return incident

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                print(f"detector poll failed: {type(exc).__name__}: {exc}", flush=True)
            self.stop.wait(self.interval)
