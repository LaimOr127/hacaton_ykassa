import json
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .agent import ModelAgent, ModelFailure, ModelNotSent
from .diagnostics import clear_failure, record_failure
from .catalog import Catalog
from .checkout import checkout_plan, public_plan
from .storage import Store

MESSAGE_LEASE_SECONDS = 30


class OrderError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status = status
        self.body = {"code": code, "message": message, **details}


def lookup_order(db, session, order_id):
    owner = session_owner(db, session)
    row = db.execute(
        """SELECT o.* FROM orders o LEFT JOIN shopper_owners s ON s.session_id = o.session_id
           WHERE o.id = ? AND o.merchant = ? AND
           ((? IS NULL AND s.owner_id IS NULL AND o.session_id = ?) OR s.owner_id = ?)""",
        (order_id, session["merchant"], owner, session["id"], owner),
    ).fetchone()
    if row is None:
        raise OrderError(404, "order_not_found", "Заказ не найден.")
    return row


def session_owner(db, session):
    owner = session["owner_id"] if "owner_id" in session.keys() else None
    linked = db.execute("SELECT owner_id FROM shopper_owners WHERE session_id = ?", (session["id"],)).fetchone()
    if (linked["owner_id"] if linked else None) != owner:
        raise OrderError(401, "session_required", "Войдите в учётную запись владельца заказа.")
    if "auth_session_id" in session.keys() and session["auth_session_id"]:
        active = db.execute(
            "SELECT account_id FROM account_sessions WHERE id = ? AND expires_at > ?",
            (session["auth_session_id"], int(time.time())),
        ).fetchone()
        if not active or "local:" + active["account_id"] != owner:
            raise OrderError(401, "session_required", "Сессия входа истекла.")
    return owner


def public_order(db, row):
    data = {
        "id": row["id"],
        "version": row["version"],
        "status": row["state"],
        "agreed_version": row["agreed_version"],
        "payment_attempt_id": row["attempt_id"],
        **json.loads(row["quote"]),
    }
    refunds = db.execute(
        """SELECT COALESCE(SUM(r.amount_kopecks), 0) FROM payment_refunds r
           JOIN attempts a ON a.id = r.attempt_id WHERE a.order_id = ?""", (row["id"],),
    ).fetchone()[0]
    if refunds:
        data["refunded_kopecks"] = refunds
    review = db.execute("SELECT reply FROM seller_reviews WHERE order_id = ? AND closed_at IS NULL", (row["id"],)).fetchone()
    plan = checkout_plan(db, row["id"])
    if plan:
        data["checkout"] = public_plan(plan)
    if review:
        data["seller_review"] = {"status": "answered", "reply": review["reply"]} if review["reply"] else {"status": "pending"}
    if db.execute("SELECT 1 FROM fulfilled_orders WHERE order_id = ?", (row["id"],)).fetchone():
        data["fulfilled"] = True
    return data


def seller_transcript(db, order_id):
    rows = db.execute(
        """SELECT m.text, m.result, r.reply FROM messages m LEFT JOIN seller_reviews r
           ON r.opened_message = m.id AND r.closed_at IS NOT NULL
           WHERE m.order_id = ? AND m.result IS NOT NULL ORDER BY m.rowid DESC LIMIT 50""",
        (order_id,),
    ).fetchall()
    return [(row["text"], json.loads(row["result"])["reply"], row["reply"]) for row in reversed(rows)]


def checkout_review(db, order_id):
    if db.execute("SELECT 1 FROM seller_reviews WHERE order_id = ? AND closed_at IS NULL", (order_id,)).fetchone():
        raise OrderError(409, "seller_review_required", "Уточните заказ с продавцом перед оплатой.")


def reopen_draft(db, order_id):
    db.execute(
        """UPDATE orders SET version = version + 1, state = 'draft',
           agreed_version = NULL, attempt_id = NULL WHERE id = ?""",
        (order_id,),
    )


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
    def __init__(self, store: Store, catalog_for, agent, settings):
        self.store = store
        self.catalog_for = catalog_for
        self.agent = agent
        self.settings = settings

    def create(self, session, key: str):
        with self.store.transaction() as db:
            session_owner(db, session)
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
            return public_order(db, lookup_order(db, session, order_id))

    def transcript(self, session, order_id):
        with self.store.read() as db:
            lookup_order(db, session, order_id)
            return seller_transcript(db, order_id)

    @staticmethod
    def ensure_can_message(db, row):
        Orders.ensure_editable(row)
        if db.execute(
            "SELECT 1 FROM seller_reviews WHERE order_id = ? AND reply IS NULL AND closed_at IS NULL", (row["id"],),
        ).fetchone():
            raise OrderError(409, "seller_review_required", "Заказ у продавца. Дождитесь его ответа.")

    def reserve_model_call(self, session, message_id, lease):
        now = int(time.time())
        with self.store.transaction() as db:
            claim = db.execute("SELECT lease FROM messages WHERE id = ?", (message_id,)).fetchone()
            if claim is None or claim["lease"] != lease:
                raise OrderError(409, "request_in_progress", "Сообщение ещё обрабатывается.")
            db.execute("DELETE FROM model_calls WHERE started_at < ?", (now - 3600,))
            all_calls, own_calls, shop_calls = db.execute(
                """SELECT COUNT(*), COUNT(CASE WHEN c.session_id = ? THEN 1 END),
                   COUNT(CASE WHEN s.merchant = ? THEN 1 END)
                   FROM model_calls c JOIN sessions s ON s.id = c.session_id""",
                (session["id"], session["merchant"]),
            ).fetchone()
            if (
                all_calls >= self.settings.model_instance_limit or shop_calls >= self.settings.model_merchant_limit
                or own_calls >= self.settings.model_session_limit
            ):
                raise OrderError(429, "rate_limited", "Лимит сообщений исчерпан. Попробуйте позже.")
            db.execute("INSERT INTO model_calls VALUES (?, ?, ?)", (lease, session["id"], now))

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
                if now - previous["started_at"] < MESSAGE_LEASE_SECONDS:
                    raise OrderError(409, "request_in_progress", "Сообщение ещё обрабатывается.")
            check_version(row, version)
            self.ensure_can_message(db, row)
            catalog = self.catalog_for(db, session["merchant"])
            recent = db.execute(
                "SELECT text, result FROM messages WHERE order_id = ? AND result IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 6",
                (order_id,),
            ).fetchall()
            message_id = previous["id"] if previous else uuid4().hex
            db.execute(
                """INSERT INTO messages(id, order_id, request_key, base_version, text, lease, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(order_id, request_key)
                   DO UPDATE SET lease = excluded.lease, started_at = excluded.started_at""",
                (message_id, order_id, key, version, message, lease, now),
            )
            quantities = json.loads(row["quantities"])
            seller = db.execute("SELECT reply FROM seller_reviews WHERE order_id = ? AND closed_at IS NULL", (order_id,)).fetchone()

        reserved = False
        not_sent = False

        def reserve_quota():
            nonlocal reserved
            self.reserve_model_call(session, message_id, lease)
            reserved = True

        try:
            context = [(r["text"][:300], json.loads(r["result"])["reply"][:250]) for r in reversed(recent)]
            if seller and seller["reply"]:
                context.append(("", f"Ответ продавца: {seller['reply'][:400]}"))
            if isinstance(self.agent, ModelAgent):
                proposal = self.agent.propose(catalog, quantities, context, message, before_send=reserve_quota)
                clear_failure(session["merchant"], "llm")
            else:
                proposal = self.agent.propose(catalog, quantities, context, message)
            outcome, reply = proposal.outcome, proposal.reply

            with self.store.transaction() as db:
                row = lookup_order(db, session, order_id)
                check_version(row, version)
                self.ensure_can_message(db, row)
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
                            changed = quote_for(db, row, catalog, proposal.quantities)
                        except ValueError as error:
                            outcome, reply = "clarification", str(error)
                        else:
                            reviewed = db.execute(
                                "UPDATE seller_reviews SET closed_at = ? WHERE order_id = ? AND closed_at IS NULL",
                                (now, order_id),
                            ).rowcount
                            if reviewed and not changed:
                                reopen_draft(db, order_id)
                            row = lookup_order(db, session, order_id)
                if outcome == "clarification" and len(recent) >= 2 and all(
                    json.loads(item["result"])["outcome"] == "clarification" for item in recent[:2]
                ):
                    outcome = "handoff"
                if outcome == "handoff":
                    db.execute(
                        "UPDATE seller_reviews SET closed_at = ? WHERE order_id = ? AND closed_at IS NULL",
                        (now, order_id),
                    )
                    db.execute(
                        "INSERT INTO seller_reviews(order_id, opened_message, opened_at) VALUES (?, ?, ?)",
                        (order_id, message_id, now),
                    )
                    reopen_draft(db, order_id)
                    row = lookup_order(db, session, order_id)
                    reply = "Передал запрос продавцу. Ответ появится в карточке заказа."
                result = {
                    "message_id": message_id, "outcome": outcome, "reply": reply, "order": public_order(db, row),
                }
                db.execute("UPDATE messages SET result = ? WHERE id = ?", (json.dumps(result), message_id))
                return result
        except ModelNotSent as error:
            not_sent = True
            record_failure(session["merchant"], "llm", "message", "model_not_sent", error.__cause__)
            raise OrderError(503, "agent_unavailable", str(error)) from error
        except ModelFailure as error:
            not_sent = error.rejected
            record_failure(session["merchant"], "llm", "message", error.code)
            raise OrderError(503, "agent_unavailable", str(error)) from error
        except (httpx.HTTPError, ValidationError, KeyError, IndexError, ValueError, TypeError) as error:
            record_failure(session["merchant"], "llm", "message", "model_request_error", error)
            raise OrderError(503, "agent_unavailable", "Ответ пока недоступен. Повторите сообщение.") from error
        finally:
            with self.store.transaction() as db:
                if reserved and not_sent:
                    db.execute("DELETE FROM model_calls WHERE call_id = ?", (lease,))
                db.execute("DELETE FROM messages WHERE id = ? AND lease = ? AND result IS NULL", (message_id, lease))

    @staticmethod
    def ensure_editable(row):
        if row["state"] not in {"draft", "agreed"}:
            raise OrderError(409, "order_locked", "Этот заказ уже подтверждён для оплаты.")
