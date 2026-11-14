"""Application settings. Paper trading only. No wallet or API secrets."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MEME HUNTER AI"
    app_env: str = "development"
    app_version: str = "0.1.0"
    database_url: str = "sqlite:///./meme_hunter.db"
    paper_trading_enabled: bool = True
    paper_starting_balance: float = Field(default=100, gt=0)
    paper_auto_trading: bool = False
    paper_fee_bps: float = Field(default=30.0, ge=0, le=500)
    paper_max_slippage_bps: float = Field(default=50.0, ge=0, le=500)
    paper_slippage_bps: float = Field(default=50.0, ge=0)
    paper_max_quote_age_seconds: float = Field(default=60.0, gt=0, le=3600)
    paper_max_allocation_pct: float = Field(default=0.10, gt=0, le=1)
    paper_max_exposure_pct: float = Field(default=0.50, gt=0, le=1)
    paper_max_daily_realized_loss_usd: float = Field(default=10.0, ge=0)
    paper_max_open_positions: int = Field(default=3, ge=1, le=25)
    paper_min_liquidity_usd: float = Field(default=10000.0, ge=0)
    paper_quote_cache_seconds: float = Field(default=5.0, ge=0, le=60)
    paper_quote_max_move_multiple: float = Field(default=10.0, gt=1)
    paper_decision_scan_limit: int = Field(default=25, ge=1, le=200)
    paper_decision_rescan_seconds: int = Field(default=300, ge=0, le=86400)
    paper_auto_entry_enabled: bool = False
    paper_auto_entry_interval_minutes: float = Field(default=5.0, gt=0, le=1440)
    paper_auto_entry_max_tokens: int = Field(default=10, ge=1, le=50)
    paper_auto_entry_allocation_pct: float = Field(default=0.05, gt=0, le=0.10)
    paper_auto_entry_lease_seconds: float = Field(default=60.0, gt=0, le=600)
    paper_auto_exit_enabled: bool = False
    paper_auto_exit_interval_seconds: float = Field(default=60.0, gt=0, le=3600)
    paper_auto_exit_lease_seconds: float = Field(default=60.0, gt=0, le=600)
    paper_stop_loss_pct: float = Field(default=0.10, gt=0, lt=1)
    paper_take_profit_pct: float = Field(default=0.20, gt=0, lt=10)
    paper_trailing_stop_pct: float = Field(default=0.10, gt=0, lt=1)
    paper_trailing_activate_pct: float = Field(default=0.15, gt=0, lt=10)
    paper_max_hold_hours: float = Field(default=24.0, gt=0, le=720)

    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_timeout_seconds: float = 10.0
    dexscreener_max_retries: int = 3
    dexscreener_token_batch_size: int = 30

    discovery_min_liquidity_usd: float = 10000
    discovery_min_volume_24h_usd: float = 5000
    discovery_max_age_hours: float = 48
    discovery_min_txns_24h: int = 10
    discovery_refresh_stored_tokens: bool = True
    discovery_refresh_stored_limit: int = 200
    discovery_recommended_interval_minutes: int = 15
    auto_discovery_enabled: bool = True
    auto_discovery_interval_minutes: float = Field(default=15, gt=0, le=1440)
    discovery_scan_deadline_seconds: float = Field(default=600.0, gt=0, le=840)
    discovery_startup_catchup_delay_seconds: float = Field(default=2.0, ge=0, le=60)
    dexscreener_request_deadline_seconds: float = Field(default=20.0, gt=0, le=120)

    news_timeout_seconds: float = 10.0
    news_max_items_per_feed: int = 40

    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = ""
    x_bearer_token: str = ""
    telegram_bot_token: str = ""
    telegram_public_channels: str = ""

    narrative_score_mention_growth: float = 0.25
    narrative_score_engagement_growth: float = 0.20
    narrative_score_source_diversity: float = 0.15
    narrative_score_recency: float = 0.15
    narrative_score_acceleration: float = 0.15
    narrative_score_persistence: float = 0.10
    narrative_recent_hours: float = 6
    narrative_match_min_confidence: float = 0.35

    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_rpc_timeout_seconds: float = 15.0
    solana_rpc_max_retries: int = 2
    solana_rpc_max_backoff_seconds: float = Field(default=5.0, ge=0, le=30)
    solana_rpc_stop_after_429: int = Field(default=2, ge=1, le=20)
    solana_rpc_cooldown_base_seconds: float = Field(default=5.0, ge=0, le=300)
    solana_rpc_cooldown_max_seconds: float = Field(default=60.0, ge=0, le=600)
    creator_signature_page_limit: int = Field(default=20, ge=1, le=50)
    security_min_completeness: float = 0.50
    security_min_confidence: float = 0.50
    security_high_risk_threshold: float = 60
    security_rescan_seconds: int = 300
    security_scan_limit: int = 25
    creator_scan_limit: int = Field(default=5, ge=1, le=25)
    risk_weight_authorities: float = 0.20
    risk_weight_holders: float = 0.20
    risk_weight_liquidity: float = 0.25
    risk_weight_trading: float = 0.15
    risk_weight_deployer: float = 0.20

    opportunity_weight_momentum: float = 0.25
    opportunity_weight_narrative: float = 0.25
    opportunity_weight_discovery: float = 0.20
    opportunity_weight_liquidity: float = 0.10
    opportunity_max_security_penalty: float = 20.0
    opportunity_min_completeness: float = 0.40
    opportunity_min_confidence: float = 0.50
    opportunity_high_threshold: float = 70.0
    opportunity_watch_threshold: float = 45.0
    opportunity_stale_snapshot_hours: float = 6.0
    opportunity_rescan_seconds: int = 300
    opportunity_scan_limit: int = 50

    sqlite_backup_enabled: bool = True
    sqlite_backup_on_startup: bool = True
    sqlite_backup_dir: str = "./data/backups"
    sqlite_backup_retain: int = Field(default=14, ge=1, le=365)
    sqlite_backup_min_interval_seconds: float = Field(default=900.0, ge=0, le=86400)

    evaluation_fee_bps: float = Field(default=30.0, ge=0)
    evaluation_slippage_bps: float = Field(default=50.0, ge=0)
    evaluation_max_gap_minutes: float = Field(default=30.0, gt=0)
    evaluation_min_sample_for_claims: int = Field(default=30, ge=1)

    quality_extreme_move_multiple: float = Field(default=10.0, gt=1)
    quality_extreme_reversal: float = Field(default=0.90, gt=0, lt=1)
    quality_stale_hours: float = Field(default=6.0, gt=0)
    quality_liquidity_dislocation_ratio: float = Field(default=20.0, gt=1)

    @field_validator("paper_slippage_bps")
    @classmethod
    def slippage_within_max(cls, value: float, info) -> float:
        maximum = info.data.get("paper_max_slippage_bps", 50.0)
        if value > maximum:
            raise ValueError("PAPER_SLIPPAGE_BPS cannot exceed PAPER_MAX_SLIPPAGE_BPS")
        return value

    @field_validator("auto_discovery_interval_minutes")
    @classmethod
    def validate_auto_discovery_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("AUTO_DISCOVERY_INTERVAL_MINUTES must be greater than 0")
        if value > 1440:
            raise ValueError("AUTO_DISCOVERY_INTERVAL_MINUTES must be at most 1440 (24 hours)")
        return value

    @property
    def sqlite_backend(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def auto_discovery_interval_seconds(self) -> float:
        return float(self.auto_discovery_interval_minutes) * 60.0

    @property
    def paper_auto_entry_interval_seconds(self) -> float:
        return float(self.paper_auto_entry_interval_minutes) * 60.0

    def effective_scan_deadline_seconds(self) -> float:
        """Hard cap for one scan. Always strictly below the collection interval."""
        cap = self.auto_discovery_interval_seconds * 0.8
        return max(0.05, min(float(self.discovery_scan_deadline_seconds), cap))


@lru_cache
def get_settings() -> Settings:
    return Settings()
