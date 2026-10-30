"""Minimal async PayPal REST client: OAuth, Orders v2, webhook verification."""
import time
import uuid
from typing import Optional

import httpx


class PayPalError(Exception):
    pass


class PayPal:
    def __init__(self, client_id: str, secret: str, base_url: str,
                 client: Optional[httpx.AsyncClient] = None):
        self.client_id, self.secret, self.base = client_id, secret, base_url
        self.http = client or httpx.AsyncClient(timeout=20)
        self._token, self._exp = None, 0.0

    async def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = await self.http.post(f"{self.base}/v1/oauth2/token", auth=(self.client_id, self.secret),
                                 data={"grant_type": "client_credentials"})
        if r.status_code != 200:
            raise PayPalError(f"auth failed: {r.status_code} {r.text[:200]}")
        j = r.json()
        self._token, self._exp = j["access_token"], time.time() + j.get("expires_in", 3000)
        return self._token

    async def _req(self, method, path, json=None, idempotent=False):
        headers = {"Authorization": f"Bearer {await self.token()}", "Content-Type": "application/json"}
        if idempotent:
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        r = await self.http.request(method, f"{self.base}{path}", json=json, headers=headers)
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def create_order(self, items: list[dict], currency: str, store_name: str,
                           return_url: str, cancel_url: str, reference: str) -> dict:
        """items: [{name, sku, price, qty, size}] -> returns {id, approve_url, total}"""
        total = round(sum(i["price"] * i["qty"] for i in items), 2)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference[:256],
                "description": f"Order from {store_name}"[:127],
                "amount": {"currency_code": currency, "value": f"{total:.2f}",
                           "breakdown": {"item_total": {"currency_code": currency, "value": f"{total:.2f}"}}},
                "items": [{"name": f'{i["name"]} ({i["size"]})'[:127], "sku": i["sku"],
                           "quantity": str(i["qty"]), "category": "PHYSICAL_GOODS",
                           "unit_amount": {"currency_code": currency, "value": f'{i["price"]:.2f}'}}
                          for i in items],
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": store_name[:127], "user_action": "PAY_NOW",
                "shipping_preference": "GET_FROM_FILE",
                "return_url": return_url, "cancel_url": cancel_url}}},
        }
        j = await self._req("POST", "/v2/checkout/orders", body, idempotent=True)
        link = next((l["href"] for l in j.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return {"id": j["id"], "approve_url": link, "total": total, "status": j.get("status")}

    async def get_order(self, order_id: str) -> dict:
        return await self._req("GET", f"/v2/checkout/orders/{order_id}")

    async def capture_order(self, order_id: str) -> dict:
        return await self._req("POST", f"/v2/checkout/orders/{order_id}/capture", {}, idempotent=True)

    async def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id, "webhook_event": event,
        }
        j = await self._req("POST", "/v1/notifications/verify-webhook-signature", body)
        return j.get("verification_status") == "SUCCESS"
