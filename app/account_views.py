import json
import re
import secrets
import smtplib
import time
from email.message import EmailMessage
from typing import Annotated
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .accounts import ACCOUNT_AGE, account_required, active_account
from .diagnostics import record_failure
from .mail import smtp_connection
from .orders import OrderError
from .payment_documents import EMAIL
from .session_security import SESSION_TOKEN, check_csrf, csrf, digest
from .social_login import PROVIDERS, authorize_url, identity_subject, verify_max


class MaxLaunch(BaseModel):
    init_data: str = Field(min_length=10, max_length=8192)


def account_routes(accounts, templates, viewer, claim_guests, default_catalog):
    router = APIRouter(prefix="/account")
    settings, store = accounts.settings, accounts.store
    local = settings.accounts_enabled and settings.access == "local"
    max_enabled = bool(local and settings.max_bot_token and settings.secure)

    def require_local():
        if not local:
            raise OrderError(404, "login_unavailable", "Этот способ входа здесь недоступен.")

    def browser_secret(request, browser):
        current = request.state.account
        return browser + ":" + (current["id"] if current else "guest")

    def from_browser(request, token):
        require_local()
        browser = request.cookies.get(accounts.browser_cookie, "")
        if not SESSION_TOKEN.fullmatch(browser):
            raise OrderError(403, "invalid_origin", "Откройте страницу входа заново.")
        check_csrf(request, settings.origin, browser_secret(request, browser), token)
        return browser

    def sign_in(token):
        response = RedirectResponse("/account", status_code=303)
        response.set_cookie(accounts.cookie, token, max_age=ACCOUNT_AGE, httponly=True, secure=settings.secure, samesite="lax")
        response.delete_cookie(accounts.legacy_seller_cookie, httponly=True, secure=settings.secure, samesite="strict")
        return response

    def page(request, *, notice="", error="", status=200, history_page=1):
        if not local and settings.access != "private":
            require_local()
        owner, token = viewer(request)
        current = request.state.account
        browser = request.cookies.get(accounts.browser_cookie, "")
        if not SESSION_TOKEN.fullmatch(browser):
            browser = secrets.token_urlsafe(32)
        identities, purchases, stores = [], [], []
        if owner:
            with store.read() as db:
                purchases = db.execute(
                    """SELECT o.id, o.merchant, o.state,
                       COALESCE(m.name, CASE WHEN o.merchant = ? THEN ? ELSE o.merchant END) AS name
                       FROM shopper_owners s JOIN orders o ON o.session_id = s.session_id
                       LEFT JOIN merchants m ON o.merchant = m.slug
                       WHERE s.owner_id = ? ORDER BY o.rowid DESC LIMIT 31 OFFSET ?""",
                    (default_catalog.merchant_id, default_catalog.title, owner, (history_page - 1) * 30),
                ).fetchall()
                if current:
                    identities = [PROVIDERS[r[0]] for r in db.execute(
                        "SELECT DISTINCT provider FROM account_identities WHERE account_id = ? ORDER BY provider",
                        (current["account_id"],),
                    )]
                    stores = db.execute(
                        """SELECT m.slug, m.name FROM account_merchants a JOIN merchants m ON m.slug = a.merchant
                           WHERE a.account_id = ? ORDER BY m.name, m.slug""", (current["account_id"],),
                    ).fetchall()
        response = templates.TemplateResponse(request, "account.html", {
            "browser_csrf": csrf(browser_secret(request, browser)), "session_csrf": csrf(token) if owner else "",
            "logged_in": bool(owner), "local": local, "username": current["username"] if current else None,
            "methods": identities, "purchases": purchases[:30], "stores": stores,
            "history_page": history_page, "history_more": len(purchases) > 30,
            "providers": {name: PROVIDERS[name] for name in settings.social_clients} if local else {},
            "email_ready": local and settings.mail_ready, "max_enabled": max_enabled,
            "notice": notice, "error": error,
        }, status_code=status)
        if local:
            response.set_cookie(accounts.browser_cookie, browser, max_age=1800, httponly=True, secure=settings.secure, samesite="lax")
        return response

    @router.get("")
    def home(request: Request, history_page: Annotated[int, Query(alias="page", ge=1, le=10_000_000)] = 1):
        return page(request,
                    notice="Гостевые заказы на этом устройстве проверены." if request.query_params.get("claimed") == "1" else "",
                    error="Вход не завершён. Запустите его заново." if request.query_params.get("login") == "interrupted" else "",
                    history_page=history_page)

    @router.post("/password/{action}")
    def password_login(
        request: Request, action: str, token: Annotated[str, Form(max_length=128)],
        username: Annotated[str, Form(max_length=40)], password: Annotated[str, Form(max_length=128)],
    ):
        from_browser(request, token)
        if action not in {"register", "login", "add"}:
            raise OrderError(404, "login_unavailable", "Способ входа недоступен.")
        if (action == "register" and request.state.account) or (action == "add" and not request.state.account):
            raise OrderError(409, "login_context_changed", "Проверьте аккаунт в открытой вкладке.")
        username = username.strip().lower()
        try:
            accounts.rate(request, "password:" + username)
            return sign_in(accounts.password(request, username, password, register=action != "login"))
        except OrderError as error:
            return page(request, error=error.body["message"], status=error.status)

    @router.post("/oauth/{provider}")
    def start_social(request: Request, provider: str, token: Annotated[str, Form(max_length=128)]):
        browser = from_browser(request, token)
        if provider not in settings.social_clients:
            raise OrderError(404, "login_unavailable", "Этот провайдер входа ещё не подключён.")
        accounts.rate(request)
        verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        state = accounts.flow(request, provider, browser, json.dumps([verifier, nonce]))
        url = authorize_url(provider, settings.origin, settings.social_clients[provider][0], state, verifier, nonce)
        if request.headers.get("Accept") == "application/json":
            return JSONResponse({"url": url})
        return RedirectResponse(url, status_code=303)

    @router.get("/oauth/{provider}/callback")
    def finish_social(request: Request, provider: str):
        require_local()
        if provider not in settings.social_clients:
            raise OrderError(404, "login_unavailable", "Этот провайдер входа ещё не подключён.")
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not code or len(code) > 4096:
            return RedirectResponse("/account?login=interrupted", status_code=303)
        try:
            flow = accounts.consume(request, provider, state)
            verifier, nonce = json.loads(flow["secret"])
            subject = identity_subject(provider, settings.origin, settings.social_clients[provider], code, verifier, nonce,
                                       state, request.query_params.get("device_id", ""))
            return sign_in(accounts.identity(request, provider, subject, flow["session_hash"], flow["legacy_merchant"]))
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError, KeyError, OrderError):
            return RedirectResponse("/account?login=interrupted", status_code=303)

    @router.post("/email")
    def start_email(request: Request, token: Annotated[str, Form(max_length=128)], email: Annotated[str, Form(max_length=254)]):
        browser = from_browser(request, token)
        if not settings.mail_ready:
            raise OrderError(404, "login_unavailable", "Вход по почте ещё не настроен.")
        email = email.strip()
        if not EMAIL.fullmatch(email) or len(email.split("@", 1)[0]) > 64:
            return page(request, error="Проверьте адрес почты.", status=400)
        name, domain = email.rsplit("@", 1)
        entered_address = f"{name}@{domain.lower()}"
        recipient = entered_address.casefold()
        accounts.rate(request, "email:" + digest(recipient))
        if entered_address != recipient:
            with store.read() as db:
                legacy = db.execute(
                    "SELECT 1 FROM account_identities WHERE provider = 'email' AND subject = ?", (digest(entered_address),),
                ).fetchone()
            if legacy:
                recipient = entered_address
        state = accounts.flow(request, "email", browser, "", digest(recipient))
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = settings.mail_from, recipient, "Вход в «Заказ из переписки»"
        url = settings.origin + "/account/email/confirm?" + urlencode({"state": state})
        message.set_content(f"Откройте ссылку в браузере, где запросили вход. Она действует десять минут.\n\n{url}")
        try:
            with smtp_connection(settings) as smtp:
                smtp.send_message(message)
        except (OSError, ValueError, smtplib.SMTPException) as error:
            record_failure("", "smtp", "login_mail", "mail_failed", error)
            return page(request, error="Почтовый сервер не подтвердил отправку.", status=503)
        return page(request, notice="Ссылка отправлена. Откройте её в этом браузере в течение десяти минут.")

    @router.get("/email/confirm")
    def finish_email(request: Request, state: str = ""):
        require_local()
        if not settings.mail_ready:
            raise OrderError(404, "login_unavailable", "Вход по почте недоступен.")
        try:
            flow = accounts.consume(request, "email", state, require_subject=True)
            return sign_in(accounts.identity(request, "email", flow["verified_subject"],
                                             flow["session_hash"], flow["legacy_merchant"]))
        except OrderError:
            return RedirectResponse("/account?login=interrupted", status_code=303)

    @router.post("/max")
    def start_max(request: Request, token: Annotated[str, Form(max_length=128)]):
        browser = from_browser(request, token)
        if not max_enabled:
            raise OrderError(404, "login_unavailable", "Вход через MAX ещё не настроен.")
        accounts.rate(request)
        state = accounts.flow(request, "max", browser, "")
        return templates.TemplateResponse(request, "account_max.html", {
            "state": state, "token": csrf(browser_secret(request, browser)),
            "url": f"https://max.ru/{settings.max_bot_name}?startapp={state}",
        })

    @router.get("/max/miniapp")
    def max_miniapp(request: Request):
        if not max_enabled:
            raise OrderError(404, "login_unavailable", "Вход через MAX недоступен.")
        return templates.TemplateResponse(request, "account_max.html", {"miniapp": True})

    @router.post("/max/miniapp")
    def confirm_max_identity(request: Request, body: MaxLaunch):
        if not max_enabled or request.headers.get("Origin") != settings.origin:
            raise OrderError(403, "invalid_origin", "Откройте мини-приложение MAX из ссылки входа.")
        accounts.rate(request)
        try:
            state, subject = verify_max(body.init_data, settings.max_bot_token)
        except (KeyError, ValueError, TypeError) as error:
            raise OrderError(401, "login_invalid", "MAX не подтвердил этот вход.") from error
        if not SESSION_TOKEN.fullmatch(state):
            raise OrderError(400, "login_expired", "Откройте новую ссылку входа.")
        code = f"{secrets.randbelow(100_000_000):08d}"
        with store.transaction() as db:
            if not db.execute(
                """UPDATE account_flows SET verified_subject = ?, secret = ? WHERE id = ? AND provider = 'max'
                   AND verified_subject IS NULL AND expires_at > ?""",
                (subject, digest(state + ":" + code), digest(state), int(time.time())),
            ).rowcount:
                raise OrderError(400, "login_expired", "Откройте новую ссылку входа.")
        return {"status": "confirmed", "code": code}

    @router.post("/max/finish")
    def finish_max(
        request: Request, token: Annotated[str, Form(max_length=128)], state: Annotated[str, Form(max_length=128)],
        code: Annotated[str, Form(max_length=8)] = "",
    ):
        browser = from_browser(request, token)
        if not max_enabled:
            raise OrderError(404, "login_unavailable", "Вход через MAX недоступен.")
        try:
            accounts.rate(request, "max-finish:" + state)
            if not re.fullmatch(r"[0-9]{8}", code):
                raise OrderError(400, "login_incomplete", "Введите восемь цифр из мини-приложения MAX.")
            flow = accounts.consume(request, "max", state, require_subject=True, proof=digest(state + ":" + code))
            return sign_in(accounts.identity(request, "max", flow["verified_subject"],
                                             flow["session_hash"], flow["legacy_merchant"]))
        except OrderError as error:
            if not SESSION_TOKEN.fullmatch(state):
                return RedirectResponse("/account?login=interrupted", status_code=303)
            return templates.TemplateResponse(request, "account_max.html", {
                "state": state, "token": csrf(browser_secret(request, browser)), "error": error.body["message"],
                "url": f"https://max.ru/{settings.max_bot_name}?startapp={state}",
            }, status_code=error.status)

    def require_account(request, token):
        require_local()
        current = request.state.account
        if not current:
            raise account_required()
        check_csrf(request, settings.origin, request.state.account_token, token)
        return current

    @router.post("/claim")
    def claim(request: Request, token: Annotated[str, Form(max_length=128)]):
        current = require_account(request, token)
        with store.transaction() as db:
            if not active_account(db, current["id"]):
                raise account_required()
            claim_guests(db, request, "local:" + current["account_id"])
        return RedirectResponse("/account?claimed=1", status_code=303)

    @router.post("/merchant")
    def choose_merchant(request: Request, token: Annotated[str, Form(max_length=128)], slug: Annotated[str, Form(max_length=32)]):
        current = require_account(request, token)
        with store.transaction() as db:
            if not db.execute(
                """UPDATE account_sessions SET merchant = ? WHERE id = ? AND expires_at > ? AND EXISTS(
                   SELECT 1 FROM account_merchants WHERE account_id = account_sessions.account_id AND merchant = ?)""",
                (slug, current["id"], int(time.time()), slug),
            ).rowcount:
                raise OrderError(404, "merchant_not_assigned", "Магазин недоступен этому аккаунту.")
        return RedirectResponse("/merchant", status_code=303)

    @router.post("/logout")
    def logout(request: Request, token: Annotated[str, Form(max_length=128)], everywhere: Annotated[bool, Form()] = False):
        current = require_account(request, token)
        with store.transaction() as db:
            if everywhere:
                db.execute("DELETE FROM account_sessions WHERE account_id = ?", (current["account_id"],))
            else:
                db.execute("DELETE FROM account_sessions WHERE id = ?", (current["id"],))
            db.execute("DELETE FROM merchant_sessions WHERE id = ?", (digest(request.cookies.get(accounts.legacy_seller_cookie, "")),))
        response = RedirectResponse("/account", status_code=303)
        for cookie in (accounts.cookie, accounts.browser_cookie, accounts.legacy_seller_cookie):
            response.delete_cookie(cookie, httponly=True, secure=settings.secure, samesite="lax")
        return response

    return router
