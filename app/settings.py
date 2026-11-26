import os
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
        if llm_url.scheme != "https" or not llm_url.hostname or llm_url.username is not None:
            raise ValueError("LLM_BASE_URL должен использовать HTTPS")
        if self.mode not in {"simulated", "live"}:
            raise ValueError("APP_MODE должен быть simulated или live")
        if self.mode == "live" and not self.llm_key:
            raise ValueError("Для live нужен LLM_API_KEY")
        if self.setup_key and len(self.setup_key) < 24:
            raise ValueError("MERCHANT_SETUP_KEY должен содержать не менее 24 символов")
        if self.default_owner_code and not 16 <= len(self.default_owner_code) <= 128:
            raise ValueError("COFFEE_OWNER_CODE должен содержать от 16 до 128 символов")

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def cookie(self) -> str:
        return "__Host-customer_session" if self.secure else "customer_session_dev"

    @classmethod
    def from_env(cls):
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
        )
