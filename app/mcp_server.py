"""A tiny MCP server (streamable HTTP, stateless JSON responses) exposing the shop catalog.

Otari connects to it during a chat completion (``mcp_servers`` in the request), so
the sales model can search the catalog through MCP while cart/checkout stay as the
app's own function tools. Any other MCP client can use it too.
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "search_products",
     "description": "Search the shop catalog by keywords and optional filters. Returns up to 5 products "
                    "with sku, name, price, sizes, color and stock.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords, e.g. 'red kurta'"},
         "max_price": {"type": "number"}, "category": {"type": "string"},
         "size": {"type": "string"}, "color": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_product",
     "description": "Full details (description, sizes, stock, price) for one SKU.",
     "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
     "annotations": {"readOnlyHint": True}},
]


def _brief(p):
    return {k: p[k] for k in ("sku", "name", "price", "sizes", "color", "stock") if k in p}


def call_tool(catalog, name: str, args: dict) -> dict:
    if name == "search_products":
        res = catalog.search(**{k: v for k, v in (args or {}).items()
                                if k in ("query", "max_price", "category", "size", "color")})
        return {"results": [_brief(p) for p in res]}
    if name == "get_product":
        return catalog.get((args or {}).get("sku")) or {"error": "unknown SKU"}
    raise KeyError(name)


def handle(catalog, msg: dict, currency: str = "USD") -> Any:
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if mid is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        v = params.get("protocolVersion")
        return ok({"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "shoppal-catalog", "version": "1.0.0"},
                   "instructions": f"Read-only product catalog. Prices are in {currency}."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        try:
            data = call_tool(catalog, name, params.get("arguments") or {})
        except KeyError:
            return err(-32602, f"unknown tool {name}")
        return ok({"content": [{"type": "text", "text": json.dumps(data)}],
                   "structuredContent": data, "isError": "error" in data})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"method not found: {method}")
