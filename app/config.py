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
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    store_name: str = os.getenv("STORE_NAME", "Noor Boutique")
    db_path: str = os.getenv("DB_PATH", "shoppal.db")
    catalog_path: str = os.getenv("CATALOG_PATH", "data/catalog.json")

    @property
    def paypal_base(self) -> str:
        return ("https://api-m.paypal.com" if self.paypal_env == "live"
                else "https://api-m.sandbox.paypal.com")


settings = Settings()
