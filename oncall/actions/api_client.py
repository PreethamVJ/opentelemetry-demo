from __future__ import annotations

import os
from typing import Any

import httpx

from oncall.schema import ACTION_API_TYPE, Plan

ACTION_API_URL = os.getenv("ACTION_API_URL", "http://host.docker.internal:8098")


def build_request(plan: Plan, dry_run: bool) -> dict[str, Any]:
    kind = ACTION_API_TYPE[plan.action]
    if kind == "restart_service":
        params = {"service": plan.target}
    elif kind == "toggle_flag":
        params = {"flag": plan.target, "variant": plan.params.get("variant", "off")}
    else:
        params = {"users": int(plan.params.get("users", 10))}
    return {"type": kind, "params": params, "reason": plan.reason[:300], "dry_run": dry_run}


def send(plan: Plan, dry_run: bool = False) -> dict[str, Any]:
    if plan.action not in ACTION_API_TYPE:
        return {"ok": False, "error": f"{plan.action} is not an Action API action"}
    try:
        response = httpx.post(f"{ACTION_API_URL}/action", json=build_request(plan, dry_run), timeout=30)
        return response.json()
    except Exception as exc:
        return {"ok": False, "error": f"Action API unreachable: {type(exc).__name__}: {exc}"}
