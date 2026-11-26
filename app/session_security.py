import hashlib
import hmac
import re

from .orders import OrderError

SESSION_TOKEN = re.compile(r"[A-Za-z0-9_\-]{43}")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf(token: str) -> str:
    return digest("csrf:" + token)


def check_csrf(request, origin: str, token: str, submitted: str):
    if request.headers.get("Origin") != origin or not hmac.compare_digest(submitted.encode(), csrf(token).encode()):
        raise OrderError(403, "invalid_origin", "Обновите страницу и повторите действие.")
