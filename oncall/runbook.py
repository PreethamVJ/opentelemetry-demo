from __future__ import annotations

from oncall.config import settings
from oncall.schema import Diagnosis, Plan

FLAG_BY_SERVICE = {
    "payment": "paymentFailure",
    "cart": "cartFailure",
    "product-catalog": "productCatalogFailure",
    "recommendation": "recommendationCacheFailure",
    "ad": "adHighCpu",
}
RESTARTABLE = {
    "ad", "cart", "checkout", "currency", "email", "frontend", "image-provider", "payment",
    "product-catalog", "quote", "recommendation", "shipping", "valkey-cart", "astronomy-db",
}
THROTTLE_USERS = 10

PREVENTION = {
    "container_down": "Alert on container state and restart count, and add a health check with automatic restart for this service.",
    "app_error": "Gate risky changes behind a canary and alert on the service's error rate so a bad toggle is caught within minutes.",
    "cpu_throttle": "Set CPU and memory limits with headroom, and alert on sustained CPU saturation before latency degrades.",
    "traffic_flood": "Rate-limit the entry point and alert on request-rate spikes against the normal baseline.",
    "network_fault": "Add dependency latency alerts between services and test degraded-network behaviour regularly.",
    "unknown": "Collect more telemetry for this failure shape and review the escalation notes for a new runbook entry.",
}


def prevention_for(fault_class: str) -> str:
    return PREVENTION.get(fault_class, PREVENTION["unknown"])


def affected_service(plan: Plan) -> str:
    owners = {flag: service for service, flag in FLAG_BY_SERVICE.items()}
    return {"THROTTLE_TRAFFIC": "frontend", "RESET_FEATURE_FLAG": owners.get(plan.target, plan.target)}.get(plan.action, plan.target)


def _escalate(diagnosis: Diagnosis, why: str) -> Plan:
    return Plan(
        incident_id=diagnosis.incident_id, action="ESCALATE_TO_HUMAN", target="on-call",
        reason=f"{why} Findings: {diagnosis.cause or 'no conclusion'}", evidence_ids=diagnosis.evidence_ids,
    )


def plan_for(diagnosis: Diagnosis) -> Plan:
    component, fault = diagnosis.component, diagnosis.fault_class
    if diagnosis.escalate or diagnosis.confidence < settings.CONFIDENCE_THRESHOLD or component == "unknown":
        return _escalate(diagnosis, "Not confident enough for an automatic fix.")

    def plan(action: str, target: str, params: dict | None = None) -> Plan:
        return Plan(incident_id=diagnosis.incident_id, action=action, target=target, params=params or {},
                    reason=diagnosis.cause, evidence_ids=diagnosis.evidence_ids)

    if fault == "traffic_flood":
        return plan("THROTTLE_TRAFFIC", "load-generator", {"users": THROTTLE_USERS})
    flag = FLAG_BY_SERVICE.get(component)
    if fault == "app_error" and flag:
        return plan("RESET_FEATURE_FLAG", flag, {"variant": "off"})
    if fault == "cpu_throttle" and component == "ad" and flag:
        return plan("RESET_FEATURE_FLAG", flag, {"variant": "off"})
    if fault in ("container_down", "app_error", "cpu_throttle") and component in RESTARTABLE:
        return plan("RESTART_CONTAINER", component)
    return _escalate(diagnosis, "No allowed fix for this failure.")
