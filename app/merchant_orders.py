import time
from typing import Annotated

from fastapi import Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from .checkout import checkout_flags
from .orders import OrderError, public_order, reopen_draft, seller_transcript
from .payment_documents import PDF_LIMIT, documents_for
from .session_security import check_csrf, csrf


def mount_order_routes(router, merchants, templates, payments, manager, guard_for):
    store, settings = merchants.store, merchants.settings

    def merchant_order(db, slug, order_id):
        row = db.execute("SELECT * FROM orders WHERE merchant = ? AND id = ?", (slug, order_id)).fetchone()
        if row is None:
            raise OrderError(404, "order_not_found", "Заказ не найден.")
        return row

    def render_order(request, slug, token, order_id, *, refunds=None, error="", status=200):
        with store.read() as db:
            order = public_order(db, merchant_order(db, slug, order_id))
            attempt = db.execute("SELECT state, kind, external_id, created_at FROM attempts WHERE id = ?",
                                 (order["payment_attempt_id"],)).fetchone()
            transcript = seller_transcript(db, order_id)
            has_paid = db.execute("SELECT 1 FROM attempts WHERE order_id = ? AND state = 'succeeded'", (order_id,)).fetchone()
            features = checkout_flags(db, slug)
            payment_list = documents_for(db, order_id) if settings.mode == "live" and (
                features["receipt_download"] or features["receipt_email"]
            ) else []
            review = db.execute(
                "SELECT opened_message FROM seller_reviews WHERE order_id = ? AND closed_at IS NULL AND reply IS NULL",
                (order_id,),
            ).fetchone()
        return templates.TemplateResponse(request, "merchant_order.html", {
            "order": order, "transcript": transcript, "csrf": csrf(token),
            "simulated": settings.mode == "simulated", "review_id": review["opened_message"] if review else None,
            "refunds": refunds, "error": error, "payment_documents": payment_list,
            "payment_attempt": attempt, "has_paid": bool(has_paid),
            "review_payment": attempt and settings.mode == "live" and (attempt["state"] in {"pending", "unknown"}
                               or (attempt["state"] == "creating" and attempt["created_at"] + 30 <= time.time())),
            "can_fulfill": not order.get("refunded_kopecks"),
        }, status_code=status)

    @router.get("/orders/{order_id}")
    def order_page(request: Request, order_id: str):
        slug, token = manager(request)
        return render_order(request, slug, token, order_id)

    @router.post("/orders/{order_id}/payment-review")
    def order_payment_review(
        request: Request, order_id: str, token: Annotated[str, Form(max_length=128)],
        payment_id: Annotated[str, Form(max_length=128)] = "",
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(404, "payment_not_found", "Платёжная попытка не найдена.")
        with store.read() as db:
            attempt_id = merchant_order(db, slug, order_id)["attempt_id"]
        try:
            payments.reconcile(slug, attempt_id, payment_id)
        except OrderError as error:
            return render_order(request, slug, session, order_id, error=error.body["message"], status=error.status)
        return RedirectResponse(f"/merchant/orders/{order_id}#payment-attempt", status_code=303)

    @router.post("/orders/{order_id}/receipts/{attempt_id}")
    def upload_receipt(
        request: Request, order_id: str, attempt_id: str, token: Annotated[str, Form(max_length=128)], file: UploadFile,
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        content = file.file.read(PDF_LIMIT + 1)
        if len(content) > PDF_LIMIT or not content.startswith(b"%PDF-") or not content.rstrip().endswith(b"%%EOF"):
            raise OrderError(400, "invalid_request", "Загрузите PDF-файл размером до 512 КБ.")
        with store.transaction() as db:
            guard_for(request, slug, session)(db)
            merchant_order(db, slug, order_id)
            flags = checkout_flags(db, slug)
            if settings.mode != "live" or not (flags["receipt_download"] or flags["receipt_email"]) or not db.execute(
                "SELECT 1 FROM attempts WHERE id = ? AND order_id = ? AND state = 'succeeded'", (attempt_id, order_id),
            ).fetchone():
                raise OrderError(404, "document_not_found", "Прикрепить чек можно к подтверждённому платежу.")
            db.execute(
                """INSERT INTO payment_receipts VALUES (?, ?) ON CONFLICT(attempt_id) DO UPDATE SET pdf=excluded.pdf""",
                (attempt_id, content),
            )
        return RedirectResponse(f"/merchant/orders/{order_id}#payment-documents", status_code=303)

    @router.post("/orders/{order_id}/refunds")
    def order_refunds(
        request: Request, order_id: str, token: Annotated[str, Form(max_length=128)],
        refund_id: Annotated[str, Form(pattern=r"^[a-zA-Z0-9_-]{0,128}$")] = "",
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        if settings.mode != "live":
            raise OrderError(404, "refund_not_found", "Проверка возвратов доступна при подключении ЮKassa.")
        try:
            refunds = payments.refunds(slug, order_id, refund_id)
        except OrderError as error:
            return render_order(request, slug, session, order_id, error=error.body["message"], status=error.status)
        return render_order(request, slug, session, order_id, refunds=refunds)

    @router.post("/orders/{order_id}/reply")
    def reply_to_order(
        request: Request, order_id: str, token: Annotated[str, Form(max_length=128)],
        reply: Annotated[str, Form(min_length=1, max_length=1000)],
        review_id: Annotated[str, Form(min_length=1, max_length=128)],
    ):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        reply = reply.strip()
        with store.transaction() as db:
            guard_for(request, slug, session)(db)
            order = merchant_order(db, slug, order_id)
            if not reply:
                raise OrderError(400, "invalid_reply", "Введите текст ответа.")
            if order["state"] not in {"draft", "agreed"}:
                raise OrderError(409, "order_locked", "Заказ уже подтверждён для оплаты.")
            if not db.execute(
                """UPDATE seller_reviews SET reply = ?, answered_at = ?
                   WHERE order_id = ? AND opened_message = ? AND reply IS NULL AND closed_at IS NULL""",
                (reply, int(time.time()), order_id, review_id),
            ).rowcount:
                raise OrderError(409, "review_unavailable", "Заказ больше не ждёт ответа.")
            reopen_draft(db, order_id)
        return RedirectResponse(f"/merchant/orders/{order_id}", status_code=303)

    @router.post("/orders/{order_id}/fulfill")
    def fulfill_order(request: Request, order_id: str, token: Annotated[str, Form(max_length=128)]):
        slug, session = manager(request)
        check_csrf(request, settings.origin, session, token)
        location = f"/merchant/orders/{order_id}"
        with store.read() as db:
            order = merchant_order(db, slug, order_id)
            if order["state"] not in {"paid", "deposit_received"}:
                raise OrderError(409, "order_not_paid", "Дождитесь оплаты или предоплаты.")
            if db.execute("SELECT 1 FROM fulfilled_orders WHERE order_id = ?", (order_id,)).fetchone():
                return RedirectResponse(location, status_code=303)
        if settings.mode == "live":
            try:
                refunds = payments.refunds(slug, order_id, "")
                if any(refund.status == "pending" for refund in refunds):
                    raise OrderError(409, "refund_pending", "Дождитесь результата возврата в ЮKassa.")
            except OrderError as error:
                return render_order(request, slug, session, order_id, error=error.body["message"], status=error.status)
        with store.transaction() as db:
            guard_for(request, slug, session)(db)
            current = merchant_order(db, slug, order_id)
            if current["attempt_id"] != order["attempt_id"] or current["state"] != order["state"]:
                raise OrderError(409, "payment_changed", "Статус оплаты изменился. Проверьте актуальный заказ.")
            if public_order(db, current).get("refunded_kopecks"):
                blocked = True
            else:
                blocked = False
                db.execute("INSERT OR IGNORE INTO fulfilled_orders VALUES (?, ?)", (order_id, int(time.time())))
        if blocked:
            return render_order(request, slug, session, order_id, status=409,
                                error="Часть оплаты возвращена. Сверьте расчёт с покупателем перед исполнением.")
        return RedirectResponse(location, status_code=303)
