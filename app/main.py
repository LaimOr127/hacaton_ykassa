import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import DemoAgent, ModelAgent
from .catalog import COFFEE, Catalog
from .corporate_identity import CorporateIdentity
from .gateway import Simulator, YooKassa
from .merchant_views import merchant_routes
from .merchants import Merchants
from .orders import OrderError, Orders
from .payments import Payments
from .session_security import SESSION_TOKEN, check_csrf, csrf, digest
from .settings import Settings
from .storage import Store, StoreBusy

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


class PaymentConfirm(Confirm):
    kind: Literal["payment", "invoice"] = "payment"


def create_app(settings: Settings | None = None, *, catalog: Catalog = COFFEE, agent=None, gateway=None):
    settings = settings or Settings.from_env()
    identity = CorporateIdentity(settings) if settings.access == "private" else None
    store = Store(settings.db_path, settings.mode)
    merchants = Merchants(store, settings, catalog)
    agent = agent if agent is not None else (
        DemoAgent() if settings.mode == "simulated" else ModelAgent(settings, merchants.model_connection)
    )
    if gateway is None and settings.mode == "simulated":
        gateway = Simulator(store)

    def gateway_for(slug, db=None):
        if gateway is not None:
            return gateway
        token = merchants.token(slug, db)
        return YooKassa(token) if token else None

    orders = Orders(store, merchants.catalog, agent, settings)
    payments = Payments(store, merchants.catalog, gateway_for)

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
    app.include_router(merchant_routes(merchants, TEMPLATES, payments), include_in_schema=False)

    @app.middleware("http")
    async def security_headers(request, call_next):
        if identity is not None and request.url.path != "/healthz":
            authorization = request.headers.get("Authorization", "")
            scheme, _, token = authorization.partition(" ")
            subject = await run_in_threadpool(identity.verify, token) if scheme.lower() == "bearer" else None
            if subject is None:
                return JSONResponse(
                    {"code": "corporate_sign_in_required", "message": "Войдите через корпоративный аккаунт."},
                    status_code=401, headers={"Cache-Control": "no-store"},
                )
            request.state.corporate_subject = subject
            request.state.corporate_token = token
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isascii() or not length.isdecimal():
                return JSONResponse({"code": "length_required", "message": "Укажите размер запроса."}, status_code=411)
            limit = 80_000 if request.url.path == "/merchant/catalog" else 20_000
            if len(length) > 5 or int(length) > limit:
                return JSONResponse({"code": "request_too_large", "message": "Запрос слишком длинный."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        form_action = " https://openrouter.ai" if settings.openrouter_oauth and request.url.path.startswith("/merchant") else ""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            f"base-uri 'none'; frame-ancestors 'none'; form-action 'self'{form_action}"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer" if request.url.path.startswith("/merchant/model/openrouter") else "same-origin"
        return response

    @app.exception_handler(OrderError)
    async def domain_error(_request, error):
        return JSONResponse(error.body, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"code": "invalid_request", "message": "Проверьте данные запроса."}, status_code=400)

    @app.exception_handler(StoreBusy)
    async def write_overloaded(_request, _error):
        return JSONResponse(
            {"code": "storage_busy", "message": "Сервис занят. Повторите действие через несколько секунд."},
            status_code=503, headers={"Retry-After": "2"},
        )

    def session_id(request, token, slug):
        if identity is not None:
            return digest(f"{len(request.state.corporate_subject)}:{request.state.corporate_subject}:{slug}:{token}")
        return digest(token if slug == catalog.merchant_id else f"{slug}:{token}")

    def customer_cookie(slug):
        return settings.cookie if slug == catalog.merchant_id else f"{settings.cookie}-{slug}"

    def session(request, *, write=False, slug=None):
        slug = slug or request.headers.get("X-Shop", catalog.merchant_id)
        if len(slug) > 32:
            raise OrderError(401, "session_required", "Откройте страницу магазина заново.")
        token = request.cookies.get(customer_cookie(slug), "")
        if not SESSION_TOKEN.fullmatch(token):
            raise OrderError(401, "session_required", "Откройте страницу магазина заново.")
        with store.read() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND merchant = ? AND expires_at > ?",
                (session_id(request, token, slug), slug, int(time.time())),
            ).fetchone()
        if row is None:
            raise OrderError(401, "session_required", "Откройте страницу магазина заново.")
        if write:
            check_csrf(request, settings.origin, token, request.headers.get("X-CSRF-Token", ""))
        return row

    @app.get("/healthz")
    def health():
        with store.read() as db:
            db.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.get("/")
    def default_storefront(request: Request):
        return storefront(request, catalog.merchant_id)

    @app.get("/s/{slug}")
    def storefront(request: Request, slug: str):
        with store.read() as db:
            view_catalog = merchants.catalog(db, slug)
            if view_catalog is None:
                raise OrderError(404, "shop_not_found", "Магазин ещё не опубликовал витрину.")
        cookie_name = customer_cookie(slug)
        token = request.cookies.get(cookie_name, "")
        try:
            row = session(request, slug=slug)
        except OrderError:
            token = secrets.token_urlsafe(32)
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?)",
                    (session_id(request, token, slug), slug, int(time.time()) + STALE_AFTER),
                )
            current = None
            transcript = []
            new_session = True
        else:
            with store.read() as db:
                latest = db.execute(
                    "SELECT id FROM orders WHERE session_id = ? AND merchant = ? ORDER BY rowid DESC LIMIT 1",
                    (row["id"], slug),
                ).fetchone()
            current = orders.get(row, latest["id"]) if latest else None
            transcript = orders.transcript(row, latest["id"]) if latest else []
            new_session = False
        response = TEMPLATES.TemplateResponse(
            request,
            "storefront.html",
            {
                "title": view_catalog.title, "csrf": csrf(token), "mode": settings.mode, "slug": slug,
                "invoices": settings.mode == "live" and settings.mcp_invoices and bool(merchants.token(slug)),
                "coffee_example": slug == "coffee" and {"breakfast", "vegetarian"} <= view_catalog.products.keys(),
                "cleaning_example": {item.basis for item in view_catalog.products.values()} >= {"area", "fixed"}
                and {"cleaning", "oven"} <= view_catalog.products.keys(),
                "order": current, "transcript": transcript,
            },
        )
        if new_session:
            response.set_cookie(
                cookie_name, token, max_age=STALE_AFTER, httponly=True, secure=settings.secure, samesite="lax",
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
    def confirm(request: Request, order_id: str, body: PaymentConfirm, key: Annotated[UUID, Header(alias="Idempotency-Key")]):
        attempt, created = payments.create(
            session(request, write=True), order_id, str(key), body.order_version, body.kind,
            allow_invoice=settings.mode == "live" and settings.mcp_invoices,
        )
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
