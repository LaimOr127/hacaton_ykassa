import json
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from hashlib import sha256

from .checkout import checkout_flags
from .gateway import amount_value, utc_iso
from .orders import OrderError, lookup_order

PDF_LIMIT = 512 * 1024
EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
                   r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")


def documents_for(db, order_id):
    return db.execute(
        """SELECT a.id, a.part_number, a.snapshot, r.attempt_id IS NOT NULL AS has_receipt
           FROM attempts a LEFT JOIN payment_receipts r ON r.attempt_id = a.id
           WHERE a.order_id = ? AND a.state = 'succeeded' ORDER BY a.part_number, a.created_at, a.id""",
        (order_id,),
    ).fetchall()


def delivery_status(row, now):
    if row["state"] == "sending" and now - row["created_at"] >= 30:
        return {"status": "unknown"}
    return {"status": row["state"]}


def get_document(db, order, attempt_id, kind, simulated):
    payment = db.execute(
        """SELECT a.*, m.name AS merchant_name FROM attempts a
           JOIN orders o ON o.id = a.order_id LEFT JOIN merchants m ON m.slug = o.merchant
           WHERE a.id = ? AND a.order_id = ? AND a.state = 'succeeded'""",
        (attempt_id, order["id"]),
    ).fetchone()
    if not payment:
        raise OrderError(404, "document_not_found", "Документ доступен после подтверждения оплаты.")
    short_id = attempt_id[:10]
    if kind == "receipt":
        saved = db.execute("SELECT pdf FROM payment_receipts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if not saved:
            raise OrderError(404, "document_not_found", "Продавец ещё не прикрепил кассовый чек.")
        return saved["pdf"], f"receipt-{short_id}.pdf", "application/pdf"
    snapshot = json.loads(payment["snapshot"])
    amount = amount_value(snapshot["payable_kopecks"])
    lines = [
        "Подтверждение оплаты",
        "Симуляция оплаты" if simulated else "Платёж подтверждён ЮKassa",
        f"Магазин: {payment['merchant_name'] or order['merchant']}",
        f"Заказ: {order['id']}",
        f"Платёж: {payment['settled_payment_id'] or payment['external_id'] or payment['id']}",
        f"Оплачено: {amount} ₽",
    ]
    if payment["part_number"] is not None:
        lines.append(f"Доля: {payment['part_number']} из {snapshot['parts']}")
    if payment["verified_at"]:
        lines.append(f"Подтверждено: {utc_iso(payment['verified_at'])}")
    lines.append("Справочный документ. Кассовый чек предоставляет продавец.")
    return ("\n".join(lines) + "\n").encode(), f"payment-{short_id}.txt", "text/plain"


class PaymentDocuments:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def list(self, session, order_id):
        with self.store.read() as db:
            order = lookup_order(db, session, order_id)
            flags = checkout_flags(db, order["merchant"])
            download, email = flags["receipt_download"], flags["receipt_email"] and self.settings.mail_ready
            rows = documents_for(db, order_id) if download or email else []
        return {
            "download": download, "email": email,
            "payments": [
                {"attempt_id": row["id"], "part_number": row["part_number"], "has_receipt": bool(row["has_receipt"]),
                 "amount_kopecks": json.loads(row["snapshot"])["payable_kopecks"]}
                for row in rows
            ],
        }

    def download(self, session, order_id, attempt_id, kind):
        with self.store.read() as db:
            order = lookup_order(db, session, order_id)
            if not checkout_flags(db, order["merchant"])["receipt_download"]:
                raise OrderError(404, "document_not_found", "Скачивание отключено в настройках магазина.")
            return get_document(db, order, attempt_id, kind, self.settings.mode == "simulated")

    def send(self, session, order_id, attempt_id, kind, recipient, key):
        if not EMAIL.fullmatch(recipient) or len(recipient.split("@", 1)[0]) > 64:
            raise OrderError(400, "invalid_request", "Проверьте адрес электронной почты.")
        now = int(time.time())
        local, domain = recipient.rsplit("@", 1)
        recipient_hash = sha256(f"{local}@{domain.lower()}".encode()).hexdigest()
        with self.store.transaction() as db:
            order = lookup_order(db, session, order_id)
            if not self.settings.mail_ready or not checkout_flags(db, order["merchant"])["receipt_email"]:
                raise OrderError(404, "document_not_found", "Почтовая отправка недоступна.")
            prior = db.execute(
                "SELECT * FROM receipt_deliveries WHERE order_id = ? AND request_key = ?", (order_id, key),
            ).fetchone()
            if prior:
                if (prior["recipient_hash"], prior["attempt_id"], prior["document_kind"]) != (recipient_hash, attempt_id, kind):
                    raise OrderError(409, "idempotency_conflict", "Ключ отправки уже использован.")
                return delivery_status(prior, now)
            document = get_document(db, order, attempt_id, kind, False)
            db.execute("DELETE FROM receipt_deliveries WHERE created_at < ?", (now - 7 * 86400,))
            document_hash = sha256(document[0]).hexdigest()
            previous_copy = db.execute(
                """SELECT state, created_at FROM receipt_deliveries
                   WHERE attempt_id = ? AND recipient_hash = ? AND document_kind = ? AND document_hash = ?
                   AND state != 'failed'
                   ORDER BY created_at DESC LIMIT 1""",
                (attempt_id, recipient_hash, kind, document_hash),
            ).fetchone()
            if previous_copy:
                return delivery_status(previous_copy, now)
            counts = db.execute(
                """SELECT COUNT(CASE WHEN order_id = ? AND created_at >= ? THEN 1 END),
                          COUNT(CASE WHEN recipient_hash = ? AND created_at >= ? THEN 1 END),
                          COUNT(CASE WHEN created_at >= ? THEN 1 END),
                          COUNT(CASE WHEN order_id = ? AND created_at >= ? THEN 1 END)
                   FROM receipt_deliveries WHERE created_at >= ?""",
                (order_id, now - 60, recipient_hash, now - 3600, now - 3600, order_id, now - 86400, now - 86400),
            ).fetchone()
            if counts[0] or counts[1] >= 2 or counts[2] >= 60 or counts[3] >= 8:
                raise OrderError(429, "rate_limited", "Достигнут лимит отправки. Попробуйте позже.")
            db.execute(
                """INSERT INTO receipt_deliveries
                   (request_key, order_id, attempt_id, recipient_hash, document_kind, document_hash, created_at, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'sending')""",
                (key, order_id, attempt_id, recipient_hash, kind, document_hash, now),
            )
        result = self.deliver(recipient, document, kind)
        with self.store.transaction() as db:
            db.execute("UPDATE receipt_deliveries SET state = ? WHERE order_id = ? AND request_key = ?", (result, order_id, key))
        return {"status": result}

    def deliver(self, recipient, document, kind):
        data, filename, media = document
        message = EmailMessage()
        message["From"] = self.settings.mail_from
        message["To"] = recipient
        message["Subject"] = "Кассовый чек по заказу" if kind == "receipt" else "Подтверждение оплаты заказа"
        message.set_content("Запрошенный документ во вложении.")
        major, minor = media.split("/")
        message.add_attachment(data, maintype=major, subtype=minor, filename=filename)
        started = False
        try:
            with smtplib.SMTP_SSL(
                self.settings.mail_host, self.settings.mail_port, timeout=8, context=ssl.create_default_context(),
            ) as smtp:
                if self.settings.mail_user:
                    smtp.login(self.settings.mail_user, self.settings.mail_password)
                started = True
                smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            return "failed"
        except (OSError, ValueError, smtplib.SMTPException):
            return "unknown" if started else "failed"
        return "accepted"
