CHECKOUT_FLAGS = ("split_payments", "tips", "receipt_download", "receipt_email")


def checkout_flags(db, merchant):
    saved = db.execute("SELECT * FROM checkout_settings WHERE merchant = ?", (merchant,)).fetchone()
    return {flag: bool(saved and saved[flag]) for flag in CHECKOUT_FLAGS}


def checkout_plan(db, order_id):
    return db.execute("SELECT * FROM checkout_plans WHERE order_id = ?", (order_id,)).fetchone()


def portion(total, parts, index):
    base, remainder = divmod(total, parts)
    return base + (index < remainder)


def public_plan(plan):
    parts, count, total = plan["parts"], plan["paid_parts"], plan["total_kopecks"]
    paid = sum(portion(total, parts, index) for index in range(count))
    return {
        "parts": parts, "paid_parts": count, "tip_percent": plan["tip_percent"],
        "tip_kopecks": plan["tip_kopecks"], "total_kopecks": total, "paid_kopecks": paid,
        "remaining_kopecks": total - paid, "next_kopecks": portion(total, parts, count) if count < parts else 0,
    }
