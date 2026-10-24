from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oncall.actions import api_client
from oncall.config import settings
from oncall.contracts import Incident
from oncall.ledger import EvidenceLedger
from oncall.runbook import affected_service, plan_for, prevention_for
from oncall.schema import ActionResult, Diagnosis, Plan

RECOVERY_WAIT_SECONDS = 60
RECOVERY_POLL_SECONDS = 10


def incidents_dir() -> Path:
    path = Path(settings.RUNS_DIR) / "incidents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def kill_switch_on() -> bool:
    return (Path(settings.RUNS_DIR) / "KILL").exists()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(incident_id: str, suffix: str, payload: Any) -> None:
    (incidents_dir() / f"{incident_id}.{suffix}").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _read(incident_id: str, suffix: str) -> Any:
    path = incidents_dir() / f"{incident_id}.{suffix}"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def log_event(incident_id: str, event: str) -> None:
    with (incidents_dir() / f"{incident_id}.timeline.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps({"ts": _now(), "event": event}) + "\n")


def prepare(incident: Incident, diagnosis: Diagnosis, ledger: EvidenceLedger) -> Plan:
    diagnosis.incident_id = incident.id
    plan = plan_for(diagnosis)
    ledger.dump_jsonl(str(incidents_dir() / f"{incident.id}.evidence.jsonl"))
    _write(incident.id, "diagnosis.json", diagnosis.to_dict())
    _write(incident.id, "plan.json", plan.model_dump())
    log_event(incident.id, f"diagnosis: {diagnosis.component} ({diagnosis.confidence:.0%})")
    log_event(incident.id, f"plan proposed: {plan.action} {plan.target}, waiting for a human")
    return plan


def _evidence(incident_id: str) -> list[dict[str, Any]]:
    path = incidents_dir() / f"{incident_id}.evidence.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def status_of(plan: dict | None, result: dict | None, diagnosis: dict | None) -> str:
    if result:
        if not result["executed"]:
            return "rejected" if result["detail"].startswith("rejected") else "escalated"
        return "resolved" if result["recovered"] else "failed"
    if not diagnosis or not plan:
        return "investigating"
    return "needs_human" if plan["action"] == "ESCALATE_TO_HUMAN" else "awaiting_approval"


def view(incident_id: str) -> dict[str, Any] | None:
    base = _read(incident_id, "json")
    if base is None:
        return None
    diagnosis, plan, result = _read(incident_id, "diagnosis.json"), _read(incident_id, "plan.json"), _read(incident_id, "result.json")
    report = incidents_dir() / f"{incident_id}.report.md"
    timeline = incidents_dir() / f"{incident_id}.timeline.jsonl"
    return {
        "incident": base["incident"], "breaches": base["breaches"], "diagnosis": diagnosis, "plan": plan, "result": result,
        "evidence": _evidence(incident_id), "status": status_of(plan, result, diagnosis),
        "report": report.read_text(encoding="utf-8") if report.exists() else None,
        "timeline": [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()] if timeline.exists() else [],
    }


def list_incidents(limit: int = 30) -> list[dict[str, Any]]:
    files = sorted((p for p in incidents_dir().glob("INC-*.json") if p.name.count(".") == 1), key=lambda p: p.stat().st_mtime, reverse=True)
    views = [view(p.stem) for p in files[:limit]]
    return [v for v in views if v]


def _recovered(plan: Plan) -> bool:
    from oncall.detect import poller

    service = affected_service(plan)
    deadline = time.monotonic() + RECOVERY_WAIT_SECONDS
    while True:
        containers = poller.sample_containers()
        running = {name for name, state in containers.items() if state == "running"} | {service}
        breaches = poller.find_breaches(poller.sample_metrics(), containers, running)
        if not any(b["service"] == service for b in breaches):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RECOVERY_POLL_SECONDS)


def decide(incident_id: str, approve: bool, approver: str = "operator", dry_run: bool = False) -> ActionResult:
    plan_data = _read(incident_id, "plan.json")
    if plan_data is None:
        raise LookupError(f"no plan for {incident_id}")
    if _read(incident_id, "result.json") is not None:
        raise ValueError(f"{incident_id} was already decided")
    plan = Plan.model_validate(plan_data)

    def finish(executed: bool, detail: str, recovered: bool | None = None) -> ActionResult:
        result = ActionResult(incident_id=incident_id, action=plan.action, target=plan.target, executed=executed, dry_run=dry_run,
                              recovered=recovered, detail=detail, evidence_ids=plan.evidence_ids)
        _write(incident_id, "result.json", result.model_dump())
        log_event(incident_id, f"result: {detail}")
        (incidents_dir() / f"{incident_id}.report.md").write_text(build_report(incident_id), encoding="utf-8")
        return result

    log_event(incident_id, f"{approver} {'approved' if approve else 'rejected'} the plan" + (" (dry run)" if dry_run else ""))
    if not approve:
        return finish(False, f"rejected by {approver}; incident stays open")
    if plan.action == "ESCALATE_TO_HUMAN":
        return finish(False, f"escalated to a human by {approver}; no automatic change made")
    if kill_switch_on():
        return finish(False, "blocked: kill switch is on, no change made")
    sent = api_client.send(plan, dry_run=dry_run)
    if not sent.get("ok"):
        return finish(False, f"action refused or failed: {sent.get('error', sent)}")
    if dry_run:
        return finish(False, f"dry run only, nothing changed: {sent.get('result')}")
    log_event(incident_id, f"action done: {sent.get('result')}; checking recovery")
    recovered = _recovered(plan)
    detail = f"{sent.get('result')}; " + ("service recovered" if recovered else "service did NOT recover, escalate to a human")
    return finish(True, detail, recovered)


def build_report(incident_id: str) -> str:
    v = view(incident_id)
    if v is None:
        return ""
    d, plan, result = v["diagnosis"] or {}, v["plan"] or {}, v["result"] or {}
    lines = [f"# Incident report {incident_id}", "", f"**Symptom:** {v['incident']['symptom']}  ", f"**Started:** {v['incident']['started_at']}", "",
             "## Timeline"]
    lines += [f"- `{e['ts']}` {e['event']}" for e in v["timeline"]] or ["- no events recorded"]
    lines += ["", "## Root cause", f"**{d.get('component', 'unknown')}** ({d.get('fault_class', 'unknown')}), confidence {d.get('confidence', 0):.0%}", "", d.get("cause", ""), "",
              "## Evidence"]
    cited = set(d.get("evidence_ids", []))
    lines += [f"- **{e['id']}** ({e['signal']}, {e['tool']}): {e['summary']}" for e in v["evidence"] if e["id"] in cited] or ["- none cited"]
    lines += ["", "## Action taken", f"- Proposed: `{plan.get('action')}` on `{plan.get('target')}` {plan.get('params') or ''}",
              f"- Outcome: {result.get('detail', 'pending a human decision')}", f"- Recovered: {result.get('recovered')}", "",
              "## What prevents a repeat", prevention_for(d.get("fault_class", "unknown"))]
    return "\n".join(lines) + "\n"
