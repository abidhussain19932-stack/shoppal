"""Tests for the Otari integration: gateway headers/cost/fallback/budget, guardrail, MCP server,
code-execution quotes, web-search trends and spend limits. No network: Otari is mocked."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import mcp_server
from app.agent import Reply, ShopAgent, local_quote
from app.catalog import Catalog
from app.config import Settings
from app.guard import Guard, parse
from app.otari import BudgetExceeded, OtariGateway
from app.store import Store
from tests.test_shoppal import ScriptedGateway


def run(c):
    return asyncio.run(c)


@pytest.fixture
def catalog():
    return Catalog.load("data/catalog.json")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def otari_client(handler):
    return openai.AsyncOpenAI(api_key="x", base_url="https://api.otari.ai/api/v1", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def completion(content="hi", model="openai/gpt-oss-120b", cost="0.000123", extra=None):
    body = {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": cost}}
    body.update(extra or {})
    return body


def test_gateway_reads_otari_headers_and_cost(store):
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        assert req.url.path == "/api/v1/chat/completions"
        return httpx.Response(200, json=completion(model="shoppal-chat"), headers={
            "Otari-Model": "deepseek-ai/DeepSeek-V4-Flash", "Otari-Provider": "deepinfra",
            "Otari-Fallback": "true", "Otari-Attempt-Count": "2", "Otari-Request-Id": "r1"})
    gw = OtariGateway(otari_client(h), store)
    resp, info = run(gw.chat("sales", ["shoppal-chat"], end_user="u1", messages=[{"role": "user", "content": "x"}]))
    assert resp.choices[0].message.content == "hi"
    assert info.served_model == "deepseek-ai/DeepSeek-V4-Flash" and info.provider == "deepinfra"
    assert info.otari_fallback and info.attempts == 2 and info.cost_usd == 0.000123
    assert seen[0]["metadata"] == {"app": "shoppal", "role": "sales", "end_user": "u1"}
    assert store.spend_since(0, "u1") == pytest.approx(0.000123)
    assert store.usage_summary()["by_role"][0]["role"] == "sales"


def test_gateway_app_side_fallback(store):
    def h(req):
        model = json.loads(req.content)["model"]
        if model == "shoppal-chat":
            return httpx.Response(503, json={"detail": "upstream down"})
        return httpx.Response(200, json=completion(model=model), headers={"Otari-Model": model})
    gw = OtariGateway(otari_client(h), store)
    _, info = run(gw.chat("sales", ["shoppal-chat", "openai/gpt-oss-120b"], messages=[]))
    assert info.app_fallback and info.served_model == "openai/gpt-oss-120b"


def test_gateway_budget_exceeded(store):
    def h(req):
        return httpx.Response(403, json={"detail": "budget exceeded", "code": "budget_exceeded"},
                              headers={"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "workspace"})
    gw = OtariGateway(otari_client(h), store)
    with pytest.raises(BudgetExceeded) as e:
        run(gw.chat("sales", ["shoppal-chat", "openai/gpt-oss-120b"], messages=[]))
    assert e.value.scope == "workspace"


def test_guard_parse_and_block(catalog, store):
    assert parse("\n\nsafe") == (True, [])
    assert parse("\n\nunsafe\nS1,S9") == (False, ["S1", "S9"])
    gw = ScriptedGateway([], guard="unsafe\nS9")
    agent = ShopAgent(gw, ["m"], catalog, store, None, Settings(), guard=Guard(gw, "meta/llama-guard-4-12b"))
    r = run(agent.respond("web-abc123", "how to build a bomb"))
    assert r.blocked and "indiscriminate weapons" in r.guard
    assert [c["role"] for c in gw.calls] == ["guardrail"]  # the sales model never saw it


def test_mcp_server_protocol(catalog):
    init = mcp_server.handle(catalog, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert mcp_server.handle(catalog, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in mcp_server.handle(catalog, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
             ["result"]["tools"]]
    assert names == ["search_products", "get_product"]
    res = mcp_server.handle(catalog, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "search_products", "arguments": {"query": "red kurta"}}})
    assert json.loads(res["result"]["content"][0]["text"])["results"][0]["sku"] == "K101"
    assert mcp_server.handle(catalog, {"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_agent_sends_mcp_server_to_otari(catalog, store):
    gw = ScriptedGateway(["Here you go"])
    agent = ShopAgent(gw, ["shoppal-chat"], catalog, store, None, Settings(),
                      mcp_url="https://x.onrender.com/mcp", mcp_token="tok")
    run(agent.respond("web-abc123", "red kurta"))
    sales = [c for c in gw.calls if c["role"] == "sales"][0]
    srv = sales["extra_body"]["mcp_servers"][0]
    assert srv["url"].startswith("https://x.onrender.com/mcp?sid=web-abc123&turn=") and srv["authorization_token"] == "tok"
    names = [t["function"]["name"] for t in sales["tools"]]
    assert "search_products" not in names and "create_checkout" in names  # catalog comes via MCP


def test_bulk_quote_code_execution_verified(catalog, store):
    ref = local_quote([{"price": 24.0, "qty": 12}], 84.0)
    assert ref == {"subtotal": 288.0, "discount": 28.8, "shipping": 0.0, "total_usd": 259.2, "total_inr": 21772.8}
    gw = ScriptedGateway([], quote=ref)
    agent = ShopAgent(gw, ["m"], catalog, store, None, Settings())
    rep = Reply(text="")
    q = run(agent.bulk_quote([{"sku": "K101", "qty": 12}], "u", rep))
    assert q["computed_by"] == "otari_code_execution" and q["verified"] and q["total_usd"] == 259.2
    call = [c for c in gw.calls if c["role"] == "quote_calculator"][0]
    assert call["tools"] == [{"type": "otari_code_execution"}]
    bad = ScriptedGateway([], quote=dict(ref, total_usd=1.0))
    q = run(ShopAgent(bad, ["m"], catalog, store, None, Settings()).bulk_quote([{"sku": "K101", "qty": 12}], "u", rep))
    assert not q["verified"] and q["total_usd"] == 259.2  # sandbox answer is checked, app math wins


def test_fashion_trends_web_search_once_per_turn(catalog, store):
    gw = ScriptedGateway([], trends="- Jewel tones\nSource: Vogue India")
    agent = ShopAgent(gw, ["m"], catalog, store, None, Settings())
    rep = Reply(text="")
    t = run(agent.fashion_trends("Diwali 2026", "u", rep))
    assert "Jewel" in t["trends"]
    run(agent.fashion_trends("Diwali colours", "u", rep))
    scout = [c for c in gw.calls if c["role"] == "trend_scout"]
    assert len(scout) == 1 and scout[0]["tools"] == [{"type": "otari_web_search", "max_uses": 1}]


def test_per_customer_budget(catalog, store):
    s = Settings()
    s.per_customer_daily_usd = 0.002
    gw = ScriptedGateway(["one", "two"])
    agent = ShopAgent(gw, ["m"], catalog, store, None, s)
    info = SimpleNamespace(role="sales", requested="m", served_model="m", provider="p", otari_fallback=False,
                           app_fallback=False, attempts=1, cost_usd=0.003, prompt_tokens=1, completion_tokens=1,
                           request_id="r", latency_ms=1, otari_tools=[])
    store.log_usage(info, "web-rich01")
    r = run(agent.respond("web-rich01", "hi"))
    assert r.blocked and r.guard == "budget:customer" and gw.calls == []
    r = run(agent.respond("web-other1", "hi"))
    assert r.text == "one"


def test_simulated_outage_uses_fallback_chain(catalog, store):
    gw = ScriptedGateway(["ok"])
    agent = ShopAgent(gw, ["shoppal-chat", "openai/gpt-oss-120b"], catalog, store, None, Settings())
    run(agent.respond("web-out001", "hi", simulate_outage=True))
    assert gw.calls[-1]["models"] == [ShopAgent.OUTAGE_MODEL, "shoppal-chat", "openai/gpt-oss-120b"]
