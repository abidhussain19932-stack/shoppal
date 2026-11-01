import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent import ShopAgent
from app.catalog import Catalog
from app.config import Settings
from app.paypal import PayPal
from app.store import Store


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


@pytest.fixture
def catalog():
    return Catalog.load("data/catalog.json")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_search(catalog):
    res = catalog.search("red kurta", max_price=30)
    assert res and res[0]["sku"] == "K101"
    assert all(p["price"] <= 30 for p in res)
    assert catalog.search("kurti")[0]["category"] == "kurta"  # synonym
    assert catalog.search("saree", size="M")  # Free-size items allowed


def fake_paypal_transport():
    state = {"orders": {}}

    def handler(req: httpx.Request):
        p = req.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if p == "/v2/checkout/orders" and req.method == "POST":
            body = json.loads(req.content)
            state["orders"]["ORD1"] = body
            return httpx.Response(201, json={"id": "ORD1", "status": "PAYER_ACTION_REQUIRED", "links": [
                {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORD1"}]})
        if p == "/v2/checkout/orders/ORD1" and req.method == "GET":
            return httpx.Response(200, json={"id": "ORD1", "status": "APPROVED"})
        if p == "/v2/checkout/orders/ORD1/capture":
            return httpx.Response(201, json={"id": "ORD1", "status": "COMPLETED",
                                             "payer": {"name": {"given_name": "Test", "surname": "Buyer"}},
                                             "purchase_units": [{"payments": {"captures": [{"id": "CAP1"}]}}]})
        return httpx.Response(404, json={"error": p})
    return httpx.MockTransport(handler), state


def test_paypal_create_and_capture():
    transport, state = fake_paypal_transport()
    pp = PayPal("id", "sec", "https://api-m.sandbox.paypal.com", httpx.AsyncClient(transport=transport))

    async def go():
        o = await pp.create_order([{"name": "Red Cotton Kurta", "sku": "K101", "price": 24.0, "qty": 2,
                                    "size": "M"}], "USD", "Noor", "http://x/r", "http://x/c", "42")
        cap = await pp.capture_order(o["id"])
        return o, cap
    o, cap = run(go())
    assert o["approve_url"].endswith("ORD1") and o["total"] == 48.0
    body = state["orders"]["ORD1"]
    assert body["purchase_units"][0]["amount"]["value"] == "48.00"
    assert body["purchase_units"][0]["items"][0]["quantity"] == "2"
    assert cap["status"] == "COMPLETED"


class ScriptedGateway:
    """Fake Otari gateway: plays back scripted sales-model steps; canned answers for the other roles."""
    def __init__(self, steps, guard="safe", trends="Jewel tones", quote=None):
        self.steps, self.guard, self.trends, self.quote = list(steps), guard, trends, quote
        self.calls = []

    async def chat(self, role, models, end_user="", extra_body=None, **kw):
        from app.otari import CallInfo
        self.calls.append({"role": role, "models": models, "extra_body": extra_body, **kw})
        if role == "guardrail":
            msg = SimpleNamespace(content=self.guard, tool_calls=None)
        elif role == "trend_scout":
            msg = SimpleNamespace(content=self.trends, tool_calls=None)
        elif role == "quote_calculator":
            msg = SimpleNamespace(content=json.dumps(self.quote), tool_calls=None)
        else:
            step = self.steps.pop(0)
            if isinstance(step, str):
                msg = SimpleNamespace(content=step, tool_calls=None)
            else:
                msg = SimpleNamespace(content="", tool_calls=[
                    SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                    for i, (n, a) in enumerate(step)])
        resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)], model_dump=lambda: {})
        return resp, CallInfo(role=role, requested=models[0], served_model=models[0], provider="test",
                              cost_usd=0.001)


def test_agent_full_purchase(catalog, store):
    transport, _ = fake_paypal_transport()
    pp = PayPal("id", "sec", "https://api-m.sandbox.paypal.com", httpx.AsyncClient(transport=transport))
    gw = ScriptedGateway([
        [("search_products", {"query": "red kurta", "max_price": 30})],
        [("add_to_cart", {"sku": "K101", "size": "m", "qty": 2})],
        [("create_checkout", {})],
        "Done! Tap the PayPal button to pay $48.00.",
    ])
    from app.guard import Guard
    agent = ShopAgent(gw, ["shoppal-chat", "openai/gpt-oss-120b"], catalog, store, pp, Settings(),
                      guard=Guard(gw, "meta/llama-guard-4-12b"))
    r = run(agent.respond(7, "2 red kurtas size M, checkout"))
    assert r.tools_used == ["search_products", "add_to_cart", "create_checkout"]
    assert r.pay_url and r.order_id == "ORD1"
    assert store.cart(7) == []
    o = store.order("ORD1")
    assert o["total"] == 48.0 and o["items"][0]["size"] == "M"
    assert len(store.history(7)) == 2
    assert r.guard == "safe" and r.meta()["model"] == "shoppal-chat"
    assert gw.calls[0]["role"] == "guardrail" and gw.calls[1]["models"][0] == "shoppal-chat"
    assert r.cost_usd == 0.005  # 1 guard + 4 sales calls


def test_add_to_cart_needs_size(catalog, store):
    agent = ShopAgent(None, "m", catalog, store, None, Settings())
    r = run(agent.run_tool("add_to_cart", {"sku": "K101"}, 1, None))
    assert "size" in r["error"]
    r = run(agent.run_tool("add_to_cart", {"sku": "J401"}, 1, None))  # single size auto
    assert r["ok"] and r["cart"]["total"] == 12.0
    r = run(agent.run_tool("add_to_cart", {"sku": "S201", "qty": 99}, 1, None))
    assert "stock" in r["error"]
