import argparse
import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def checkout(origin):
    started = time.perf_counter()
    with httpx.Client(base_url=origin, timeout=12) as client:
        page = client.get("/")
        page.raise_for_status()
        if 'data-mode="simulated"' not in page.text:
            raise RuntimeError("Нагрузочная проверка доступна только в симуляции.")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        created = client.post("/api/v1/orders", headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
        created.raise_for_status()
        order = created.json()
        url = created.headers["Location"]
        for message, expected in (
            ("20 завтраков, из них 5 вегетарианских", 725_000),
            ("Замените два обычных завтрака на вегетарианские.", 735_000),
        ):
            sent = client.post(url + "/messages", headers=headers, json={
                "client_message_id": str(uuid4()), "order_version": order["version"], "text": message,
            })
            sent.raise_for_status()
            order = sent.json()["order"]
            if order["total_kopecks"] != expected:
                raise RuntimeError("Сумма сценария расходится с контрольной.")
        key = str(uuid4())
        for _ in range(2):
            attempt = client.post(url + "/payment-attempts", headers={**headers, "Idempotency-Key": key},
                                  json={"order_version": order["version"]})
            attempt.raise_for_status()
        location = attempt.headers["Location"]
        paid = client.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={})
        paid.raise_for_status()
        final = client.get(url)
        final.raise_for_status()
        if final.json()["status"] != "paid" or paid.json()["status"] != "succeeded":
            raise RuntimeError("Не удалось подтвердить оплату в симуляции.")
    return time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if (
        url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.path
        or url.username or url.query or url.fragment
        or not 1 <= args.concurrency <= 48 or not 1 <= args.users <= 500
    ):
        parser.error("Используйте отдельную локальную симуляцию, 1–500 заказов и 1–48 потоков.")
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = sorted(pool.map(checkout, [args.origin] * args.users))
    duration = time.perf_counter() - start
    print(json.dumps({
        "orders": len(results), "concurrency": args.concurrency, "seconds": round(duration, 3),
        "orders_per_second": round(len(results) / duration, 2),
        "p50_ms": round(statistics.median(results) * 1000),
        "p95_ms": round(results[math.ceil(len(results) * 0.95) - 1] * 1000),
    }))


if __name__ == "__main__":
    main()
