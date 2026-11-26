import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import DemoAgent, ModelAgent
from .catalog import COFFEE, Catalog
from .gateway import Simulator, YooKassa
from .orders import OrderError, Orders
from .payments import Payments
from .settings import Settings
from .storage import Store

HERE = Path(__file__).resolve().parent
STALE_AFTER = 7 * 24 * 60 * 60
API = "/api/v1"
TEMPLATES = Jinja2Templates(directory=HERE / "templates")


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_version: int = Field(strict=True, ge=1, le=9_007_199_254_740_991)


class Message(Confirm):
    client_message_id: UUID
    text: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf(token: str) -> str:
    return digest("csrf:" + token)


def create_app(settings: Settings | None = None, *, catalog: Catalog = COFFEE, agent=None, gateway=None):
    settings = settings or Settings.from_env()
    store = Store(settings.db_path, settings.mode)
    agent = agent if agent is not None else (DemoAgent() if settings.mode == "simulated" else ModelAgent(settings))
    gateway = gateway if gateway is not None else (
        Simulator(store) if settings.mode == "simulated" else YooKassa(settings.mcp_token)
    )
    orders = Orders(store, catalog, agent)
    payments = Payments(store, catalog, gateway)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        yield

    app = FastAPI(
        title="Заказ из переписки", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.origin).hostname, "127.0.0.1", "localhost"],
        www_redirect=False,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isascii() or not length.isdecimal():
                return JSONResponse({"code": "length_required", "message": "Укажите размер запроса."}, status_code=411)
            if len(length) > 5 or int(length) > 20_000:
                return JSONResponse({"code": "request_too_large", "message": "Запрос слишком длинный."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(OrderError)
    async def domain_error(_request, error):
        return JSONResponse(error.body, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"code": "invalid_request", "message": "Проверьте данные запроса."}, status_code=400)

    def session(request, *, write=False):
        token = request.cookies.get(settings.cookie, "")
        if len(token) != 43 or not token.isascii():
            raise OrderError(401, "session_required", "Откройте страницу магазина заново.")
        with store.read() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND merchant = ? AND expires_at > ?",
                (digest(token), catalog.merchant_id, int(time.time())),
            ).fetchone()
        if row is None:
            raise OrderError(401, "session_required", "Откройте страницу магазина заново.")
        if write:
            submitted = request.headers.get("X-CSRF-Token", "")
            if request.headers.get("Origin") != settings.origin or not hmac.compare_digest(
                submitted.encode(), csrf(token).encode()
            ):
                raise OrderError(403, "invalid_origin", "Обновите страницу и повторите действие.")
        return row

    @app.get("/healthz")
    def health():
        with store.read() as db:
            db.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.get("/")
    def storefront(request: Request):
        try:
            row = session(request)
        except OrderError:
            token = secrets.token_urlsafe(32)
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?)",
                    (digest(token), catalog.merchant_id, int(time.time()) + STALE_AFTER),
                )
            current = None
            transcript = []
            new_session = True
        else:
            token = request.cookies[settings.cookie]
            with store.read() as db:
                latest = db.execute(
                    "SELECT id FROM orders WHERE session_id = ? AND merchant = ? ORDER BY rowid DESC LIMIT 1",
                    (row["id"], catalog.merchant_id),
                ).fetchone()
            current = orders.get(row, latest["id"]) if latest else None
            transcript = orders.transcript(row, latest["id"]) if latest else []
            new_session = False
        response = TEMPLATES.TemplateResponse(
            request,
            "storefront.html",
            {
                "title": catalog.title, "csrf": csrf(token), "mode": settings.mode,
                "order": current, "transcript": transcript,
            },
        )
        if new_session:
            response.set_cookie(
                settings.cookie, token, max_age=STALE_AFTER, httponly=True, secure=settings.secure, samesite="lax",
            )
        return response

    @app.post(API + "/orders", status_code=201)
    def create_order(request: Request, key: Annotated[UUID, Header(alias="Idempotency-Key")]):
        order = orders.create(session(request, write=True), str(key))
        return JSONResponse(order, status_code=201, headers={"Location": f"{API}/orders/{order['id']}"})

    @app.get(API + "/orders/{order_id}")
    def get_order(request: Request, order_id: str):
        return orders.get(session(request), order_id)

    @app.post(API + "/orders/{order_id}/messages")
    def send_message(request: Request, order_id: str, body: Message):
        return orders.message(
            session(request, write=True), order_id, str(body.client_message_id), body.order_version, body.text,
        )

    @app.post(API + "/orders/{order_id}/payment-attempts", status_code=201)
    def confirm(request: Request, order_id: str, body: Confirm, key: Annotated[UUID, Header(alias="Idempotency-Key")]):
        attempt, created = payments.create(session(request, write=True), order_id, str(key), body.order_version)
        location = f"{API}/orders/{order_id}/payment-attempts/{attempt['id']}"
        return JSONResponse(attempt, status_code=201 if created else 200, headers={"Location": location})

    @app.get(API + "/orders/{order_id}/payment-attempts/{attempt_id}")
    def attempt_status(request: Request, order_id: str, attempt_id: str):
        return payments.get(session(request), order_id, attempt_id)

    if settings.mode == "simulated":
        @app.post("/demo/orders/{order_id}/payment-attempts/{attempt_id}/approve")
        def approve_demo(request: Request, order_id: str, attempt_id: str):
            return payments.simulate(session(request, write=True), order_id, attempt_id)

    return app
