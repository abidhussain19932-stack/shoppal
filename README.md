# ShopPal: an AI sales agent that sells and gets paid inside Telegram

**Demo video (2 min):** https://youtu.be/oycBfRE6OjY  
**Try the bot:** https://t.me/shoppalpaypalbot (PayPal Sandbox) · **Live API:** https://shoppal-fp5x.onrender.com


## Built with Otari 🦦 (Mozilla.ai "Devs Wilding Online" hackathon)

**Live demo (web chat, PayPal Sandbox):** https://shoppal-otari.onrender.com · **Usage & cost dashboard:** https://shoppal-otari.onrender.com/stats · branch [`otari-hackathon`](https://github.com/abidhussain19932-stack/shoppal/tree/otari-hackathon)

On this branch every model call ShopPal makes goes through the hosted [Otari](https://otari.ai) gateway (`https://api.otari.ai/api/v1`, OpenAI-compatible), and every model is **open-weight**.

| Role | Model (via Otari) | Otari feature |
|---|---|---|
| Sales agent (tool calling, cart, checkout) | `shoppal-chat` routing policy → `openai/gpt-oss-120b`, falls back to `deepseek/deepseek-v4-flash`, then `meta/llama-3.3-70b-instruct` | Model routing & fallbacks (policy in Otari + app-side chain) |
| Safety guardrail on every customer message | `meta/llama-guard-4-12b` | Guardrails (Llama Guard 4 served through Otari) |
| Trend scout ("what's trending for Diwali?") | `deepseek/deepseek-v4-flash` + `otari_web_search` | Built-in web search (sources returned in `otari_tool_calls`) |
| Bulk/wholesale quote calculator | `deepseek/deepseek-v4-flash` + `otari_code_execution` | Sandboxed code execution (result cross-checked by the app) |
| Catalog lookups | ShopPal's own MCP server at `/mcp`, passed in `mcp_servers` | Managed MCP: Otari connects to our server and runs the tool loop |

**Cost, budgets and per-user limits**
- Every response's `usage.cost_usd` plus the `Otari-Model`, `Otari-Provider`, `Otari-Fallback`, `Otari-Attempt-Count` and `Otari-Request-Id` headers are recorded per call (`app/otari.py`) and shown under each chat reply and on [`/stats`](https://shoppal-otari.onrender.com/stats) (also `/admin/usage`, JSON at `/api/stats`).
- Requests are tagged with Otari `metadata` (`app`, `role`, `end_user`) so spend can be sliced by customer and role in Otari's Usage view.
- **Gateway budget:** the Otari workspace has a monthly budget with a spend ceiling (Spend & Budgets). When Otari answers `403 budget_exceeded`, ShopPal replies politely instead of crashing.
- **Per-user budget:** each customer (Telegram chat or web session) has a rolling 24 h spend cap (`PER_CUSTOMER_DAILY_USD`, default $0.05), metered from Otari's per-request `cost_usd`; there is also an app-wide cap (`APP_DAILY_USD`). Otari's own end-user budgets need a service key, which the hosted dashboard didn't offer at the time of building, so this cap is enforced in the app.

```
customer ──▶ Llama Guard 4 ──unsafe──▶ polite refusal
                │ safe
                ▼
      sales agent (Otari policy "shoppal-chat": gpt-oss-120b ↪ deepseek-v4-flash ↪ llama-3.3-70b)
        ├─ MCP (Otari ⇄ ShopPal /mcp): search_products, get_product
        ├─ fashion_trends ─▶ deepseek-v4-flash + otari_web_search
        ├─ bulk_quote ────▶ deepseek-v4-flash + otari_code_execution
        └─ add_to_cart / create_checkout ─▶ PayPal Orders v2 (Sandbox)
```

### Setup
1. Create an Otari workspace API key at [otari.ai](https://otari.ai) (Keys). The base URL is `https://api.otari.ai/api/v1` (note `/api/v1`; `/v1` returns 404).
2. In the Otari dashboard: Routing → Create policy → Fallback named `shoppal-chat` (main `openai/gpt-oss-120b`, backups `deepseek/deepseek-v4-flash`, `meta/llama-3.3-70b-instruct`); Tools → Web search → allow web access; Spend & Budgets → add a budget and a workspace ceiling. Without the policy the app falls back to its own chain automatically.
3. `cp .env.example .env` and set at least `OTARI_API_KEY`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`. Then `uvicorn app.main:api --port 8000` and open http://localhost:8000.

| Env var | Default | Purpose |
|---|---|---|
| `OTARI_API_KEY` | (required) | Otari workspace API key |
| `OTARI_BASE_URL` | `https://api.otari.ai/api/v1` | Otari gateway |
| `LLM_MODEL` | `openai/gpt-oss-120b` (Render: `shoppal-chat`) | Model or Otari routing policy for the sales agent |
| `LLM_FALLBACK_MODELS` | `openai/gpt-oss-120b,deepseek/deepseek-v4-flash,meta/llama-3.3-70b-instruct` | App-side fallback chain |
| `GUARD_MODEL` / `GUARD_ENABLED` | `meta/llama-guard-4-12b` / `true` | Safety guardrail |
| `SCOUT_MODEL` / `WEB_SEARCH_ENABLED` | `deepseek/deepseek-v4-flash` / `true` | Web-search trend scout |
| `QUOTE_MODEL` / `CODE_EXEC_ENABLED` | `deepseek/deepseek-v4-flash` / `true` | Code-execution quotes |
| `MCP_ENABLED` / `MCP_TOKEN` | `true` / empty | Serve `/mcp` (https deployments) and protect it with a bearer token |
| `PER_CUSTOMER_DAILY_USD` / `APP_DAILY_USD` | `0.05` / `0.50` | Spend caps (rolling 24 h) |
| `TELEGRAM_ENABLED` | `true` | Set `false` to run only the web chat |
| `LLM_API_KEY` / `LLM_BASE_URL` | Otari values | Optional override for any other OpenAI-compatible endpoint |

**Demo tip:** tick *simulate outage* in the web chat to prepend a model that doesn't exist; Otari rejects it and the reply badge shows the app falling back to the `shoppal-chat` policy.

Tests (`pytest -q`) mock Otari: header/cost parsing, routing fallback, `budget_exceeded`, guardrail blocking, the MCP server protocol, MCP hand-off, code-execution quotes, web-search trends and spend caps.


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
