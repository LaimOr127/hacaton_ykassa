import json
import re
import time
from uuid import uuid4

from .checkout import checkout_flags, checkout_plan, portion
from .gateway import INVOICE_LIFETIME, InvalidPaymentResponse, PaymentNotSent, payment_reference, trusted_checkout, utc_iso
from .orders import MESSAGE_LEASE_SECONDS, OrderError, check_version, checkout_review, lookup_order, quote_for
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
    shareable = row["state"] == "pending" and row["confirmation_url"]
    if row["kind"] == "invoice":
        result["kind"] = "invoice"
        expires_at = row["created_at"] + INVOICE_LIFETIME
        if row["state"] == "pending":
            result["expires_at"] = utc_iso(expires_at)
        shareable = shareable and time.time() < expires_at
    if row["part_number"] is not None:
        result["part_number"] = row["part_number"]
    if shareable:
        result["confirmation_url"] = row["confirmation_url"]
    return result


class Payments:
    def __init__(self, store: Store, catalog_for, gateway_for):
        self.store = store
        self.catalog_for = catalog_for
        self.gateway_for = gateway_for

    def reserve(self, session, order_id, key, version, kind, allow_invoice, parts, tip_percent, part_number):
        with self.store.transaction() as db:
            order = lookup_order(db, session, order_id)
            old = db.execute(
                "SELECT * FROM attempts WHERE order_id = ? AND request_key = ?", (order_id, key),
            ).fetchone()
            if old:
                snapshot = json.loads(old["snapshot"])
                if (
                    old["order_version"] != version or old["kind"] != kind
                    or (snapshot.get("parts", 1), snapshot.get("tip_percent", 0), old["part_number"] or 1)
                    != (parts, tip_percent, part_number)
                ):
                    raise OrderError(409, "idempotency_conflict", "Ключ оплаты уже использован.")
                return old, None
            check_version(order, version)
            checkout_review(db, order_id)
            if db.execute(
                "SELECT 1 FROM messages WHERE order_id = ? AND result IS NULL AND started_at > ? LIMIT 1",
                (order_id, int(time.time()) - MESSAGE_LEASE_SECONDS),
            ).fetchone():
                raise OrderError(409, "request_in_progress", "Дождитесь обработки сообщения перед оплатой.")
            plan = checkout_plan(db, order_id)
            if order["state"] == "payment_pending" and (
                plan is None or db.execute(
                    "SELECT 1 FROM attempts WHERE order_id = ? AND state IN ('creating','pending','unknown')", (order_id,),
                ).fetchone()
            ):
                raise OrderError(409, "payment_in_progress", "У заказа уже есть активная попытка.")
            if order["state"] not in {"draft", "agreed", "payment_pending"}:
                raise OrderError(409, "order_not_payable", "Заказ уже оплачен.")
            snapshot = json.loads(order["quote"])
            if plan:
                if (
                    kind != "payment" or parts != plan["parts"] or tip_percent != plan["tip_percent"]
                    or part_number != plan["paid_parts"] + 1
                ):
                    raise OrderError(409, "checkout_changed", "Сверьте текущую долю и сумму оплаты.")
            else:
                quantities = json.loads(order["quantities"])
                catalog = self.catalog_for(db, session["merchant"])
                if not quantities.keys() <= catalog.products.keys():
                    raise OrderError(409, "order_not_payable", "Некоторые позиции больше недоступны.")
                try:
                    if quote_for(db, order, catalog, quantities):
                        return None, None
                except ValueError as error:
                    raise OrderError(409, "order_not_payable", str(error)) from error
            if snapshot["payable_kopecks"] <= 0:
                raise OrderError(409, "order_not_payable", "Сначала добавьте позиции в заказ.")
            if kind == "invoice" and not allow_invoice:
                raise OrderError(409, "invoice_unavailable", "Выставление счёта для этой витрины недоступно.")
            gateway = self.gateway_for(session["merchant"], db)
            if gateway is None:
                raise OrderError(503, "payment_unavailable", "Продавец ещё не подключил оплату.")
            if not plan:
                self.start_plan(db, session["merchant"], order_id, snapshot, kind, parts, tip_percent, part_number)
                plan = checkout_plan(db, order_id)
            if plan:
                snapshot.update(parts=parts, tip_percent=tip_percent,
                                payable_kopecks=portion(plan["total_kopecks"], parts, part_number - 1))
            attempt_id = uuid4().hex
            db.execute(
                """INSERT INTO attempts(id, order_id, request_key, order_version, snapshot, state, created_at, kind, part_number)
                   VALUES (?, ?, ?, ?, ?, 'creating', ?, ?, ?)""",
                (attempt_id, order_id, key, version, json.dumps(snapshot), int(time.time()), kind, part_number if plan else None),
            )
            db.execute(
                "UPDATE orders SET state = 'payment_pending', agreed_version = ?, attempt_id = ? WHERE id = ?",
                (version, attempt_id, order_id),
            )
            return db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone(), gateway

    @staticmethod
    def start_plan(db, merchant, order_id, snapshot, kind, parts, tip_percent, part_number):
        if part_number != 1:
            raise OrderError(409, "checkout_changed", "Начните оплату с первой доли.")
        if parts == 1 and not tip_percent:
            return
        flags = checkout_flags(db, merchant)
        if kind != "payment" or (parts > 1 and not flags["split_payments"]) or (tip_percent and not flags["tips"]):
            raise OrderError(409, "checkout_option_unavailable", "Разделение оплаты и чаевые доступны при прямой оплате, если их включил магазин.")
        if tip_percent not in {0, 5, 10, 15}:
            raise OrderError(400, "invalid_request", "Выберите 0, 5, 10 или 15 процентов чаевых.")
        payable = snapshot["payable_kopecks"]
        tip = (payable * tip_percent + 50) // 100
        if (payable + tip) // parts < 100:
            raise OrderError(409, "checkout_option_unavailable", "Сумма каждой доли должна быть не меньше 1 ₽.")
        db.execute(
            "INSERT INTO checkout_plans(order_id, parts, tip_percent, tip_kopecks, total_kopecks) VALUES (?, ?, ?, ?, ?)",
            (order_id, parts, tip_percent, tip, payable + tip),
        )

    def create(
        self, session, order_id, key, version, kind="payment", *, allow_invoice=False, parts=1, tip_percent=0, part_number=1,
    ):
        attempt, gateway = self.reserve(session, order_id, key, version, kind, allow_invoice, parts, tip_percent, part_number)
        if attempt is None:
            raise OrderError(409, "stale_version", "Цена изменилась. Подтвердите новый итог.", current_version=version + 1)
        if gateway is None:
            return public_attempt(attempt), False
        try:
            if kind == "invoice":
                payment = gateway.create_invoice(json.loads(attempt["snapshot"]), attempt["id"], order_id, attempt["created_at"])
            else:
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
            target = attempt["external_id"] or attempt["candidate_id"]
            needs_check = (
                attempt["state"] in {"pending", "unknown"} and target is not None
                and attempt["next_check"] <= now
            )
            if needs_check:
                db.execute("UPDATE attempts SET next_check = ? WHERE id = ?", (now + 20, attempt_id))
        if needs_check:
            gateway = self.gateway_for(session["merchant"])
            if gateway is None:
                return public_attempt(attempt)
            try:
                payment = self.read_external(gateway, attempt, target)
                if payment.id != target:
                    raise InvalidPaymentResponse()
            except InvalidPaymentResponse:
                self.mark_unknown(attempt_id)
            except Exception:
                pass
            else:
                self.record(attempt, payment, verified=True)
            with self.store.read() as db:
                attempt = self.lookup(db, session, order_id, attempt_id)
        return public_attempt(attempt)

    @staticmethod
    def read_external(gateway, attempt, external_id):
        if attempt["kind"] == "payment":
            return gateway.get(external_id)
        invoice = gateway.get_invoice(external_id)
        if (
            invoice.id != external_id or invoice.description != payment_reference(attempt["id"])
            or invoice.amount.currency != "RUB" or invoice.kopecks != public_attempt(attempt)["amount_kopecks"]
        ):
            raise InvalidPaymentResponse()
        if invoice.status == "succeeded":
            linked = invoice.payment_details
            if not linked or linked.status != "succeeded" or linked.id == invoice.id:
                raise InvalidPaymentResponse()
            try:
                payment = gateway.get(linked.id)
            except Exception as error:
                raise InvalidPaymentResponse() from error
            if payment.id != linked.id or payment.status != "succeeded" or payment.amount != invoice.amount:
                raise InvalidPaymentResponse()
        return invoice

    def record(self, attempt, payment, *, verified):
        snapshot = json.loads(attempt["snapshot"])
        reference = payment_reference(attempt["id"])
        wrong_reference = payment.description is not None and payment.description != reference
        unconfirmed_reference = verified and (
            attempt["state"] == "unknown" or attempt["external_id"] is None
        ) and payment.description != reference
        invoice = attempt["kind"] == "invoice"
        invalid_invoice = invoice and (
            payment.description != reference
            or (payment.status == "pending" and (
                payment.expires_at is None or payment.expires_at.tzinfo is None
                or int(payment.expires_at.timestamp()) != attempt["created_at"] + INVOICE_LIFETIME
            ))
        )
        if payment.amount.currency != "RUB" or payment.kopecks != snapshot["payable_kopecks"] or (
            attempt["external_id"] and payment.id != attempt["external_id"]
        ) or wrong_reference or unconfirmed_reference or invalid_invoice:
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
        linked = payment.payment_details.id if invoice and verified and local_status == "succeeded" else None
        with self.store.transaction() as db:
            if db.execute(
                """SELECT 1 FROM attempts WHERE id != ? AND
                   (external_id IN (?, ?) OR settled_payment_id IN (?, ?))""",
                (attempt["id"], payment.id, linked, payment.id, linked),
            ).fetchone():
                db.execute(
                    "UPDATE attempts SET state = 'unknown', confirmation_url = NULL WHERE id = ? AND state IN ('creating','pending','unknown')",
                    (attempt["id"],),
                )
                return
            changed = db.execute(
                """UPDATE attempts SET state = ?, external_id = ?, confirmation_url = ?, next_check = ?,
                   settled_payment_id = ?, candidate_id = NULL, verified_at = ?
                   WHERE id = ? AND state IN ('creating', 'pending', 'unknown')
                   AND (external_id IS NULL OR external_id = ?)""",
                (local_status, payment.id, checkout, int(time.time()) + (2 if verified else 0), linked,
                 int(time.time()) if local_status == "succeeded" else None, attempt["id"], payment.id),
            ).rowcount
            if changed and local_status in {"succeeded", "canceled"}:
                self.finish_attempt(db, attempt, local_status == "succeeded")

    @staticmethod
    def finish_attempt(db, attempt, paid):
        plan = checkout_plan(db, attempt["order_id"]) if attempt["part_number"] is not None else None
        if plan and paid:
            if plan["paid_parts"] + 1 != attempt["part_number"]:
                raise RuntimeError("Подтверждение доли вне последовательности")
            db.execute("UPDATE checkout_plans SET paid_parts = paid_parts + 1 WHERE order_id = ?", (attempt["order_id"],))
            if attempt["part_number"] < plan["parts"]:
                return
        elif plan:
            if plan["paid_parts"]:
                return
            db.execute("DELETE FROM checkout_plans WHERE order_id = ?", (attempt["order_id"],))
        order_state = "agreed"
        if paid:
            order_state = "deposit_received" if json.loads(attempt["snapshot"])["balance_kopecks"] else "paid"
        db.execute(
            "UPDATE orders SET state = ? WHERE id = ? AND version = ? AND attempt_id = ?",
            (order_state, attempt["order_id"], attempt["order_version"], attempt["id"]),
        )

    def mark_unknown(self, attempt_id, external_id=None):
        with self.store.transaction() as db:
            if external_id and db.execute(
                "SELECT 1 FROM attempts WHERE external_id = ? OR settled_payment_id = ?", (external_id, external_id),
            ).fetchone():
                external_id = None
            db.execute(
                """UPDATE attempts SET state = 'unknown', confirmation_url = NULL,
                   candidate_id = CASE WHEN external_id IS NULL THEN COALESCE(candidate_id, ?) END
                   WHERE id = ? AND state IN ('creating','pending','unknown')""",
                (external_id, attempt_id),
            )

    def mark_failed(self, attempt):
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE attempts SET state = 'failed' WHERE id = ? AND state = 'creating'", (attempt["id"],),
            ).rowcount
            if changed:
                self.finish_attempt(db, attempt, False)

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

    def reconcile(self, merchant, attempt_id: str, supplied_id: str):
        with self.store.transaction() as db:
            attempt = db.execute(
                """SELECT a.* FROM attempts a JOIN orders o ON o.id = a.order_id
                   WHERE a.id = ? AND o.merchant = ? AND o.attempt_id = a.id""",
                (attempt_id, merchant),
            ).fetchone()
            if not attempt:
                raise OrderError(404, "payment_not_found", "Платёжная попытка не найдена.")
            if attempt["state"] == "creating" and attempt["created_at"] + 30 <= int(time.time()):
                db.execute("UPDATE attempts SET state = 'unknown' WHERE id = ?", (attempt_id,))
            elif attempt["state"] != "unknown":
                raise OrderError(409, "review_unavailable", "Эта попытка больше не требует сверки.")

        gateway = self.gateway_for(merchant)
        if gateway is None:
            raise OrderError(503, "payment_unavailable", "Сначала восстановите подключение ЮKassa.")
        external_id = attempt["external_id"] or supplied_id.strip() or attempt["candidate_id"] or ""
        if not external_id and attempt["kind"] == "payment":
            try:
                external_id = gateway.find_payment(attempt["created_at"], attempt_id, public_attempt(attempt)["amount_kopecks"])
            except Exception as error:
                raise OrderError(503, "payment_unavailable", "Список платежей ЮKassa недоступен для сверки.") from error
            if not external_id:
                raise OrderError(409, "payment_mismatch", "Однозначного совпадения нет. Укажите платёж из кабинета ЮKassa.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_id) or (
            attempt["external_id"] and supplied_id.strip() not in {"", external_id}
        ):
            raise OrderError(400, "invalid_payment_id", "Введите идентификатор платежа или счёта из кабинета ЮKassa.")
        try:
            payment = self.read_external(gateway, attempt, external_id)
        except Exception as error:
            raise OrderError(503, "payment_unavailable", "ЮKassa не подтвердила платёж. Попытка остаётся на сверке.") from error
        if (
            payment.id != external_id
            or payment.amount.currency != "RUB"
            or payment.kopecks != public_attempt(attempt)["amount_kopecks"]
            or payment.description != payment_reference(attempt_id)
        ):
            raise OrderError(409, "payment_mismatch", "Платёж не совпадает с номером попытки или суммой заказа.")
        self.record(attempt, payment, verified=True)
        with self.store.read() as db:
            resolved = db.execute("SELECT external_id, state FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if resolved["external_id"] != external_id:
            raise OrderError(409, "payment_mismatch", "Этот идентификатор связан с другой попыткой.")
        if resolved["state"] == "unknown":
            raise OrderError(409, "payment_mismatch", "Попытка остаётся на сверке. Проверьте реквизиты в ЮKassa.")

    def refunds(self, merchant, order_id, refund_id):
        with self.store.read() as db:
            attempts = db.execute(
                """SELECT a.external_id, a.settled_payment_id, a.kind FROM attempts a
                   JOIN orders o ON o.id = a.order_id
                   WHERE o.id = ? AND o.merchant = ? AND a.state = 'succeeded'""", (order_id, merchant),
            ).fetchall()
        if not attempts:
            raise OrderError(404, "payment_not_found", "Подтверждённый платёж для заказа не найден.")
        payment_ids = {a["settled_payment_id"] if a["kind"] == "invoice" else a["external_id"] for a in attempts}
        gateway = self.gateway_for(merchant)
        if None in payment_ids or "" in payment_ids or gateway is None:
            raise OrderError(503, "payment_unavailable", "Для проверки возвратов нужно подключение ЮKassa.")
        try:
            refunds = {
                item.id: item for payment_id in sorted(payment_ids) for item in gateway.list_refunds(payment_id)
                if item.payment_id == payment_id and item.amount.currency == "RUB"
            }
            if refund_id:
                if refund_id not in refunds:
                    raise OrderError(404, "refund_not_found", "Возврат по этому заказу не найден.")
                selected = gateway.get_refund(refund_id)
                if selected.id != refund_id or selected.payment_id != refunds[refund_id].payment_id or selected.amount.currency != "RUB":
                    raise OrderError(404, "refund_not_found", "Возврат по этому заказу не найден.")
                return [selected]
            return list(refunds.values())
        except OrderError:
            raise
        except Exception as error:
            raise OrderError(503, "payment_unavailable", "ЮKassa не вернула сведения о возвратах. Повторите запрос.") from error
