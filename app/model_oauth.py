import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from .session_security import SESSION_TOKEN, digest

CALLBACK_PATH = "/merchant/model/openrouter/callback"


def start_authorization(merchants, slug, owner):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    now = int(time.time())
    with merchants.store.transaction() as db:
        db.execute("DELETE FROM model_authorizations WHERE expires_at <= ?", (now,))
        db.execute(
            """INSERT INTO model_authorizations VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(merchant, owner_hash) DO UPDATE SET
               state_hash=excluded.state_hash, verifier=excluded.verifier,
               revision=excluded.revision, expires_at=excluded.expires_at""",
            (digest(state), slug, owner, verifier, merchants.model_revision(db, slug), now + 600),
        )
    return "https://openrouter.ai/auth?" + urlencode({
        "callback_url": merchants.settings.origin + CALLBACK_PATH,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "key_label": f"Заказ из переписки · {slug}", "limit": "1.00", "expiration": "7-days",
    })


def finish_authorization(merchants, slug, owner, state, code):
    if not SESSION_TOKEN.fullmatch(state) or not code or len(code) > 2048:
        raise ValueError("Подключение прервано. Начните вход заново.")
    with merchants.store.transaction() as db:
        attempt = db.execute(
            """DELETE FROM model_authorizations WHERE state_hash = ? AND merchant = ?
               AND owner_hash = ? AND expires_at > ? RETURNING verifier, revision""",
            (digest(state), slug, owner, int(time.time())),
        ).fetchone()
        revision = merchants.model_revision(db, slug)
    if not attempt or revision != attempt["revision"]:
        raise ValueError("Подключение устарело. Начните вход заново.")
    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/auth/keys",
            json={"code": code, "code_verifier": attempt["verifier"], "code_challenge_method": "S256"},
            timeout=8, follow_redirects=False,
        )
        response.raise_for_status()
        key = response.json()["key"]
        if not isinstance(key, str):
            raise ValueError()
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        raise ValueError("OpenRouter не подтвердил вход. Попробуйте подключиться заново.") from error
    return merchants.connect_model(
        slug, "openrouter", merchants.settings.openrouter_model, key, expected_revision=attempt["revision"],
    )
