"""ShopPal: Telegram AI sales agent with PayPal checkout.

One process runs both the Telegram bot (long polling) and a FastAPI server
for PayPal return/cancel pages and webhooks.
"""
import asyncio
import contextlib
import hashlib
import html
import logging
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .agent import ShopAgent
from .catalog import Catalog
from .config import settings
from .paypal import PayPal, PayPalError
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shoppal")

catalog = Catalog.load(settings.catalog_path)
store = Store(settings.db_path)
paypal = (PayPal(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base)
          if settings.paypal_client_id else None)
# All model traffic goes through the Otari gateway (OpenAI-compatible, base URL .../api/v1).
llm = AsyncOpenAI(api_key=settings.llm_api_key or "missing", base_url=settings.llm_base_url)
agent = ShopAgent(llm, settings.llm_model, catalog, store, paypal, settings)
tg: Application | None = None
# Secret Telegram echoes on every webhook call, derived from the bot token so no extra env var is needed.
TG_SECRET = hashlib.sha256(f"shoppal:{settings.telegram_token}".encode()).hexdigest()[:48]


# ---------------- Telegram ----------------
def to_html(text: str) -> str:
    """Render the LLM's light Markdown (**bold**, *italic*, `code`) as Telegram HTML."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.reset_history(update.effective_chat.id)
    await update.message.reply_text(
        f"👋 Namaste! I'm the shopping assistant for {settings.store_name}.\n"
        "Tell me what you're looking for, e.g. \"red kurta under $30, size M\" or \"Eid outfit for men\".\n"
        "Commands: /cart /orders /reset")


async def whoami(update: Update, ctx):
    await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


async def cart_cmd(update: Update, ctx):
    c = agent.cart_view(update.effective_chat.id)
    if not c["items"]:
        await update.message.reply_text("🛒 Your cart is empty.")
        return
    lines = [f'• {i["qty"]}× {i["name"]} ({i["size"]}) — {i["subtotal"]:.2f}' for i in c["items"]]
    await update.message.reply_text("🛒 Cart\n" + "\n".join(lines) + f'\nTotal: {c["total"]:.2f} {c["currency"]}'
                                    "\nSay \"checkout\" when you're ready.")


async def orders_cmd(update: Update, ctx):
    os_ = store.orders_for(update.effective_chat.id)
    if not os_:
        await update.message.reply_text("No orders yet.")
        return
    await update.message.reply_text("\n".join(
        f'#{o["paypal_order_id"][-6:]} — {o["total"]:.2f} {o["currency"]} — {o["status"]}' for o in os_))


async def reset_cmd(update: Update, ctx):
    store.reset_history(update.effective_chat.id)
    await update.message.reply_text("Conversation reset. What can I find for you?")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        r = await agent.respond(chat_id, update.message.text)
    except Exception:
        log.exception("agent failed")
        await update.message.reply_text("Sorry, something went wrong on my side. Please try again in a moment.")
        return
    markup = None
    if r.pay_url:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay with PayPal", url=r.pay_url)]])
    await update.message.reply_text(to_html(r.text), reply_markup=markup, parse_mode=ParseMode.HTML)


def build_bot() -> Application:
    app = (Application.builder().token(settings.telegram_token)
           .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(20)
           .get_updates_read_timeout(40).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("cart", cart_cmd))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


# ---------------- Payment completion ----------------
async def finalize_order(order_id: str, source: str) -> dict | None:
    """Capture (if needed) and notify buyer + seller exactly once."""
    o = store.order(order_id)
    if not o or o["status"] == "COMPLETED":
        return o
    try:
        pp = await paypal.get_order(order_id)
        if pp["status"] == "APPROVED":
            pp = await paypal.capture_order(order_id)
    except PayPalError:
        log.exception("capture failed for %s", order_id)
        return o
    if pp.get("status") != "COMPLETED":
        store.update_order(order_id, pp.get("status", "UNKNOWN"))
        return store.order(order_id)
    payer = pp.get("payer", {}).get("name", {})
    buyer = " ".join(filter(None, [payer.get("given_name"), payer.get("surname")])) or "Customer"
    cap_id = None
    with contextlib.suppress(Exception):
        cap_id = pp["purchase_units"][0]["payments"]["captures"][0]["id"]
    store.update_order(order_id, "COMPLETED", buyer, cap_id)
    log.info("order %s completed via %s", order_id, source)
    if tg:
        items = "\n".join(f'• {i["qty"]}× {i["name"]} ({i["size"]})' for i in o["items"])
        await tg.bot.send_message(int(o["chat_id"]),
                                  f"✅ Payment received! Order #{order_id[-6:]}\n{items}\n"
                                  f'Total {o["total"]:.2f} {o["currency"]}. We\'ll ship it soon 🙏')
        if settings.seller_chat_id:
            ship = pp.get("purchase_units", [{}])[0].get("shipping", {}).get("address", {})
            addr = ", ".join(filter(None, [ship.get("address_line_1"), ship.get("admin_area_2"),
                                           ship.get("postal_code"), ship.get("country_code")]))
            await tg.bot.send_message(int(settings.seller_chat_id),
                                      f"🛍️ <b>New paid order</b> #{order_id[-6:]}\nBuyer: {html.escape(buyer)}\n"
                                      f"{html.escape(items)}\nTotal: {o['total']:.2f} {o['currency']}\n"
                                      f"Ship to: {html.escape(addr or 'see PayPal')}",
                                      parse_mode=ParseMode.HTML)
    return store.order(order_id)


# ---------------- FastAPI ----------------
api = FastAPI(title="ShopPal")

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:system-ui;max-width:420px;margin:15vh auto;text-align:center">
<h1>{emoji}</h1><h2>{title}</h2><p>{body}</p></body>"""


@api.get("/")
async def health():
    return {"ok": True, "store": settings.store_name, "products": len(catalog.products),
            "telegram": "ready" if tg else "connecting", "paypal": bool(paypal)}


@api.get("/paypal/return", response_class=HTMLResponse)
async def paypal_return(token: str = ""):
    o = await finalize_order(token, "return") if token and paypal else None
    if o and o["status"] == "COMPLETED":
        return PAGE.format(emoji="✅", title="Payment complete",
                           body="Thank you! You can close this tab and go back to Telegram.")
    return PAGE.format(emoji="⏳", title="Processing", body="We're confirming your payment. Check Telegram shortly.")


@api.get("/paypal/cancel", response_class=HTMLResponse)
async def paypal_cancel(token: str = ""):
    if token and store.order(token):
        store.update_order(token, "CANCELLED")
    return PAGE.format(emoji="↩️", title="Checkout cancelled",
                       body="No payment was taken. Return to Telegram if you'd like to try again.")


@api.post("/paypal/webhook")
async def paypal_webhook(request: Request):
    event = await request.json()
    if settings.paypal_webhook_id and paypal:
        try:
            if not await paypal.verify_webhook(dict(request.headers), event, settings.paypal_webhook_id):
                return JSONResponse({"error": "bad signature"}, status_code=400)
        except PayPalError:
            log.exception("webhook verification error")
            return JSONResponse({"error": "verification failed"}, status_code=400)
    etype = event.get("event_type", "")
    res = event.get("resource", {})
    order_id = None
    if etype == "CHECKOUT.ORDER.APPROVED":
        order_id = res.get("id")
    elif etype == "PAYMENT.CAPTURE.COMPLETED":
        order_id = res.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
    if order_id and paypal:
        await finalize_order(order_id, f"webhook:{etype}")
    return {"ok": True}


@api.on_event("startup")
async def on_startup():
    if not settings.telegram_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; running API only")
        return
    # Connect to Telegram in the background so a slow Telegram API never stops the web
    # server (and the PayPal return/webhook endpoints) from coming up.
    asyncio.create_task(start_telegram())


async def start_telegram(attempts: int = 10):
    global tg
    for i in range(1, attempts + 1):
        app = build_bot()
        try:
            await app.initialize()
            await app.start()
            if settings.telegram_mode == "webhook" and settings.public_base_url.startswith("https://"):
                await app.bot.set_webhook(f"{settings.public_base_url}/telegram/webhook",
                                          secret_token=TG_SECRET,
                                          allowed_updates=["message", "callback_query"])
                log.info("Telegram webhook set (attempt %d)", i)
            else:
                await app.bot.delete_webhook()
                await app.updater.start_polling(drop_pending_updates=True)
                log.info("Telegram bot polling started (attempt %d)", i)
            tg = app
            return
        except Exception as e:  # network hiccup or bad token: log and retry with backoff
            log.warning("Telegram start attempt %d failed: %r", i, e)
            with contextlib.suppress(Exception):
                await app.shutdown()
            await asyncio.sleep(min(5 * i, 60))
    log.error("Telegram bot could not start; API endpoints still running")


@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        raise HTTPException(status_code=403)
    if not tg:
        raise HTTPException(status_code=503)  # still connecting; Telegram retries
    await tg.process_update(Update.de_json(await request.json(), tg.bot))
    return {"ok": True}


@api.on_event("shutdown")
async def on_shutdown():
    if tg:
        if tg.updater.running:
            await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()
