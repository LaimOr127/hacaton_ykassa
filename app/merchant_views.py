import csv
import hmac
import json
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .accounts import account_required, active_account
from .checkout import checkout_flags
from .gateway import YOO_TOOLS, payment_reference
from .merchants import CLEANING_CSV, export_catalog
from .model_oauth import finish_authorization, start_authorization
from .orders import OrderError, public_order, reopen_draft, seller_transcript
from .payment_documents import PDF_LIMIT, documents_for
from .session_security import SESSION_TOKEN, check_csrf, csrf, digest


def merchant_routes(merchants, templates, payments, accounts):
    router = APIRouter(prefix="/merchant")
    store, settings = merchants.store, merchants.settings
    prefix = "__Host-" if settings.secure else ""
    entry_cookie = prefix + "merchant_entry"
    session_cookie = prefix + "merchant_session"
    age = 8 * 3600
    private = settings.access == "private"

    def assignment(request):
        slug = settings.oidc_merchants.get(request.state.corporate_subject)
        if not slug:
            raise OrderError(403, "merchant_not_assigned", "Администратор ещё не назначил вам магазин.")
        return slug, request.state.corporate_token

    def registered(slug):
        with store.read() as db:
            return db.execute("SELECT 1 FROM merchants WHERE slug = ?", (slug,)).fetchone() is not None

    def manager(request):
        if private:
            slug, token = assignment(request)
            if not registered(slug):
                raise OrderError(401, "merchant_setup_required", "Сначала создайте назначенную витрину.")
            return slug, token
        current = request.state.account
        if current:
            with store.read() as db:
                active = active_account(db, current["id"])
                if not active:
                    raise account_required()
                linked = db.execute(
                    """SELECT merchant FROM account_merchants WHERE account_id = ?
                       ORDER BY (merchant = ?) DESC, merchant LIMIT 1""", (active["account_id"], active["merchant"]),
                ).fetchone()
            if not linked:
                raise OrderError(401, "merchant_setup_required", "Подтвердите доступ кодом магазина.")
            slug = linked["merchant"]
            return slug, f"seller:{slug}:{request.state.account_token}"
        token = request.cookies.get(session_cookie, "")
        if not SESSION_TOKEN.fullmatch(token):
            raise OrderError(401, "merchant_session_required", "Войдите в кабинет продавца.")
        with store.read() as db:
            row = db.execute(
                "SELECT merchant FROM merchant_sessions WHERE id = ? AND expires_at > ?",
                (digest(token), int(time.time())),
            ).fetchone()
        if not row:
            raise OrderError(401, "merchant_session_required", "Сессия истекла. Войдите снова.")
        return row["merchant"], token

    def entrance(request, message="", status=200):
        if private:
            slug, token = assignment(request)
            return templates.TemplateResponse(request, "merchant_login.html", {
                "private": True, "assigned_slug": slug, "csrf": csrf(token), "error": message,
            }, status_code=status)
        token = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(request, "merchant_login.html", {
            "csrf": csrf(token), "enable_setup": bool(settings.setup_key), "error": message,
            "accounts_enabled": settings.accounts_enabled, "has_account": bool(request.state.account),
        }, status_code=status)
        response.set_cookie(entry_cookie, token, max_age=900, httponly=True, secure=settings.secure, samesite="strict")
        return response

    def begin(request, submitted):
        token = request.cookies.get(entry_cookie, "")
        if not SESSION_TOKEN.fullmatch(token):
            raise OrderError(403, "invalid_origin", "Откройте форму входа заново.")
        check_csrf(request, settings.origin, token, submitted)
        address = request.client.host if request.client else "unknown"
        if not merchants.limit_auth(address):
            raise OrderError(429, "rate_limited", "Слишком много попыток. Попробуйте через несколько минут.")

    def make_session(request, slug):
        token = secrets.token_urlsafe(32)
        old = request.cookies.get(session_cookie, "")
        current = request.state.account
        with store.transaction() as db:
            now = int(time.time())
            db.execute("DELETE FROM merchant_sessions WHERE id = ? OR expires_at <= ?", (digest(old), now))
            if current:
                active = active_account(db, current["id"])
                if not active:
                    raise account_required()
                db.execute("INSERT OR IGNORE INTO account_merchants VALUES (?, ?)", (active["account_id"], slug))
                db.execute("UPDATE account_sessions SET merchant = ? WHERE id = ?", (slug, active["id"]))
            else:
                db.execute("INSERT INTO merchant_sessions VALUES (?, ?, ?)", (digest(token), slug, now + age))
        response = RedirectResponse("/merchant", status_code=303)
        response.delete_cookie(entry_cookie, httponly=True, secure=settings.secure, samesite="strict")
        if current:
            response.delete_cookie(session_cookie, httponly=True, secure=settings.secure, samesite="strict")
        else:
            response.set_cookie(session_cookie, token, max_age=age, httponly=True, secure=settings.secure, samesite="strict")
        return response

    def dashboard(
        request, slug, token, *, error="", status=200, notice="", draft_csv=None, draft_deposit=None,
        page=1, account_models=None, mcp_tools=None,
    ):
        with store.read() as db:
            owner = db.execute("SELECT name, catalog_version FROM merchants WHERE slug = ?", (slug,)).fetchone()
            catalog = merchants.catalog(db, slug)
            features = checkout_flags(db, slug)
            rows = db.execute(
                """SELECT o.id, o.state, o.quote, a.state AS payment_state, p.paid_parts, p.parts,
                   (r.order_id IS NOT NULL AND r.reply IS NULL) AS needs_reply, f.fulfilled_at
                   FROM orders o LEFT JOIN attempts a ON a.id = o.attempt_id
                   LEFT JOIN checkout_plans p ON p.order_id = o.id
                   LEFT JOIN seller_reviews r ON r.order_id = o.id AND r.closed_at IS NULL
                   LEFT JOIN fulfilled_orders f ON f.order_id = o.id
                   WHERE o.merchant = ? ORDER BY needs_reply DESC, o.rowid DESC LIMIT 60""",
                (slug,),
            ).fetchall()
            review = db.execute(
                """SELECT a.id, a.external_id, a.candidate_id, a.snapshot, a.kind, o.id AS order_id FROM attempts a
                   JOIN orders o ON o.attempt_id = a.id AND o.id = a.order_id
                   WHERE o.merchant = ? AND (a.state = 'unknown' OR (a.state = 'creating' AND a.created_at <= ?))
                   ORDER BY a.created_at, a.id LIMIT 21 OFFSET ?""",
                (slug, int(time.time()) - 30, (page - 1) * 20),
            ).fetchall() if settings.mode == "live" else []
        types = {"fixed": "Фиксированная", "unit": "За единицу", "area": "По площади"}
        states = {
            "draft": "Черновик", "agreed": "Готов к оплате", "payment_pending": "Ожидает оплаты",
            "paid": "Оплачен", "deposit_received": "Предоплата получена",
        }
        orders = [
            {
                "id": row["id"], "short_id": row["id"][:8],
                "state": "Нужен ответ" if row["needs_reply"] else
                "Требует сверки" if row["payment_state"] == "unknown" else
                f"Оплачено {row['paid_parts']} из {row['parts']}" if row["paid_parts"] and row["state"] == "payment_pending"
                else states[row["state"]],
                "status_class": "review" if row["payment_state"] == "unknown" else row["state"],
                "total": json.loads(row["quote"])["total_kopecks"],
                "fulfilled": row["fulfilled_at"] is not None,
            }
            for row in rows
        ]
        products = [
            {"sku": sku, "title": product.title, "basis": types[product.basis]}
            for sku, product in catalog.products.items()
        ] if catalog else []
        return templates.TemplateResponse(request, "merchant.html", {
            "slug": slug, "name": owner["name"], "csrf": csrf(token), "error": error, "notice": notice,
            "private": private, "model_connection": merchants.model_summary(slug),
            "account_link": bool(settings.accounts_enabled or private),
            "model_available": merchants.model_connection(slug) is not None,
            "openrouter_oauth": settings.openrouter_oauth,
            "openrouter_model": settings.openrouter_model,
            "account_models": account_models,
            "mcp_tools": mcp_tools,
            "default_model": settings.llm_model,
            "model_providers": {key: value[0] for key, value in merchants.model_providers().items()},
            "mode": settings.mode, "connected": bool(merchants.token(slug)), "vault_ready": merchants.vault is not None,
            "checkout_features": features, "mail_ready": settings.mail_ready,
            "catalog_ready": catalog is not None,
            "catalog_version": owner["catalog_version"],
            "deposit": draft_deposit if draft_deposit is not None else catalog.deposit_percent if catalog else 100,
            "orders": orders, "products": products,
            "catalog_csv": draft_csv if draft_csv is not None else export_catalog(catalog.products) if catalog else "",
            "paid_count": sum(row["state"] in {"paid", "deposit_received"} for row in rows),
            "review_more": len(review) > 20,
            "review_page": page,
            "reviews": [
                {
                    "id": row["id"], "order": row["order_id"][:8],
                    "reference": payment_reference(row["id"]), "known": row["external_id"] is not None, "kind": row["kind"],
                    "candidate": row["candidate_id"] is not None,
                    "amount": json.loads(row["snapshot"])["payable_kopecks"],
                }
                for row in review[:20]
            ],
        }, status_code=status)

    def merchant_order(db, slug, order_id):
        row = db.execute("SELECT * FROM orders WHERE merchant = ? AND id = ?", (slug, order_id)).fetchone()
        if row is None:
            raise OrderError(404, "order_not_found", "Заказ не найден.")
        return row

    def render_order(request, slug, token, order_id, *, refunds=None, error="", status=200):
        with store.read() as db:
            order = public_order(db, merchant_order(db, slug, order_id))
            transcript = seller_transcript(db, order_id)
            features = checkout_flags(db, slug)
            payment_list = documents_for(db, order_id) if settings.mode == "live" and (
                features["receipt_download"] or features["receipt_email"]
            ) else []
            review = db.execute(
                "SELECT opened_message FROM seller_reviews WHERE order_id = ? AND closed_at IS NULL AND reply IS NULL",
                (order_id,),
            ).fetchone()
        return templates.TemplateResponse(request, "merchant_order.html", {
            "order": order, "transcript": transcript, "csrf": csrf(token),
            "simulated": settings.mode == "simulated", "review_id": review["opened_message"] if review else None,
            "refunds": refunds, "error": error, "payment_documents": payment_list,
        }, status_code=status)

    @router.get("/orders/{order_id}")
    def order_page(request: Request, order_id: str):
        slug, token = manager(request)
        return render_order(request, slug, token, order_id)

    @router.post("/orders/{order_id}/receipts/{attempt_id}")
    def upload_receipt(
        request: Request, order_id: str, attempt_id: str, token: Annotated[str, Form(max_length=128)], file: UploadFile,
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        content = file.file.read(PDF_LIMIT + 1)
        if len(content) > PDF_LIMIT or not content.startswith(b"%PDF-") or not content.rstrip().endswith(b"%%EOF"):
            raise OrderError(400, "invalid_request", "Загрузите PDF-файл размером до 512 КБ.")
        with store.transaction() as db:
            merchant_order(db, slug, order_id)
            flags = checkout_flags(db, slug)
            if settings.mode != "live" or not (flags["receipt_download"] or flags["receipt_email"]) or not db.execute(
                "SELECT 1 FROM attempts WHERE id = ? AND order_id = ? AND state = 'succeeded'", (attempt_id, order_id),
            ).fetchone():
                raise OrderError(404, "document_not_found", "Прикрепить чек можно к подтверждённому платежу.")
            db.execute(
                """INSERT INTO payment_receipts VALUES (?, ?) ON CONFLICT(attempt_id) DO UPDATE SET pdf=excluded.pdf""",
                (attempt_id, content),
            )
        return RedirectResponse(f"/merchant/orders/{order_id}#payment-documents", status_code=303)

    @router.post("/orders/{order_id}/refunds")
    def order_refunds(
        request: Request, order_id: str, token: Annotated[str, Form(max_length=128)],
        refund_id: Annotated[str, Form(pattern=r"^[a-zA-Z0-9_-]{0,128}$")] = "",
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(404, "refund_not_found", "Проверка возвратов доступна при подключении ЮKassa.")
        try:
            refunds = payments.refunds(slug, order_id, refund_id)
        except OrderError as error:
            return render_order(request, slug, session, order_id, error=error.body["message"], status=error.status)
        return render_order(request, slug, session, order_id, refunds=refunds)

    @router.post("/orders/{order_id}/reply")
    def reply_to_order(
        request: Request, order_id: str, token: Annotated[str, Form(max_length=128)],
        reply: Annotated[str, Form(min_length=1, max_length=1000)],
        review_id: Annotated[str, Form(min_length=1, max_length=128)],
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        reply = reply.strip()
        with store.transaction() as db:
            order = merchant_order(db, slug, order_id)
            if not reply:
                raise OrderError(400, "invalid_reply", "Введите текст ответа.")
            if order["state"] not in {"draft", "agreed"}:
                raise OrderError(409, "order_locked", "Заказ уже подтверждён для оплаты.")
            if not db.execute(
                """UPDATE seller_reviews SET reply = ?, answered_at = ?
                   WHERE order_id = ? AND opened_message = ? AND reply IS NULL AND closed_at IS NULL""",
                (reply, int(time.time()), order_id, review_id),
            ).rowcount:
                raise OrderError(409, "review_unavailable", "Заказ больше не ждёт ответа.")
            reopen_draft(db, order_id)
        return RedirectResponse(f"/merchant/orders/{order_id}", status_code=303)

    @router.post("/orders/{order_id}/fulfill")
    def fulfill_order(request: Request, order_id: str, token: Annotated[str, Form(max_length=128)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        with store.transaction() as db:
            order = merchant_order(db, slug, order_id)
            if order["state"] not in {"paid", "deposit_received"}:
                raise OrderError(409, "order_not_paid", "Дождитесь оплаты или предоплаты.")
            db.execute("INSERT OR IGNORE INTO fulfilled_orders VALUES (?, ?)", (order_id, int(time.time())))
        return RedirectResponse(f"/merchant/orders/{order_id}", status_code=303)

    @router.get("/login")
    def login_page(request: Request):
        if private and registered(assignment(request)[0]):
            return RedirectResponse("/merchant", status_code=303)
        return entrance(request)

    @router.post("/login")
    def login(request: Request, token: Annotated[str, Form(max_length=128)], slug: Annotated[str, Form(max_length=32)],
              code: Annotated[str, Form(max_length=128)]):
        if private:
            raise OrderError(403, "corporate_sign_in_required", "Для этого кабинета используйте корпоративный вход.")
        begin(request, token)
        slug = slug.strip().lower()
        if not merchants.valid_code(slug, code):
            return entrance(request, "Проверьте адрес магазина и код доступа.", 401)
        return make_session(request, slug)

    @router.post("/register")
    def register(request: Request, token: Annotated[str, Form(max_length=128)], slug: Annotated[str, Form(max_length=32)],
                 name: Annotated[str, Form(max_length=80)], code: Annotated[str, Form(max_length=128)] = "",
                 setup: Annotated[str, Form(max_length=256)] = ""):
        if private:
            assigned_slug, identity_token = assignment(request)
            check_csrf(request, settings.origin, identity_token, token)
            if slug != assigned_slug:
                raise OrderError(403, "merchant_not_assigned", "Можно создать только назначенную вам витрину.")
            try:
                merchants.register(slug, name.strip(), secrets.token_urlsafe(32), corporate=True)
            except ValueError as error:
                return entrance(request, str(error), 400)
            return RedirectResponse("/merchant", status_code=303)
        begin(request, token)
        if not settings.setup_key or not hmac.compare_digest(setup.encode(), settings.setup_key.encode()):
            return entrance(request, "Проверьте код подключения.", 403)
        slug = slug.strip().lower()
        try:
            merchants.register(slug, name.strip(), code)
        except ValueError as error:
            return entrance(request, str(error), 400)
        return make_session(request, slug)

    @router.get("")
    def home(request: Request, review_page: Annotated[int, Query(ge=1, le=10_000)] = 1):
        try:
            slug, token = manager(request)
        except OrderError as error:
            if error.status == 403:
                raise
            return RedirectResponse("/merchant/login", status_code=303)
        notices = {
            "catalog": "Прайс опубликован.", "connection": "Токен сохранён.",
            "model": "Модель проверена и сохранена.", "model-off": "Модель отключена.",
            "model-account": "Аккаунт OpenRouter подключён. Модель проверена.",
            "checkout": "Настройки оплаты сохранены.",
        }
        updated = request.query_params.get("updated")
        error = "OpenRouter не подключён. Проверьте доступ к модели и повторите вход." if updated == "model-account-error" else ""
        return dashboard(request, slug, token, notice=notices.get(updated, ""), error=error, page=review_page)

    @router.post("/checkout-settings")
    def save_checkout_settings(
        request: Request, token: Annotated[str, Form(max_length=128)],
        split_payments: Annotated[bool, Form()] = False, tips: Annotated[bool, Form()] = False,
        receipt_download: Annotated[bool, Form()] = False, receipt_email: Annotated[bool, Form()] = False,
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        with store.transaction() as db:
            db.execute(
                """INSERT INTO checkout_settings VALUES (?, ?, ?, ?, ?) ON CONFLICT(merchant) DO UPDATE SET
                   split_payments=excluded.split_payments, tips=excluded.tips,
                   receipt_download=excluded.receipt_download, receipt_email=excluded.receipt_email""",
                (slug, split_payments, tips, receipt_download, receipt_email),
            )
        return RedirectResponse("/merchant?updated=checkout#checkout-settings", status_code=303)

    @router.get("/example.csv")
    def example():
        return Response(CLEANING_CSV, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=cleaning.csv"})

    @router.post("/catalog")
    def publish(request: Request, token: Annotated[str, Form(max_length=128)],
                catalog_csv: Annotated[str, Form(max_length=12_000)], deposit: Annotated[int, Form()],
                catalog_version: Annotated[int, Form(ge=0)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        try:
            updated = merchants.publish(slug, catalog_csv, deposit, catalog_version)
        except (ValueError, csv.Error) as error:
            return dashboard(
                request, slug, session, error=str(error), status=400,
                draft_csv=catalog_csv, draft_deposit=deposit,
            )
        if not updated:
            return dashboard(
                request, slug, session, error="Прайс изменился в другой вкладке. Сравните свой ввод с опубликованными позициями.",
                status=409, draft_csv=catalog_csv, draft_deposit=deposit,
            )
        return RedirectResponse("/merchant?updated=catalog", status_code=303)

    @router.post("/connection")
    def connect(request: Request, token: Annotated[str, Form(max_length=128)],
                mcp_token: Annotated[str, Form(max_length=4096)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(409, "simulated_mode", "Подключение доступно в режиме тестовой кассы.")
        try:
            merchants.connect(slug, mcp_token.strip())
        except ValueError as error:
            return dashboard(request, slug, session, error=str(error), status=400)
        return RedirectResponse("/merchant?updated=connection", status_code=303)

    @router.post("/connection/tools")
    def check_mcp_tools(request: Request, token: Annotated[str, Form(max_length=128)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        gateway = payments.gateway_for(slug) if settings.mode == "live" else None
        if gateway is None:
            raise OrderError(409, "payment_unavailable", "Сначала подключите ЮKassa.")
        try:
            available = gateway.tools()
        except Exception:
            return dashboard(request, slug, session, error="ЮKassa не вернула список инструментов.", status=503)
        return dashboard(request, slug, session, mcp_tools=[
            (label, tool in available, tool in {"create_invoice", "get_invoice"} and not settings.mcp_invoices)
            for tool, label in YOO_TOOLS.items()
        ])

    @router.post("/model")
    def connect_model(
        request: Request, token: Annotated[str, Form(max_length=128)],
        provider: Annotated[str, Form(max_length=32)], model: Annotated[str, Form(max_length=128)],
        api_key: Annotated[str, Form(max_length=4096)],
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(409, "simulated_mode", "Подключение доступно в режиме внешнего API.")
        try:
            updated = merchants.connect_model(slug, provider, model.strip(), api_key.strip())
        except ValueError as error:
            return dashboard(request, slug, session, error=str(error), status=400)
        if not updated:
            return dashboard(request, slug, session, error="Подключение модели уже изменилось. Проверьте текущие настройки.", status=409)
        return RedirectResponse("/merchant?updated=model", status_code=303)

    @router.post("/model/disconnect")
    def disconnect_model(request: Request, token: Annotated[str, Form(max_length=128)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        merchants.disconnect_model(slug)
        return RedirectResponse("/merchant?updated=model-off", status_code=303)

    def model_account(request):
        if settings.mode != "live" or not settings.openrouter_oauth or merchants.vault is None:
            raise OrderError(404, "model_account_unavailable", "Вход через OpenRouter недоступен.")
        slug, session = manager(request)
        owner = "oidc:" + request.state.corporate_subject if private else "session:" + session
        return slug, session, digest(owner)

    @router.post("/model/openrouter")
    def authorize_model_account(request: Request, token: Annotated[str, Form(max_length=128)]):
        slug, session, owner = model_account(request)
        check_csrf(request, settings.origin, session, token)
        response = RedirectResponse(start_authorization(merchants, slug, owner), status_code=303)
        if not private:
            response.set_cookie(
                session_cookie, session, max_age=age, httponly=True, secure=settings.secure, samesite="lax",
            )
        return response

    @router.get("/model/openrouter/callback")
    def connect_model_account(request: Request):
        try:
            slug, _, owner = model_account(request)
            connected = finish_authorization(
                merchants, slug, owner, request.query_params.get("state", ""), request.query_params.get("code", ""),
            )
        except (ValueError, OrderError):
            connected = False
        updated = "model-account" if connected else "model-account-error"
        return RedirectResponse("/merchant?updated=" + updated + "#model", status_code=303)

    @router.post("/model/openrouter/models")
    def account_model_options(request: Request, token: Annotated[str, Form(max_length=128)]):
        slug, session, _ = model_account(request)
        check_csrf(request, settings.origin, session, token)
        account = merchants.model_connection(slug, expected_provider="openrouter")
        if account is None:
            raise OrderError(409, "model_account_unavailable", "Сначала подключите аккаунт OpenRouter.")
        try:
            models = merchants.model_options("openrouter", account.key)
        except ValueError as error:
            return dashboard(request, slug, session, error=str(error), status=400)
        return dashboard(request, slug, session, account_models=models)

    @router.post("/model/openrouter/select")
    def select_account_model(
        request: Request, token: Annotated[str, Form(max_length=128)], model: Annotated[str, Form(max_length=128)],
    ):
        slug, session, _ = model_account(request)
        check_csrf(request, settings.origin, session, token)
        with store.read() as db:
            account = merchants.model_connection(slug, expected_provider="openrouter", db=db)
            revision = merchants.model_revision(db, slug)
        if account is None:
            raise OrderError(409, "model_account_unavailable", "Сначала подключите аккаунт OpenRouter.")
        try:
            updated = merchants.connect_model(slug, "openrouter", model.strip(), account.key, expected_revision=revision)
        except ValueError as error:
            return dashboard(request, slug, session, error=str(error), status=400)
        if not updated:
            return dashboard(request, slug, session, error="Подключение модели уже изменилось.", status=409)
        return RedirectResponse("/merchant?updated=model#model", status_code=303)

    @router.post("/model/options")
    def available_models(
        request: Request, token: Annotated[str, Form(max_length=128)],
        provider: Annotated[str, Form(max_length=32)], api_key: Annotated[str, Form(max_length=4096)],
    ):
        _, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(409, "simulated_mode", "Список моделей доступен при внешнем подключении.")
        try:
            return {"models": merchants.model_options(provider, api_key.strip())}
        except ValueError as error:
            raise OrderError(400, "model_options_unavailable", str(error)) from error

    @router.post("/logout")
    def logout(request: Request, token: Annotated[str, Form(max_length=128)]):
        _, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if private:
            return RedirectResponse("/oauth2/sign_out", status_code=303)
        if request.state.account:
            with store.transaction() as db:
                db.execute("DELETE FROM account_sessions WHERE id = ?", (request.state.account["id"],))
            response = RedirectResponse("/account", status_code=303)
            response.delete_cookie(accounts.cookie, httponly=True, secure=settings.secure, samesite="lax")
            return response
        with store.transaction() as db:
            db.execute("DELETE FROM merchant_sessions WHERE id = ?", (digest(session),))
        response = RedirectResponse("/merchant/login", status_code=303)
        response.delete_cookie(session_cookie, httponly=True, secure=settings.secure, samesite="strict")
        return response

    @router.post("/payment-review/{attempt_id}")
    def payment_review(request: Request, attempt_id: str, token: Annotated[str, Form(max_length=128)],
                       payment_id: Annotated[str, Form(max_length=128)] = "",
                       review_page: Annotated[int, Form(ge=1, le=10_000)] = 1):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(404, "payment_not_found", "Платёжная попытка не найдена.")
        try:
            payments.reconcile(slug, attempt_id, payment_id)
        except OrderError as error:
            return dashboard(request, slug, session, error=error.body["message"], status=error.status, page=review_page)
        return RedirectResponse("/merchant#orders", status_code=303)

    return router
