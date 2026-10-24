import json
from types import SimpleNamespace

import pytest

from oncall.actions import api_client
from oncall.agent import pipeline
from oncall.contracts import Evidence, Fact, Incident, Signal
from oncall.ledger import EvidenceLedger
from oncall.runbook import plan_for
from oncall.schema import Diagnosis, Plan


def diagnosis(component, fault, confidence=0.9, escalate=False):
    return Diagnosis(incident_id="INC-T", component=component, fault_class=fault, confidence=confidence, escalate=escalate,
                     cause=f"{component} is broken", evidence_ids=["E-001"])


@pytest.mark.parametrize("component,fault,action,target", [
    ("payment", "app_error", "RESET_FEATURE_FLAG", "paymentFailure"),
    ("ad", "cpu_throttle", "RESET_FEATURE_FLAG", "adHighCpu"),
    ("shipping", "container_down", "RESTART_CONTAINER", "shipping"),
    ("valkey-cart", "container_down", "RESTART_CONTAINER", "valkey-cart"),
    ("product-catalog", "cpu_throttle", "RESTART_CONTAINER", "product-catalog"),
    ("frontend", "traffic_flood", "THROTTLE_TRAFFIC", "load-generator"),
    ("otel-collector", "container_down", "ESCALATE_TO_HUMAN", "on-call"),
    ("frontend", "network_fault", "ESCALATE_TO_HUMAN", "on-call"),
])
def test_runbook_picks_the_allowed_fix(component, fault, action, target):
    plan = plan_for(diagnosis(component, fault))
    assert (plan.action, plan.target) == (action, target)


def test_unsure_diagnosis_escalates_instead_of_acting():
    assert plan_for(diagnosis("payment", "app_error", confidence=0.3)).action == "ESCALATE_TO_HUMAN"
    assert plan_for(diagnosis("payment", "app_error", escalate=True)).action == "ESCALATE_TO_HUMAN"


def test_action_api_request_shapes():
    assert api_client.build_request(Plan(action="RESTART_CONTAINER", target="shipping", reason="r"), False) == {
        "type": "restart_service", "params": {"service": "shipping"}, "reason": "r", "dry_run": False}
    flag = api_client.build_request(Plan(action="RESET_FEATURE_FLAG", target="paymentFailure", params={"variant": "off"}, reason="r"), True)
    assert flag["type"] == "toggle_flag" and flag["params"] == {"flag": "paymentFailure", "variant": "off"} and flag["dry_run"]
    assert api_client.build_request(Plan(action="THROTTLE_TRAFFIC", target="load-generator", params={"users": 5}, reason="r"), False)["params"] == {"users": 5}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "settings", SimpleNamespace(RUNS_DIR=str(tmp_path)))
    incident = Incident(id="INC-T", symptom="shipping container is exited", started_at="2026-01-01T00:00:00+00:00", window="2m", source="poller")
    (pipeline.incidents_dir() / "INC-T.json").write_text(json.dumps({"incident": incident.model_dump(), "breaches": []}))
    ledger = EvidenceLedger()
    ledger.add("get_deploy_history", "q", Fact(signal=Signal.CONTAINER, summary="shipping exited", data={}))
    pipeline.prepare(incident, diagnosis("shipping", "container_down"), ledger)
    return tmp_path


def test_nothing_runs_until_a_human_says_yes(prepared, monkeypatch):
    sent = []
    monkeypatch.setattr(api_client, "send", lambda plan, dry_run=False: sent.append(plan) or {"ok": True, "result": "restart shipping"})
    monkeypatch.setattr(pipeline, "_recovered", lambda plan: True)
    assert pipeline.view("INC-T")["status"] == "awaiting_approval"
    assert sent == []

    result = pipeline.decide("INC-T", approve=True)
    assert result.executed and result.recovered and len(sent) == 1
    assert pipeline.view("INC-T")["status"] == "resolved"
    assert "## Root cause" in pipeline.view("INC-T")["report"]
    with pytest.raises(ValueError):
        pipeline.decide("INC-T", approve=True)


def test_rejection_and_kill_switch_change_nothing(prepared, monkeypatch):
    sent = []
    monkeypatch.setattr(api_client, "send", lambda plan, dry_run=False: sent.append(plan) or {"ok": True})
    (prepared / "KILL").write_text("on")
    blocked = pipeline.decide("INC-T", approve=True)
    assert not blocked.executed and "kill switch" in blocked.detail and sent == []


def test_rejected_plan_is_not_executed(prepared, monkeypatch):
    sent = []
    monkeypatch.setattr(api_client, "send", lambda plan, dry_run=False: sent.append(plan) or {"ok": True})
    result = pipeline.decide("INC-T", approve=False)
    assert not result.executed and sent == [] and pipeline.view("INC-T")["status"] == "rejected"


def test_failed_recovery_is_reported_not_hidden(prepared, monkeypatch):
    monkeypatch.setattr(api_client, "send", lambda plan, dry_run=False: {"ok": True, "result": "restart shipping"})
    monkeypatch.setattr(pipeline, "_recovered", lambda plan: False)
    result = pipeline.decide("INC-T", approve=True)
    assert result.executed and result.recovered is False and pipeline.view("INC-T")["status"] == "failed"


def test_diagnosis_without_a_plan_is_not_offered_for_approval():
    assert pipeline.status_of(None, None, {"component": "x"}) == "investigating"
