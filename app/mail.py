import smtplib
import ssl
from contextlib import contextmanager
from threading import BoundedSemaphore

mail_slots = BoundedSemaphore(4)


@contextmanager
def smtp_connection(settings):
    if not mail_slots.acquire(blocking=False):
        raise smtplib.SMTPException("SMTP queue is full")
    try:
        context = ssl.create_default_context()
        with (
            smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=8) if settings.mail_port == 587
            else smtplib.SMTP_SSL(settings.mail_host, settings.mail_port, timeout=8, context=context)
        ) as smtp:
            if settings.mail_port == 587:
                smtp.starttls(context=context)
            if settings.mail_user:
                smtp.login(settings.mail_user, settings.mail_password)
            yield smtp
    finally:
        mail_slots.release()
