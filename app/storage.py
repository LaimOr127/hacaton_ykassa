import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock

import anyio

from .diagnostics import record_failure


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merchants (
    slug TEXT PRIMARY KEY, name TEXT NOT NULL, owner_hash TEXT NOT NULL,
    catalog_json TEXT, deposit INTEGER NOT NULL DEFAULT 100, catalog_version INTEGER NOT NULL DEFAULT 0,
    mcp_ciphertext TEXT, mcp_fingerprint TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS merchant_sessions (
    id TEXT PRIMARY KEY, merchant TEXT NOT NULL REFERENCES merchants(slug), expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_connectors (
    merchant TEXT PRIMARY KEY REFERENCES merchants(slug),
    provider TEXT NOT NULL, model TEXT NOT NULL, ciphertext TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_authorizations (
    state_hash TEXT PRIMARY KEY, merchant TEXT NOT NULL REFERENCES merchants(slug),
    owner_hash TEXT NOT NULL, verifier TEXT NOT NULL, revision TEXT, expires_at INTEGER NOT NULL,
    UNIQUE (merchant, owner_hash)
);
CREATE TABLE IF NOT EXISTS auth_attempts (
    address TEXT NOT NULL, attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_attempts_time ON auth_attempts(attempted_at);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, merchant TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT
);
CREATE TABLE IF NOT EXISTS account_identities (
    provider TEXT NOT NULL, subject TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS account_identities_owner ON account_identities(account_id);
CREATE TABLE IF NOT EXISTS account_sessions (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
    expires_at INTEGER NOT NULL, merchant TEXT
);
CREATE INDEX IF NOT EXISTS account_sessions_owner ON account_sessions(account_id);
CREATE INDEX IF NOT EXISTS account_sessions_expiry ON account_sessions(expires_at);
CREATE TABLE IF NOT EXISTS account_auth_attempts (
    address TEXT NOT NULL, attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS account_auth_attempts_time ON account_auth_attempts(attempted_at);
CREATE TABLE IF NOT EXISTS account_merchants (
    account_id TEXT NOT NULL REFERENCES accounts(id), merchant TEXT NOT NULL REFERENCES merchants(slug),
    PRIMARY KEY (account_id, merchant)
);
CREATE TABLE IF NOT EXISTS shopper_owners (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id), owner_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shopper_owners_owner ON shopper_owners(owner_id, session_id);
CREATE TABLE IF NOT EXISTS account_flows (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, browser TEXT NOT NULL, session_hash TEXT,
    secret TEXT NOT NULL, verified_subject TEXT, expires_at INTEGER NOT NULL, legacy_merchant TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_flows_expiry ON account_flows(expires_at);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    merchant TEXT NOT NULL, create_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft', quantities TEXT NOT NULL DEFAULT '{}',
    quote TEXT NOT NULL, agreed_version INTEGER, attempt_id TEXT,
    UNIQUE (session_id, create_key)
);
CREATE INDEX IF NOT EXISTS merchant_orders ON orders(merchant, state);
CREATE INDEX IF NOT EXISTS orders_current_attempt ON orders(attempt_id) WHERE attempt_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS checkout_settings (
    merchant TEXT PRIMARY KEY REFERENCES merchants(slug),
    split_payments INTEGER NOT NULL, tips INTEGER NOT NULL,
    receipt_download INTEGER NOT NULL, receipt_email INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkout_plans (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    parts INTEGER NOT NULL CHECK (parts BETWEEN 1 AND 4),
    tip_percent INTEGER NOT NULL CHECK (tip_percent IN (0, 5, 10, 15)),
    tip_kopecks INTEGER NOT NULL CHECK (tip_kopecks >= 0),
    total_kopecks INTEGER NOT NULL CHECK (total_kopecks > 0),
    paid_parts INTEGER NOT NULL DEFAULT 0 CHECK (paid_parts BETWEEN 0 AND parts)
);
CREATE TRIGGER IF NOT EXISTS prevent_incomplete_checkout
    BEFORE UPDATE OF state ON orders
    WHEN OLD.state = 'payment_pending' AND NEW.state != 'payment_pending'
         AND EXISTS (SELECT 1 FROM checkout_plans WHERE order_id = OLD.id AND paid_parts < parts)
    BEGIN SELECT RAISE(ABORT, 'checkout plan incomplete'); END;
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    request_key TEXT NOT NULL, base_version INTEGER NOT NULL, text TEXT NOT NULL,
    lease TEXT NOT NULL, started_at INTEGER NOT NULL, result TEXT,
    UNIQUE (order_id, request_key)
);
CREATE INDEX IF NOT EXISTS messages_started ON messages(started_at);
CREATE TABLE IF NOT EXISTS seller_reviews (
    order_id TEXT NOT NULL REFERENCES orders(id),
    opened_message TEXT PRIMARY KEY REFERENCES messages(id),
    opened_at INTEGER NOT NULL, reply TEXT, answered_at INTEGER, closed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS open_seller_review ON seller_reviews(order_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS fulfilled_orders (
    order_id TEXT PRIMARY KEY REFERENCES orders(id), fulfilled_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS model_calls_time ON model_calls(started_at);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    request_key TEXT NOT NULL, order_version INTEGER NOT NULL, snapshot TEXT NOT NULL,
    state TEXT NOT NULL, external_id TEXT UNIQUE, confirmation_url TEXT,
    created_at INTEGER NOT NULL, next_check INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment','invoice')),
    settled_payment_id TEXT, candidate_id TEXT, part_number INTEGER, verified_at INTEGER,
    UNIQUE (order_id, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS active_payment ON attempts(order_id)
    WHERE state IN ('creating', 'pending', 'unknown');
CREATE INDEX IF NOT EXISTS due_attempts ON attempts(next_check, created_at)
    WHERE state IN ('pending','unknown');
CREATE TABLE IF NOT EXISTS payment_refunds (
    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
    amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0)
);
CREATE INDEX IF NOT EXISTS payment_refunds_attempt ON payment_refunds(attempt_id);
CREATE TABLE IF NOT EXISTS payment_receipts (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), pdf BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_deliveries (
    request_key TEXT NOT NULL, order_id TEXT NOT NULL REFERENCES orders(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id), recipient_hash TEXT NOT NULL, document_kind TEXT NOT NULL, document_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (order_id, request_key)
);
CREATE INDEX IF NOT EXISTS receipt_delivery_time ON receipt_deliveries(created_at);
CREATE INDEX IF NOT EXISTS receipt_delivery_document ON receipt_deliveries(attempt_id, recipient_hash, document_kind, document_hash);
CREATE TABLE IF NOT EXISTS demo_payments (
    id TEXT PRIMARY KEY, amount_kopecks INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
"""


class StoreBusy(Exception):
    pass


class Store:
    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode
        self.writer = Lock()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('mode', ?)", (self.mode,))
            if db.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()[0] != self.mode:
                raise ValueError("Для каждого платёжного режима требуется отдельная база данных")
            fields = {row["name"] for row in db.execute("PRAGMA table_info(attempts)")}
            if "kind" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN kind TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment','invoice'))")
            if "settled_payment_id" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN settled_payment_id TEXT")
            if "candidate_id" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN candidate_id TEXT")
            if "part_number" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN part_number INTEGER")
            if "verified_at" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN verified_at INTEGER")
            if "invoice_expires_at" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN invoice_expires_at INTEGER")
            if "balance_payment" not in fields:
                db.execute("ALTER TABLE attempts ADD COLUMN balance_payment INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS settled_invoice_payment ON attempts(settled_payment_id)")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS paid_order_part ON attempts(order_id, part_number)
                          WHERE state = 'succeeded' AND part_number IS NOT NULL""")
            self.backfill_reviews(db)

    @staticmethod
    def backfill_reviews(db):
        marker = "migration:seller-reviews"
        if db.execute("SELECT 1 FROM settings WHERE key = ?", (marker,)).fetchone():
            return
        db.execute(
            """INSERT INTO seller_reviews(order_id, opened_message, opened_at)
               SELECT o.id, m.id, m.started_at FROM orders o JOIN messages m ON m.rowid = (
                   SELECT max(p.rowid) FROM messages p WHERE p.order_id = o.id AND p.result IS NOT NULL
               )
               WHERE o.state IN ('draft', 'agreed') AND json_extract(m.result, '$.outcome') = 'handoff'
               AND NOT EXISTS (SELECT 1 FROM seller_reviews r WHERE r.order_id = o.id)""",
        )
        db.execute(
            """UPDATE orders SET version = version + 1, state = 'draft', agreed_version = NULL, attempt_id = NULL
               WHERE id IN (SELECT order_id FROM seller_reviews WHERE closed_at IS NULL)""",
        )
        db.execute("INSERT INTO settings VALUES (?, '1')", (marker,))

    async def expire_sessions(self):
        while True:
            await anyio.sleep(60)
            try:
                await anyio.to_thread.run_sync(self.prune_sessions)
            except StoreBusy as error:
                record_failure("", "storage", "expire_sessions", "storage_busy", error)

    def prune_sessions(self):
        now = int(time.time())
        with self.transaction() as db:
            db.execute("DELETE FROM model_calls WHERE started_at < ?", (now - 3600,))
            db.execute(
                """DELETE FROM sessions WHERE id IN (
                       SELECT s.id FROM sessions s WHERE s.expires_at <= ?
                       AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.session_id = s.id)
                       AND NOT EXISTS (SELECT 1 FROM shopper_owners own WHERE own.session_id = s.id)
                       AND NOT EXISTS (SELECT 1 FROM model_calls c WHERE c.session_id = s.id)
                       LIMIT 1000
                   )""", (now,),
            )

    @contextmanager
    def read(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            yield db

    @contextmanager
    def transaction(self):
        if not self.writer.acquire(timeout=3):
            raise StoreBusy()
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StoreBusy() from error
            raise
        finally:
            self.writer.release()
