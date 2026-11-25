"""Settings from the environment (backend/.env). Secrets never leave the backend."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase project (Settings > API in the Supabase dashboard)
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""   # backend only, never sent to the browser

    # LLM: "azure" (Azure OpenAI deployments) or "openai"
    llm_provider: str = "azure"
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-5"
    azure_openai_embedding_deployment: str = "text-embedding-ada-002"
    azure_openai_api_version: str = "2025-01-01-preview"
    openai_model: str = "gpt-5"
    openai_embedding_model: str = "text-embedding-3-small"
    llm_daily_token_cap: int = 400_000        # all model calls together, per day

    # TradingView webhooks: accept alerts only from TradingView's four addresses (set false only to test locally)
    webhook_require_tradingview_ip: bool = True

    openai_api_key: str = ""

    # Live trading through Zerodha Kite (Phase 8). OFF unless LIVE_TRADING_ENABLED=true here AND you arm it
    # in the app each day with two-factor sign-in AND you logged in to Kite today.
    live_trading_enabled: bool = False
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_algo_id: str = ""                    # only if Zerodha assigns your app an algo ID (SEBI 2026 framework)
    token_encryption_key: str = ""            # Fernet key for the daily Kite token: see README "Live trading"
    web_url: str = "http://localhost:3100"    # where the Kite login sends you back to

    # Running on a server: every /api route needs a signed-in user, and only these emails may use the app
    require_auth: bool = False
    allowed_emails: str = ""                  # comma separated; empty = anyone who can sign in to your Supabase

    # Global setup (XAUUSD): live spot prices, and the slow history download that runs while the app is up
    twelvedata_api_key: str = ""
    global_backfill_enabled: bool = True
    global_backfill_start: str = "2021-01-01"
    # The gold agent (paper): GPT-5 looks at the live market when something happens and proposes trades for you to confirm
    global_agent_enabled: bool = True
    global_agent_looks_per_day: int = 16      # each look is one strategist call (plus a reviewer call for a trade idea)
    global_agent_cooldown_minutes: int = 15   # at most one look this often, unless you press "Ask the agent now"

    # Telegram approval: ideas and paper tickets on your phone, confirmed with a tap. Make a bot with @BotFather and put
    # its token here; then link your chat from the XAUUSD agent page. Only one backend may run with a token at a time.
    telegram_bot_token: str = ""

    # MT5 (demo only until live gold trading is cleared): the bridge runs on the Windows machine next to the MT5 terminal
    # (backend/app/global_markets/mt5/bridge.py). The token is the bridge's BRIDGE_TOKEN; nothing here places orders yet.
    mt5_bridge_url: str = ""                  # e.g. http://127.0.0.1:8710
    mt5_bridge_token: str = ""

    # NSE option-chain recorder: a NIFTY chain snapshot every 5 minutes in market hours, saved under backend/data
    chain_recorder_enabled: bool = True

    # App
    mode: str = "paper"                   # "paper" until a live adapter exists and you switch it
    paper_starting_capital: float = 1_000_000.0
    cors_origins: str = "http://localhost:3100"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_email_list(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret and self.token_encryption_key)

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "azure":
            return bool(self.azure_openai_endpoint and self.azure_openai_api_key)
        return bool(self.openai_api_key)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
