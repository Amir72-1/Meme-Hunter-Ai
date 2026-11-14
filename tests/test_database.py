"""Database connectivity and schema tests against the test SQLite file."""

from sqlalchemy import inspect

from app.database import Base, check_database_connection, get_engine, get_session_factory
from app.models import (
    CreatorObservation,
    TokenAccountOwnerObservation,
    MetaplexCreatorObservation,
    DiscoveryScan,
    HolderAnalysis,
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    Narrative,
    NarrativeMention,
    NarrativeScoreHistory,
    NewsItem,
    RiskScoreHistory,
    SocialPost,
    Token,
    TokenNarrativeMatch,
    OpportunityAssessment,
    PaperDecision,
    PaperEquitySnapshot,
    PaperAutoEntryAttempt,
    PaperAutoEntryCycle,
    PaperAutoEntryLock,
    PaperAutoExitAttempt,
    PaperAutoExitCycle,
    PaperPositionObservation,
    PaperMarketQuote,
    PaperOpportunityDecision,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperTrade,
    RpcCooldownState,
    TokenRiskAssessment,
    TokenSecurityScan,
    WalletActivity,
)


def test_database_connectivity() -> None:
    assert check_database_connection() is True
    session = get_session_factory()()
    try:
        assert check_database_connection(session) is True
    finally:
        session.close()


def test_model_tables_are_created() -> None:
    inspector = inspect(get_engine())
    tables = set(inspector.get_table_names())
    assert "tokens" in tables
    assert "market_snapshots" in tables
    assert "narratives" in tables
    assert "news_items" in tables
    assert "discovery_scans" in tables
    assert "catalog_backup_events" in tables
    assert "social_posts" in tables
    assert "narrative_mentions" in tables
    assert "token_narrative_matches" in tables
    assert "narrative_score_history" in tables
    assert "intelligence_scans" in tables
    assert "token_security_scans" in tables
    assert "holder_analyses" in tables
    assert "liquidity_analyses" in tables
    assert "wallet_activities" in tables
    assert "token_risk_assessments" in tables
    assert "risk_score_history" in tables
    assert "opportunity_assessments" in tables
    assert OpportunityAssessment.__tablename__ in Base.metadata.tables
    assert TokenSecurityScan.__tablename__ in Base.metadata.tables
    assert HolderAnalysis.__tablename__ in Base.metadata.tables
    assert LiquidityAnalysisRecord.__tablename__ in Base.metadata.tables
    assert WalletActivity.__tablename__ in Base.metadata.tables
    assert TokenRiskAssessment.__tablename__ in Base.metadata.tables
    assert RiskScoreHistory.__tablename__ in Base.metadata.tables
    assert SocialPost.__tablename__ in Base.metadata.tables
    assert NarrativeMention.__tablename__ in Base.metadata.tables
    assert TokenNarrativeMatch.__tablename__ in Base.metadata.tables
    assert NarrativeScoreHistory.__tablename__ in Base.metadata.tables
    assert IntelligenceScan.__tablename__ in Base.metadata.tables
    assert DiscoveryScan.__tablename__ in Base.metadata.tables
    assert "paper_portfolios" in tables
    assert "paper_orders" in tables
    assert "paper_positions" in tables
    assert "paper_trades" in tables
    assert "paper_equity_snapshots" in tables
    assert "paper_decisions" in tables
    assert "paper_market_quotes" in tables
    assert "paper_opportunity_decisions" in tables
    assert "paper_auto_entry_cycles" in tables
    assert "paper_auto_entry_attempts" in tables
    assert "paper_auto_entry_locks" in tables
    assert "paper_auto_exit_cycles" in tables
    assert "paper_auto_exit_attempts" in tables
    assert "paper_position_observations" in tables
    assert PaperAutoEntryCycle.__tablename__ in Base.metadata.tables
    assert PaperAutoEntryAttempt.__tablename__ in Base.metadata.tables
    assert PaperAutoEntryLock.__tablename__ in Base.metadata.tables
    assert PaperAutoExitCycle.__tablename__ in Base.metadata.tables
    assert PaperAutoExitAttempt.__tablename__ in Base.metadata.tables
    assert PaperPositionObservation.__tablename__ in Base.metadata.tables
    lock_columns = {column["name"] for column in inspector.get_columns("paper_auto_entry_locks")}
    assert "cycle_kind" in lock_columns
    assert PaperMarketQuote.__tablename__ in Base.metadata.tables
    assert PaperOpportunityDecision.__tablename__ in Base.metadata.tables
    assert PaperPortfolio.__tablename__ in Base.metadata.tables
    assert PaperOrder.__tablename__ in Base.metadata.tables
    assert PaperPosition.__tablename__ in Base.metadata.tables
    assert PaperTrade.__tablename__ in Base.metadata.tables
    assert PaperEquitySnapshot.__tablename__ in Base.metadata.tables
    assert PaperDecision.__tablename__ in Base.metadata.tables
    assert "rpc_cooldown_states" in tables
    assert RpcCooldownState.__tablename__ in Base.metadata.tables
    cooldown_columns = {column["name"] for column in inspector.get_columns("rpc_cooldown_states")}
    assert {
        "scope",
        "until_at",
        "reason",
        "retry_after_seconds",
        "backoff_seconds",
        "consecutive_429",
        "activated_at",
        "cleared_at",
        "last_http_429_at",
        "updated_at",
    }.issubset(cooldown_columns)
    assert "creator_observations" in tables
    assert CreatorObservation.__tablename__ in Base.metadata.tables
    creator_columns = {column["name"] for column in inspector.get_columns("creator_observations")}
    assert {
        "token_id",
        "mint_address",
        "token_program",
        "inspection_status",
        "mint_init_signature",
        "mint_init_slot",
        "mint_init_block_time",
        "mint_init_signer",
        "mint_init_status",
        "fee_payer",
        "signer_addresses_json",
        "create_program_id",
        "create_instruction",
        "source",
        "observed_at",
        "recorded_at",
        "attempted_at",
        "slot",
        "parser_notes",
        "errors_json",
    }.issubset(creator_columns)
    assert "token_account_owner_observations" in tables
    assert TokenAccountOwnerObservation.__tablename__ in Base.metadata.tables
    owner_columns = {column["name"] for column in inspector.get_columns("token_account_owner_observations")}
    assert {
        "token_id",
        "mint_address",
        "token_account_address",
        "token_account_owner",
        "token_account_amount",
        "token_account_status",
        "owner_classification",
        "classification_status",
        "classification_evidence",
        "token_program",
        "source",
        "observed_at",
        "recorded_at",
        "attempted_at",
        "slot",
        "parser_notes",
        "errors_json",
    }.issubset(owner_columns)
    assert "metaplex_creator_observations" in tables
    assert MetaplexCreatorObservation.__tablename__ in Base.metadata.tables
    meta_cols = {column["name"] for column in inspector.get_columns("metaplex_creator_observations")}
    assert {
        "token_id",
        "mint_address",
        "metadata_account",
        "metadata_status",
        "creator_vector_status",
        "creators_json",
        "creator_count",
        "verified_creator_count",
        "source",
        "observed_at",
        "recorded_at",
        "attempted_at",
        "slot",
        "parser_notes",
        "errors_json",
    }.issubset(meta_cols)
    token_columns = {column["name"] for column in inspector.get_columns("tokens")}
    snapshot_columns = {column["name"] for column in inspector.get_columns("market_snapshots")}
    assert "discovery_source" in token_columns
    assert "filter_status" in token_columns
    assert "fdv" in snapshot_columns
    risk_columns = {column["name"] for column in inspector.get_columns("token_risk_assessments")}
    assert "observed_risk" in risk_columns
    assert "observed_risk_band" in risk_columns
    opportunity_columns = {column["name"] for column in inspector.get_columns("opportunity_assessments")}
    assert "snapshot_count" in opportunity_columns
    assert "last_snapshot_at" in opportunity_columns
    assert "narrative_match_count" in opportunity_columns
    assert "security_coverage" in opportunity_columns
    assert "active_boosts" in snapshot_columns
    assert Token.__tablename__ in Base.metadata.tables
    assert MarketSnapshot.__tablename__ in Base.metadata.tables
    assert Narrative.__tablename__ in Base.metadata.tables
    assert NewsItem.__tablename__ in Base.metadata.tables


def test_token_address_is_unique() -> None:
    inspector = inspect(get_engine())
    indexes = inspector.get_indexes("tokens")
    unique_indexes = [index for index in indexes if index.get("unique")]
    indexed_columns = {tuple(index["column_names"]) for index in unique_indexes}
    assert ("token_address",) in indexed_columns
