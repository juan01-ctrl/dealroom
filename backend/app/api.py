import os
import secrets
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import Field, model_validator

from .api_contracts import (
    HistoryPage,
    ProfileView,
    SessionView,
    ThreadSnapshot,
    ThreadSummary,
    TimelinePage,
)
from .campaign_briefs import IDENTIFIER, BriefRevision, CampaignBriefs
from .configuration import validate_configuration
from .domain import Input, Policy, Proposal
from .evaluations import get_run, queue_run
from .identity import IdentityStore, configured_identity_gateway
from .mail_delivery import (
    MAX_INBOUND_BYTES,
    CreatorMailBackpressure,
    MailAuthenticationError,
    MailStore,
    configured_inbound_gateway,
)
from .operations import operations_status
from .read_models import HistoryCursor
from .retrieval import brief_document
from .storage import Conflict, Limit, NotFound, Store, pooled_store

COOKIE = "negotiation_session"


class Inbound(Input):
    event_id: str = Field(min_length=16, max_length=80, pattern=r"^[0-9a-zA-Z_-]+$")
    body: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def require_content(self):
        if not self.body.strip():
            raise ValueError("Enter a message.")
        return self


class ThreadRequest(Input):
    label: str = Field(min_length=1, max_length=60)
    event_id: str | None = Field(default=None, min_length=16, max_length=80, pattern=r"^[0-9a-zA-Z_-]+$")


class Recovery(Input):
    revision: int = Field(ge=0)
    job_id: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")
    action: Literal["retry", "skip"]


class PolicyUpdate(Input):
    version: int = Field(ge=1)
    policy: Policy


class Review(Input):
    action: Literal["approve", "edit", "reject"]
    version: int = Field(ge=1)
    proposal: Proposal | None = None
    mail_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_proposal(self):
        if (self.action == "edit") != (self.proposal is not None):
            raise ValueError("An edit requires a presentation proposal.")
        return self


class Transition(Input):
    revision: int = Field(ge=0)
    action: Literal["close", "reopen", "reanalyze", "retry"]


class Fault(Input):
    revision: int = Field(ge=0)
    boundary: Literal["checkpoint", "delivery"]


class BrowserBudget:
    """A process-local burst limit with a bounded number of client buckets."""

    def __init__(self):
        self.lock = Lock()
        self.buckets = defaultdict(deque)

    def allow(self, client):
        now = monotonic()
        with self.lock:
            if len(self.buckets) >= 2048:
                for key, values in list(self.buckets.items()):
                    if not values or now - values[-1] >= 60:
                        del self.buckets[key]
                if client not in self.buckets and len(self.buckets) >= 2048:
                    return False
            bucket = self.buckets[client]
            while bucket and now - bucket[0] >= 60:
                bucket.popleft()
            if len(bucket) >= 12:
                return False
            bucket.append(now)
            return True


def create_app(store=None, *, identity_gateway=None, inbound_gateway=None):
    identity_gateway = identity_gateway or configured_identity_gateway()
    inbound_gateway = inbound_gateway or configured_inbound_gateway()

    runtime_store = store

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal runtime_store
        if store is not None:
            yield
            return
        validate_configuration("api")
        with pooled_store(os.environ["DATABASE_URL"], max_size=16) as runtime_store:
            yield

    app = FastAPI(
        title="Negotiation Sandbox", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    budget = BrowserBudget()
    proxy_secret = os.getenv("API_PROXY_SECRET")
    mode = os.getenv("MODEL_MODE", "scripted")
    secure = os.getenv("COOKIE_SECURE") == "1"
    operational_token = os.getenv("OPERATIONS_TOKEN", "")
    capabilities = {
        "evaluations": mode != "live" or os.getenv("ALLOW_LIVE_EVALUATIONS") == "1",
        "simulated_failures": os.getenv("APP_ENV", "local") != "production",
    }

    def database():
        return runtime_store if runtime_store is not None else Store(os.environ["DATABASE_URL"])

    def creator_member(request, *, write=False):
        auth = request.headers.get("Authorization")
        workspace = request.headers.get("X-Workspace-Id")
        if auth is None and workspace is None:
            return None
        if not identity_gateway or not auth or not auth.startswith("Bearer ") or not workspace:
            raise HTTPException(401, "Creator identity is required.")
        try:
            claims = identity_gateway.verify(auth.removeprefix("Bearer "))
            member = IdentityStore(database()).member(claims, UUID(workspace))
        except ValueError as exc:
            raise HTTPException(401, "Creator identity is invalid or unavailable.") from exc
        if write and not member.can_approve:
            raise HTTPException(403, "Creator write access is required.")
        return member

    def tenant(request, *, write=False):
        member = creator_member(request, write=write)
        if member:
            return member.tenant_id
        identifier = database().session(request.cookies.get(COOKIE))
        if not identifier:
            raise HTTPException(401, "Start a new sandbox session.")
        return identifier

    def error_status(status_code):
        async def handler(request, error):
            return JSONResponse({"detail": str(error)}, status_code=status_code)

        return handler

    app.add_exception_handler(NotFound, error_status(404))
    app.add_exception_handler(Conflict, error_status(409))
    app.add_exception_handler(Limit, error_status(429))

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(TooManyRequests)
    async def database_busy(_request, _error):
        return JSONResponse(
            {"detail": "The database is busy. Retry shortly."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            passed_secret = request.headers.get("X-API-Proxy", "")
            if proxy_secret and not secrets.compare_digest(
                passed_secret.encode(), proxy_secret.encode()
            ):
                return JSONResponse({"detail": "Use the sandbox frontend."}, status_code=403)
            if request.method not in {"GET", "HEAD"} and (
                request.headers.get("X-Sandbox-Request") != "1"
                or request.headers.get("Sec-Fetch-Site") == "cross-site"
            ):
                return JSONResponse(
                    {"detail": "Request must originate from the sandbox."}, status_code=403
                )
            raw_length = request.headers.get("content-length", "0")
            if not raw_length.isdigit() or int(raw_length) > 16384:
                return JSONResponse({"detail": "Request body is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/"):
            response.headers["X-Dealroom-Schema"] = "1"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            database().check_schema()
        except Exception as exc:
            raise HTTPException(503, "Database unavailable.") from exc
        return {"status": "ok"}

    @app.post("/api/session", response_model=SessionView)
    def session(request: Request, response: Response):
        db = database()
        member = creator_member(request)
        if member:
            return {
                "threads": db.threads(member.tenant_id),
                "mode": mode,
                "capabilities": {
                    "evaluations": False,
                    "simulated_failures": False,
                    "can_write": member.can_approve,
                },
                "workspace_kind": "creator",
            }
        identifier = db.session(request.cookies.get(COOKIE))
        if not identifier:
            client = (request.headers.get("X-Sandbox-Client") if proxy_secret else None) or (
                request.client.host if request.client else "unknown"
            )
            if not budget.allow(client):
                raise Limit("Too many new sessions. Try again shortly.")
            token, identifier, _ = db.new_browser_session()
            response.set_cookie(
                COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=604800
            )
        return {
            "threads": db.threads(identifier),
            "mode": mode,
            "capabilities": capabilities,
            "workspace_kind": "sandbox",
        }

    @app.get("/api/briefs/{document_id}", response_class=PlainTextResponse)
    def public_brief(document_id: str):
        document = brief_document(document_id)
        if not document:
            raise NotFound("Campaign brief not found.")
        sections = [f"{section['title']}\n{section['body']}" for section in document["sections"]]
        return "\n\n".join([document["title"], f"Revision {document['revision']}", *sections])

    @app.get(
        "/api/negotiations/{thread}/briefs/{document_id}",
        response_model=BriefRevision,
        response_model_exclude_unset=True,
    )
    def inspect_brief(
        thread: UUID,
        document_id: Annotated[str, Path(max_length=80, pattern=IDENTIFIER)],
        revision: Annotated[int, Query(ge=1, le=2_147_483_647)],
        request: Request,
    ):
        member = creator_member(request)
        if not member:
            raise HTTPException(401, "Creator identity is required.")
        return CampaignBriefs(database()).inspect(member.tenant_id, thread, document_id, revision)

    @app.get("/api/profile", response_model=ProfileView)
    def profile(request: Request):
        return database().policy(tenant(request))

    @app.put("/api/profile")
    def update_profile(payload: PolicyUpdate, request: Request):
        return {
            "version": database().update_policy(
                tenant(request, write=True), payload.version, payload.policy
            )
        }

    @app.get("/api/negotiations", response_model=list[ThreadSummary])
    def list_threads(request: Request):
        return database().threads(tenant(request))

    @app.post("/api/negotiations", status_code=201)
    def new_thread(payload: ThreadRequest, request: Request):
        return {
            "id": database().create_thread(
                tenant(request, write=True), payload.label.strip() or "Campaign", payload.event_id
            )
        }

    @app.get(
        "/api/negotiations/{thread}", response_model=ThreadSnapshot, response_model_exclude_unset=True
    )
    def read_thread(
        thread: UUID, request: Request, response: Response, history: Literal["paged"] | None = None
    ):
        etag, snapshot = database().reads.snapshot_result(
            tenant(request), thread, request.headers.get("If-None-Match"), paged=history == "paged"
        )
        if snapshot is None:
            return Response(status_code=304, headers={"ETag": etag})
        if history is None:
            # Previously deployed browsers only accept the original draft status enum.
            for draft in snapshot["drafts"]:
                if draft["status"] == "DELIVERY_FAILED":
                    draft["status"] = "REJECTED"
        response.headers["ETag"] = etag
        return snapshot

    @app.get(
        "/api/negotiations/{thread}/history",
        response_model=HistoryPage,
        response_model_exclude_unset=True,
    )
    def history(thread: UUID, request: Request, before: Annotated[str, Query(max_length=58)]):
        workspace = tenant(request)
        try:
            cursor = HistoryCursor.parse(before)
        except ValueError:
            raise HTTPException(422, "Invalid history cursor.") from None
        return database().reads.history_page(workspace, thread, cursor)

    @app.get("/api/negotiations/{thread}/timeline", response_model=TimelinePage)
    def timeline(thread: UUID, request: Request, before: int | None = None):
        if before is not None and not 0 < before <= 9_223_372_036_854_775_807:
            raise HTTPException(422, "Invalid timeline cursor.")
        return database().reads.timeline_page(tenant(request), thread, before)

    @app.post("/api/negotiations/{thread}/inbound", status_code=202)
    def inbound(thread: UUID, payload: Inbound, request: Request):
        if creator_member(request):
            raise HTTPException(403, "Creator mail enters through authenticated ingestion.")
        return database().ingest(
            tenant(request), thread, payload.event_id, payload.body, quota=12 if mode == "live" else 80
        )

    @app.post("/api/negotiations/{thread}/drafts/{draft_id}", status_code=202)
    def review(thread: UUID, draft_id: UUID, payload: Review, request: Request):
        member = creator_member(request, write=True)
        identifier = database().review(
            member.tenant_id if member else tenant(request),
            thread,
            draft_id,
            payload.version,
            payload.action,
            payload.proposal,
            account_id=member.account_id if member else None,
            presented_digest=payload.mail_digest,
        )
        return {"id": identifier}

    @app.post("/api/negotiations/{thread}/transition", status_code=202)
    def transition(thread: UUID, payload: Transition, request: Request):
        database().transition(tenant(request, write=True), thread, payload.revision, payload.action)
        return {"status": "ok"}

    @app.post("/api/negotiations/{thread}/recovery", status_code=202)
    def recover(thread: UUID, payload: Recovery, request: Request):
        try:
            identifier = UUID(payload.job_id)
        except ValueError as exc:
            raise HTTPException(422, "Invalid job ID.") from exc
        database().recover_turn(
            tenant(request, write=True), thread, payload.revision, identifier, payload.action
        )
        return {"status": "ok"}

    @app.post("/api/negotiations/{thread}/failure", status_code=202)
    def failure(thread: UUID, payload: Fault, request: Request):
        if not capabilities["simulated_failures"] or creator_member(request):
            raise HTTPException(403, "Fault injection is disabled.")
        database().arm_fault(tenant(request), thread, payload.revision, payload.boundary)
        return {"status": "armed"}

    @app.post("/api/evaluations", status_code=202)
    def start_evaluation(request: Request):
        if not capabilities["evaluations"] or creator_member(request):
            raise Limit("Live evaluation runs are disabled for this environment.")
        return {"id": queue_run(database(), tenant(request), mode)}

    @app.get("/api/evaluations/{identifier}")
    def evaluation(identifier: UUID, request: Request):
        return get_run(database(), tenant(request), identifier)

    @app.post("/integrations/mail/inbound", status_code=202)
    async def inbound_mail(request: Request):
        if not inbound_gateway:
            raise HTTPException(404, "Mail integration is disabled.")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_INBOUND_BYTES:
                raise HTTPException(413, "Message exceeds the ingress limit.")
            raw.extend(chunk)
        try:
            payload = inbound_gateway.verify(
                bytes(raw),
                request.headers.get("X-Mail-Timestamp", ""),
                request.headers.get("X-Mail-Signature", ""),
            )
        except MailAuthenticationError as exc:
            raise HTTPException(401, "Mail event could not be authenticated.") from exc
        from starlette.concurrency import run_in_threadpool

        try:
            return await run_in_threadpool(MailStore(database()).ingest, payload)
        except CreatorMailBackpressure as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "3600"}) from exc

    @app.get("/api/operations")
    def operations(request: Request, response: Response):
        provided = request.headers.get("X-Operations-Token", "").encode()
        if len(operational_token) < 32 or not secrets.compare_digest(
            provided, operational_token.encode()
        ):
            raise HTTPException(403, "Service access is required.")
        db = database()
        status = operations_status(db, require_evaluation=capabilities["evaluations"])
        if db.pool is not None:
            stats = db.pool.get_stats()
            status["api_connection_pool"] = {
                key: stats.get(key, 0)
                for key in ("pool_size", "pool_available", "requests_waiting", "requests_errors")
            }
        if status["status"] != "ok":
            response.status_code = 503
        return status

    return app


app = create_app()
