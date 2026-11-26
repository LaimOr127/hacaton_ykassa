import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, merchant TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    merchant TEXT NOT NULL, create_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft', quantities TEXT NOT NULL DEFAULT '{}',
    quote TEXT NOT NULL, agreed_version INTEGER, attempt_id TEXT,
    UNIQUE (session_id, create_key)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    request_key TEXT NOT NULL, base_version INTEGER NOT NULL, text TEXT NOT NULL,
    lease TEXT NOT NULL, started_at INTEGER NOT NULL, result TEXT,
    UNIQUE (order_id, request_key)
);
CREATE INDEX IF NOT EXISTS messages_started ON messages(started_at);
CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    request_key TEXT NOT NULL, order_version INTEGER NOT NULL, snapshot TEXT NOT NULL,
    state TEXT NOT NULL, external_id TEXT UNIQUE, confirmation_url TEXT,
    created_at INTEGER NOT NULL, next_check INTEGER NOT NULL DEFAULT 0,
    UNIQUE (order_id, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS active_payment ON attempts(order_id)
    WHERE state IN ('creating', 'pending', 'unknown');
CREATE TABLE IF NOT EXISTS demo_payments (
    id TEXT PRIMARY KEY, amount_kopecks INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
"""


class Store:
    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode

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

    @contextmanager
    def read(self):
        with closing(self.connect()) as db:
            yield db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db
