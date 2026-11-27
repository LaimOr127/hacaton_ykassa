import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode

import httpx
import jwt

PROVIDERS = {
    "google": "Google", "yandex": "Яндекс", "vk": "VK ID", "telegram": "Telegram", "max": "MAX", "email": "Почта",
}
AUTH_URL = {
    "google": "https://accounts.google.com/o/oauth2/v2/auth",
    "yandex": "https://oauth.yandex.ru/authorize",
    "vk": "https://id.vk.com/authorize",
    "telegram": "https://oauth.telegram.org/auth",
}
TOKEN_URL = {
    "google": "https://oauth2.googleapis.com/token",
    "yandex": "https://oauth.yandex.ru/token",
    "vk": "https://id.vk.com/oauth2/auth",
    "telegram": "https://oauth.telegram.org/token",
}
SCOPES = {"google": "openid", "yandex": "login:info", "vk": "vkid.personal_info", "telegram": "openid"}
ISSUER = {"google": "https://accounts.google.com", "telegram": "https://oauth.telegram.org"}
JWKS = {
    "google": jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", timeout=4),
    "telegram": jwt.PyJWKClient("https://oauth.telegram.org/.well-known/jwks.json", timeout=4),
}


def callback_url(origin, provider):
    return f"{origin}/account/oauth/{provider}/callback"


def authorize_url(provider, origin, client_id, state, verifier, nonce):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "client_id": client_id, "redirect_uri": callback_url(origin, provider), "response_type": "code",
        "scope": SCOPES[provider], "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if provider == "google":
        params["nonce"] = nonce
    if provider == "vk":
        params["code_challenge_method"] = "s256"
    return AUTH_URL[provider] + "?" + urlencode(params)


def identity_subject(provider, origin, client, code, verifier, nonce, state, device):
    client_id, secret = client
    data = {
        "client_id": client_id, "grant_type": "authorization_code", "redirect_uri": callback_url(origin, provider),
        "code": code, "code_verifier": verifier,
    }
    auth = None
    if provider == "telegram":
        auth = (client_id, secret)
    elif provider == "vk":
        if not device or len(device) > 512:
            raise ValueError("Нет идентификатора устройства VK")
        data.update(device_id=device, state=state)
    else:
        data["client_secret"] = secret
    query = {}
    if provider == "vk":
        query, data = {key: value for key, value in data.items() if key != "code"}, {"code": code}
    response = httpx.post(TOKEN_URL[provider], data=data, params=query, auth=auth, timeout=8, follow_redirects=False)
    response.raise_for_status()
    tokens = response.json()
    if provider in JWKS:
        token = tokens["id_token"]
        if not isinstance(token, str) or len(token) > 8192 or jwt.get_unverified_header(token)["alg"] != "RS256":
            raise ValueError("Неверный токен входа")
        key = JWKS[provider].get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ISSUER[provider], audience=client_id,
                            options={"require": ["sub", "iss", "aud", "iat", "exp"]}, leeway=30)
        audience = claims["aud"]
        if (isinstance(audience, list) and len(audience) > 1 or "azp" in claims) and claims.get("azp") != client_id:
            raise ValueError("Ответ выдан другому приложению")
        if provider == "google" and claims.get("nonce") != nonce:
            raise ValueError("Ответ не соответствует попытке входа")
        subject = claims["sub"]
    elif provider == "yandex":
        info = httpx.get(
            "https://login.yandex.ru/info?format=json", headers={"Authorization": "OAuth " + tokens["access_token"]},
            timeout=8, follow_redirects=False,
        )
        info.raise_for_status()
        subject = info.json()["id"]
    else:
        if tokens.get("state") != state:
            raise ValueError("Ответ VK не соответствует попытке входа")
        subject = tokens["user_id"]
    return stable_subject(subject)


def stable_subject(value):
    if not isinstance(value, (int, str)) or isinstance(value, bool) or not 1 <= len(str(value)) <= 255:
        raise ValueError("Провайдер не вернул идентификатор пользователя")
    return str(value)


def verify_max(init_data, bot_token):
    pairs = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True, max_num_fields=24)
    fields = dict(pairs)
    if len(fields) != len(pairs) or any("\n" in key or "\n" in value or "\r" in key or "\r" in value for key, value in pairs):
        raise ValueError("Некорректные поля MAX")
    supplied_hash = fields.pop("hash")
    signed = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
    signature = hmac.new(secret, signed.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(signature, supplied_hash):
        raise ValueError("Подпись MAX недействительна")
    if not -30 <= time.time() - int(fields["auth_date"]) <= 300:
        raise ValueError("Данные MAX устарели")
    return fields["start_param"], stable_subject(json.loads(fields["user"])["id"])
