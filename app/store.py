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
