import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    db_path: str
    origin: str
    mode: str = "simulated"
    llm_key: str = field(default="", repr=False)
    mcp_token: str = field(default="", repr=False)
    llm_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    setup_key: str = field(default="", repr=False)
    vault_key: str = field(default="", repr=False)
    default_owner_code: str = field(default="", repr=False)
    access: str = "local"
    oidc_issuer: str = ""
    oidc_jwks: str = ""
    oidc_client_id: str = ""
    oidc_email_domain: str = ""
    oidc_merchants: dict[str, str] = field(default_factory=dict, repr=False)
    model_session_limit: int = 25
    model_merchant_limit: int = 60
    model_instance_limit: int = 120
    model_parallel_limit: int = 8
    openrouter_oauth: bool = False
    openrouter_model: str = "openai/gpt-4.1-mini"
    mcp_invoices: bool = False
    mail_host: str = ""
    mail_port: int = 465
    mail_from: str = ""
    mail_user: str = ""
    mail_password: str = field(default="", repr=False)
    accounts_enabled: bool = False
    social_clients: dict[str, list[str]] = field(default_factory=dict, repr=False)
    max_bot_name: str = ""
    max_bot_token: str = field(default="", repr=False)

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username is not None
            or (origin.scheme == "http" and origin.hostname not in {"127.0.0.1", "localhost"})
        ):
            raise ValueError("PUBLIC_ORIGIN должен быть HTTPS или локальным HTTP без пути")
        llm_url = urlsplit(self.llm_url)
        if (
            llm_url.scheme != "https" or not llm_url.hostname or llm_url.username is not None
            or llm_url.query or llm_url.fragment
        ):
            raise ValueError("LLM_BASE_URL должен использовать HTTPS")
        if self.mode not in {"simulated", "live"}:
            raise ValueError("APP_MODE должен быть simulated или live")
        if self.access not in {"local", "private"}:
            raise ValueError("APP_ACCESS должен быть local или private")
        if self.access == "private":
            endpoints = [urlsplit(self.oidc_issuer), urlsplit(self.oidc_jwks)]
            if not self.secure or not self.oidc_client_id or not re.fullmatch(
                r"[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+", self.oidc_email_domain,
            ) or any(
                url.scheme != "https" or not url.hostname or url.username is not None or url.fragment for url in endpoints
            ):
                raise ValueError("Для закрытого запуска нужны HTTPS origin, issuer, JWKS и клиент OIDC")
            if not isinstance(self.oidc_merchants, dict) or any(
                not isinstance(subject, str) or not subject or not isinstance(slug, str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", slug)
                for subject, slug in self.oidc_merchants.items()
            ):
                raise ValueError("OIDC_MERCHANTS_JSON должен сопоставлять идентификаторы пользователей с адресами магазинов")
        if self.setup_key and len(self.setup_key) < 24:
            raise ValueError("MERCHANT_SETUP_KEY должен содержать не менее 24 символов")
        if self.default_owner_code and not 16 <= len(self.default_owner_code) <= 128:
            raise ValueError("COFFEE_OWNER_CODE должен содержать от 16 до 128 символов")
        if not (
            1 <= self.model_session_limit <= self.model_merchant_limit <= self.model_instance_limit <= 100_000
            and 1 <= self.model_parallel_limit <= 24
        ):
            raise ValueError("Проверьте порядок квот модели и предел параллельных запросов")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}", self.openrouter_model):
            raise ValueError("OPENROUTER_MODEL должен содержать идентификатор модели")
        if not 1 <= self.mail_port <= 65535 or bool(self.mail_user) != bool(self.mail_password) or any(
            separator in self.mail_from for separator in "\r\n"
        ):
            raise ValueError("Проверьте порт, отправителя и учётные данные SMTP")
        if not isinstance(self.social_clients, dict) or any(
            key not in {"google", "yandex", "vk", "telegram"}
            or not isinstance(value, list) or len(value) != 2
            or any(not isinstance(part, str) or len(part) > 512 for part in value)
            or not value[0] or (key != "vk" and not value[1])
            for key, value in self.social_clients.items()
        ):
            raise ValueError("SOCIAL_AUTH_JSON должен содержать пары идентификатора и секрета OAuth")
        if bool(self.max_bot_name) != bool(self.max_bot_token) or (
            self.max_bot_name and not re.fullmatch(r"[a-zA-Z0-9_]{3,80}", self.max_bot_name)
        ):
            raise ValueError("Проверьте настройки бота MAX")

    @property
    def mail_ready(self) -> bool:
        return self.mode == "live" and bool(self.mail_host and self.mail_from)

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def cookie(self) -> str:
        return "__Host-customer_session" if self.secure else "customer_session_dev"

    @classmethod
    def from_env(cls):
        if "*" in [item.strip() for item in os.getenv("FORWARDED_ALLOW_IPS", "").split(",")]:
            raise ValueError("FORWARDED_ALLOW_IPS должен перечислять только доверенные прокси.")
        return cls(
            db_path=os.getenv("DATABASE_PATH", str(Path("data") / "orders.sqlite3")),
            origin=os.getenv("PUBLIC_ORIGIN", "http://127.0.0.1:8000"),
            mode=os.getenv("APP_MODE", "simulated"),
            llm_key=os.getenv("LLM_API_KEY", ""),
            mcp_token=os.getenv("YOOKASSA_MCP_TOKEN", ""),
            llm_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            setup_key=os.getenv("MERCHANT_SETUP_KEY", ""),
            vault_key=os.getenv("MERCHANT_VAULT_KEY", ""),
            default_owner_code=os.getenv("COFFEE_OWNER_CODE", ""),
            access=os.getenv("APP_ACCESS", "local"),
            oidc_issuer=os.getenv("OIDC_ISSUER", ""),
            oidc_jwks=os.getenv("OIDC_JWKS_URL", ""),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_email_domain=os.getenv("OIDC_EMAIL_DOMAIN", ""),
            oidc_merchants=json.loads(os.getenv("OIDC_MERCHANTS_JSON") or "{}"),
            model_session_limit=int(os.getenv("MODEL_SESSION_LIMIT", "25")),
            model_merchant_limit=int(os.getenv("MODEL_MERCHANT_LIMIT", "60")),
            model_instance_limit=int(os.getenv("MODEL_INSTANCE_LIMIT", "120")),
            model_parallel_limit=int(os.getenv("MODEL_PARALLEL_LIMIT", "8")),
            openrouter_oauth=os.getenv("OPENROUTER_OAUTH_ENABLED") == "1",
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
            mcp_invoices=os.getenv("MCP_INVOICES_ENABLED") == "1",
            mail_host=os.getenv("MAIL_SMTP_HOST", ""),
            mail_port=int(os.getenv("MAIL_SMTP_PORT", "465")),
            mail_from=os.getenv("MAIL_FROM", ""),
            mail_user=os.getenv("MAIL_SMTP_USER", ""),
            mail_password=os.getenv("MAIL_SMTP_PASSWORD", ""),
            accounts_enabled=os.getenv("ACCOUNT_AUTH_ENABLED") == "1",
            social_clients=json.loads(os.getenv("SOCIAL_AUTH_JSON") or "{}"),
            max_bot_name=os.getenv("MAX_BOT_NAME", ""),
            max_bot_token=os.getenv("MAX_BOT_TOKEN", ""),
        )
