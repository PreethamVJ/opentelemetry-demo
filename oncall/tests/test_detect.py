import json
from pathlib import Path

from oncall.contracts import Incident
from oncall.detect import poller

FIXTURE = Path(__file__).parent.parent / "fixtures" / "incident_payment.json"


def healthy(name: str = "cart") -> dict:
    return {name: {"requests": 100.0, "errors": 0.0, "p95_ms": 20.0, "baseline_p95_ms": 18.0}}


def test_incident_fixture_matches_contract():
    Incident.model_validate(json.loads(FIXTURE.read_text()))


def test_healthy_stack_has_no_breach():
    assert poller.find_breaches(healthy(), {"cart": "running"}, set()) == []


def test_error_rate_breach_needs_enough_requests():
    busy = {"checkout": {"requests": 100.0, "errors": 40.0, "p95_ms": 50.0, "baseline_p95_ms": 50.0}}
    quiet = {"checkout": {"requests": 3.0, "errors": 3.0, "p95_ms": 50.0, "baseline_p95_ms": 50.0}}
    assert poller.find_breaches(busy, {}, set())[0]["kind"] == "error_rate"
    assert poller.find_breaches(quiet, {}, set()) == []


def test_latency_breach_compares_with_baseline():
    slow = {"ad": {"requests": 100.0, "errors": 0.0, "p95_ms": 2500.0, "baseline_p95_ms": 50.0}}
    always_slow = {"ad": {"requests": 100.0, "errors": 0.0, "p95_ms": 1200.0, "baseline_p95_ms": 1100.0}}
    assert poller.find_breaches(slow, {}, set())[0]["kind"] == "latency"
    assert poller.find_breaches(always_slow, {}, set()) == []


def test_stopped_container_is_flagged_only_after_it_was_seen_running():
    seen: set[str] = set()
    assert poller.find_breaches({}, {"shipping": "running"}, seen) == []
    breaches = poller.find_breaches({}, {"shipping": "exited"}, seen)
    assert breaches and breaches[0]["service"] == "shipping" and breaches[0]["kind"] == "container"
    assert poller.find_breaches({}, {"never-started": "exited"}, set()) == []


def test_detector_opens_one_incident_after_confirmation(tmp_path, monkeypatch):
    bad = {"checkout": {"requests": 100.0, "errors": 40.0, "p95_ms": 50.0, "baseline_p95_ms": 50.0}}
    monkeypatch.setattr(poller, "sample_metrics", lambda: bad)
    monkeypatch.setattr(poller, "sample_containers", lambda: {})
    opened = []
    detector = poller.Detector(on_incident=opened.append, confirm=2, cooldown=600, runs_dir=str(tmp_path))
    assert detector.poll_once(now=0) is None
    incident = detector.poll_once(now=15)
    assert incident is not None and incident.source == "poller" and "checkout" in incident.symptom
    assert detector.poll_once(now=30) is None
    assert detector.poll_once(now=45) is None
    assert len(opened) == 1
    assert (tmp_path / "incidents" / f"{incident.id}.json").exists()
