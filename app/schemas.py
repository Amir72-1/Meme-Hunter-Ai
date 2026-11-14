"""Pydantic response schemas. No secrets are included."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RootResponse(BaseModel):
    name: str
    version: str


class HealthResponse(BaseModel):
    status: str
    environment: str
    database_connected: bool
    paper_trading_enabled: bool
    paper_auto_trading: bool
    paper_starting_balance: float


class DatabaseStatus(BaseModel):
    connected: bool
    backend: str


class PaperTradingStatus(BaseModel):
    enabled: bool
    auto_trading: bool
    starting_balance: float
    live_trading: bool = False
    mode: str = "paper"


class ApplicationStatus(BaseModel):
    name: str
    version: str
    environment: str
    status: str


class SystemStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application: ApplicationStatus
    database: DatabaseStatus
    paper_trading: PaperTradingStatus
    message: str = Field(
        default="Paper trading only. No live wallet or private keys are used.",
    )


class ScanIssueOut(BaseModel):
    stage: str
    detail: str
    token_address: str | None = None
    category: str | None = None
    retryable: bool | None = None


class DiscoveryScanResponse(BaseModel):
    status: str
    profiles_fetched: int
    latest_boosts_fetched: int
    top_boosts_fetched: int
    unique_solana_addresses: int
    tokens_processed: int
    snapshots_saved: int
    stored_tokens_queued: int = 0
    below_threshold_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[ScanIssueOut]
    skipped: list[ScanIssueOut]
    safety_note: str = (
        "DexScreener listing is not a safety approval. Paper trading only."
    )


class DiscoveryStatusResponse(BaseModel):
    integration: str
    configured: bool
    live_verified: bool
    base_url: str
    last_scan_at: datetime | None
    last_scan_status: str
    last_scan: DiscoveryScanResponse | None
    note: str


class SchedulerStatusResponse(BaseModel):
    enabled: bool
    running: bool
    scan_in_progress: bool
    interval_minutes: float
    last_started_at: datetime | str | None
    last_finished_at: datetime | str | None
    last_status: str
    last_error: str | None
    successful_runs: int
    failed_runs: int
    skipped_runs: int
    next_run_at: datetime | None
    catchup_pending: bool = False
    recovered_abandoned_scan_ids: list[int] = []
    completed_scans_total: int = 0
    failed_scans_total: int = 0
    interrupted_scans_total: int = 0
    last_successful_scan_id: int | None = None
    last_successful_scan_at: datetime | str | None = None
    last_failed_scan_id: int | None = None
    last_failed_scan_at: datetime | str | None = None
    last_scan_duration_seconds: float | None = None
    consecutive_scan_failures: int = 0
    database_identity: dict | None = None
    last_successful_backup: dict | None = None
    note: str


class MarketSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    price_usd: float | None
    market_cap: float | None
    fdv: float | None
    liquidity_usd: float | None
    volume_5m: float | None
    volume_1h: float | None
    volume_24h: float | None
    price_change_5m: float | None
    price_change_1h: float | None
    price_change_24h: float | None
    buys_5m: int | None
    sells_5m: int | None
    buys_1h: int | None
    sells_1h: int | None
    buys_24h: int | None
    sells_24h: int | None
    dex_name: str | None
    pair_address: str | None
    pair_created_at: datetime | None
    active_boosts: int | None
    discovery_source: str | None
    filter_status: str | None
    filter_reasons: str | None
    recorded_at: datetime


class TokenSummaryOut(BaseModel):
    token_address: str
    name: str | None
    symbol: str | None
    chain: str
    dex_name: str | None
    pair_address: str | None
    pair_created_at: datetime | None
    discovery_source: str | None
    last_discovered_at: datetime | None
    filter_status: str | None
    filter_reasons: str | None
    latest_snapshot: MarketSnapshotOut | None
    safety_status: str = "unassessed"


class TokenListResponse(BaseModel):
    items: list[TokenSummaryOut]
    total: int
    limit: int
    offset: int


class TokenDetailResponse(TokenSummaryOut):
    id: int
    created_at: datetime
    updated_at: datetime
    snapshot_note: str = (
        "Snapshots are point-in-time DexScreener observations, not complete candles."
    )


class TokenHistoryResponse(BaseModel):
    token_address: str
    items: list[MarketSnapshotOut]
    snapshot_note: str = (
        "Snapshots are point-in-time DexScreener observations, not complete candles."
    )


class NewsScanResponse(BaseModel):
    status: str
    feeds_attempted: int
    feeds_ok: int
    items_fetched: int
    items_saved: int
    duplicates_skipped: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict]
    skipped_feeds: list[dict]


class SocialAdapterStatusOut(BaseModel):
    platform: str
    status: str
    posts_fetched: int = 0
    detail: str | None = None


class SocialScanResponse(BaseModel):
    status: str
    items_saved: int
    duplicates_skipped: int
    adapters: list[SocialAdapterStatusOut]
    errors: list[dict]
    started_at: datetime | None = None
    finished_at: datetime | None = None


class NarrativeScanResponse(BaseModel):
    status: str
    narratives_upserted: int
    mentions_saved: int
    scores_saved: int
    matches_saved: int = 0
    weak_matches_skipped: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict]


class NewsItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    source: str
    url: str
    published_at: datetime | None
    summary: str | None
    category: str | None
    narrative_id: int | None
    created_at: datetime


class NewsListResponse(BaseModel):
    items: list[NewsItemOut]
    total: int
    limit: int
    offset: int


class TokenNarrativeMatchOut(BaseModel):
    token_address: str | None = None
    token_symbol: str | None = None
    token_name: str | None = None
    narrative_name: str | None = None
    matching_keywords: list[str] = Field(default_factory=list)
    explanation: str
    confidence: float
    method: str
    source_refs: list[str] = Field(default_factory=list)


class NarrativeOut(BaseModel):
    id: int
    name: str
    category: str | None
    description: str | None
    trend_score: float
    mention_count: int | None
    mention_growth: float | None
    engagement_growth: float | None
    independent_sources: int | None
    independent_authors: int | None
    recency_score: float | None
    acceleration: float | None
    persistence: float | None
    spam_score: float | None
    confidence: float | None
    data_completeness: float | None
    explanation: str | None
    keywords: list[str] = Field(default_factory=list)
    first_detected_at: datetime
    last_detected_at: datetime
    matches: list[TokenNarrativeMatchOut] = Field(default_factory=list)


class NarrativeListResponse(BaseModel):
    items: list[NarrativeOut]
    total: int
    limit: int
    offset: int


class IntelligenceStatusResponse(BaseModel):
    news: dict
    social: dict
    narratives: dict
    note: str


class SecurityScanRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)
    force: bool = False


class SecurityScanResponse(BaseModel):
    status: str
    tokens_considered: int
    tokens_scanned: int
    skipped: int
    insufficient_data: int
    high_risk: int
    errors: list[dict]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    safety_note: str = (
        "Paper trading only. A low score is not a safety certification. "
        "UNKNOWN measurements are not treated as safe."
    )


class SecurityStatusResponse(BaseModel):
    solana_rpc_url: str
    solana_rpc_configured: bool
    last_scan_status: str
    last_scan_at: str | None
    tokens_scanned: int | None = None
    note: str


class RiskCategoryOut(BaseModel):
    name: str
    weight: float
    score: float | None
    status: str
    reason: str


class RiskAssessmentOut(BaseModel):
    status: str
    observed_risk: float | None = None
    observed_risk_band: str | None = None
    risk_score: float | None
    risk_band: str | None
    confidence: float
    completeness: float
    classification_reliable: bool = False
    unknown_categories: list[str] = Field(default_factory=list)
    explanation: str
    warnings: list[str] = Field(default_factory=list)
    categories: list[RiskCategoryOut] = Field(default_factory=list)
    scanned_at: datetime


class ControlFindingOut(BaseModel):
    code: str
    severity: str
    status: str
    explanation: str
    evidence: dict = Field(default_factory=dict)
    source_observation_id: int | None = None
    source_observation_kind: str | None = None
    source: str | None = None
    observed_at: datetime | None = None
    slot: int | None = None
    confidence: float = 0.0
    limitations: str = ""
    verified: bool = False


class ControlFindingsResponse(BaseModel):
    token_address: str
    signal_time: datetime
    findings: list[ControlFindingOut]
    note: str = (
        "Explainable evidence only. Findings are not a scam, rug, or honeypot label "
        "and do not change opportunity scores."
    )


class TokenSecurityOut(BaseModel):
    token_address: str
    symbol: str | None
    name: str | None
    assessment: RiskAssessmentOut | None
    mint_authority_status: str | None = None
    freeze_authority_status: str | None = None
    lock_status: str | None = None
    concentration_status: str | None = None
    deployer_status: str | None = None
    metadata_status: str | None = None
    metadata_account: str | None = None
    metadata_update_authority: str | None = None
    metadata_mutability: str | None = None
    token_2022_inspection_status: str | None = None
    token_2022_extension_types: list[str] = Field(default_factory=list)
    token_2022_parser_complete: bool | None = None
    control_findings: list[ControlFindingOut] = Field(default_factory=list)
    observed_at: datetime | None = None
    recorded_at: datetime | None = None
    slot: int | None = None
    source: str | None = None
    safety_note: str = "Never treat this report as a guarantee that a token is safe."


class TokenSecurityHistoryResponse(BaseModel):
    token_address: str
    items: list[RiskAssessmentOut]


class HighRiskListResponse(BaseModel):
    items: list[TokenSecurityOut]
    provisional: list[TokenSecurityOut] = Field(default_factory=list)
    total: int
    provisional_total: int = 0
    limit: int
    offset: int
    note: str = (
        "items are confirmed scored assessments only. "
        "provisional holds LOW_CONFIDENCE or INSUFFICIENT_DATA observations and is not a final classification."
    )


class OpportunityScanRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    force: bool = False


class OpportunityScanResponse(BaseModel):
    status: str
    tokens_considered: int
    tokens_scanned: int
    skipped: int
    scored: int = 0
    low_confidence: int = 0
    insufficient_data: int
    high_opportunity: int = 0
    watchlist: int = 0
    low_opportunity: int = 0
    reliable_classifications: int = 0
    tokens_with_narrative_matches: int = 0
    tokens_with_confirmed_security: int = 0
    tokens_with_single_snapshot: int = 0
    errors: list[dict]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    safety_note: str = (
        "Paper intelligence only. Rankings are not predictions of profit and are not live trades. "
        "tokens_scanned counts processed tokens; reliable_classifications counts SCORED assessments only."
    )


class OpportunityStatusResponse(BaseModel):
    last_scan_status: str
    last_scan_at: str | None
    tokens_scanned: int | None = None
    note: str


class OpportunityComponentOut(BaseModel):
    name: str
    weight: float
    score: float | None
    confidence: float
    status: str
    explanation: str
    metrics: dict = Field(default_factory=dict)


class OpportunityAssessmentOut(BaseModel):
    status: str
    ranking_label: str
    opportunity_score: float | None
    positive_opportunity_score: float | None
    security_adjustment: float
    confidence: float
    data_completeness: float
    available_components: list[str] = Field(default_factory=list)
    missing_components: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    components: list[OpportunityComponentOut] = Field(default_factory=list)
    explanation: str
    assessed_at: datetime
    snapshot_count: int | None = None
    last_snapshot_at: datetime | None = None
    snapshot_age_hours: float | None = None
    narrative_match_count: int | None = None
    security_coverage: str | None = None


class OpportunityFreshnessOut(BaseModel):
    assessed_at: datetime | None = None
    persisted_assessed_at: datetime | None = None
    assessment_snapshot_count: int | None = None
    current_snapshot_count: int
    assessment_age_seconds: float | None = None
    is_stale: bool
    stale_reason: str | None = None
    source: str


class TokenOpportunityOut(BaseModel):
    token_address: str
    symbol: str | None
    name: str | None
    liquidity_usd: float | None = None
    snapshot_count: int = 0
    last_snapshot_at: datetime | None = None
    assessment: OpportunityAssessmentOut | None
    freshness: OpportunityFreshnessOut | None = None
    safety_note: str = (
        "Not a guarantee of returns. High scores with weak evidence are not reliable recommendations."
    )


class OpportunityListResponse(BaseModel):
    items: list[TokenOpportunityOut]
    total: int
    limit: int
    offset: int
    note: str = (
        "Sorted by current opportunity_score descending. Stale persisted assessments are "
        "recomputed from stored snapshots so scores and snapshot counts refer to the same set. "
        "INSUFFICIENT_DATA is not a buy signal. HIGH_OPPORTUNITY requires SCORED status plus "
        "configured score and confidence floors."
    )


class OpportunityHistoryResponse(BaseModel):
    token_address: str
    items: list[OpportunityAssessmentOut]


class HistoricalEvaluateRequest(BaseModel):
    horizon: str = Field(default="next_observation")
    fee_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    token_address: str | None = None


class HistoricalCoverageOut(BaseModel):
    tokens_considered: int
    signals_considered: int
    outcomes_measured: int
    outcome_coverage: float | None = None
    excluded_gap: int
    excluded_missing_valuation: int
    excluded_missing_outcome: int
    excluded_insufficient_history: int
    valuation_changes_measured: int
    volume_changes_measured: int
    liquidity_changes_measured: int
    liquidity_missing_at_signal: int
    liquidity_missing_rate_at_signal: float | None = None
    mfe_mae_measured: int


class HistoricalStatsOut(BaseModel):
    avg: float | None = None
    median: float | None = None
    min: float | None = None
    max: float | None = None
    count: int = 0


class HistoricalCategoryOut(BaseModel):
    sample_size: int
    positive_outcomes: int
    negative_outcomes: int
    zero_outcomes: int
    win_rate: float | None = None
    gross_return: HistoricalStatsOut
    net_return: HistoricalStatsOut


class HistoricalEvaluationOut(BaseModel):
    evaluation_id: int | None = None
    status: str
    horizon: str
    fee_bps: float
    slippage_bps: float
    round_trip_cost: float
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sample_size: int
    positive_outcomes: int
    negative_outcomes: int
    zero_outcomes: int
    win_rate: float | None = None
    gross_return: HistoricalStatsOut
    net_return: HistoricalStatsOut
    max_favorable_excursion: HistoricalStatsOut
    max_adverse_excursion: HistoricalStatsOut
    by_category: dict[str, HistoricalCategoryOut] = Field(default_factory=dict)
    coverage: HistoricalCoverageOut
    claim_ready: bool
    explanation: str
    paper_trading_only: bool = True
    safety_note: str = (
        "Paper intelligence only. Hypothetical fees and slippage are not fills. "
        "Past snapshot paths do not guarantee future profitability."
    )


class HistoricalEvaluationListResponse(BaseModel):
    items: list[HistoricalEvaluationOut]
    total: int
    limit: int
    offset: int


class HistoricalStatusResponse(BaseModel):
    last_status: str
    last_horizon: str | None = None
    last_evaluation_at: str | None = None
    last_sample_size: int | None = None
    note: str


class DataQualityScanRequest(BaseModel):
    token_address: str | None = None


class DataQualityLiquidityOut(BaseModel):
    total_observations: int
    non_null_liquidity: int
    missing_liquidity: int
    missing_pct: float | None = None
    note: str = "Missing liquidity is not treated as zero."


class DataQualityFlagOut(BaseModel):
    code: str
    detail: str
    from_at: datetime | None = None
    to_at: datetime | None = None


class DataQualityTokenOut(BaseModel):
    token_address: str
    symbol: str | None = None
    classification: str
    flags: list[DataQualityFlagOut] = Field(default_factory=list)
    snapshot_count: int
    liquidity: DataQualityLiquidityOut
    explanation: str
    safety_note: str = (
        "CLEAN is not a safety or profit rating. Flags are investigations of stored DexScreener prints."
    )


class DataQualityScanResponse(BaseModel):
    scan_id: int | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tokens_scanned: int
    snapshots_scanned: int
    classifications: dict[str, int]
    flag_token_counts: dict[str, int] = Field(default_factory=dict)
    liquidity: DataQualityLiquidityOut
    thresholds: dict[str, float]
    explanation: str
    paper_trading_only: bool = True
    items: list[DataQualityTokenOut] | None = None


class DataQualityStatusResponse(BaseModel):
    last_status: str
    last_scan_at: str | None = None
    last_tokens_scanned: int | None = None
    last_classifications: dict[str, int] | None = None
    note: str


class QualityHistoricalEvaluateRequest(BaseModel):
    horizon: str = Field(default="next_observation")
    fee_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    token_address: str | None = None


class QualityHistoricalEvaluationOut(BaseModel):
    evaluation_id: int | None = None
    status: str
    horizon: str
    fee_bps: float
    slippage_bps: float
    round_trip_cost: float
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tokens_considered: int
    signals_considered: int
    outcomes_measured: int
    snapshots_scanned: int
    coverage: dict
    cohorts: dict
    overlapping_windows: dict
    thresholds: dict[str, float] = Field(default_factory=dict)
    explanation: str
    look_ahead_safeguard: str | None = None
    paper_trading_only: bool = True
    outcomes: list[dict] | None = None


class QualityHistoricalEvaluationListResponse(BaseModel):
    items: list[QualityHistoricalEvaluationOut]
    total: int
    limit: int
    offset: int


class QualityHistoricalStatusResponse(BaseModel):
    last_status: str
    last_horizon: str | None = None
    last_evaluation_at: str | None = None
    last_sample_size: int | None = None
    last_evaluation_id: int | None = None
    note: str


class PointInTimeEvaluateRequest(BaseModel):
    token_address: str | None = None


class PointInTimeRunOut(BaseModel):
    run_id: int | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    snapshots_scanned: int
    candidate_count: int
    groups_before_overlap: dict
    g1_missing_liquidity: dict
    g3_count: int
    g3_insufficient: bool
    split: dict
    metrics: dict
    thresholds: dict
    look_ahead_safeguard: str | None = None
    explanation: str
    paper_trading_only: bool = True


class PointInTimeRunListResponse(BaseModel):
    items: list[PointInTimeRunOut]
    total: int
    limit: int
    offset: int


class PointInTimeStatusResponse(BaseModel):
    last_status: str
    last_run_at: str | None = None
    last_run_id: int | None = None
    last_candidate_count: int | None = None
    last_g3_count: int | None = None
    note: str


class ReadinessReportOut(BaseModel):
    status: str
    generated_at: datetime | str
    paper_trading_only: bool = True
    note: str
    coverage: dict
    observation_counts: dict
    history_span: dict
    snapshot_intervals: dict
    liquidity: dict
    valuation: dict
    stale_snapshots: dict
    pair_changes: dict
    security: dict
    narratives: dict
    opportunity_classifications: dict
    g3: dict
    collection_health: dict
    collection_continuity: dict
    preserved_records: dict
    repeat_phase6_when: dict


class CollectionContinuityOut(BaseModel):
    calendar_span_days: float | None = None
    earliest_snapshot_at: str | None = None
    latest_snapshot_at: str | None = None
    latest_snapshot_age_minutes: float | None = None
    accumulated_scan_execution_seconds: float = 0
    scans_with_duration: int = 0
    completed_scans_total: int = 0
    failed_scans_total: int = 0
    interrupted_scans_total: int = 0
    longest_gap_between_completed_scans_minutes: float | None = None
    longest_gap: dict | None = None
    median_gap_between_completed_scans_minutes: float | None = None
    expected_interval_minutes: float
    successful_scans_per_utc_day: list[dict]
    snapshots_per_utc_day: list[dict]
    note: str


class CollectionHealthOut(BaseModel):
    scheduler_enabled: bool
    scheduler_running: bool
    scan_in_progress: bool
    expected_interval_minutes: float
    last_successful_discovery_scan_id: int | None = None
    last_successful_discovery_at: str | None = None
    last_failed_discovery_scan_id: int | None = None
    last_failed_discovery_at: str | None = None
    consecutive_scan_failures: int
    minutes_since_last_completed_scan: float | None = None
    latest_snapshot_at: str | None = None
    minutes_since_latest_snapshot: float | None = None
    recent_snapshots: bool
    snapshot_count: int = 0
    tokens_with_history: int = 0
    collection_gaps: dict = Field(default_factory=dict)
    database_identity: dict = Field(default_factory=dict)
    last_successful_backup: dict | None = None
    last_backup: dict | None = None
    collection_healthy: bool
    unhealthy_reasons: list[str]
    note: str


class SecuritySummaryResponse(BaseModel):
    tokens_assessed: int
    insufficient_data: int
    low_confidence: int = 0
    high_or_critical: int
    lower: int
    moderate: int
    last_scan_at: str | None
    note: str


class MarketMonitorScanRequest(BaseModel):
    token_address: str | None = None
    limit: int = Field(default=25, ge=1, le=50)


class MarketConditionFindingOut(BaseModel):
    code: str
    severity: str
    status: str
    token_address: str
    previous_observation_id: int | None = None
    current_observation_id: int | None = None
    previous_value: object | None = None
    current_value: object | None = None
    percentage_change: float | None = None
    previous_at: datetime | None = None
    current_at: datetime | None = None
    data_source: str | None = None
    market_data_provider: str | None = None
    discovery_source: str | None = None
    explanation: str
    confidence: float = 0.0
    limitations: str = ""
    evidence: dict = Field(default_factory=dict)


class MarketMonitorTokenOut(BaseModel):
    token_address: str
    symbol: str | None = None
    signal_time: datetime
    snapshot_count: int
    availability: dict = Field(default_factory=dict)
    findings: list[MarketConditionFindingOut] = Field(default_factory=list)
    note: str


class MarketMonitorScanResponse(BaseModel):
    scan_id: int | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    signal_time: datetime | None = None
    tokens_scanned: int
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int | None = None
    liquidity_decreases: int = 0
    stale_tokens: int = 0
    thresholds: dict = Field(default_factory=dict)
    items: list[MarketMonitorTokenOut] | None = None
    note: str


class MarketMonitorStatusResponse(BaseModel):
    last_status: str
    last_scan_at: str | None = None
    last_tokens_scanned: int | None = None
    last_liquidity_decreases: int | None = None
    note: str


class EvidenceBatchRequest(BaseModel):
    limit: int = Field(default=1, ge=1, le=25)
    token_address: str | None = None


class EvidenceOperationOut(BaseModel):
    name: str
    token_address: str | None = None
    status: str
    stop_reason: str | None = None
    rpc_calls: int = 0
    rpc_accounting: dict | None = None


class EvidenceBatchResponse(BaseModel):
    status: str
    stop_reason: str | None = None
    stop_token: str | None = None
    operations: list[EvidenceOperationOut] = Field(default_factory=list)
    tokens_planned: list[str] = Field(default_factory=list)
    tokens_started: list[str] = Field(default_factory=list)
    tokens_skipped: list[str] = Field(default_factory=list)
    rpc_calls: int = 0
    rpc_accounting: dict | None = None
    cooldown: dict | None = None
    errors: list[dict] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str


class RpcCooldownResponse(BaseModel):
    active: bool
    scope: str | None = None
    reason: str | None = None
    status: str | None = None
    until_at: str | None = None
    retry_after_seconds: float | None = None
    backoff_seconds: float | None = None
    consecutive_429: int | None = None
    activated_at: str | None = None
    cleared_at: str | None = None
    expired: bool = False
    last_until_at: str | None = None
    last_reason: str | None = None
    blocking: bool = False
    busy: bool = False
    session_closed: bool = False
    note: str


class CreatorScanRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)
    token_address: str | None = None


class CreatorObservationOut(BaseModel):
    token_address: str
    mint_address: str | None = None
    token_program: str | None = None
    inspection_status: str
    mint_init_signature: str | None = None
    mint_init_slot: int | None = None
    mint_init_block_time: int | None = None
    mint_init_signer: str | None = None
    mint_init_status: str
    fee_payer: str | None = None
    signer_addresses: list[str] = Field(default_factory=list)
    create_program_id: str | None = None
    create_instruction: str | None = None
    source: str | None = None
    observed_at: datetime | None = None
    recorded_at: datetime | None = None
    attempted_at: datetime | None = None
    slot: int | None = None
    parser_notes: str | None = None
    errors: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)


class CreatorScanResponse(BaseModel):
    status: str
    tokens_considered: int
    tokens_scanned: int
    verified: int
    unknown: int
    unavailable: int
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int | None = None
    rpc_calls: int = 0
    rpc_accounting: dict | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    errors: list[dict] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str


class CreatorStatusResponse(BaseModel):
    last_status: str
    last_scan_at: str | None = None
    last_tokens_scanned: int | None = None
    verified: int | None = None
    unknown: int | None = None
    unavailable: int | None = None
    rpc_calls: int | None = None
    rpc_accounting: dict | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    note: str


class CreatorTokenResponse(BaseModel):
    token_address: str
    symbol: str | None = None
    name: str | None = None
    as_of: datetime | None = None
    observation: CreatorObservationOut | None = None
    note: str


class CreatorHistoryResponse(BaseModel):
    token_address: str
    observations: list[CreatorObservationOut]
    note: str


class HolderOwnerScanRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)
    token_address: str | None = None


class TokenAccountOwnerOut(BaseModel):
    mint_address: str
    token_account_address: str | None = None
    token_account_owner: str | None = None
    token_account_amount: float | None = None
    token_account_status: str | None = None
    owner_classification: str
    classification_status: str
    classification_evidence: str | None = None
    token_program: str | None = None
    source: str | None = None
    observed_at: datetime | None = None
    recorded_at: datetime | None = None
    attempted_at: datetime | None = None
    slot: int | None = None
    parser_notes: str | None = None
    errors: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)


class HolderOwnerScanResponse(BaseModel):
    status: str
    tokens_considered: int
    tokens_scanned: int
    accounts_observed: int
    verified: int
    unknown: int
    unavailable: int
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int | None = None
    rpc_calls: int = 0
    rpc_accounting: dict | None = None
    largest_account_requests: int = 0
    account_info_requests: int = 0
    unique_owner_lookups: int = 0
    stop_reason: str | None = None
    stop_token: str | None = None
    errors: list[dict] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str


class HolderOwnerStatusResponse(BaseModel):
    last_status: str
    last_scan_at: str | None = None
    last_tokens_scanned: int | None = None
    accounts_observed: int | None = None
    verified: int | None = None
    unknown: int | None = None
    unavailable: int | None = None
    rpc_calls: int | None = None
    rpc_accounting: dict | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    note: str


class HolderOwnerTokenResponse(BaseModel):
    token_address: str
    symbol: str | None = None
    name: str | None = None
    as_of: datetime | None = None
    observations: list[TokenAccountOwnerOut]
    note: str


class HolderOwnerHistoryResponse(BaseModel):
    token_address: str
    observations: list[TokenAccountOwnerOut]
    note: str


class MetaplexCreatorScanRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)
    token_address: str | None = None


class MetaplexCreatorEntryOut(BaseModel):
    address: str | None = None
    verified: bool | None = None
    share: int | None = None
    order: int | None = None


class MetaplexCreatorObservationOut(BaseModel):
    mint_address: str
    metadata_account: str | None = None
    metadata_status: str
    creator_vector_status: str
    creators: list[MetaplexCreatorEntryOut] | None = None
    creator_count: int | None = None
    verified_creator_count: int | None = None
    source: str | None = None
    observed_at: datetime | None = None
    recorded_at: datetime | None = None
    attempted_at: datetime | None = None
    slot: int | None = None
    parser_notes: str | None = None
    errors: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)


class MetaplexCreatorScanResponse(BaseModel):
    status: str
    tokens_considered: int
    tokens_scanned: int
    verified: int
    verified_absent: int
    unknown: int
    unavailable: int
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int | None = None
    rpc_calls: int = 0
    rpc_accounting: dict | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    errors: list[dict] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str


class MetaplexCreatorStatusResponse(BaseModel):
    last_status: str
    last_scan_at: str | None = None
    last_tokens_scanned: int | None = None
    verified: int | None = None
    verified_absent: int | None = None
    unknown: int | None = None
    unavailable: int | None = None
    rpc_calls: int | None = None
    rpc_accounting: dict | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    note: str


class MetaplexCreatorTokenResponse(BaseModel):
    token_address: str
    symbol: str | None = None
    name: str | None = None
    as_of: datetime | None = None
    observation: MetaplexCreatorObservationOut | None = None
    note: str


class MetaplexCreatorHistoryResponse(BaseModel):
    token_address: str
    observations: list[MetaplexCreatorObservationOut]
    note: str


class MintInitEvidenceSection(BaseModel):
    available: bool
    status: str
    observed_at: datetime | None = None
    slot: int | None = None
    observation: CreatorObservationOut | None = None
    note: str


class HolderEvidenceSection(BaseModel):
    available: bool
    status: str
    observed_at: datetime | None = None
    slot: int | None = None
    observations: list[TokenAccountOwnerOut] = Field(default_factory=list)
    note: str


class MetaplexEvidenceSection(BaseModel):
    available: bool
    status: str
    observed_at: datetime | None = None
    slot: int | None = None
    observation: MetaplexCreatorObservationOut | None = None
    note: str


class AddressMatchOut(BaseModel):
    address: str
    kind: str = "ADDRESS_MATCH_ACROSS_SOURCES"
    sources: list[str]
    roles: list[dict]
    note: str


class CreatorEvidencePanelResponse(BaseModel):
    token_address: str
    symbol: str | None = None
    name: str | None = None
    as_of: datetime | None = None
    mint_initialization: MintInitEvidenceSection
    token_account_ownership: HolderEvidenceSection
    metaplex_creators: MetaplexEvidenceSection
    address_matches: list[AddressMatchOut]
    limitations: list[str]
    note: str
    read_only: bool = True
    rpc_calls: int = 0


class PaperSimulateRequest(BaseModel):
    token_address: str
    side: str
    quantity: str | None = None
    notional_usd: str | None = None
    client_order_id: str | None = None


class PaperOrderOut(BaseModel):
    id: int
    client_order_id: str
    mint_address: str
    side: str
    status: str
    requested_notional_usd: str | None = None
    requested_quantity: str | None = None
    quote_snapshot_id: int | None = None
    paper_quote_id: int | None = None
    observed_price_usd: str | None = None
    observed_at: datetime | None = None
    simulated_price_usd: str | None = None
    fill_quantity: str | None = None
    fill_notional_usd: str | None = None
    fee_usd: str | None = None
    slippage_usd: str | None = None
    reject_reason: str | None = None
    assumptions: dict | None = None
    created_at: datetime | None = None
    filled_at: datetime | None = None
    note: str = "Simulated paper order. Not a blockchain transaction."


class PaperPositionOut(BaseModel):
    id: int
    mint_address: str
    quantity: str
    average_cost_usd: str
    cost_basis_usd: str
    realized_pnl_usd: str
    mark_price_usd: str | None = None
    market_value_usd: str | None = None
    unrealized_pnl_usd: str | None = None
    valuation_status: str
    updated_at: datetime | None = None


class PaperTradeOut(BaseModel):
    id: int
    order_id: int
    mint_address: str
    side: str
    quantity: str
    observed_price_usd: str
    simulated_price_usd: str
    notional_usd: str
    fee_usd: str
    slippage_usd: str
    realized_pnl_usd: str
    quote_snapshot_id: int | None = None
    created_at: datetime | None = None


class PaperDecisionOut(BaseModel):
    id: int
    mint_address: str | None = None
    order_id: int | None = None
    decision_type: str
    outcome: str
    reason: str
    details: dict | None = None
    created_at: datetime | None = None


class PaperOpportunityDecisionOut(BaseModel):
    id: int
    paper_decision_id: int
    token_id: int | None = None
    mint_address: str
    decided_at: datetime | None = None
    result: str
    reason: str
    explanation: str
    gates: list[dict] = Field(default_factory=list)
    evidence: dict | None = None
    opportunity_assessment_id: int | None = None
    security_assessment_id: int | None = None
    paper_quote_id: int | None = None
    snapshot_id: int | None = None
    scan_id: int | None = None
    opportunity_confidence: float | None = None
    opportunity_completeness: float | None = None
    security_confidence: float | None = None
    security_completeness: float | None = None
    duplicate_of_id: int | None = None
    orders_created: int = 0
    automatic_execution: bool = False
    note: str


class PaperDecisionScanRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200)
    force: bool = False
    token_addresses: list[str] | None = None


class PaperDecisionScanResponse(BaseModel):
    status: str
    scan_id: int | None = None
    tokens_considered: int
    tokens_evaluated: int
    skipped: int
    buy_candidate: int
    wait: int
    reject: int
    orders_created: int = 0
    automatic_execution: bool = False
    external_provider_calls: int = 0
    decisions: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    note: str


class PaperLimitsOut(BaseModel):
    max_allocation_pct: float
    max_exposure_pct: float
    max_daily_realized_loss_usd: float
    max_open_positions: int
    min_liquidity_usd: float
    max_quote_age_seconds: float
    fee_bps: float
    slippage_bps: float
    max_slippage_bps: float


class PaperPortfolioResponse(BaseModel):
    id: int
    name: str
    mode: str
    currency: str
    starting_cash_usd: str
    cash_available_usd: str
    cash_reserved_usd: str
    realized_pnl_usd: str
    fees_paid_usd: str
    slippage_paid_usd: str
    unrealized_pnl_usd: str | None = None
    positions_market_value_usd: str | None = None
    equity_usd: str | None = None
    valuation_status: str
    unknown_positions: list[str] = Field(default_factory=list)
    positions: list[PaperPositionOut] = Field(default_factory=list)
    note: str


class PaperListResponse(BaseModel):
    items: list
    note: str


class PaperQuoteOut(BaseModel):
    id: int
    token_id: int
    mint_address: str
    pair_address: str | None = None
    dex_name: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    source: str
    retrieved_at: datetime | None = None
    market_observed_at: datetime | None = None
    provider_timestamp_raw: str | None = None
    freshness_basis: str
    status: str
    reject_reason: str | None = None
    pair_match_status: str | None = None
    discontinuity_flag: str | None = None
    previous_quote_id: int | None = None
    cache_hit: bool = False
    http_status: int | None = None
    notes: str | None = None
    retrieval_age_seconds: float | None = None
    quote_age_seconds: float | None = None
    executable: bool = False
    external_request: bool = False
    note: str


class PaperStatusResponse(BaseModel):
    mode: str = "PAPER_ONLY"
    auto_trading: bool = False
    live_trading: bool = False
    wallet_connected: bool = False
    automatic_orders: bool = False
    starting_cash_usd: str
    cash_available_usd: str
    equity_usd: str | None = None
    valuation_status: str
    limits: PaperLimitsOut
    note: str


class PaperAutoEntryRunRequest(BaseModel):
    token_addresses: list[str] | None = None


class PaperAutoEntryCycleOut(BaseModel):
    id: int | None = None
    trigger: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tokens_evaluated: int = 0
    eligible: int = 0
    entered: int = 0
    rejected: int = 0
    waiting: int = 0
    failed: int = 0
    skipped: int = 0
    orders_created: int = 0
    attempts: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    config: dict | None = None
    automatic_selling: bool = False
    market_execution: bool = False
    note: str


class PaperAutoEntryStatusOut(BaseModel):
    enabled: bool
    running: bool
    cycle_in_progress: bool
    interval_minutes: float
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_status: str
    last_error: str | None = None
    successful_runs: int = 0
    failed_runs: int = 0
    skipped_runs: int = 0
    next_run_at: datetime | None = None
    catchup_pending: bool = False
    automatic_selling: bool = False
    live_trading: bool = False
    wallet_connected: bool = False
    dexscreener_refresh: bool = False
    last_cycle: dict | None = None
    note: str


class PaperAutoExitCycleOut(BaseModel):
    id: int | None = None
    trigger: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    positions_evaluated: int = 0
    triggered: int = 0
    exited: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    orders_created: int = 0
    attempts: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    config: dict | None = None
    market_execution: bool = False
    wallet: bool = False
    note: str


class PaperAutoExitStatusOut(BaseModel):
    enabled: bool
    running: bool
    cycle_in_progress: bool
    interval_seconds: float
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_status: str
    last_error: str | None = None
    successful_runs: int = 0
    failed_runs: int = 0
    skipped_runs: int = 0
    next_run_at: datetime | None = None
    catchup_pending: bool = False
    live_trading: bool = False
    wallet_connected: bool = False
    dexscreener_refresh: bool = False
    last_cycle: dict | None = None
    note: str
