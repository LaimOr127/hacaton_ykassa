import json
import time
from uuid import uuid4

from .gateway import InvalidPaymentResponse, PaymentNotSent, trusted_checkout
from .orders import OrderError, check_version, lookup_order, quote_for
from .storage import Store


def public_attempt(row):
    result = {
        "id": row["id"],
        "order_id": row["order_id"],
        "order_version": row["order_version"],
        "amount_kopecks": json.loads(row["snapshot"])["payable_kopecks"],
        "currency": "RUB",
        "status": row["state"],
    }
    if row["state"] == "pending" and row["confirmation_url"]:
        result["confirmation_url"] = row["confirmation_url"]
    return result


class Payments:
    def __init__(self, store: Store, catalog_for, gateway_for):
        self.store = store
        self.catalog_for = catalog_for
        self.gateway_for = gateway_for

    def reserve(self, session, order_id, key, version, connected):
        with self.store.transaction() as db:
            order = lookup_order(db, session, order_id)
            old = db.execute(
                "SELECT * FROM attempts WHERE order_id = ? AND request_key = ?", (order_id, key),
            ).fetchone()
            if old:
                if old["order_version"] != version:
                    raise OrderError(409, "idempotency_conflict", "Ключ оплаты уже использован.")
                return old, False
            check_version(order, version)
            if order["state"] == "payment_pending":
                raise OrderError(409, "payment_in_progress", "У заказа уже есть активная попытка.")
            if order["state"] not in {"draft", "agreed"}:
                raise OrderError(409, "order_not_payable", "Заказ уже оплачен.")
            quantities = json.loads(order["quantities"])
            catalog = self.catalog_for(db, session["merchant"])
            if not quantities.keys() <= catalog.products.keys():
                raise OrderError(409, "order_not_payable", "Некоторые позиции больше недоступны.")
            try:
                repriced = quote_for(db, order, catalog, quantities)
            except ValueError as error:
                raise OrderError(409, "order_not_payable", str(error)) from error
            if repriced:
                return None, False
            if json.loads(order["quote"])["payable_kopecks"] <= 0:
                raise OrderError(409, "order_not_payable", "Сначала добавьте позиции в заказ.")
            if not connected:
                raise OrderError(503, "payment_unavailable", "Продавец ещё не подключил оплату.")
            attempt_id = uuid4().hex
            db.execute(
                """INSERT INTO attempts(id, order_id, request_key, order_version, snapshot, state, created_at)
                   VALUES (?, ?, ?, ?, ?, 'creating', ?)""",
                (attempt_id, order_id, key, version, order["quote"], int(time.time())),
            )
            db.execute(
                "UPDATE orders SET state = 'payment_pending', agreed_version = ?, attempt_id = ? WHERE id = ?",
                (version, attempt_id, order_id),
            )
            return db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone(), True

    def create(self, session, order_id, key, version):
        gateway = self.gateway_for(session["merchant"])
        attempt, created = self.reserve(session, order_id, key, version, gateway is not None)
        if attempt is None:
            raise OrderError(409, "stale_version", "Цена изменилась. Подтвердите новый итог.", current_version=version + 1)
        if not created:
            return public_attempt(attempt), False
        try:
            payment = gateway.create(public_attempt(attempt)["amount_kopecks"], attempt["id"])
        except PaymentNotSent:
            self.mark_failed(attempt)
        except InvalidPaymentResponse as error:
            self.mark_unknown(attempt["id"], error.external_id)
        except Exception:
            # Запрос мог дойти до провайдера до обрыва соединения.
            self.mark_unknown(attempt["id"])
        else:
            self.record(attempt, payment, verified=False)
        with self.store.read() as db:
            return public_attempt(self.lookup(db, session, order_id, attempt["id"])), True

    def get(self, session, order_id, attempt_id):
        now = int(time.time())
        with self.store.transaction() as db:
            attempt = self.lookup(db, session, order_id, attempt_id)
            if attempt["state"] == "creating" and attempt["created_at"] + 30 <= now:
                db.execute("UPDATE attempts SET state = 'unknown' WHERE id = ?", (attempt_id,))
                return public_attempt(self.lookup(db, session, order_id, attempt_id))
            needs_check = (
                attempt["state"] in {"pending", "unknown"} and attempt["external_id"] is not None
                and attempt["next_check"] <= now
            )
            if needs_check:
                db.execute("UPDATE attempts SET next_check = ? WHERE id = ?", (now + 20, attempt_id))
        if needs_check:
            gateway = self.gateway_for(session["merchant"])
            if gateway is None:
                return public_attempt(attempt)
            try:
                payment = gateway.get(attempt["external_id"])
            except InvalidPaymentResponse:
                self.mark_unknown(attempt_id)
            except Exception:
                pass
            else:
                self.record(attempt, payment, verified=True)
            with self.store.read() as db:
                attempt = self.lookup(db, session, order_id, attempt_id)
        return public_attempt(attempt)

    def record(self, attempt, payment, *, verified):
        snapshot = json.loads(attempt["snapshot"])
        if payment.amount.currency != "RUB" or payment.kopecks != snapshot["payable_kopecks"] or (
            attempt["external_id"] and payment.id != attempt["external_id"]
        ):
            self.mark_unknown(attempt["id"], None if verified else payment.id)
            return
        try:
            checkout = trusted_checkout(payment.confirmation_url or attempt["confirmation_url"])
        except ValueError:
            checkout = None
        if self.store.mode == "live" and payment.status == "pending" and checkout is None:
            # Известный платёж можно переопрашивать, но выдавать непроверенную ссылку нельзя.
            local_status = "unknown"
        elif not verified and payment.status != "pending":
            local_status = "pending"
            checkout = None
        else:
            local_status = payment.status
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM attempts WHERE external_id = ? AND id != ?", (payment.id, attempt["id"])).fetchone():
                db.execute(
                    "UPDATE attempts SET state = 'unknown', confirmation_url = NULL WHERE id = ? AND state IN ('creating','pending','unknown')",
                    (attempt["id"],),
                )
                return
            changed = db.execute(
                """UPDATE attempts SET state = ?, external_id = ?, confirmation_url = ?, next_check = ?
                   WHERE id = ? AND state IN ('creating', 'pending', 'unknown')""",
                (local_status, payment.id, checkout, int(time.time()) + (2 if verified else 0), attempt["id"]),
            ).rowcount
            if changed and local_status in {"succeeded", "canceled"}:
                order_state = "agreed"
                if local_status == "succeeded":
                    order_state = "deposit_received" if snapshot["balance_kopecks"] else "paid"
                db.execute(
                    "UPDATE orders SET state = ? WHERE id = ? AND version = ? AND attempt_id = ?",
                    (order_state, attempt["order_id"], attempt["order_version"], attempt["id"]),
                )

    def mark_unknown(self, attempt_id, external_id=None):
        with self.store.transaction() as db:
            if external_id and db.execute("SELECT 1 FROM attempts WHERE external_id = ?", (external_id,)).fetchone():
                external_id = None
            db.execute(
                """UPDATE attempts SET state = 'unknown', confirmation_url = NULL,
                   external_id = COALESCE(external_id, ?)
                   WHERE id = ? AND state IN ('creating','pending','unknown')""",
                (external_id, attempt_id),
            )

    def mark_failed(self, attempt):
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE attempts SET state = 'failed' WHERE id = ? AND state = 'creating'", (attempt["id"],),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE orders SET state = 'agreed' WHERE id = ? AND attempt_id = ?",
                    (attempt["order_id"], attempt["id"]),
                )

    @staticmethod
    def lookup(db, session, order_id, attempt_id):
        lookup_order(db, session, order_id)
        row = db.execute("SELECT * FROM attempts WHERE id = ? AND order_id = ?", (attempt_id, order_id)).fetchone()
        if row is None:
            raise OrderError(404, "payment_not_found", "Платёжная попытка не найдена.")
        return row

    def simulate(self, session, order_id, attempt_id):
        with self.store.read() as db:
            row = self.lookup(db, session, order_id, attempt_id)
        if row["state"] != "pending":
            raise OrderError(409, "order_not_payable", "Оплачивать можно ожидающий платёж.")
        gateway = self.gateway_for(session["merchant"])
        gateway.approve(row["external_id"])
        self.record(row, gateway.get(row["external_id"]), verified=True)
        return self.get(session, order_id, attempt_id)
