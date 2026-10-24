from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

UI_FILE = Path(__file__).parent / "ui" / "index.html"

LINKS = [
    {"name": "Shop", "url": "http://localhost:8080/", "note": "the demo store"},
    {"name": "Jaeger", "url": "http://localhost:16686/jaeger/ui/", "note": "traces"},
    {"name": "Prometheus", "url": "http://localhost:9090/", "note": "metrics"},
    {"name": "OpenSearch", "url": "http://localhost:9200/", "note": "logs API"},
    {"name": "Load generator", "url": "http://localhost:8080/loadgen/", "note": "traffic"},
    {"name": "Feature flag UI", "url": "http://localhost:8080/feature", "note": "fault switches (operators only)"},
    {"name": "API docs", "url": "http://localhost:8099/docs", "note": "this service"},
]


class Decision(BaseModel):
    approve: bool
    dry_run: bool = False
    approver: str = "operator"


@asynccontextmanager
async def lifespan(_: FastAPI):
    detector = None
    if os.getenv("ONCALL_DETECT") == "1":
        from oncall.detect.poller import Detector

        detector = Detector()
        threading.Thread(target=detector.run, daemon=True, name="detector").start()
    yield
    if detector:
        detector.stop.set()


app = FastAPI(lifespan=lifespan, title="AI On-Call Engineer")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_FILE)


@app.get("/api/links")
def links() -> list[dict[str, str]]:
    return LINKS


@app.get("/api/graph")
def graph() -> dict[str, str]:
    from oncall.agent.graph import mermaid

    return {"mermaid": mermaid()}


@app.get("/api/incidents")
def incidents() -> list[dict]:
    from oncall.agent.pipeline import kill_switch_on, list_incidents

    return [{"kill_switch": kill_switch_on()}, *list_incidents()]


@app.post("/api/incidents/{incident_id}/decision")
def decision(incident_id: str, body: Decision) -> dict:
    from oncall.agent import pipeline

    try:
        return pipeline.decide(incident_id, body.approve, body.approver, body.dry_run).model_dump()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/kill-switch/{state}")
def kill_switch(state: str) -> dict[str, bool]:
    from oncall.agent.pipeline import kill_switch_on
    from oncall.config import settings

    flag = Path(settings.RUNS_DIR) / "KILL"
    if state == "on":
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("on", encoding="utf-8")
    elif state == "off":
        flag.unlink(missing_ok=True)
    else:
        raise HTTPException(400, "state must be on or off")
    return {"kill_switch": kill_switch_on()}


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8099)
