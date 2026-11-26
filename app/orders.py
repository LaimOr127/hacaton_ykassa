import json
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .catalog import Catalog
from .storage import Store


class OrderError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status = status
        self.body = {"code": code, "message": message, **details}


def lookup_order(db, session, order_id):
    row = db.execute(
        "SELECT * FROM orders WHERE id = ? AND session_id = ? AND merchant = ?",
        (order_id, session["id"], session["merchant"]),
    ).fetchone()
    if row is None:
        raise OrderError(404, "order_not_found", "Заказ не найден.")
    return row


def public_order(row):
    return {
        "id": row["id"],
        "version": row["version"],
        "status": row["state"],
        "agreed_version": row["agreed_version"],
        "payment_attempt_id": row["attempt_id"],
        **json.loads(row["quote"]),
    }


def check_version(row, version: int):
    if row["version"] != version:
        raise OrderError(
            409, "stale_version", "Заказ изменился. Проверьте актуальный состав.",
            current_version=row["version"],
        )


def quote_for(db, row, catalog: Catalog, quantities: dict) -> bool:
    quote = catalog.price(quantities)
    if quote == json.loads(row["quote"]) and quantities == json.loads(row["quantities"]):
        return False
    db.execute(
        """UPDATE orders SET version = version + 1, state = 'draft',
           quantities = ?, quote = ?, agreed_version = NULL, attempt_id = NULL WHERE id = ?""",
        (json.dumps(quantities), json.dumps(quote), row["id"]),
    )
    return True


class Orders:
    def __init__(self, store: Store, catalog_for, agent):
        self.store = store
        self.catalog_for = catalog_for
        self.agent = agent

    def create(self, session, key: str):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT id FROM orders WHERE session_id = ? AND create_key = ?",
                (session["id"], key),
            ).fetchone()
            if row:
                return self.empty_card(row["id"])
            order_id = uuid4().hex
            db.execute(
                "INSERT INTO orders(id, session_id, merchant, create_key, quote) VALUES (?, ?, ?, ?, ?)",
                (order_id, session["id"], session["merchant"], key, json.dumps(self.empty_card(order_id, quote_only=True))),
            )
            return self.empty_card(order_id)

    def empty_card(self, order_id, *, quote_only=False):
        prices = {"items": [], "total_kopecks": 0, "payable_kopecks": 0, "balance_kopecks": 0, "currency": "RUB"}
        if quote_only:
            return prices
        return {
            "id": order_id, "version": 1, "status": "draft", "agreed_version": None,
            "payment_attempt_id": None, **prices,
        }

    def get(self, session, order_id) -> dict:
        with self.store.read() as db:
            return public_order(lookup_order(db, session, order_id))

    def transcript(self, session, order_id):
        with self.store.read() as db:
            lookup_order(db, session, order_id)
            rows = db.execute(
                "SELECT text, result FROM messages WHERE order_id = ? AND result IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 50",
                (order_id,),
            ).fetchall()
        return [(row["text"], json.loads(row["result"])["reply"]) for row in reversed(rows)]

    def message(self, session, order_id, key, version, message):
        lease = uuid4().hex
        now = int(time.time())
        with self.store.transaction() as db:
            row = lookup_order(db, session, order_id)
            previous = db.execute(
                "SELECT * FROM messages WHERE order_id = ? AND request_key = ?", (order_id, key),
            ).fetchone()
            if previous:
                if previous["base_version"] != version or previous["text"] != message:
                    raise OrderError(409, "idempotency_conflict", "Ключ сообщения уже использован.")
                if previous["result"] is not None:
                    return json.loads(previous["result"])
                if now - previous["started_at"] < 30:
                    raise OrderError(409, "request_in_progress", "Сообщение ещё обрабатывается.")
            check_version(row, version)
            self.ensure_editable(row)
            catalog = self.catalog_for(db, session["merchant"])
            recent = db.execute(
                "SELECT text, result FROM messages WHERE order_id = ? AND result IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 2",
                (order_id,),
            ).fetchall()
            message_id = previous["id"] if previous else uuid4().hex
            if self.store.mode == "live":
                db.execute("DELETE FROM model_calls WHERE started_at < ?", (now - 3600,))
                all_calls, own_calls = db.execute(
                    "SELECT COUNT(*), COUNT(CASE WHEN session_id = ? THEN 1 END) FROM model_calls",
                    (session["id"],),
                ).fetchone()
                if all_calls >= 120 or own_calls >= 25:
                    raise OrderError(429, "rate_limited", "Лимит сообщений исчерпан. Попробуйте позже.")
                db.execute("INSERT INTO model_calls VALUES (?, ?, ?)", (lease, session["id"], now))
            db.execute(
                """INSERT INTO messages(id, order_id, request_key, base_version, text, lease, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(order_id, request_key)
                   DO UPDATE SET lease = excluded.lease, started_at = excluded.started_at""",
                (message_id, order_id, key, version, message, lease, now),
            )
            quantities = json.loads(row["quantities"])

        try:
            context = [(r["text"][:300], json.loads(r["result"])["reply"][:250]) for r in reversed(recent)]
            proposal = self.agent.propose(catalog, quantities, context, message)
            outcome, reply = proposal.outcome, proposal.reply

            with self.store.transaction() as db:
                row = lookup_order(db, session, order_id)
                check_version(row, version)
                self.ensure_editable(row)
                claim = db.execute("SELECT lease FROM messages WHERE id = ?", (message_id,)).fetchone()
                if claim is None or claim["lease"] != lease:
                    raise OrderError(409, "request_in_progress", "Сообщение ещё обрабатывается.")
                if outcome == "order_updated":
                    current_catalog = self.catalog_for(db, session["merchant"])
                    if current_catalog != catalog:
                        outcome, reply = "clarification", "Прайс обновился. Повторите запрос по актуальным условиям."
                    elif not proposal.quantities.keys() <= catalog.products.keys():
                        outcome, reply = "clarification", "Какие позиции из нашего каталога вам нужны?"
                    else:
                        try:
                            quote_for(db, row, catalog, proposal.quantities)
                        except ValueError as error:
                            outcome, reply = "clarification", str(error)
                        else:
                            row = lookup_order(db, session, order_id)
                if outcome == "clarification" and len(recent) == 2 and all(
                    json.loads(item["result"])["outcome"] == "clarification" for item in recent
                ):
                    outcome, reply = "handoff", "Потребуется уточнение у продавца. Покажите ему номер заказа."
                result = {
                    "message_id": message_id, "outcome": outcome, "reply": reply, "order": public_order(row),
                }
                db.execute("UPDATE messages SET result = ? WHERE id = ?", (json.dumps(result), message_id))
                return result
        except (httpx.HTTPError, ValidationError, KeyError, IndexError, ValueError, TypeError) as error:
            raise OrderError(503, "agent_unavailable", "Ответ пока недоступен. Повторите сообщение.") from error
        finally:
            with self.store.transaction() as db:
                db.execute("DELETE FROM messages WHERE id = ? AND lease = ? AND result IS NULL", (message_id, lease))

    @staticmethod
    def ensure_editable(row):
        if row["state"] not in {"draft", "agreed"}:
            raise OrderError(409, "order_locked", "Этот заказ уже подтверждён для оплаты.")
