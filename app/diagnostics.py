import json
import logging
import time
from collections import OrderedDict
from threading import Lock

import httpx

logger = logging.getLogger("uvicorn.error")
_lock = Lock()
_recent = OrderedDict()

_LABELS = {
    "model_credentials": "Проверьте доступ и срок действия ключа модели.",
    "model_balance": "Проверьте баланс и лимит расхода в кабинете провайдера.",
    "model_provider_limit": "Провайдер модели временно ограничил запросы.",
    "model_not_sent": "Проверьте подключение модели. Запрос не был отправлен.",
    "model_truncated": "Модель достигла лимита длины ответа.",
    "model_invalid_response": "Модель вернула ответ в неподдерживаемом формате.",
    "model_request_rejected": "Проверьте совместимость выбранной модели.",
    "model_request_error": "Не удалось получить ответ модели.",
    "payment_unknown": "Исход создания оплаты неизвестен. Проверьте попытку в кабинете.",
    "payment_not_sent": "Не удалось отправить создание оплаты в ЮKassa.",
    "payment_status": "ЮKassa не вернула подтверждение статуса. Проверьте подключение.",
    "payment_refunds": "ЮKassa не вернула полный список возвратов.",
    "payment_token": "Проверка MCP-токена не завершилась.",
    "storage_busy": "Очистка истёкших сессий ждёт освобождения хранилища.",
    "payment_busy": "Очередь обращений к ЮKassa занята. Повторите позже.",
    "payment_worker": "Фоновая сверка не завершилась. Следующая проверка выполнится автоматически.",
    "mail_failed": "Проверьте параметры SMTP и доступность почты.",
    "mail_unknown": "SMTP не подтвердил результат отправки.",
}

def record_failure(merchant, service, operation, code, error=None):
    # Provider exceptions may contain credentials, URLs, or user data.
    event = {"merchant": merchant, "service": service, "operation": operation,
             "code": code, "at": int(time.time())}
    for _ in range(4):
        if error is None:
            break
        event["error_type"] = type(error).__name__
        if isinstance(error, httpx.HTTPStatusError):
            event["http_status"] = error.response.status_code
            break
        error = error.exceptions[0] if isinstance(error, BaseExceptionGroup) else error.__cause__
    logger.warning("%s", json.dumps(event, ensure_ascii=False))
    with _lock:
        key = (merchant, service)
        _recent[key] = {**event, "message": _LABELS[code]}
        _recent.move_to_end(key)
        if len(_recent) > 512:
            _recent.popitem(last=False)

def recent_failures(merchant):
    now = int(time.time())
    with _lock:
        return [event for (slug, _), event in reversed(_recent.items())
                if slug == merchant and now - event["at"] <= 86400]

def clear_failure(merchant, service):
    with _lock:
        _recent.pop((merchant, service), None)
