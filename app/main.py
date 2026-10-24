"""FastAPI entrypoint for the SafeRepo trusted control plane."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("saferepo")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    log.info("run ledger ready")
    yield


app = FastAPI(
    title="SafeRepo Run Ledger",
    version="1.0.0",
    description=(
        "Trusted control plane for SafeRepo. Stores run state; the untrusted "
        "work happens inside Celesto sandboxes."
    ),
    lifespan=lifespan,
)
app.include_router(router)
