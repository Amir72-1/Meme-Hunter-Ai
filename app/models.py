"""Initial SQLAlchemy models. Tables are empty until real ingest exists."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UtcDateTime
from app.timeutil import as_utc, utc_now


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="solana")
    dex_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    pair_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovery_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filter_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    filter_reasons: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    market_snapshots: Mapped[list[MarketSnapshot]] = relationship(
        back_populates="token",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_tokens_token_address", "token_address", unique=True),
        Index("ix_tokens_symbol", "symbol"),
        Index("ix_tokens_chain", "chain"),
        Index("ix_tokens_discovery_source", "discovery_source"),
        Index("ix_tokens_filter_status", "filter_status"),
        Index("ix_tokens_pair_created_at", "pair_created_at"),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"),
        nullable=False,
    )
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    fdv: Mapped[float | None] = mapped_column(Float, nullable=True)
    buys_5m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sells_5m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buys_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sells_1h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buys_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sells_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dex_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    pair_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_boosts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovery_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filter_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    filter_reasons: Mapped[str | None] = mapped_column(String(256), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    token: Mapped[Token] = relationship(back_populates="market_snapshots")

    __table_args__ = (
        Index("ix_market_snapshots_token_id", "token_id"),
        Index("ix_market_snapshots_recorded_at", "recorded_at"),
        Index("ix_market_snapshots_token_recorded", "token_id", "recorded_at"),
    )


class Narrative(Base):
    __tablename__ = "narratives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    trend_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mention_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    engagement_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    independent_sources: Mapped[int | None] = mapped_column(Integer, nullable=True)
    independent_authors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence: Mapped[float | None] = mapped_column(Float, nullable=True)
    spam_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    news_items: Mapped[list[NewsItem]] = relationship(back_populates="narrative")
    mentions: Mapped[list[NarrativeMention]] = relationship(back_populates="narrative")
    token_matches: Mapped[list[TokenNarrativeMatch]] = relationship(back_populates="narrative")
    score_history: Mapped[list[NarrativeScoreHistory]] = relationship(back_populates="narrative")

    __table_args__ = (
        Index("ix_narratives_name", "name", unique=True),
        Index("ix_narratives_category", "category"),
        Index("ix_narratives_last_detected_at", "last_detected_at"),
    )


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    feed_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    narrative_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    narrative: Mapped[Narrative | None] = relationship(back_populates="news_items")

    __table_args__ = (
        Index("ix_news_items_url", "url", unique=True),
        Index("ix_news_items_source", "source"),
        Index("ix_news_items_published_at", "published_at"),
        Index("ix_news_items_narrative_id", "narrative_id"),
        Index("ix_news_items_normalized_title", "normalized_title"),
        Index("ix_news_items_category", "category"),
    )


class SocialPost(Base):
    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    narrative_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    narrative: Mapped[Narrative | None] = relationship()

    __table_args__ = (
        Index("ix_social_posts_platform", "platform"),
        Index("ix_social_posts_url", "url", unique=True),
        Index("ix_social_posts_published_at", "published_at"),
        Index("ix_social_posts_narrative_id", "narrative_id"),
    )


class NarrativeMention(Base):
    __tablename__ = "narrative_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    narrative_id: Mapped[int] = mapped_column(
        ForeignKey("narratives.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engagement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    narrative: Mapped[Narrative] = relationship(back_populates="mentions")

    __table_args__ = (
        Index("ix_narrative_mentions_narrative_id", "narrative_id"),
        Index("ix_narrative_mentions_source", "source_type", "source_id"),
        Index("ix_narrative_mentions_observed_at", "observed_at"),
    )


class TokenNarrativeMatch(Base):
    __tablename__ = "token_narrative_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    narrative_id: Mapped[int] = mapped_column(
        ForeignKey("narratives.id", ondelete="CASCADE"),
        nullable=False,
    )
    matching_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    token: Mapped[Token] = relationship()
    narrative: Mapped[Narrative] = relationship(back_populates="token_matches")

    __table_args__ = (
        Index("ix_token_narrative_matches_unique", "token_id", "narrative_id", unique=True),
        Index("ix_token_narrative_matches_confidence", "confidence"),
    )


class NarrativeScoreHistory(Base):
    __tablename__ = "narrative_score_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    narrative_id: Mapped[int] = mapped_column(
        ForeignKey("narratives.id", ondelete="CASCADE"),
        nullable=False,
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    mention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mention_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    engagement_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_diversity: Mapped[float | None] = mapped_column(Float, nullable=True)
    recency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence: Mapped[float | None] = mapped_column(Float, nullable=True)
    spam_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    components_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    narrative: Mapped[Narrative] = relationship(back_populates="score_history")

    __table_args__ = (
        Index("ix_narrative_score_history_narrative_id", "narrative_id"),
        Index("ix_narrative_score_history_recorded_at", "recorded_at"),
    )


class IntelligenceScan(Base):
    __tablename__ = "intelligence_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_type: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    items_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_intelligence_scans_type_started", "scan_type", "started_at"),
    )


class DiscoveryScan(Base):
    __tablename__ = "discovery_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    profiles_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_boosts_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_boosts_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unique_solana_addresses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    below_threshold_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_discovery_scans_started_at", "started_at"),)


class TokenSecurityScan(Base):
    __tablename__ = "token_security_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    mint_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    mint_authority_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    freeze_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    freeze_authority_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    token_program: Mapped[str | None] = mapped_column(String(88), nullable=True)
    token_standard: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supply: Mapped[float | None] = mapped_column(Float, nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_mutability: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    metadata_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_account: Mapped[str | None] = mapped_column(String(88), nullable=True)
    metadata_update_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    metadata_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_is_mutable: Mapped[int | None] = mapped_column(Integer, nullable=True)
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_token_security_scans_token_scanned", "token_id", "scanned_at"),)


class Token2022ExtensionObservation(Base):
    """Append-only Token-2022 mint extension inspection. Historical rows are never rewritten."""

    __tablename__ = "token_2022_extension_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    token_program: Mapped[str | None] = mapped_column(String(88), nullable=True)
    inspection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    extension_types_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    transfer_fee_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_fee_max: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_fee_config_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    transfer_fee_withdraw_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    transfer_hook_program_id: Mapped[str | None] = mapped_column(String(88), nullable=True)
    transfer_hook_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    permanent_delegate: Mapped[str | None] = mapped_column(String(88), nullable=True)
    default_account_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pausable_paused: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pausable_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    mint_close_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    confidential_transfer_present: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidential_auto_approve: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_pointer_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    metadata_pointer_authority: Mapped[str | None] = mapped_column(String(88), nullable=True)
    token_metadata_present: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scaled_ui_amount_multiplier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_complete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parser_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_token_2022_ext_obs_token_observed", "token_id", "observed_at"),
    )


class HolderAnalysis(Base):
    __tablename__ = "holder_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    top_holder_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    top5_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    top10_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_accounts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    concentration_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    holders_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_holder_analyses_token_scanned", "token_id", "scanned_at"),)


class LiquidityAnalysisRecord(Base):
    __tablename__ = "liquidity_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="dexscreener_snapshots")
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pool_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    pool_age_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    lock_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    lock_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_liquidity_analyses_token_scanned", "token_id", "scanned_at"),)


class CreatorObservation(Base):
    """Append-only mint-initialization evidence. Historical rows are never rewritten."""

    __tablename__ = "creator_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    token_program: Mapped[str | None] = mapped_column(String(88), nullable=True)
    inspection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    mint_init_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mint_init_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_init_block_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_init_signer: Mapped[str | None] = mapped_column(String(88), nullable=True)
    mint_init_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    fee_payer: Mapped[str | None] = mapped_column(String(88), nullable=True)
    signer_addresses_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_program_id: Mapped[str | None] = mapped_column(String(88), nullable=True)
    create_instruction: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parser_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_creator_observations_token_observed", "token_id", "observed_at"),
        Index("ix_creator_observations_mint", "mint_address"),
    )


class TokenAccountOwnerObservation(Base):
    """Append-only token-account owner classification. Historical rows are never rewritten."""

    __tablename__ = "token_account_owner_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    token_account_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    token_account_owner: Mapped[str | None] = mapped_column(String(88), nullable=True)
    token_account_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_account_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owner_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    classification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    classification_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_program: Mapped[str | None] = mapped_column(String(88), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parser_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_token_acct_owner_obs_token_observed", "token_id", "observed_at"),
        Index("ix_token_acct_owner_obs_account", "token_id", "token_account_address"),
    )


class MetaplexCreatorObservation(Base):
    """Append-only Metaplex creators[] evidence. Historical rows are never rewritten."""

    __tablename__ = "metaplex_creator_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    metadata_account: Mapped[str | None] = mapped_column(String(88), nullable=True)
    metadata_status: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_vector_status: Mapped[str] = mapped_column(String(32), nullable=False)
    creators_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    creator_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_creator_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parser_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_metaplex_creator_obs_token_observed", "token_id", "observed_at"),
        Index("ix_metaplex_creator_obs_mint", "mint_address"),
    )


class WalletActivity(Base):
    __tablename__ = "wallet_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="solana_rpc")
    deployer_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    deployer_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_wallet_activities_token_scanned", "token_id", "scanned_at"),)


class TokenRiskAssessment(Base):
    __tablename__ = "token_risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_risk_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    completeness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    category_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_token_risk_assessments_token_scanned", "token_id", "scanned_at"),)


class OpportunityAssessment(Base):
    __tablename__ = "opportunity_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ranking_label: Mapped[str] = mapped_column(String(32), nullable=False)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    positive_opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    security_adjustment: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    data_completeness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    available_components_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_components_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    component_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    positive_signals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_signals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_age_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    narrative_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    security_coverage: Mapped[str | None] = mapped_column(String(32), nullable=True)

    token: Mapped[Token] = relationship()

    __table_args__ = (
        Index("ix_opportunity_assessments_token_assessed", "token_id", "assessed_at"),
        Index("ix_opportunity_assessments_status", "status"),
        Index("ix_opportunity_assessments_score", "opportunity_score"),
    )


class RiskScoreHistory(Base):
    __tablename__ = "risk_score_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_risk_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    completeness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    category_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_risk_score_history_token_scanned", "token_id", "scanned_at"),)


class HistoricalEvaluation(Base):
    """Append-only summaries of market-only historical evaluations. Snapshots are never rewritten."""

    __tablename__ = "historical_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    fee_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tokens_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcomes_measured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_gap: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_missing_valuation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_missing_outcome: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_insufficient_history: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_historical_evaluations_started_at", "started_at"),)


class DataQualityScan(Base):
    """Append-only scan of stored snapshots. Does not rewrite market_snapshots."""

    __tablename__ = "data_quality_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    tokens_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clean_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspicious_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_data_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    liquidity_non_null: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    liquidity_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_data_quality_scans_started_at", "started_at"),)


class DataQualityFinding(Base):
    __tablename__ = "data_quality_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("data_quality_scans.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    liquidity_non_null: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    liquidity_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_data_quality_findings_scan_token", "scan_id", "token_id"),
        Index("ix_data_quality_findings_classification", "classification"),
    )


class QualityHistoricalEvaluation(Base):
    """Additive quality-aware evaluation run. Does not rewrite historical_evaluations."""

    __tablename__ = "quality_historical_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    fee_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tokens_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcomes_measured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_quality_historical_evaluations_started_at", "started_at"),)


class QualityHistoricalOutcome(Base):
    """One measured historical path with time-specific quality flags. Missing fields stay null."""

    __tablename__ = "quality_historical_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("quality_historical_evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_valuation: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_valuation: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_valuation_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    end_valuation_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    end_pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    gross_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    exclusion_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_same_pair: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_same_pair_and_valuation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_complete_liquidity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_quality_filtered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_suspicious_or_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overlaps_prior_window: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_quality_historical_outcomes_eval", "evaluation_id"),
        Index("ix_quality_historical_outcomes_token", "token_id"),
        Index("ix_quality_historical_outcomes_class", "quality_classification"),
    )


class PointInTimeRun(Base):
    """Additive point-in-time validation run. Does not rewrite snapshots or prior evaluations."""

    __tablename__ = "point_in_time_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    t0: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    holdout_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidates_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_point_in_time_runs_started_at", "started_at"),)


class PointInTimeCandidate(Base):
    __tablename__ = "point_in_time_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("point_in_time_runs.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    token_address: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    in_g0: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    in_g1: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_g2: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_g3: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    opportunity_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ranking_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    overlap_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_point_in_time_candidates_run", "run_id"),
        Index("ix_point_in_time_candidates_token", "token_id"),
        Index("ix_point_in_time_candidates_split", "split"),
    )


class RpcCooldownState(Base):
    """Operational Solana RPC cooldown. Not evidence; singleton row per scope."""

    __tablename__ = "rpc_cooldown_states"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    until_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    backoff_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    consecutive_429: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_http_429_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)


class PointInTimeOutcome(Base):
    __tablename__ = "point_in_time_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("point_in_time_runs.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("point_in_time_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    coverage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gross_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_quality_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_point_in_time_outcomes_run", "run_id"),
        Index("ix_point_in_time_outcomes_candidate", "candidate_id"),
        Index("ix_point_in_time_outcomes_horizon", "horizon"),
    )


class PaperMarketQuote(Base):
    """On-demand DexScreener quote. Append-only and distinct from discovery snapshots."""

    __tablename__ = "paper_market_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    dex_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    fdv: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="dexscreener_on_demand")
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    market_observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    provider_timestamp_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    freshness_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pair_match_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    discontinuity_flag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_quote_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_hit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_paper_market_quotes_token_retrieved", "token_id", "retrieved_at"),
        Index("ix_paper_market_quotes_mint", "mint_address"),
    )


class PaperPortfolio(Base):
    """Simulated PAPER_ONLY book. Never a live wallet."""

    __tablename__ = "paper_portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, default="default")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="PAPER_ONLY")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    starting_cash_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    cash_available_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    cash_reserved_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    realized_pnl_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    fees_paid_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    slippage_paid_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_paper_portfolios_name", "name", unique=True),)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_notional_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paper_quote_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_price_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    simulated_price_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fill_quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fill_notional_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slippage_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slippage_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assumptions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("portfolio_id", "client_order_id", name="uq_paper_orders_client_id"),
        Index("ix_paper_orders_portfolio", "portfolio_id"),
        Index("ix_paper_orders_created", "created_at"),
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    quantity: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    average_cost_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    cost_basis_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    realized_pnl_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("portfolio_id", "mint_address", name="uq_paper_positions_mint"),
        Index("ix_paper_positions_portfolio", "portfolio_id"),
    )


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("paper_positions.id", ondelete="SET NULL"), nullable=True)
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_price_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    simulated_price_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    notional_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    fee_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    slippage_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    realized_pnl_usd: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    quote_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_trades_portfolio", "portfolio_id"),
        Index("ix_paper_trades_order", "order_id"),
        Index("ix_paper_trades_created", "created_at"),
    )


class PaperEquitySnapshot(Base):
    __tablename__ = "paper_equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    cash_available_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    cash_reserved_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    positions_market_value_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unrealized_pnl_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realized_pnl_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    equity_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valuation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    assumptions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_equity_portfolio_recorded", "portfolio_id", "recorded_at"),
    )


class PaperDecision(Base):
    __tablename__ = "paper_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_decisions_portfolio", "portfolio_id"),
        Index("ix_paper_decisions_created", "created_at"),
    )


class PaperOpportunityDecision(Base):
    """Append-only opportunity BUY_CANDIDATE/WAIT/REJECT record. Not an order."""

    __tablename__ = "paper_opportunity_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_decision_id: Mapped[int] = mapped_column(
        ForeignKey("paper_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    opportunity_assessment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    security_assessment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paper_quote_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opportunity_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    opportunity_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    security_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    security_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    gates_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orders_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_paper_opp_decisions_token_decided", "token_id", "decided_at"),
        Index("ix_paper_opp_decisions_result", "result"),
        Index("ix_paper_opp_decisions_paper_decision", "paper_decision_id"),
    )


class PaperAutoEntryLock(Base):
    """Singleton lease. Fail closed if a cycle is already running."""

    __tablename__ = "paper_auto_entry_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cycle_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cycle_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class PaperAutoEntryCycle(Base):
    """Append-only automatic paper-entry cycle. Simulated fills only."""

    __tablename__ = "paper_auto_entry_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    tokens_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    waiting: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_paper_auto_entry_cycles_started", "started_at"),)


class PaperAutoEntryAttempt(Base):
    """One token attempt inside an auto-entry cycle."""

    __tablename__ = "paper_auto_entry_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("paper_auto_entry_cycles.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paper_decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    notional_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_auto_entry_attempts_cycle", "cycle_id"),
        Index("ix_paper_auto_entry_attempts_mint", "mint_address"),
    )


class PaperPositionObservation(Base):
    """Append-only valid or rejected mark used for trailing highs. Never backfilled from the future."""

    __tablename__ = "paper_position_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id", ondelete="CASCADE"), nullable=False)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    retrieved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pair_address: Mapped[str | None] = mapped_column(String(88), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    freshness_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paper_quote_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    high_water_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_active: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_position_obs_position_observed", "position_id", "observed_at"),
    )


class PaperAutoExitCycle(Base):
    """Append-only automatic paper-exit cycle. Simulated sells only."""

    __tablename__ = "paper_auto_exit_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    positions_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_paper_auto_exit_cycles_started", "started_at"),)


class PaperAutoExitAttempt(Base):
    __tablename__ = "paper_auto_exit_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("paper_auto_exit_cycles.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mint_address: Mapped[str] = mapped_column(String(88), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_paper_auto_exit_attempts_cycle", "cycle_id"),
        Index("ix_paper_auto_exit_attempts_mint", "mint_address"),
    )


class SqliteBackupEvent(Base):
    __tablename__ = "catalog_backup_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    backup_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    integrity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skipped_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_catalog_backup_events_started", "started_at"),)
