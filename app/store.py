"""SQLite persistence for carts, orders and conversation history."""
import json
import sqlite3
import threading
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS carts (chat_id TEXT, sku TEXT, size TEXT, qty INTEGER,
    PRIMARY KEY (chat_id, sku, size));
CREATE TABLE IF NOT EXISTS orders (paypal_order_id TEXT PRIMARY KEY, chat_id TEXT,
    items TEXT, total REAL, currency TEXT, status TEXT, created REAL, updated REAL,
    buyer_name TEXT, capture_id TEXT);
CREATE TABLE IF NOT EXISTS history (chat_id TEXT PRIMARY KEY, messages TEXT);
CREATE TABLE IF NOT EXISTS usage (ts REAL, end_user TEXT, role TEXT, requested TEXT, model TEXT,
    provider TEXT, otari_fallback INTEGER, app_fallback INTEGER, attempts INTEGER, cost_usd REAL,
    prompt_tokens INTEGER, completion_tokens INTEGER, request_id TEXT, latency_ms INTEGER, tools TEXT);
CREATE INDEX IF NOT EXISTS usage_user_ts ON usage (end_user, ts);
CREATE TABLE IF NOT EXISTS mcp_log (ts REAL, sid TEXT, turn TEXT, tool TEXT, args TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # cart
    def add_to_cart(self, chat_id, sku, size, qty):
        self._exec("""INSERT INTO carts VALUES (?,?,?,?)
            ON CONFLICT(chat_id, sku, size) DO UPDATE SET qty = qty + excluded.qty""",
                   (str(chat_id), sku, size, qty))

    def remove_from_cart(self, chat_id, sku, size=None):
        if size:
            self._exec("DELETE FROM carts WHERE chat_id=? AND sku=? AND size=?", (str(chat_id), sku, size))
        else:
            self._exec("DELETE FROM carts WHERE chat_id=? AND sku=?", (str(chat_id), sku))

    def cart(self, chat_id) -> list[dict]:
        rows = self._exec("SELECT sku, size, qty FROM carts WHERE chat_id=?", (str(chat_id),)).fetchall()
        return [dict(r) for r in rows]

    def clear_cart(self, chat_id):
        self._exec("DELETE FROM carts WHERE chat_id=?", (str(chat_id),))

    # orders
    def save_order(self, paypal_order_id, chat_id, items, total, currency, status="CREATED"):
        now = time.time()
        self._exec("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (paypal_order_id, str(chat_id), json.dumps(items), total, currency, status, now, now, None, None))

    def update_order(self, paypal_order_id, status, buyer_name=None, capture_id=None):
        self._exec("""UPDATE orders SET status=?, updated=?, buyer_name=COALESCE(?, buyer_name),
            capture_id=COALESCE(?, capture_id) WHERE paypal_order_id=?""",
                   (status, time.time(), buyer_name, capture_id, paypal_order_id))

    def order(self, paypal_order_id) -> Optional[dict]:
        r = self._exec("SELECT * FROM orders WHERE paypal_order_id=?", (paypal_order_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["items"] = json.loads(d["items"])
        return d

    def orders_for(self, chat_id, limit=5) -> list[dict]:
        rows = self._exec("SELECT * FROM orders WHERE chat_id=? ORDER BY created DESC LIMIT ?",
                          (str(chat_id), limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"])
            out.append(d)
        return out

    # history
    def history(self, chat_id) -> list[dict]:
        r = self._exec("SELECT messages FROM history WHERE chat_id=?", (str(chat_id),)).fetchone()
        return json.loads(r["messages"]) if r else []

    def save_history(self, chat_id, messages, keep=20):
        self._exec("INSERT OR REPLACE INTO history VALUES (?,?)", (str(chat_id), json.dumps(messages[-keep:])))

    def reset_history(self, chat_id):
        self._exec("DELETE FROM history WHERE chat_id=?", (str(chat_id),))

    # usage (one row per Otari call: model served, provider, fallback, cost)
    def log_usage(self, info, end_user=""):
        self._exec("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), str(end_user or ""), info.role, info.requested, info.served_model,
                    info.provider, int(info.otari_fallback), int(info.app_fallback), info.attempts,
                    info.cost_usd, info.prompt_tokens, info.completion_tokens, info.request_id,
                    info.latency_ms, json.dumps(info.otari_tools)))

    def spend_since(self, since_ts: float, end_user=None) -> float:
        if end_user is None:
            r = self._exec("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE ts>=?", (since_ts,)).fetchone()
        else:
            r = self._exec("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE ts>=? AND end_user=?",
                           (since_ts, str(end_user))).fetchone()
        return float(r["s"])

    def usage_summary(self) -> dict:
        def rows(sql, args=()):
            return [dict(r) for r in self._exec(sql, args).fetchall()]
        by_role = rows("""SELECT role, COUNT(*) calls, SUM(cost_usd) cost_usd,
            SUM(prompt_tokens+completion_tokens) tokens, SUM(otari_fallback) otari_fallbacks,
            SUM(app_fallback) app_fallbacks, CAST(AVG(latency_ms) AS INTEGER) avg_ms
            FROM usage GROUP BY role ORDER BY cost_usd DESC""")
        by_model = rows("""SELECT model, provider, COUNT(*) calls, SUM(cost_usd) cost_usd
            FROM usage GROUP BY model, provider ORDER BY calls DESC""")
        by_user = rows("""SELECT end_user, COUNT(*) calls, SUM(cost_usd) cost_usd
            FROM usage WHERE ts>=? GROUP BY end_user ORDER BY cost_usd DESC LIMIT 20""",
                       (time.time() - 86400,))
        recent = rows("""SELECT ts, end_user, role, requested, model, provider, otari_fallback, app_fallback,
            cost_usd, latency_ms, tools, request_id FROM usage ORDER BY ts DESC LIMIT 25""")
        tot = self._exec("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM usage").fetchone()
        mcp_n = self._exec("SELECT COUNT(*) n FROM mcp_log").fetchone()["n"]
        return {"mcp_tool_calls": mcp_n, "mcp_recent": self.mcp_recent(), "total_calls": tot["n"], "total_cost_usd": round(tot["c"], 6), "by_role": by_role,
                "by_model": by_model, "by_customer_24h": by_user, "recent": recent}

    # MCP calls received from Otari (Otari runs the MCP loop server-side, so we log them on arrival)
    def log_mcp(self, sid, turn, tool, args):
        self._exec("INSERT INTO mcp_log VALUES (?,?,?,?,?)",
                   (time.time(), str(sid or ""), str(turn or ""), tool, json.dumps(args)[:500]))

    def mcp_calls(self, turn) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT tool, args FROM mcp_log WHERE turn=? ORDER BY ts",
                                            (str(turn),)).fetchall()]

    def mcp_recent(self, limit=15) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT ts, sid, tool, args FROM mcp_log ORDER BY ts DESC LIMIT ?",
                                            (limit,)).fetchall()]
