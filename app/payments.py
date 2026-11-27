import json
import re
import time

import anyio
from uuid import uuid4

from .checkout import checkout_flags, checkout_plan, portion
from .diagnostics import clear_failure, record_failure
from .gateway import INVOICE_LIFETIME, InvalidPaymentResponse, McpBusy, PaymentNotSent, payment_reference, trusted_checkout, utc_iso
from .orders import MESSAGE_LEASE_SECONDS, OrderError, check_version, checkout_review, lookup_order, quote_for
from .storage import Store

RECONCILE_INTERVAL = 5


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
        deadline = row["created_at"] + INVOICE_LIFETIME
        expires_at = min(row["invoice_expires_at"] or deadline, deadline)
        if row["state"] == "pending":
            result["expires_at"] = utc_iso(expires_at)
        shareable = shareable and time.time() < expires_at
    if row["balance_payment"]:
        result["balance_payment"] = True
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

    def reserve(self, session, order_id, key, version, kind, allow_invoice, parts, tip_percent, part_number, balance_payment):
        with self.store.transaction() as db:
            order = lookup_order(db, session, order_id)
            old = db.execute(
                "SELECT * FROM attempts WHERE order_id = ? AND request_key = ?", (order_id, key),
            ).fetchone()
            if old:
                snapshot = json.loads(old["snapshot"])
                if (
                    old["order_version"] != version or old["kind"] != kind or bool(old["balance_payment"]) != balance_payment
                    or (snapshot.get("parts", 1), snapshot.get("tip_percent", 0), old["part_number"] or 1)
                    != (parts, tip_percent, part_number)
                ):
                    raise OrderError(409, "idempotency_conflict", "Ключ оплаты уже использован.")
                return old, None
            check_version(order, version)
            if db.execute(
                """SELECT 1 FROM payment_refunds r JOIN attempts a ON a.id = r.attempt_id
                   WHERE a.order_id = ? LIMIT 1""", (order_id,),
            ).fetchone():
                raise OrderError(409, "payment_refunded", "По заказу есть возврат. Уточните дальнейшую оплату у продавца.")
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
            snapshot = json.loads(order["quote"])
            if balance_payment:
                if (order["state"] != "deposit_received" or not snapshot["balance_kopecks"]
                    or (kind, parts, tip_percent, part_number) != ("payment", 1, 0, 1)):
                    raise OrderError(409, "order_not_payable", "Доплата доступна после предоплаты одним прямым платежом.")
                snapshot.update(payable_kopecks=snapshot["balance_kopecks"], balance_kopecks=0)
                plan = None
            elif order["state"] not in {"draft", "agreed", "payment_pending"}:
                raise OrderError(409, "order_not_payable", "Заказ уже оплачен.")
            if plan:
                if (
                    kind != "payment" or parts != plan["parts"] or tip_percent != plan["tip_percent"]
                    or part_number != plan["paid_parts"] + 1
                ):
                    raise OrderError(409, "checkout_changed", "Сверьте текущую долю и сумму оплаты.")
            elif not balance_payment:
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
            if not plan and not balance_payment:
                self.start_plan(db, session["merchant"], order_id, snapshot, kind, parts, tip_percent, part_number)
                plan = checkout_plan(db, order_id)
            if plan:
                snapshot.update(parts=parts, tip_percent=tip_percent,
                                payable_kopecks=portion(plan["total_kopecks"], parts, part_number - 1))
            attempt_id = uuid4().hex
            db.execute(
                """INSERT INTO attempts(id, order_id, request_key, order_version, snapshot, state, created_at,
                                        kind, part_number, balance_payment)
                   VALUES (?, ?, ?, ?, ?, 'creating', ?, ?, ?, ?)""",
                (attempt_id, order_id, key, version, json.dumps(snapshot), int(time.time()), kind,
                 part_number if plan else None, balance_payment),
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
        self, session, order_id, key, version, kind="payment", *,
        allow_invoice=False, parts=1, tip_percent=0, part_number=1, balance_payment=False,
    ):
        attempt, gateway = self.reserve(
            session, order_id, key, version, kind, allow_invoice, parts, tip_percent, part_number, balance_payment,
        )
        if attempt is None:
            raise OrderError(409, "stale_version", "Цена изменилась. Подтвердите новый итог.", current_version=version + 1)
        if gateway is None:
            return public_attempt(attempt), False
        try:
            if kind == "invoice":
                payment = gateway.create_invoice(json.loads(attempt["snapshot"]), attempt["id"], order_id, attempt["created_at"])
            else:
                payment = gateway.create(public_attempt(attempt)["amount_kopecks"], attempt["id"])
        except PaymentNotSent as error:
            code = "payment_busy" if isinstance(error.__cause__, McpBusy) else "payment_not_sent"
            record_failure(session["merchant"], "mcp", "create", code, error.__cause__)
            self.mark_failed(attempt)
        except InvalidPaymentResponse as error:
            record_failure(session["merchant"], "mcp", "create", "payment_unknown", error)
            self.mark_unknown(attempt["id"], error.external_id)
        except Exception as error:
            record_failure(session["merchant"], "mcp", "create", "payment_unknown", error)
            self.mark_unknown(attempt["id"])
        else:
            if not self.record(attempt, payment, verified=False):
                record_failure(session["merchant"], "mcp", "create", "payment_unknown")
        with self.store.read() as db:
            return public_attempt(self.lookup(db, session, order_id, attempt["id"])), True

    def get(self, session, order_id, attempt_id):
        with self.store.read() as db:
            attempt = self.lookup(db, session, order_id, attempt_id)
        if not self.can_refresh(attempt, int(time.time())):
            return public_attempt(attempt)
        self.refresh(session["merchant"], attempt_id)
        with self.store.read() as db:
            return public_attempt(self.lookup(db, session, order_id, attempt_id))

    @staticmethod
    def can_refresh(attempt, now):
        if attempt["state"] == "creating":
            return attempt["created_at"] + 30 <= now
        return (
            attempt["state"] in {"pending", "unknown"} and attempt["next_check"] <= now
            and bool(attempt["external_id"] or attempt["candidate_id"])
        )

    def refresh(self, merchant, attempt_id, *, report_errors=False):
        now = int(time.time())
        with self.store.transaction() as db:
            attempt = db.execute(
                """SELECT a.* FROM attempts a JOIN orders o ON a.order_id = o.id
                   WHERE a.id = ? AND o.merchant = ?""", (attempt_id, merchant),
            ).fetchone()
            if attempt is None or not self.can_refresh(attempt, now):
                return
            if attempt["state"] == "creating":
                db.execute("UPDATE attempts SET state = 'unknown' WHERE id = ?", (attempt_id,))
                return
            lease = now + 45
            db.execute("UPDATE attempts SET next_check = ? WHERE id = ?", (lease, attempt_id))
        gateway = self.gateway_for(merchant)
        target = attempt["external_id"] or attempt["candidate_id"]
        try:
            if gateway is None:
                raise OrderError(503, "payment_unavailable", "Подключите ЮKassa для проверки оплаты.")
            payment = self.read_external(gateway, attempt, target)
            if payment.id != target:
                raise InvalidPaymentResponse()
        except InvalidPaymentResponse as error:
            self.mark_unknown(attempt_id)
            record_failure(merchant, "mcp", "check_status", "payment_status", error)
            if report_errors:
                raise OrderError(409, "payment_mismatch", "Ответ ЮKassa не совпал с попыткой. Откройте сверку.") from error
        except Exception as error:
            code = "payment_busy" if isinstance(error, McpBusy) else "payment_status"
            record_failure(merchant, "mcp", "check_status", code, error)
            with self.store.transaction() as db:
                db.execute("UPDATE attempts SET next_check = ? WHERE id = ? AND next_check = ?",
                           (int(time.time()) + (5 if report_errors else 10), attempt_id, lease))
            if report_errors:
                raise OrderError(503, "payment_unavailable", "ЮKassa не вернула статус. Проверьте подключение и повторите.") from error
        else:
            if self.record(attempt, payment, verified=True):
                clear_failure(merchant, "mcp")
            else:
                record_failure(merchant, "mcp", "check_status", "payment_status")
                if report_errors:
                    raise OrderError(409, "payment_mismatch", "Ответ ЮKassa не совпал с попыткой. Откройте сверку.")

    def due_attempts(self):
        now = int(time.time())
        with self.store.read() as db:
            return db.execute(
                """SELECT o.merchant, a.id FROM attempts a JOIN orders o ON a.id = o.attempt_id
                   WHERE (
                       a.state IN ('pending','unknown') AND a.next_check <= ?
                       AND COALESCE(a.external_id, a.candidate_id) IS NOT NULL
                   ) OR (a.state = 'creating' AND a.created_at <= ?)
                   ORDER BY a.next_check, a.created_at LIMIT 4""", (now, now - 30),
            ).fetchall()

    async def watch_pending(self):
        while True:
            await anyio.sleep(RECONCILE_INTERVAL)
            try:
                due = await anyio.to_thread.run_sync(self.due_attempts)
                async with anyio.create_task_group() as batch:
                    for attempt in due:
                        batch.start_soon(anyio.to_thread.run_sync, self.refresh, attempt["merchant"], attempt["id"])
            except Exception as error:
                record_failure("", "mcp", "watch_pending", "payment_worker", error)

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
        invoice_expiry = None
        if invoice and payment.expires_at and payment.expires_at.tzinfo is not None:
            invoice_expiry = int(payment.expires_at.timestamp())
        expected_expiry = attempt["created_at"] + INVOICE_LIFETIME
        invalid_invoice = invoice and (
            payment.description != reference
            or (payment.status == "pending" and (
                invoice_expiry is None or abs(invoice_expiry - expected_expiry) > 300
            ))
        )
        if payment.amount.currency != "RUB" or payment.kopecks != snapshot["payable_kopecks"] or (
            attempt["external_id"] and payment.id != attempt["external_id"]
        ) or wrong_reference or unconfirmed_reference or invalid_invoice:
            self.mark_unknown(attempt["id"], None if verified else payment.id)
            return False
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
                return False
            changed = db.execute(
                """UPDATE attempts SET state = ?, external_id = ?, confirmation_url = ?, next_check = ?,
                   settled_payment_id = ?, candidate_id = NULL, verified_at = ?,
                   invoice_expires_at = COALESCE(?, invoice_expires_at)
                   WHERE id = ? AND state IN ('creating', 'pending', 'unknown')
                   AND (external_id IS NULL OR external_id = ?)""",
                (local_status, payment.id, checkout, int(time.time()) + (2 if verified else 0), linked,
                 int(time.time()) if local_status == "succeeded" else None,
                 min(invoice_expiry, expected_expiry) if invoice_expiry is not None else None,
                 attempt["id"], payment.id),
            ).rowcount
            if changed and local_status in {"succeeded", "canceled"}:
                self.finish_attempt(db, attempt, local_status == "succeeded")
            return local_status != "unknown"

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
        order_state = "deposit_received" if attempt["balance_payment"] else "agreed"
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
            elif attempt["state"] not in {"pending", "unknown"}:
                raise OrderError(409, "review_unavailable", "Эта попытка больше не требует сверки.")

        if attempt["state"] == "pending":
            self.refresh(merchant, attempt_id, report_errors=True)
            return

        gateway = self.gateway_for(merchant)
        if gateway is None:
            raise OrderError(503, "payment_unavailable", "Сначала восстановите подключение ЮKassa.")
        external_id = attempt["external_id"] or supplied_id.strip() or attempt["candidate_id"] or ""
        if not external_id and attempt["kind"] == "payment":
            try:
                external_id = gateway.find_payment(attempt["created_at"], attempt_id, public_attempt(attempt)["amount_kopecks"])
            except Exception as error:
                record_failure(merchant, "mcp", "find_payment", "payment_status", error)
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
            record_failure(merchant, "mcp", "reconcile", "payment_status", error)
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
                """SELECT a.id, a.external_id, a.settled_payment_id, a.kind FROM attempts a
                   JOIN orders o ON o.id = a.order_id
                   WHERE o.id = ? AND o.merchant = ? AND a.state = 'succeeded'""", (order_id, merchant),
            ).fetchall()
        if not attempts:
            raise OrderError(404, "payment_not_found", "Подтверждённый платёж для заказа не найден.")
        payment_ids = {a["settled_payment_id"] if a["kind"] == "invoice" else a["external_id"]: a["id"] for a in attempts}
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
                refunds[refund_id] = selected
            with self.store.transaction() as db:
                for item in refunds.values():
                    if item.status != "succeeded" or item.kopecks <= 0:
                        continue
                    attempt_id = payment_ids[item.payment_id]
                    db.execute("INSERT OR IGNORE INTO payment_refunds VALUES (?, ?, ?)", (item.id, attempt_id, item.kopecks))
                    saved = db.execute("SELECT attempt_id, amount_kopecks FROM payment_refunds WHERE id = ?", (item.id,)).fetchone()
                    if (saved["attempt_id"], saved["amount_kopecks"]) != (attempt_id, item.kopecks):
                        raise InvalidPaymentResponse()
            clear_failure(merchant, "mcp")
            return [refunds[refund_id]] if refund_id else list(refunds.values())
        except OrderError:
            raise
        except Exception as error:
            record_failure(merchant, "mcp", "refunds", "payment_refunds", error)
            raise OrderError(503, "payment_unavailable", "ЮKassa не вернула сведения о возвратах. Повторите запрос.") from error
