import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass


@dataclass
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    seller_chat_id: str = os.getenv("SELLER_CHAT_ID", "")
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_env: str = os.getenv("PAYPAL_ENV", "sandbox")
    paypal_webhook_id: str = os.getenv("PAYPAL_WEBHOOK_ID", "")
    currency: str = os.getenv("CURRENCY", "USD")
    # Otari (Mozilla.ai) LLM gateway: one OpenAI-compatible endpoint in front of every provider.
    # Note the path is /api/v1, not /v1.
    otari_api_key: str = os.getenv("OTARI_API_KEY", "")
    otari_base_url: str = os.getenv("OTARI_BASE_URL", "https://api.otari.ai/api/v1")
    # LLM_* still work for any other OpenAI-compatible endpoint; by default they point at Otari.
    llm_api_key: str = os.getenv("LLM_API_KEY") or os.getenv("OTARI_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL") or os.getenv("OTARI_BASE_URL", "https://api.otari.ai/api/v1")
    llm_model: str = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    # App-side fallback chain, tried in order if the primary model / routing policy fails.
    llm_fallback_models: str = os.getenv("LLM_FALLBACK_MODELS",
                                         "openai/gpt-oss-120b,deepseek/deepseek-v4-flash,meta/llama-3.3-70b-instruct")
    # Distinct roles, each served by an open-weight model through Otari
    guard_model: str = os.getenv("GUARD_MODEL", "meta/llama-guard-4-12b")       # safety guardrail
    scout_model: str = os.getenv("SCOUT_MODEL", "deepseek/deepseek-v4-flash")      # web-search trend scout
    quote_model: str = os.getenv("QUOTE_MODEL", "deepseek/deepseek-v4-flash")   # code-execution quotes
    guard_enabled: bool = os.getenv("GUARD_ENABLED", "true").lower() == "true"
    web_search_enabled: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
    code_exec_enabled: bool = os.getenv("CODE_EXEC_ENABLED", "true").lower() == "true"
    # Serve the catalog as an MCP server at /mcp and hand it to Otari (mcp_servers) on each agent call
    mcp_enabled: bool = os.getenv("MCP_ENABLED", "true").lower() == "true"
    mcp_token: str = os.getenv("MCP_TOKEN", "")
    # Spending limits enforced by the app on top of Otari's own budgets/ceilings (USD, rolling 24h)
    per_customer_daily_usd: float = float(os.getenv("PER_CUSTOMER_DAILY_USD", "0.05"))
    app_daily_usd: float = float(os.getenv("APP_DAILY_USD", "0.50"))
    inr_rate: float = float(os.getenv("INR_PER_USD", "84.0"))  # indicative rate for quotes
    telegram_enabled: bool = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
    # Render sets RENDER_EXTERNAL_URL automatically, so PUBLIC_BASE_URL is optional there.
    public_base_url: str = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
                            or "http://localhost:8000").rstrip("/")
    # "webhook" (best for hosts that sleep, like Render free) or "polling" (local dev).
    telegram_mode: str = os.getenv("TELEGRAM_MODE", "webhook" if os.getenv("RENDER_EXTERNAL_URL") else "polling")
    store_name: str = os.getenv("STORE_NAME", "Noor Boutique")
    db_path: str = os.getenv("DB_PATH", "shoppal.db")
    catalog_path: str = os.getenv("CATALOG_PATH", "data/catalog.json")

    @property
    def model_chain(self) -> list[str]:
        return [self.llm_model] + [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def mcp_url(self) -> str:
        """Public URL Otari can reach for our MCP server (https only), else ''."""
        if self.mcp_enabled and self.public_base_url.startswith("https://"):
            return f"{self.public_base_url}/mcp"
        return ""

    @property
    def paypal_base(self) -> str:
        return ("https://api-m.paypal.com" if self.paypal_env == "live"
                else "https://api-m.sandbox.paypal.com")


settings = Settings()
