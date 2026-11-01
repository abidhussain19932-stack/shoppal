"""LLM shopping agent with tool calls over the catalog, cart and PayPal checkout.

All model traffic goes through the Otari gateway (see app/otari.py):
* the sales agent runs on an Otari routing policy / model chain (open-weight gpt-oss-120b first),
* customer messages are screened by Llama Guard 4 first (app/guard.py),
* catalog lookups are served to the model over MCP (Otari connects to our /mcp server),
* `fashion_trends` asks a second open-weight model to use Otari's built-in web search,
* `bulk_quote` asks a third model to compute the quote with Otari's sandboxed code execution.
"""
import json
import logging
import secrets
import time
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Optional

from .catalog import Catalog
from .guard import Guard
from .otari import BudgetExceeded, GatewayError, OtariGateway
from .paypal import PayPal, PayPalError
from .store import Store

log = logging.getLogger(__name__)

SYSTEM = """You are ShopPal, the friendly sales assistant of {store}, an Indian ethnic-wear shop, chatting with customers.
- Reply in the customer's language (English, Hindi or Hinglish). Keep replies short: 1-4 lines, a few emojis at most. No tables.
- Search once per distinct item (combine filters in one call); don't repeat the same search.
- ALWAYS use tools for products, prices, stock, cart and orders. Never invent products or prices. Prices are in {currency}.
- When recommending, show at most 3 items as: name, price, SKU, and sizes.
- Only add items when the customer asks to buy/add them. Check CURRENT CART below first so you never add an item twice.
- Before adding to cart, make sure you know the size (if the item has more than one) and quantity (default 1).
- If the customer asks what is trending / in fashion / what to wear for a festival or season, call fashion_trends once (one topic), then recommend matching items from the catalog.
- For bulk / wholesale questions (5 or more pieces) or a price in rupees, call bulk_quote and quote its numbers exactly.
- When the customer is ready to pay, call create_checkout. The PayPal button is attached automatically; just tell them to tap it.
- Payment is confirmed only by PayPal; never claim an order is paid unless order_status says COMPLETED.
- Politely decline anything unrelated to shopping at {store}."""

CATALOG_TOOLS = [
    {"type": "function", "function": {"name": "search_products", "description": "Search the catalog.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords e.g. 'red kurta'"},
         "max_price": {"type": "number"}, "category": {"type": "string"},
         "size": {"type": "string"}, "color": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_product", "description": "Full details for a SKU.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}}},
]
SHOP_TOOLS = [
    {"type": "function", "function": {"name": "add_to_cart", "description": "Add an item to the cart.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}, "size": {"type": "string"},
                    "qty": {"type": "integer", "minimum": 1}}, "required": ["sku"]}}},
    {"type": "function", "function": {"name": "remove_from_cart", "description": "Remove an item from the cart.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}, "size": {"type": "string"}},
                    "required": ["sku"]}}},
    {"type": "function", "function": {"name": "view_cart", "description": "Show the cart and total.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "create_checkout",
     "description": "Create a PayPal checkout for the whole cart and return the pay link.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "order_status", "description": "Status of the customer's recent orders.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "fashion_trends",
     "description": "Live web search (via Otari) for what is trending in Indian ethnic wear for an occasion or season.",
     "parameters": {"type": "object", "properties": {"topic": {"type": "string",
                    "description": "e.g. 'Diwali 2026 women ethnic wear colours'"}}, "required": ["topic"]}}},
    {"type": "function", "function": {"name": "bulk_quote",
     "description": "Exact quote for several items incl. bulk discount, shipping and an indicative INR total.",
     "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {
         "type": "object", "properties": {"sku": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}},
         "required": ["sku", "qty"]}}}, "required": ["items"]}}},
]
TOOLS = CATALOG_TOOLS + SHOP_TOOLS

QUOTE_RULES = ("Bulk discount per line: 5% when qty is 5-9, 10% when qty is 10 or more. "
               "Shipping: free when the discounted subtotal is at least 50 USD, otherwise 5 USD flat. "
               "Round every money value to 2 decimals.")


def local_quote(lines: list[dict], inr_rate: float) -> dict:
    """Reference implementation of QUOTE_RULES (used to verify / back up the sandbox result)."""
    subtotal = discount = 0.0
    for ln in lines:
        amt = ln["price"] * ln["qty"]
        pct = 0.10 if ln["qty"] >= 10 else 0.05 if ln["qty"] >= 5 else 0.0
        subtotal += amt
        discount += round(amt * pct, 2)
    net = round(subtotal - discount, 2)
    shipping = 0.0 if net >= 50 else 5.0
    total = round(net + shipping, 2)
    return {"subtotal": round(subtotal, 2), "discount": round(discount, 2), "shipping": shipping,
            "total_usd": total, "total_inr": round(total * inr_rate, 2)}


@dataclass
class Reply:
    text: str
    pay_url: Optional[str] = None
    order_id: Optional[str] = None
    tools_used: list = field(default_factory=list)
    calls: list = field(default_factory=list)      # CallInfo.public() for every Otari call this turn
    guard: str = ""
    blocked: bool = False
    trends: Optional[dict] = None
    simulate_outage: bool = False

    @property
    def cost_usd(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls), 6)

    def meta(self) -> dict:
        sales = [c for c in self.calls if c["role"] == "sales"]
        return {"model": sales[-1]["model"] if sales else "", "provider": sales[-1]["provider"] if sales else "",
                "requested": sales[-1]["requested"] if sales else "", "cost_usd": self.cost_usd,
                "guard": self.guard, "tools": self.tools_used, "calls": self.calls, "blocked": self.blocked,
                "fallback": any(c["otari_fallback"] or c["app_fallback"] for c in self.calls)}


class ShopAgent:
    def __init__(self, gateway: OtariGateway, models, catalog: Catalog, store: Store, paypal: Optional[PayPal],
                 settings, guard: Optional[Guard] = None, mcp_url: str = "", mcp_token: str = ""):
        self.gw, self.catalog, self.store, self.paypal = gateway, catalog, store, paypal
        self.models = [models] if isinstance(models, str) else list(models)
        self.s, self.guard, self.mcp_url, self.mcp_token = settings, guard, mcp_url, mcp_token
        self._trend_cache: dict = {}

    # ---------- tools ----------
    def _brief(self, p):
        return {k: p[k] for k in ("sku", "name", "price", "sizes", "color", "stock")}

    def cart_view(self, chat_id) -> dict:
        lines, total = [], 0.0
        for row in self.store.cart(chat_id):
            p = self.catalog.get(row["sku"])
            if not p:
                continue
            sub = p["price"] * row["qty"]
            total += sub
            lines.append({"sku": p["sku"], "name": p["name"], "size": row["size"], "qty": row["qty"],
                          "price": p["price"], "subtotal": round(sub, 2)})
        return {"items": lines, "total": round(total, 2), "currency": self.s.currency}

    async def run_tool(self, name: str, args: dict, chat_id, reply: Reply) -> dict:
        if name == "search_products":
            res = self.catalog.search(**{k: v for k, v in args.items()
                                         if k in ("query", "max_price", "category", "size", "color")})
            return {"results": [self._brief(p) for p in res]}
        if name == "get_product":
            p = self.catalog.get(args.get("sku"))
            return p or {"error": "unknown SKU"}
        if name == "add_to_cart":
            p = self.catalog.get(args.get("sku"))
            if not p:
                return {"error": "unknown SKU"}
            size = args.get("size") or (p["sizes"][0] if len(p["sizes"]) == 1 else None)
            if not size:
                return {"error": f"ask the customer for a size: {p['sizes']}"}
            if size.upper() not in [s.upper() for s in p["sizes"]]:
                return {"error": f"size {size} not available; options {p['sizes']}"}
            size = next(s for s in p["sizes"] if s.upper() == size.upper())
            qty = max(1, int(args.get("qty") or 1))
            if qty > p["stock"]:
                return {"error": f"only {p['stock']} in stock"}
            self.store.add_to_cart(chat_id, p["sku"], size, qty)
            return {"ok": True, "cart": self.cart_view(chat_id)}
        if name == "remove_from_cart":
            self.store.remove_from_cart(chat_id, (args.get("sku") or "").upper(), args.get("size"))
            return {"ok": True, "cart": self.cart_view(chat_id)}
        if name == "view_cart":
            return self.cart_view(chat_id)
        if name == "create_checkout":
            cart = self.cart_view(chat_id)
            if not cart["items"]:
                return {"error": "cart is empty"}
            if not self.paypal:
                return {"error": "payments are not configured"}
            items = [{"name": i["name"], "sku": i["sku"], "price": i["price"], "qty": i["qty"], "size": i["size"]}
                     for i in cart["items"]]
            try:
                o = await self.paypal.create_order(
                    items, self.s.currency, self.s.store_name,
                    return_url=f"{self.s.public_base_url}/paypal/return",
                    cancel_url=f"{self.s.public_base_url}/paypal/cancel",
                    reference=str(chat_id))
            except PayPalError as e:
                log.exception("create_order failed")
                return {"error": f"PayPal error: {e}"}
            self.store.save_order(o["id"], chat_id, items, o["total"], self.s.currency)
            self.store.clear_cart(chat_id)
            reply.pay_url, reply.order_id = o["approve_url"], o["id"]
            return {"ok": True, "order_id": o["id"], "total": o["total"], "currency": self.s.currency,
                    "note": "PayPal button attached to your message automatically"}
        if name == "order_status":
            return {"orders": [{"order_id": o["paypal_order_id"], "status": o["status"], "total": o["total"],
                                "items": [f'{i["qty"]}x {i["name"]} ({i["size"]})' for i in o["items"]]}
                               for o in self.store.orders_for(chat_id)]}
        if name == "fashion_trends":
            return await self.fashion_trends(args.get("topic") or "Indian ethnic wear trends", chat_id, reply)
        if name == "bulk_quote":
            return await self.bulk_quote(args.get("items") or [], chat_id, reply)
        return {"error": f"unknown tool {name}"}

    # ---------- Otari-powered tools ----------
    async def fashion_trends(self, topic: str, chat_id, reply: Reply) -> dict:
        """Second model (open-weight Llama 3.3 70B) + Otari's built-in web search."""
        if not getattr(self.s, "web_search_enabled", True):
            return {"error": "web search disabled"}
        key = topic.strip().lower()
        if reply.trends is not None:  # one live search per customer turn
            return dict(reply.trends, note="already searched this turn")
        hit = self._trend_cache.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:  # cache 6h: searches cost money
            return dict(hit[1], cached=True)
        resp, info = await self.gw.chat(
            "trend_scout", [self.s.scout_model, self.models[-1]], end_user=str(chat_id),
            messages=[{"role": "system", "content": "You are a fashion trend scout for an Indian ethnic-wear shop. "
                       f"Today is {time.strftime('%Y-%m-%d')}. Search the web once, then answer in at most 3 short bullet points (colours, styles, fabrics) "
                       "and end with 'Source: <site name>'."},
                      {"role": "user", "content": topic[:300]}],
            tools=[{"type": "otari_web_search", "max_uses": 1}], max_tokens=300, temperature=0.2)
        reply.calls.append(info.public())
        data = resp.model_dump() if hasattr(resp, "model_dump") else {}
        sources = []
        for c in data.get("otari_tool_calls") or []:
            for src in (c.get("sources") or [])[:3]:
                if src.get("url"):
                    sources.append({"title": src.get("title", ""), "url": src["url"]})
        out = {"trends": (resp.choices[0].message.content or "").strip(), "sources": sources[:3],
               "via": "otari_web_search"}
        reply.trends = out
        self._trend_cache[key] = (time.time(), out)
        return out

    async def bulk_quote(self, items: list, chat_id, reply: Reply) -> dict:
        """Third model (open-weight DeepSeek V4 Flash) runs Python in Otari's code-execution sandbox."""
        lines = []
        for it in items[:20]:
            p = self.catalog.get(it.get("sku"))
            if not p:
                return {"error": f"unknown SKU {it.get('sku')}"}
            lines.append({"sku": p["sku"], "name": p["name"], "price": p["price"], "qty": max(1, int(it.get("qty") or 1))})
        if not lines:
            return {"error": "no items"}
        ref = local_quote(lines, self.s.inr_rate)
        if not getattr(self.s, "code_exec_enabled", True):
            return dict(ref, computed_by="app")
        task = (f"Line items (USD): {json.dumps(lines)}. Rules: {QUOTE_RULES} "
                f"Convert total_usd to INR at {self.s.inr_rate}. Run Python with the code tool to compute it, then "
                "reply ONLY with JSON {\"subtotal\":..,\"discount\":..,\"shipping\":..,\"total_usd\":..,\"total_inr\":..}")
        try:
            resp, info = await self.gw.chat(
                "quote_calculator", [self.s.quote_model, self.models[-1]], end_user=str(chat_id),
                messages=[{"role": "system", "content": "You are a precise pricing calculator. Always execute Python."},
                          {"role": "user", "content": task}],
                tools=[{"type": "otari_code_execution"}], max_tokens=1500, temperature=0)
            reply.calls.append(info.public())
            txt = resp.choices[0].message.content or ""
            q = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
            q = {k: round(float(q[k]), 2) for k in ref}
        except BudgetExceeded:
            raise
        except Exception as e:
            log.warning("sandbox quote failed, using app math: %r", e)
            return dict(ref, computed_by="app")
        verified = all(abs(q[k] - ref[k]) <= 0.02 for k in ref)
        return dict(ref if not verified else q, lines=lines, computed_by="otari_code_execution",
                    verified=verified, rules=QUOTE_RULES, inr_rate=self.s.inr_rate)

    # ---------- spend limits ----------
    def over_budget(self, chat_id) -> Optional[str]:
        day = time.time() - 86400
        if self.store.spend_since(day, str(chat_id)) >= self.s.per_customer_daily_usd:
            return "customer"
        if self.store.spend_since(day) >= self.s.app_daily_usd:
            return "app"
        return None

    # ---------- loop ----------
    OUTAGE_MODEL = "demo/primary-model-outage"  # not in Otari's catalog: the call fails and the chain moves on

    def _models(self, reply: Reply) -> list:
        return ([self.OUTAGE_MODEL] + self.models) if reply.simulate_outage else self.models

    async def _sales_call(self, msgs, chat_id, reply: Reply, last: bool):
        kw = {"messages": msgs, "temperature": 0.3}
        extra, turn = {}, ""
        if not last:
            if self.mcp_url:
                turn = secrets.token_hex(6)
                url = f"{self.mcp_url}?sid={quote(str(chat_id))}&turn={turn}"  # lets us attribute MCP calls
                srv = {"name": "shoppal_catalog", "url": url,
                       "purpose_hint": "Use for every product, price, size or stock question about the shop.",
                       "allowed_tools": ["search_products", "get_product"]}
                if self.mcp_token:
                    srv["authorization_token"] = self.mcp_token
                extra["mcp_servers"] = [srv]
                kw["tools"] = SHOP_TOOLS
            else:
                kw["tools"] = TOOLS
        try:
            resp, info = await self.gw.chat("sales", self._models(reply), end_user=str(chat_id),
                                            extra_body=extra or None, **kw)
        except GatewayError:
            if not extra:
                raise
            log.warning("MCP-enabled call failed; retrying with local catalog tools")
            kw["tools"] = TOOLS
            resp, info = await self.gw.chat("sales", self._models(reply), end_user=str(chat_id), **kw)
        if extra.get("mcp_servers"):
            info.otari_tools = info.otari_tools + [f"mcp:{c['tool']}" for c in self.store.mcp_calls(turn)]
        reply.calls.append(info.public())
        reply.tools_used += info.otari_tools
        return resp

    async def respond(self, chat_id, user_text: str, max_steps: int = 8, simulate_outage: bool = False) -> Reply:
        reply = Reply(text="")
        reply.simulate_outage = simulate_outage
        history = self.store.history(chat_id)
        which = self.over_budget(chat_id)
        if which:
            reply.text = ("You've reached today's chat limit for this demo 🙏 Please come back tomorrow."
                          if which == "customer" else
                          "Our assistant has hit today's spending limit. Please try again tomorrow 🙏")
            reply.guard, reply.blocked = f"budget:{which}", True
            return reply
        try:
            return await self._respond(chat_id, user_text, history, reply, max_steps)
        except BudgetExceeded:
            reply.text = "Our assistant has reached its spending limit for now. Please try again later 🙏"
            reply.guard, reply.blocked = "budget:otari", True
            return reply

    async def _respond(self, chat_id, user_text, history, reply: Reply, max_steps: int) -> Reply:
        if self.guard:
            v = await self.guard.check(user_text, str(chat_id), history)
            reply.guard = v.label()
            if v.call:
                reply.calls.append(v.call)
            if not v.safe:
                reply.blocked = True
                reply.text = (f"Sorry, I can't help with that 🙏 I'm here for shopping at {self.s.store_name}. "
                              "Can I show you something?")
                self.store.save_history(chat_id, history + [{"role": "user", "content": "[message blocked by guardrail]"},
                                                            {"role": "assistant", "content": reply.text}])
                return reply
        system = SYSTEM.format(store=self.s.store_name, currency=self.s.currency)
        system += f"\nToday is {time.strftime('%Y-%m-%d')}."
        system += "\n\nCURRENT CART: " + json.dumps(self.cart_view(chat_id))
        msgs = [{"role": "system", "content": system}]
        msgs += history + [{"role": "user", "content": user_text}]
        new_turns = [{"role": "user", "content": user_text}]
        for step in range(max_steps):
            last = step == max_steps - 1  # final step: force a plain-text answer
            if last:
                msgs.append({"role": "system", "content": "Answer the customer now using the tool results above. Do not call tools."})
            resp = await self._sales_call(msgs, chat_id, reply, last)
            m = resp.choices[0].message
            if not m.tool_calls:
                reply.text = (m.content or "").strip() or "🙂"
                new_turns.append({"role": "assistant", "content": reply.text})
                break
            assistant = {"role": "assistant", "content": m.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name,
                                                      "arguments": tc.function.arguments}}
                                        for tc in m.tool_calls]}
            msgs.append(assistant)
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self.run_tool(tc.function.name, args, chat_id, reply)
                reply.tools_used.append(tc.function.name)
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
        else:
            reply.text = "Sorry, I got a bit lost there. Could you say that again?"
        # store only plain user/assistant turns to keep history compact and valid
        self.store.save_history(chat_id, history + new_turns)
        return reply
