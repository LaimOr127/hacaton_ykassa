import hashlib
import hmac
import re
import secrets
import time
from threading import BoundedSemaphore
from uuid import uuid4

from .orders import OrderError
from .session_security import SESSION_TOKEN, digest

USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,39}")
ACCOUNT_AGE = 30 * 86400
FLOW_AGE = 600
PASSWORD_SLOTS = BoundedSemaphore(4)


def password_hash(password, salt):
    if not PASSWORD_SLOTS.acquire(blocking=False):
        raise OrderError(503, "auth_busy", "Сервис входа занят. Повторите запрос.")
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024).hex()
    finally:
        PASSWORD_SLOTS.release()


def account_required():
    return OrderError(401, "account_required", "Войдите в учётную запись.")


def active_account(db, session_hash):
    return db.execute(
        """SELECT s.*, a.username FROM account_sessions s JOIN accounts a ON a.id = s.account_id
           WHERE s.id = ? AND s.expires_at > ?""", (session_hash, int(time.time())),
    ).fetchone()


def shopper_id(owner, slug):
    return digest(f"account-shopper:{owner}:{slug}")


def require_recent(row):
    if not row or row["expires_at"] - ACCOUNT_AGE < int(time.time()) - 900:
        raise OrderError(401, "reauth_required", "Чтобы подключить способ входа, выйдите и снова войдите в аккаунт.")


class Accounts:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        prefix = "__Host-" if settings.secure else ""
        self.cookie = prefix + "account_session"
        self.browser_cookie = prefix + "account_browser"
        self.legacy_seller_cookie = prefix + "merchant_session"

    def session(self, token):
        if SESSION_TOKEN.fullmatch(token):
            with self.store.read() as db:
                return active_account(db, digest(token))
        return None

    def rate(self, request, identifier=""):
        now = int(time.time())
        address = request.client.host if request.client else "unknown"
        keys = [("account-ip:" + digest(address), 20)]
        if identifier:
            keys.append(("account-target:" + digest(identifier), 6))
        with self.store.transaction() as db:
            db.execute("DELETE FROM account_auth_attempts WHERE attempted_at <= ?", (now - 300,))
            for key, limit in keys:
                count = db.execute("SELECT COUNT(*) FROM account_auth_attempts WHERE address = ?", (key,)).fetchone()[0]
                if count >= limit:
                    raise OrderError(429, "rate_limited", "Слишком много попыток входа. Попробуйте через несколько минут.")
            db.executemany("INSERT INTO account_auth_attempts VALUES (?, ?)", [(key, now) for key, _ in keys])

    def new_session(self, db, request, account_id, prior_merchant=""):
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM account_sessions WHERE id = ? OR expires_at <= ?",
                   (digest(request.cookies.get(self.cookie, "")), int(time.time())))
        db.execute("DELETE FROM merchant_sessions WHERE id IN (?, ?)",
                   (digest(request.cookies.get(self.legacy_seller_cookie, "")), prior_merchant))
        db.execute("INSERT INTO account_sessions VALUES (?, ?, ?, NULL)", (digest(token), account_id, int(time.time()) + ACCOUNT_AGE))
        return token

    def password(self, request, username, password, *, register=False):
        if not USERNAME.fullmatch(username) or not 12 <= len(password) <= 128:
            raise OrderError(400 if register else 401, "invalid_login", "Проверьте логин и пароль.")
        if register:
            salt = secrets.token_bytes(16)
            encoded = salt.hex() + ":" + password_hash(password, salt)
            with self.store.transaction() as db:
                current = request.state.account
                if current:
                    row = active_account(db, current["id"])
                    require_recent(row)
                    changed = db.execute(
                        "UPDATE OR IGNORE accounts SET username = ?, password_hash = ? WHERE id = ? AND username IS NULL",
                        (username, encoded, row["account_id"]),
                    ).rowcount
                    if changed:
                        return self.new_session(db, request, row["account_id"])
                else:
                    account_id = uuid4().hex
                    if db.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?, ?)", (account_id, username, encoded)).rowcount:
                        return self.new_session(db, request, account_id)
            raise OrderError(409, "login_unavailable", "Логин недоступен или уже установлен для этого аккаунта.")
        with self.store.read() as db:
            row = db.execute("SELECT id, password_hash FROM accounts WHERE username = ?", (username,)).fetchone()
        salt, expected = row["password_hash"].split(":") if row else ("00" * 16, "00" * 64)
        actual = password_hash(password, bytes.fromhex(salt))
        if row is None or not hmac.compare_digest(expected, actual):
            raise OrderError(401, "invalid_login", "Проверьте логин и пароль.")
        with self.store.transaction() as db:
            return self.new_session(db, request, row["id"])

    def identity(self, request, provider, subject, session_hash, prior_merchant):
        with self.store.transaction() as db:
            found = db.execute(
                "SELECT account_id FROM account_identities WHERE provider = ? AND subject = ?", (provider, subject),
            ).fetchone()
            if session_hash:
                current = active_account(db, session_hash)
                if current is None:
                    raise account_required()
                account_id = current["account_id"]
                if found and found["account_id"] != account_id:
                    raise OrderError(409, "identity_in_use", "Этот способ входа уже привязан к другой учётной записи.")
            elif found:
                account_id = found["account_id"]
            else:
                account_id = uuid4().hex
                db.execute("INSERT INTO accounts(id) VALUES (?)", (account_id,))
            if not found:
                db.execute("INSERT INTO account_identities VALUES (?, ?, ?)", (provider, subject, account_id))
            return self.new_session(db, request, account_id, prior_merchant)

    def flow(self, request, provider, browser, secret, subject=None):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        current = request.state.account
        with self.store.transaction() as db:
            if current:
                require_recent(active_account(db, current["id"]))
            db.execute("DELETE FROM account_flows WHERE expires_at <= ? OR browser = ?", (now, digest(browser)))
            legacy = digest(request.cookies.get(self.legacy_seller_cookie, ""))
            db.execute("INSERT INTO account_flows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (digest(token), provider, digest(browser), current["id"] if current else None,
                        secret, subject, now + FLOW_AGE, legacy))
        return token

    def consume(self, request, provider, state, *, require_subject=False, proof=None):
        browser = request.cookies.get(self.browser_cookie, "")
        if not SESSION_TOKEN.fullmatch(browser) or not SESSION_TOKEN.fullmatch(state):
            raise OrderError(400, "login_expired", "Запустите вход заново.")
        current = request.state.account
        with self.store.transaction() as db:
            result = db.execute(
                """DELETE FROM account_flows WHERE id = ? AND provider = ? AND browser = ?
                   AND session_hash IS ? AND expires_at > ? AND (? = 0 OR verified_subject IS NOT NULL)
                   AND (? IS NULL OR secret = ?)
                   RETURNING secret, verified_subject, session_hash, legacy_merchant""",
                (digest(state), provider, digest(browser), current["id"] if current else None, int(time.time()),
                 require_subject, proof, proof),
            ).fetchone()
        if result is None:
            raise OrderError(400, "login_expired", "Запустите вход заново или подтвердите его у провайдера.")
        return result

    def claim_guest(self, db, shopper, owner, slug):
        claimed = db.execute(
            """INSERT OR IGNORE INTO shopper_owners
               SELECT id, ? FROM sessions WHERE id = ? AND merchant = ? AND expires_at > ?""",
            (owner, shopper, slug, int(time.time())),
        ).rowcount
        if claimed:
            db.execute("UPDATE sessions SET expires_at = 0 WHERE id = ?", (shopper,))

    def provision_shopper(self, db, owner, slug):
        session_id = shopper_id(owner, slug)
        db.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?, ?)", (session_id, slug, 9_007_199_254_740_991))
        db.execute("INSERT OR IGNORE INTO shopper_owners VALUES (?, ?)", (session_id, owner))
