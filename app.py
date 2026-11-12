"""Bounded public demo and tenant-authenticated usage API."""

import logging
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator

from analytics import assigned_variant
from billing import month_bounds
from demo import overview, run_scenario, seed
from store import Capacity, Conflict, Store

logger = logging.getLogger("meter")
store = None
request_times = deque(maxlen=240)
rate_lock = threading.Lock()


@asynccontextmanager
async def lifespan(application):
    global store
    store = Store(os.environ["DATABASE_URL"])
    seed(store)
    yield


app = FastAPI(
    title="Meterwork - usage and product evidence",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(StrictModel):
    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:/-]+$")
    occurred_at: AwareDatetime
    units: StrictInt = Field(ge=-100000, le=100000)
    correction_of: str | None = Field(default=None, max_length=96)

    @field_validator("occurred_at")
    @classmethod
    def valid_time(cls, value):
        if value < datetime(2026, 1, 1, tzinfo=UTC) or value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Occurrence must be from 2026 onward and not in the future")
        return value


class Product(StrictModel):
    event_id: str = Field(min_length=1, max_length=96)
    actor_id: str = Field(min_length=1, max_length=96)
    event_name: Literal["signup", "activation", "api_call", "api_error", "exposure"]
    occurred_at: AwareDatetime
    properties: dict = Field(default_factory=dict)
    experiment_id: Literal["onboarding-v1"] | None = None
    variant: Literal["control", "treatment"] | None = None
    _time = field_validator("occurred_at")(Usage.valid_time.__func__)


class Assignment(StrictModel):
    actor_id: str = Field(min_length=1, max_length=96)
    experiment_id: Literal["onboarding-v1"] = "onboarding-v1"


class StatementRequest(StrictModel):
    month: str = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")


class Scenario(StrictModel):
    scenario: Literal["replay", "late", "pricing", "isolation"]


def identity(authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "A sandbox bearer token is required")
    tenant = store.authenticate(authorization[7:])
    if not tenant:
        raise HTTPException(401, "Invalid or expired sandbox token")
    return tenant


@app.middleware("http")
async def protections(request: Request, call_next):
    started = time.perf_counter()
    if request.url.path.startswith("/api/"):
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, 400)
        if size > 8192:
            return JSONResponse({"detail": "Request exceeds 8KB"}, 413)
        # Fixed-size, process-wide limit. One replica is deliberate for this demo.
        with rate_lock:
            now = time.monotonic()
            while request_times and now - request_times[0] >= 60:
                request_times.popleft()
            if len(request_times) >= 240:
                return JSONResponse(
                    {"detail": "Demo request limit; retry in one minute"}, 429, headers={"Retry-After": "60"}
                )
            request_times.append(now)
        chunks = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > 8192:
                return JSONResponse({"detail": "Request exceeds 8KB"}, 413)
            chunks.append(chunk)
        # Starlette's cached request replays this bounded body to call_next.
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["Cache-Control"] = (
        "no-store" if request.url.path.startswith("/api/") else "public, max-age=60"
    )
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.2f}"
    return response


@app.exception_handler(ValueError)
async def domain_error(request, exc):
    status = 409 if isinstance(exc, Conflict) else 429 if isinstance(exc, Capacity) else 422
    return JSONResponse({"detail": str(exc)}, status)


@app.get("/healthz")
def health():
    with store.tenant("demo") as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/demo")
def public_demo():
    return overview(store, "demo")


@app.post("/api/sandbox", status_code=201)
def sandbox():
    tenant, token = store.create_tenant()
    return {
        "token": token,
        "tenant_id": tenant["id"],
        "expires_at": tenant["expires_at"],
        "scope": tenant["scope"],
    }


@app.post("/api/demo/run")
def exercise(body: Scenario):
    return run_scenario(store, body.scenario)


@app.get("/api/overview")
def tenant_overview(tenant=Depends(identity)):
    return overview(store, tenant["id"])


@app.post("/api/usage")
def ingest(body: Usage, tenant=Depends(identity)):
    return store.ingest_usage(tenant["id"], body.model_dump())


@app.post("/api/events")
def event(body: Product, tenant=Depends(identity)):
    if body.event_name == "exposure":
        if body.experiment_id is None or body.variant != assigned_variant(body.actor_id, body.experiment_id):
            raise ValueError("Exposure must use the server-assigned experiment variant")
    return store.ingest_product(tenant["id"], body.model_dump())


@app.post("/api/assignment")
def assign(body: Assignment, tenant=Depends(identity)):
    variant = assigned_variant(body.actor_id, body.experiment_id)
    event_id = "assignment:" + str(uuid.uuid5(uuid.NAMESPACE_URL, body.experiment_id + ":" + body.actor_id))
    # Repeated assignments reuse the original immutable timestamp and event identity.
    with store.tenant(tenant["id"]) as conn:
        old = conn.execute("SELECT occurred_at FROM product_events WHERE event_id=%s", (event_id,)).fetchone()
    try:
        store.ingest_product(
            tenant["id"],
            {
                "event_id": event_id,
                "actor_id": body.actor_id,
                "event_name": "assignment",
                "occurred_at": old["occurred_at"] if old else datetime.now(UTC),
                "properties": {},
                "experiment_id": body.experiment_id,
                "variant": variant,
            },
        )
    except Conflict:
        # A concurrent first assignment may have committed between the lookup
        # and insertion. Accept only the same immutable assignment, not a reused
        # identity for a different event or experiment.
        with store.tenant(tenant["id"]) as conn:
            winner = conn.execute(
                "SELECT actor_id,event_name,experiment_id,variant,properties FROM product_events WHERE event_id=%s",
                (event_id,),
            ).fetchone()
        expected = {
            "actor_id": body.actor_id,
            "event_name": "assignment",
            "experiment_id": body.experiment_id,
            "variant": variant,
            "properties": {},
        }
        if winner != expected:
            raise
    return {"variant": variant, "event_id": event_id, "experiment_id": body.experiment_id}


@app.post("/api/statements")
def publish(body: StatementRequest, tenant=Depends(identity)):
    month_bounds(body.month)
    return store.publish(tenant["id"], body.month)


@app.get("/api/statements")
def statements(month: str | None = None, tenant=Depends(identity)):
    rows = store.snapshot(tenant["id"])[3]
    return [row for row in rows if month is None or row["month"] == month]


app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
