import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from contextlib import nullcontext
from dataclasses import asdict
from threading import BoundedSemaphore

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .agent import ModelAgent, ModelConnection, ModelFailure
from .catalog import Catalog, Product
from .diagnostics import clear_failure, record_failure
from .gateway import YooKassa, payment_reference
from .settings import Settings
from .storage import Store

SLUG = re.compile(r"[a-z][a-z0-9-]{2,31}")
SKU = re.compile(r"[a-zA-Z0-9_-]{1,40}")
COLUMNS = ["sku", "title", "basis", "price_kopecks", "area_from", "area_to"]
CURRENT_MODEL_REVISION = object()
OWNER_HASH_LIMIT = BoundedSemaphore(4)
CLEANING_CSV = """sku,title,basis,price_kopecks,area_from,area_to
cleaning,Уборка квартиры,area,350000,1,50
cleaning,Уборка квартиры,area,450000,51,70
cleaning,Уборка квартиры,area,650000,71,200
oven,Чистка духовки,fixed,80000,,
"""


def price_list(raw: str) -> dict[str, Product]:
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")), strict=True)
    if reader.fieldnames != COLUMNS:
        raise ValueError("Заголовок CSV должен содержать " + ", ".join(COLUMNS))
    products = {}
    for number, row in enumerate(reader, 2):
        if number > 121 or None in row or any(value is None for value in row.values()):
            raise ValueError(f"Проверьте столбцы и число строк CSV. Строка {number}.")
        sku, title, basis, price, lower, upper = (row[field].strip() for field in COLUMNS)
        if (
            not SKU.fullmatch(sku) or not 1 <= len(title) <= 100
            or title[0] in "=+@-" or any(char in title for char in "\t\r\n")
            or basis not in {"fixed", "unit", "area"}
            or not re.fullmatch(r"[0-9]{1,8}", price) or not 1 <= int(price) <= 10_000_000
        ):
            raise ValueError(f"Проверьте код, название, тип расчёта и цену в строке {number}.")
        previous = products.get(sku)
        if basis != "area":
            if previous or lower or upper:
                raise ValueError(f"Повтор позиции или лишние границы в строке {number}.")
            products[sku] = Product(title, int(price), basis)
            continue
        if not all(re.fullmatch(r"[0-9]{1,3}", n) for n in (lower, upper)) or not 1 <= int(lower) <= int(upper) <= 200:
            raise ValueError(f"Укажите диапазон целых м² от 1 до 200 в строке {number}.")
        bounds = (int(lower), int(upper), int(price))
        if previous and (previous.basis != "area" or previous.title != title or any(
            a <= bounds[1] and bounds[0] <= b for a, b, _ in previous.tiers
        )):
            raise ValueError(f"Диапазоны или определения позиции пересекаются в строке {number}.")
        products[sku] = Product(title, 0, basis, (*previous.tiers, bounds) if previous else (bounds,))
    if not products or len(products) > 40:
        raise ValueError("Загрузите от 1 до 40 позиций.")
    return products


def serialize(products: dict[str, Product]) -> str:
    return json.dumps({sku: asdict(product) for sku, product in products.items()}, ensure_ascii=False)


def export_catalog(products: dict[str, Product]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(COLUMNS)
    for sku, item in products.items():
        if item.basis == "area":
            for lower, upper, price in item.tiers:
                writer.writerow((sku, item.title, item.basis, price, lower, upper))
        else:
            writer.writerow((sku, item.title, item.basis, item.unit_kopecks, "", ""))
    return output.getvalue()


def hash_code(code: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    if not OWNER_HASH_LIMIT.acquire(blocking=False):
        raise ValueError("Сервис входа занят. Повторите запрос.")
    try:
        digest = hashlib.scrypt(code.encode(), salt=salt, n=2**14, r=8, p=1)
    finally:
        OWNER_HASH_LIMIT.release()
    return f"{salt.hex()}:{digest.hex()}"


class Merchants:
    def __init__(self, store: Store, settings: Settings, default: Catalog):
        self.store, self.settings, self.default = store, settings, default
        self.vault = Fernet(settings.vault_key.encode()) if settings.vault_key else None
        self.mcp_pool = None

    def catalog(self, db, slug: str) -> Catalog | None:
        merchant = db.execute("SELECT name, catalog_json, deposit FROM merchants WHERE slug = ?", (slug,)).fetchone()
        if merchant and merchant["catalog_json"]:
            products = {
                sku: Product(value["title"], value["unit_kopecks"], value["basis"], tuple(map(tuple, value["tiers"])))
                for sku, value in json.loads(merchant["catalog_json"]).items()
            }
            return Catalog(slug, merchant["name"], products, merchant["deposit"])
        return self.default if slug == self.default.merchant_id else None

    def limit_auth(self, address: str, slug: str) -> bool:
        now = int(time.time())
        with self.store.transaction() as db:
            db.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (now - 300,))
            scopes = [("ip:" + address, 20), ("merchant:" + slug, 30)]
            for scoped, limit in scopes:
                count = db.execute("SELECT count(*) FROM auth_attempts WHERE address = ?", (scoped,)).fetchone()[0]
                if count >= limit:
                    return False
            db.executemany("INSERT INTO auth_attempts VALUES (?, ?)", [(scoped, now) for scoped, _ in scopes])
            return True

    def register(self, slug: str, name: str, code: str, *, corporate: bool = False):
        if not SLUG.fullmatch(slug) or not 2 <= len(name) <= 80 or not 16 <= len(code) <= 128:
            raise ValueError("Проверьте адрес магазина, название и код доступа от 16 символов.")
        if slug == self.default.merchant_id and not corporate and not (
            self.settings.default_owner_code
            and hmac.compare_digest(code.encode(), self.settings.default_owner_code.encode())
        ):
            raise ValueError("Адрес зарезервирован. Используйте отдельный код владельца существующего магазина.")
        hashed = hash_code(code)
        with self.store.transaction() as db:
            created = db.execute(
                "INSERT OR IGNORE INTO merchants(slug, name, owner_hash) VALUES (?, ?, ?)",
                (slug, name, hashed),
            ).rowcount
            if not created:
                raise ValueError("Этот адрес магазина уже занят.")

    def valid_code(self, slug: str, code: str) -> str | None:
        if not SLUG.fullmatch(slug) or not 16 <= len(code) <= 128:
            return None
        with self.store.read() as db:
            row = db.execute("SELECT owner_hash FROM merchants WHERE slug = ?", (slug,)).fetchone()
        saved = row["owner_hash"] if row else "0" * 32 + ":" + "0" * 128
        actual = hash_code(code, bytes.fromhex(saved.split(":")[0]))
        return saved if row is not None and hmac.compare_digest(saved, actual) else None

    def publish(self, slug: str, csv_text: str, deposit: int, expected_version: int, *, guard=None) -> bool:
        parsed = price_list(csv_text)
        if not 1 <= deposit <= 100:
            raise ValueError("Предоплата должна составлять от 1 до 100%.")
        with self.store.transaction() as db:
            if guard:
                guard(db)
            return db.execute(
                """UPDATE merchants SET catalog_json = ?, deposit = ?, catalog_version = catalog_version + 1
                   WHERE slug = ? AND catalog_version = ?""",
                (serialize(parsed), deposit, slug, expected_version),
            ).rowcount == 1

    def connect(self, slug: str, token: str, *, recover=False, external_id="", guard=None):
        if self.vault is None:
            raise ValueError("На сервере требуется настройка хранилища токенов.")
        if not re.fullmatch(r"[!-~]{16,4096}", token):
            raise ValueError("Проверьте формат MCP-токена.")
        if external_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_id):
            raise ValueError("Проверьте ID платежа или счёта.")
        with self.store.read() as db:
            merchant = db.execute("SELECT mcp_ciphertext FROM merchants WHERE slug = ?", (slug,)).fetchone()
            if not merchant:
                raise ValueError("Сначала создайте магазин.")
            previous = merchant["mcp_ciphertext"]
            if not recover:
                self.require_settled_payments(db, slug)
            anchor = self.payment_anchor(db, slug)
        if previous and not anchor and not recover:
            raise ValueError("Для плановой смены сохранённого токена нужен платёж из журнала. Для восстановления используйте отдельный режим.")
        if slug != self.default.merchant_id and self.settings.mcp_token and hmac.compare_digest(token, self.settings.mcp_token):
            raise ValueError("Этот токен уже используется другим магазином.")
        try:
            candidate = YooKassa(token)
            candidate.pool = self.mcp_pool
            ready = candidate.check()
            anchored_payment = None
            reference_id = ""
            if ready and anchor:
                reference_id = anchor["external_id"] or external_id or anchor["candidate_id"] or ""
                if not reference_id and recover and anchor["kind"] == "payment":
                    amount = json.loads(anchor["snapshot"])["payable_kopecks"]
                    reference_id = candidate.find_payment(anchor["created_at"], anchor["id"], amount)
                if reference_id:
                    read_anchor = candidate.get_invoice if anchor["kind"] == "invoice" else candidate.get
                    anchored_payment = read_anchor(reference_id)
        except Exception as error:
            record_failure(slug, "mcp", "connect", "payment_token", error)
            raise ValueError("Не удалось проверить токен. Проверьте его и повторите попытку.") from error
        if not ready:
            raise ValueError("У магазина должны быть доступны создание и проверка платежей через MCP.")
        if anchor and (
            anchored_payment is None or anchored_payment.id != reference_id or anchored_payment.amount.currency != "RUB"
            or anchored_payment.kopecks != json.loads(anchor["snapshot"])["payable_kopecks"]
            or anchored_payment.description != payment_reference(anchor["id"])
        ):
            record_failure(slug, "mcp", "connect", "payment_token")
            raise ValueError("Новый токен не подтвердил платёж этого магазина. Проверьте ID операции.")
        encrypted = self.vault.encrypt(json.dumps([slug, token]).encode()).decode()
        fingerprint = hmac.new(self.settings.vault_key.encode(), b"merchant-mcp:" + token.encode(), "sha256").hexdigest()
        with self.store.transaction() as db:
            if guard:
                guard(db)
            if not recover:
                self.require_settled_payments(db, slug)
            if anchor is None and self.payment_anchor(db, slug):
                raise ValueError("В журнале появился новый платёж. Повторите подключение.")
            if db.execute(
                "SELECT 1 FROM merchants WHERE mcp_fingerprint = ? AND slug != ?", (fingerprint, slug),
            ).fetchone():
                raise ValueError("Этот токен уже используется другим магазином.")
            changed = db.execute(
                "UPDATE merchants SET mcp_ciphertext = ?, mcp_fingerprint = ? WHERE slug = ? AND mcp_ciphertext IS ?",
                (encrypted, fingerprint, slug, previous),
            ).rowcount
            if not changed:
                raise ValueError("Подключение уже изменилось. Обновите кабинет и повторите действие.")
            if recover:
                if anchor and not anchor["external_id"] and reference_id:
                    db.execute(
                        """UPDATE attempts SET candidate_id = ? WHERE id = ? AND external_id IS NULL
                           AND state IN ('unknown', 'pending')""", (reference_id, anchor["id"]),
                    )
                db.execute(
                    """UPDATE attempts SET next_check = 0 WHERE state IN ('pending','unknown')
                       AND order_id IN (SELECT id FROM orders WHERE merchant = ?)""", (slug,),
                )
        clear_failure(slug, "mcp")

    @staticmethod
    def payment_anchor(db, slug):
        return db.execute(
            """SELECT a.id, a.external_id, a.candidate_id, a.snapshot, a.kind, a.created_at
               FROM attempts a JOIN orders o ON o.id = a.order_id
               WHERE o.merchant = ? AND a.state != 'failed'
               ORDER BY (a.external_id IS NOT NULL) DESC, a.created_at DESC, a.id DESC LIMIT 1""", (slug,),
        ).fetchone()

    @staticmethod
    def require_settled_payments(db, slug):
        if db.execute(
            """SELECT 1 FROM attempts a JOIN orders o ON o.id = a.order_id
               WHERE o.merchant = ? AND a.state IN ('creating','pending','unknown') LIMIT 1""",
            (slug,),
        ).fetchone():
            raise ValueError("Завершите оплату или сверку активных платежей либо выберите восстановление токена.")

    def token(self, slug: str, db=None) -> str | None:
        with (self.store.read() if db is None else nullcontext(db)) as connection:
            row = connection.execute("SELECT mcp_ciphertext FROM merchants WHERE slug = ?", (slug,)).fetchone()
        if row and row["mcp_ciphertext"]:
            if self.vault is None:
                return None
            try:
                bound_slug, token = json.loads(self.vault.decrypt(row["mcp_ciphertext"].encode()))
            except (InvalidToken, ValueError):
                return None
            return token if bound_slug == slug else None
        return (self.settings.mcp_token or None) if slug == self.default.merchant_id else None

    def model_providers(self):
        providers = {
            "openai": ("OpenAI", "https://api.openai.com/v1"),
            "company": ("Корпоративный API", self.settings.llm_url),
        }
        if self.settings.openrouter_oauth:
            providers["openrouter"] = ("OpenRouter", "https://openrouter.ai/api/v1")
        return providers

    def model_summary(self, slug):
        with self.store.read() as db:
            row = db.execute("SELECT provider, model FROM model_connectors WHERE merchant = ?", (slug,)).fetchone()
        return dict(row) if row else None

    def disconnect_model(self, slug, *, guard=None):
        with self.store.transaction() as db:
            if guard:
                guard(db)
            db.execute(
                """INSERT INTO model_connectors VALUES (?, 'disabled', '', ?) ON CONFLICT(merchant)
                   DO UPDATE SET provider='disabled', model='', ciphertext=excluded.ciphertext""",
                (slug, secrets.token_hex(16)),
            )

    def model_options(self, provider, key):
        endpoint = self.model_providers().get(provider)
        if not endpoint or not re.fullmatch(r"[!-~]{12,4096}", key):
            raise ValueError("Выберите провайдера и введите API-ключ.")
        try:
            response = httpx.get(
                f"{endpoint[1]}/models", headers={"Authorization": f"Bearer {key}"},
                timeout=6, follow_redirects=False,
            )
            response.raise_for_status()
            options = {
                item["id"] for item in response.json()["data"]
                if isinstance(item.get("id"), str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}", item["id"])
            }
        except (httpx.HTTPError, AttributeError, KeyError, ValueError, TypeError) as error:
            raise ValueError("Провайдер не вернул список. Проверьте ключ или укажите название модели вручную.") from error
        return sorted(options)[:100]

    @staticmethod
    def model_revision(db, slug):
        row = db.execute("SELECT ciphertext FROM model_connectors WHERE merchant = ?", (slug,)).fetchone()
        return row["ciphertext"] if row else None

    def connect_model(self, slug, provider, model, key, *, expected_revision=CURRENT_MODEL_REVISION, guard=None):
        endpoint = self.model_providers().get(provider)
        if self.vault is None:
            raise ValueError("На сервере требуется настройка хранилища ключей.")
        if not endpoint or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}", model):
            raise ValueError("Выберите провайдера и укажите идентификатор модели.")
        if not re.fullmatch(r"[!-~]{12,4096}", key):
            raise ValueError("Проверьте формат API-ключа.")
        with self.store.read() as db:
            previous = self.model_revision(db, slug)
        if expected_revision is not CURRENT_MODEL_REVISION and expected_revision != previous:
            return False
        selected = ModelConnection(endpoint[1], model, key)
        try:
            ModelAgent.request(selected, self.default, {}, [], "Мне нужно уточнить условия заказа.")
        except ModelFailure as error:
            record_failure(slug, "llm", "connect", error.code)
            raise ValueError(str(error)) from error
        except Exception as error:
            record_failure(slug, "llm", "connect", "model_request_error", error)
            raise ValueError("Модель не ответила в нужном формате. Проверьте ключ, доступ и идентификатор модели.") from error
        clear_failure(slug, "llm")
        ciphertext = self.vault.encrypt(json.dumps([slug, provider, endpoint[1], model, key]).encode()).decode()
        with self.store.transaction() as db:
            if guard:
                guard(db)
            return db.execute(
                """INSERT INTO model_connectors VALUES (?, ?, ?, ?) ON CONFLICT(merchant)
                   DO UPDATE SET provider=excluded.provider, model=excluded.model, ciphertext=excluded.ciphertext
                   WHERE model_connectors.ciphertext = ?""",
                (slug, provider, model, ciphertext, previous),
            ).rowcount == 1

    def model_connection(self, slug, *, expected_provider=None, db=None) -> ModelConnection | None:
        with (self.store.read() if db is None else nullcontext(db)) as connection:
            row = connection.execute("SELECT * FROM model_connectors WHERE merchant = ?", (slug,)).fetchone()
        if expected_provider is not None and (row is None or row["provider"] != expected_provider):
            return None
        if not row:
            settings = self.settings
            if settings.llm_key and (settings.access == "local" or slug == self.default.merchant_id):
                return ModelConnection(settings.llm_url, settings.llm_model, settings.llm_key)
            return None
        if self.vault is None:
            return None
        endpoint = self.model_providers().get(row["provider"])
        if not endpoint:
            return None
        try:
            owner, provider, url, model, key = json.loads(self.vault.decrypt(row["ciphertext"].encode()))
        except (InvalidToken, ValueError, TypeError):
            return None
        if (owner, provider, url, model) != (slug, row["provider"], endpoint[1], row["model"]):
            return None
        return ModelConnection(endpoint[1], model, key)
