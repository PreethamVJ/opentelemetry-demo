from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI


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


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8099)
