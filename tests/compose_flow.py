import json
import re
import sys
from pathlib import Path
from uuid import uuid4

import httpx

ORIGIN = "http://127.0.0.1:8000"
STATE = Path("data/compose-smoke.json")

with httpx.Client(base_url=ORIGIN) as client:
    assert client.get("/healthz").json() == {"status": "ok"}
    if "--verify" in sys.argv:
        state = json.loads(STATE.read_text())
        client.cookies.update(state["cookies"])
        assert client.get(state["location"]).json()["status"] == "succeeded"
        assert client.get(state["order_url"]).json()["status"] == "paid"
        STATE.unlink()
    else:
        page = client.get("/")
        assert page.status_code == 200
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"X-CSRF-Token": token, "Origin": ORIGIN}
        response = client.post("/api/v1/orders", headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
        response.raise_for_status()
        order_url = response.headers["location"]
        order = response.json()
        for text in ("20 завтраков, из них 5 вегетарианских", "Замените два обычных завтрака на вегетарианские."):
            result = client.post(order_url + "/messages", headers=headers, json={
                "order_version": order["version"], "client_message_id": str(uuid4()), "text": text,
            })
            result.raise_for_status()
            order = result.json()["order"]
        assert order["total_kopecks"] == 735_000
        response = client.post(
            order_url + "/payment-attempts", headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"order_version": order["version"]},
        )
        response.raise_for_status()
        location = response.headers["location"]
        assert response.json()["status"] == "pending"
        approved = client.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={})
        assert approved.json()["status"] == "succeeded"
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({"cookies": dict(client.cookies), "location": location, "order_url": order_url}))
