"""Read-only query-time creator evidence panel. No RPC, no writes, no identity merge."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import CreatorObservation, MetaplexCreatorObservation, Token, TokenAccountOwnerObservation
from app.services.creator_scan import latest_creator_observation
from app.services.metaplex_creators import (
    latest_metaplex_creator_observation,
    latest_observation_as_of as latest_metaplex_as_of,
)
from app.services.mint_initialization import latest_observation_as_of as latest_mint_init_as_of
from app.services.token_account_ownership import (
    latest_account_observations,
    latest_account_observations_as_of,
)

MATCH_KIND = "ADDRESS_MATCH_ACROSS_SOURCES"
SOURCE_MINT_INIT = "mint_initialization"
SOURCE_HOLDERS = "token_account_ownership"
SOURCE_METAPLEX = "metaplex_creators"

LIMITATIONS = [
    "This panel only reads stored observations. It does not fetch chain state or start scans.",
    "Mint initialization evidence is not a person, deployer, or creator identity. The fee payer is not assumed to be the creator.",
    "Top token accounts are not a complete holder census. Token-account addresses are not wallets.",
    "Metaplex verified=true is an on-chain metadata flag only. It does not prove identity, ownership, or control.",
    "An exact address string appearing in more than one source is ADDRESS_MATCH_ACROSS_SOURCES, not common identity or ownership.",
    "UNKNOWN is not safe. UNAVAILABLE is not verified absence. VERIFIED_ABSENT is not UNAVAILABLE.",
    "Missing observations at time T are not filled from later data. Null observed_at rows are excluded from as-of queries.",
]


@dataclass
class AddressMatch:
    address: str
    kind: str = MATCH_KIND
    sources: list[str] = field(default_factory=list)
    roles: list[dict[str, str]] = field(default_factory=list)
    note: str = (
        "Exact address string appears in multiple evidence sources. "
        "This is not identity, common ownership, deployer identity, trustworthiness, or coordination."
    )


@dataclass
class CreatorEvidencePanel:
    mint: str
    symbol: str | None
    name: str | None
    as_of: datetime | None
    mint_initialization: CreatorObservation | None
    token_accounts: list[TokenAccountOwnerObservation]
    metaplex: MetaplexCreatorObservation | None
    address_matches: list[AddressMatch]
    limitations: list[str]
    note: str


def build_creator_evidence_panel(
    session: Session,
    token: Token,
    as_of: datetime | None = None,
) -> CreatorEvidencePanel:
    if as_of is not None:
        mint_row = latest_mint_init_as_of(session, token.id, as_of)
        holder_rows = latest_account_observations_as_of(session, token.id, as_of)
        meta_row = latest_metaplex_as_of(session, token.id, as_of)
        note = (
            "Point-in-time creator evidence panel. Each source uses its own latest observation "
            "with observed_at <= as_of. Later data does not fill earlier gaps. Read-only; no RPC."
        )
    else:
        mint_row = latest_creator_observation(session, token.id)
        holder_rows = latest_account_observations(session, token.id)
        meta_row = latest_metaplex_creator_observation(session, token.id)
        note = (
            "Latest stored creator evidence from independent sources. "
            "Addresses are not identified as people. Read-only; no RPC."
        )
    matches = _address_matches(mint_row, holder_rows, meta_row)
    return CreatorEvidencePanel(
        mint=token.token_address,
        symbol=token.symbol,
        name=token.name,
        as_of=as_of,
        mint_initialization=mint_row,
        token_accounts=list(holder_rows),
        metaplex=meta_row,
        address_matches=matches,
        limitations=list(LIMITATIONS),
        note=note,
    )


def _address_matches(
    mint_row: CreatorObservation | None,
    holder_rows: list[TokenAccountOwnerObservation],
    meta_row: MetaplexCreatorObservation | None,
) -> list[AddressMatch]:
    roles: dict[str, list[dict[str, str]]] = defaultdict(list)
    sources: dict[str, set[str]] = defaultdict(set)

    def add(address: str | None, source: str, role: str) -> None:
        if not isinstance(address, str) or not address.strip():
            return
        value = address.strip()
        roles[value].append({"source": source, "role": role})
        sources[value].add(source)

    if mint_row is not None:
        add(mint_row.mint_init_signer, SOURCE_MINT_INIT, "mint_init_signer")
        add(mint_row.fee_payer, SOURCE_MINT_INIT, "fee_payer")
        try:
            signers = json.loads(mint_row.signer_addresses_json or "[]")
        except json.JSONDecodeError:
            signers = []
        if isinstance(signers, list):
            for signer in signers:
                if isinstance(signer, str):
                    add(signer, SOURCE_MINT_INIT, "signer")

    for row in holder_rows:
        add(row.token_account_address, SOURCE_HOLDERS, "token_account_address")
        add(row.token_account_owner, SOURCE_HOLDERS, "token_account_owner")

    if meta_row is not None and meta_row.creators_json is not None:
        try:
            creators = json.loads(meta_row.creators_json)
        except json.JSONDecodeError:
            creators = None
        if isinstance(creators, list):
            for item in creators:
                if not isinstance(item, dict):
                    continue
                add(item.get("address") if isinstance(item.get("address"), str) else None, SOURCE_METAPLEX, "creator")

    matches: list[AddressMatch] = []
    for address, source_set in sorted(sources.items()):
        if len(source_set) < 2:
            continue
        matches.append(
            AddressMatch(
                address=address,
                sources=sorted(source_set),
                roles=roles[address],
            )
        )
    return matches
