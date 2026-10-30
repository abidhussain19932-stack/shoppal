# ShopPal: an AI sales agent that sells and gets paid inside Telegram

**Demo video (2 min):** https://youtu.be/oycBfRE6OjY  
**Try the bot:** https://t.me/shoppalpaypalbot (PayPal Sandbox) · **Live API:** https://shoppal-fp5x.onrender.com


ShopPal turns any small shop's catalog into a Telegram salesperson. Buyers chat naturally in English, Hindi or Hinglish ("lal kurta chahiye size M, $30 ke andar"). An LLM agent searches the catalog, answers questions, builds the cart and creates a **PayPal checkout** without leaving the chat. When PayPal confirms the payment, the buyer and the seller are both notified instantly.

Built for the **PayPal AI Hackathon 2026** (Best Use of Agentic Commerce / Best Use of PayPal + AI).

## Why
Millions of small sellers in India and elsewhere already sell through chat apps, but they handle every message by hand and chase payments with screenshots. ShopPal answers instantly, 24/7, and closes the sale with a trusted PayPal checkout.

## How it works
```
Buyer ──Telegram──▶ ShopPal agent (LLM + tools) ──▶ catalog / cart (SQLite)
                         │ create_checkout
                         ▼
                 PayPal Orders v2 API ──▶ buyer approves on PayPal
                         │ return URL + webhook (CHECKOUT.ORDER.APPROVED / PAYMENT.CAPTURE.COMPLETED)
                         ▼
             capture order ──▶ ✅ buyer receipt + 🛍️ seller notification (with shipping address)
```

**Agent tools:** `search_products`, `get_product`, `add_to_cart`, `remove_from_cart`, `view_cart`, `create_checkout`, `order_status`. The model never invents prices: every product fact comes from a tool, and an order counts as paid only after PayPal returns `COMPLETED`.

**PayPal features used:** OAuth client credentials, Orders v2 (create with itemised breakdown, `payment_source.paypal.experience_context`, get, capture), idempotency keys (`PayPal-Request-Id`), webhook signature verification (`/v1/notifications/verify-webhook-signature`).

**AI:** any OpenAI-compatible chat model with function calling (default: Groq `openai/gpt-oss-120b`; also works with OpenAI `gpt-4o-mini`, Gemini or OpenRouter).

## Run it locally
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. At [developer.paypal.com](https://developer.paypal.com), open Apps & Credentials, then Sandbox, create an app, and copy its Client ID and Secret. Sandbox buyer test accounts are under Testing Tools, then Sandbox Accounts.
3. Get an LLM API key (for example a free one from [console.groq.com](https://console.groq.com)).
4. Then:
```bash
git clone <this repo> && cd shoppal
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values
uvicorn app.main:api --port 8000
```
5. Message your bot `/start`, then something like *"I need a red kurta under $30 in M"*. Send `/whoami` and put that id in `SELLER_CHAT_ID` to receive seller alerts.

For PayPal's return URL and webhooks to reach a local machine, expose port 8000 with a tunnel (for example `cloudflared tunnel --url http://localhost:8000`) and set `PUBLIC_BASE_URL` to that address. The return page also captures the payment, so webhooks are optional for local testing.

## Deploy (Render, free)
Click **New, then Blueprint**, point it at this repo (it reads `render.yaml`), and fill in the secret env vars. Set `PUBLIC_BASE_URL` to the Render URL. In your PayPal sandbox app, add a webhook to `https://<your-app>.onrender.com/paypal/webhook` for `CHECKOUT.ORDER.APPROVED` and `PAYMENT.CAPTURE.COMPLETED`, then copy its id into `PAYPAL_WEBHOOK_ID`.

## Customise
Edit `data/catalog.json` (sku, name, category, price, sizes, color, description, stock) and set `STORE_NAME` and `CURRENCY`.

## Tests
```bash
pip install pytest && pytest -q
```
The tests run the whole purchase flow against a mocked PayPal API and a scripted LLM.

## Commands
`/start` · `/cart` · `/orders` · `/reset` · `/whoami`

## License
MIT
