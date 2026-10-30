"""LLM shopping agent with tool calls over the catalog, cart and PayPal checkout."""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .catalog import Catalog
from .paypal import PayPal, PayPalError
from .store import Store

log = logging.getLogger(__name__)

SYSTEM = """You are ShopPal, the friendly sales assistant of {store}, an Indian ethnic-wear shop, chatting on Telegram.
- Reply in the customer's language (English, Hindi or Hinglish). Keep replies short: 1-4 lines, a few emojis at most.
- Search once per distinct item (combine filters in one call); don't repeat the same search.
- ALWAYS use tools for products, prices, stock, cart and orders. Never invent products or prices. Prices are in {currency}.
- When recommending, show at most 3 items as: name, price, SKU, and sizes.
- Only add items when the customer asks to buy/add them. Check CURRENT CART below first so you never add an item twice.
- Before adding to cart, make sure you know the size (if the item has more than one) and quantity (default 1).
- When the customer is ready to pay, call create_checkout. The PayPal button is attached automatically; just tell them to tap it.
- Payment is confirmed only by PayPal; never claim an order is paid unless order_status says COMPLETED.
- Politely decline anything unrelated to shopping at {store}."""

TOOLS = [
    {"type": "function", "function": {"name": "search_products", "description": "Search the catalog.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords e.g. 'red kurta'"},
         "max_price": {"type": "number"}, "category": {"type": "string"},
         "size": {"type": "string"}, "color": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_product", "description": "Full details for a SKU.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}}},
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
]


@dataclass
class Reply:
    text: str
    pay_url: Optional[str] = None
    order_id: Optional[str] = None
    tools_used: list = field(default_factory=list)


class ShopAgent:
    def __init__(self, llm, model: str, catalog: Catalog, store: Store, paypal: Optional[PayPal],
                 settings):
        self.llm, self.model, self.catalog, self.store, self.paypal = llm, model, catalog, store, paypal
        self.s = settings

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
        return {"error": f"unknown tool {name}"}

    # ---------- loop ----------
    async def respond(self, chat_id, user_text: str, max_steps: int = 8) -> Reply:
        reply = Reply(text="")
        history = self.store.history(chat_id)
        system = SYSTEM.format(store=self.s.store_name, currency=self.s.currency)
        system += "\n\nCURRENT CART: " + json.dumps(self.cart_view(chat_id))
        msgs = [{"role": "system", "content": system}]
        msgs += history + [{"role": "user", "content": user_text}]
        new_turns = [{"role": "user", "content": user_text}]
        for step in range(max_steps):
            last = step == max_steps - 1  # final step: force a plain-text answer
            if last:
                msgs.append({"role": "system", "content": "Answer the customer now using the tool results above. Do not call tools."})
                resp = await self.llm.chat.completions.create(model=self.model, messages=msgs, temperature=0.3)
            else:
                resp = await self.llm.chat.completions.create(model=self.model, messages=msgs, tools=TOOLS,
                                                              temperature=0.3)
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
