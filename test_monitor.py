#!/usr/bin/env python3
"""
Solana Meme Bot v0.5 - forensic, fail-closed read-only scanner.

This scanner discovers newly surfaced Solana tokens, resolves them by exact
mint, checks market structure through DEX Screener, verifies mint/holder data
through Solana RPC, inspects Token-2022 extensions, aggregates the owners behind
large token accounts, and produces separate setup/risk scores.

Missing critical evidence is NOT treated as safe.
A clean scan is not proof that a token cannot rug, be manipulated, or lose value.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEX = "https://api.dexscreener.com"
DEX_TOKEN_PAIRS = DEX + "/token-pairs/v1/solana/{mint}"
DEX_LATEST_PROFILES = DEX + "/token-profiles/latest/v1"
DEX_LATEST_BOOSTS = DEX + "/token-boosts/latest/v1"
DEX_LATEST_CTO = DEX + "/community-takeovers/latest/v1"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

DEFAULT_RPC = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "rpc_url": DEFAULT_RPC,
    "http_timeout_seconds": 18,
    "new_pair_max_age_minutes": 120,
    "discover_limit": 15,

    # Market-quality gates
    "min_liquidity_usd": 20000,
    "preferred_liquidity_usd": 50000,
    "min_1h_volume_usd": 15000,
    "max_fdv_liquidity_ratio": 35,
    "max_1h_volume_liquidity_ratio": 12,
    "max_5m_volume_liquidity_ratio": 3,
    "extreme_5m_move_pct": 70,
    "extreme_1h_move_pct": 180,
    "min_recent_transactions": 10,
    "min_1h_transactions": 25,

    # Distribution gates
    "max_top1_account_pct": 20,
    "max_top5_accounts_pct": 45,
    "max_top10_accounts_pct": 60,
    "max_top20_accounts_pct": 80,
    "max_top1_owner_pct": 15,
    "max_top5_owners_pct": 40,
    "max_top10_owners_pct": 60,
    "min_holder_owner_resolution_pct": 90,
    "min_unique_top_owners": 8,
}

SAFE_TOKEN_2022_EXTENSIONS = {
    "metadatapointer",
    "tokenmetadata",
    "grouppointer",
    "tokengroup",
    "groupmemberpointer",
    "tokengroupmember",
}

DANGEROUS_TOKEN_2022_EXTENSIONS: dict[
    str,
    tuple[str, int, str],
] = {
    "transferfeeconfig": (
        "HIGH",
        28,
        "Transfer-fee controls are enabled.",
    ),
    "transferhook": (
        "CRITICAL",
        38,
        "A transfer hook can run custom logic during transfers.",
    ),
    "permanentdelegate": (
        "CRITICAL",
        40,
        "A permanent delegate has authority over token accounts.",
    ),
    "defaultaccountstate": (
        "HIGH",
        28,
        "Default token-account state controls are enabled.",
    ),
    "nontransferable": (
        "CRITICAL",
        45,
        "The mint is configured as non-transferable.",
    ),
    "mintcloseauthority": (
        "HIGH",
        24,
        "A mint close authority extension is enabled.",
    ),
    "confidentialtransfermint": (
        "HIGH",
        30,
        "Confidential-transfer behavior is enabled.",
    ),
    "confidentialtransferfeeconfig": (
        "HIGH",
        30,
        "Confidential transfer-fee behavior is enabled.",
    ),
    "interestbearingconfig": (
        "MEDIUM",
        16,
        "Interest-bearing token behavior is enabled.",
    ),
    "scaleduiamountconfig": (
        "HIGH",
        24,
        "Scaled UI amount behavior can alter displayed amounts.",
    ),
    "pausable": (
        "CRITICAL",
        40,
        "Transfers/minting/burning can be paused.",
    ),
    "confidentialmintburn": (
        "HIGH",
        30,
        "Confidential mint/burn behavior is enabled.",
    ),
}


def num(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value in (
            None,
            "",
        ):
            return default

        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def as_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value in (
            None,
            "",
        ):
            return default

        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def normalize_extension_name(
    value: Any,
) -> str:
    return "".join(
        ch.lower()
        for ch in str(
            value
            or ""
        )
        if ch.isalnum()
    )


def load_config(
    path: str,
) -> dict[str, Any]:

    config = dict(
        DEFAULT_CONFIG
    )

    p = Path(
        path
    )

    if p.exists():
        loaded = json.loads(
            p.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(
            loaded,
            dict,
        ):
            raise ValueError(
                "config.json must contain a JSON object"
            )

        config.update(
            loaded
        )

    return config


def http_json(
    url: str,
    *,
    timeout: int = 18,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:

    request_headers = {
        "Accept": "application/json",
        "User-Agent": "SolanaMemeBot/0.5",
    }

    if headers:
        request_headers.update(
            headers
        )

    request = urllib.request.Request(
        url,
        method=method,
        data=body,
        headers=request_headers,
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        return json.loads(
            response.read().decode(
                "utf-8"
            )
        )


def rpc_call(
    rpc_url: str,
    method: str,
    params: list[Any],
    timeout: int,
) -> Any:

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
    ).encode(
        "utf-8"
    )

    response = http_json(
        rpc_url,
        timeout=timeout,
        method="POST",
        body=body,
        headers={
            "Content-Type":
                "application/json"
        },
    )

    if (
        isinstance(
            response,
            dict,
        )
        and response.get(
            "error"
        )
    ):
        raise RuntimeError(
            f"RPC {method}: "
            f"{response['error']}"
        )

    return (
        response.get(
            "result"
        )
        if isinstance(
            response,
            dict,
        )
        else None
    )


def fetch_token_pairs(
    mint: str,
    timeout: int,
) -> list[dict[str, Any]]:

    url = DEX_TOKEN_PAIRS.format(
        mint=urllib.parse.quote(
            mint,
            safe="",
        )
    )

    payload = http_json(
        url,
        timeout=timeout,
    )

    return (
        payload
        if isinstance(
            payload,
            list,
        )
        else []
    )


def token_side(
    raw: dict[str, Any],
    mint: str,
) -> tuple[
    dict[str, Any],
    bool,
]:

    base = (
        raw.get(
            "baseToken"
        )
        or {}
    )

    quote = (
        raw.get(
            "quoteToken"
        )
        or {}
    )

    if (
        base.get(
            "address"
        )
        == mint
    ):
        return (
            base,
            True,
        )

    if (
        quote.get(
            "address"
        )
        == mint
    ):
        return (
            quote,
            False,
        )

    return (
        {},
        False,
    )


def pair_age_minutes(
    raw: dict[str, Any],
) -> float | None:

    created = raw.get(
        "pairCreatedAt"
    )

    if created in (
        None,
        "",
    ):
        return None

    try:
        return max(
            0.0,
            (
                time.time()
                * 1000
                - float(
                    created
                )
            )
            / 60000.0,
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def parse_pair(
    raw: dict[str, Any],
    mint: str,
) -> dict[str, Any] | None:

    (
        token,
        token_is_base,
    ) = token_side(
        raw,
        mint,
    )

    if not token:
        return None

    base = (
        raw.get(
            "baseToken"
        )
        or {}
    )

    quote = (
        raw.get(
            "quoteToken"
        )
        or {}
    )

    liquidity = (
        raw.get(
            "liquidity"
        )
        or {}
    )

    volume = (
        raw.get(
            "volume"
        )
        or {}
    )

    change = (
        raw.get(
            "priceChange"
        )
        or {}
    )

    txns = (
        raw.get(
            "txns"
        )
        or {}
    )

    tx5 = (
        txns.get(
            "m5"
        )
        or {}
    )

    tx1 = (
        txns.get(
            "h1"
        )
        or {}
    )

    return {
        "mint": mint,
        "name": str(
            token.get(
                "name"
            )
            or "Unknown"
        ),
        "symbol": str(
            token.get(
                "symbol"
            )
            or "?"
        ),
        "token_is_base":
            token_is_base,
        "dex_id": str(
            raw.get(
                "dexId"
            )
            or "unknown"
        ),
        "pair_address": str(
            raw.get(
                "pairAddress"
            )
            or ""
        ),
        "url": str(
            raw.get(
                "url"
            )
            or ""
        ),
        "base_mint": str(
            base.get(
                "address"
            )
            or ""
        ),
        "quote_mint": str(
            quote.get(
                "address"
            )
            or ""
        ),
        "quote_symbol": str(
            quote.get(
                "symbol"
            )
            or ""
        ),
        "price_usd": (
            None
            if raw.get(
                "priceUsd"
            )
            in (
                None,
                "",
            )
            else num(
                raw.get(
                    "priceUsd"
                )
            )
        ),
        "liquidity_usd": num(
            liquidity.get(
                "usd"
            )
        ),
        "market_cap": num(
            raw.get(
                "marketCap"
            )
        ),
        "fdv": num(
            raw.get(
                "fdv"
            )
        ),
        "pair_age_minutes":
            pair_age_minutes(
                raw
            ),
        "volume_5m_usd": num(
            volume.get(
                "m5"
            )
        ),
        "volume_1h_usd": num(
            volume.get(
                "h1"
            )
        ),
        "volume_24h_usd": num(
            volume.get(
                "h24"
            )
        ),
        "buys_5m": as_int(
            tx5.get(
                "buys"
            )
        ),
        "sells_5m": as_int(
            tx5.get(
                "sells"
            )
        ),
        "buys_1h": as_int(
            tx1.get(
                "buys"
            )
        ),
        "sells_1h": as_int(
            tx1.get(
                "sells"
            )
        ),
        "change_5m_pct": num(
            change.get(
                "m5"
            )
        ),
        "change_1h_pct": num(
            change.get(
                "h1"
            )
        ),
        "boosts_active": as_int(
            (
                raw.get(
                    "boosts"
                )
                or {}
            ).get(
                "active"
            )
        ),
    }


def select_primary_pair(
    pairs: list[
        dict[str, Any]
    ],
    mint: str,
) -> dict[str, Any] | None:

    parsed = [
        parse_pair(
            pair,
            mint,
        )
        for pair in pairs
    ]

    parsed = [
        pair
        for pair in parsed
        if pair
    ]

    if not parsed:
        return None

    primary = max(
        parsed,
        key=lambda pair: (
            num(
                pair.get(
                    "liquidity_usd"
                )
            ),
            num(
                pair.get(
                    "volume_24h_usd"
                )
            ),
        ),
    )

    total_liquidity = sum(
        num(
            pair.get(
                "liquidity_usd"
            )
        )
        for pair in parsed
    )

    sorted_liquidity = sorted(
        (
            num(
                pair.get(
                    "liquidity_usd"
                )
            )
            for pair in parsed
        ),
        reverse=True,
    )

    primary = dict(
        primary
    )

    primary[
        "pair_count"
    ] = len(
        parsed
    )

    primary[
        "dex_count"
    ] = len(
        {
            str(
                pair.get(
                    "dex_id"
                )
                or ""
            )
            for pair in parsed
            if pair.get(
                "dex_id"
            )
        }
    )

    primary[
        "total_liquidity_usd"
    ] = total_liquidity

    primary[
        "secondary_liquidity_usd"
    ] = (
        sorted_liquidity[1]
        if len(
            sorted_liquidity
        )
        > 1
        else 0.0
    )

    primary[
        "primary_liquidity_share_pct"
    ] = (
        100.0
        * num(
            primary.get(
                "liquidity_usd"
            )
        )
        / total_liquidity
        if total_liquidity
        > 0
        else 0.0
    )

    return primary


def aggregate_largest_account_owners(
    holders: list[
        dict[str, Any]
    ],
    total_supply: int,
    rpc_url: str,
    timeout: int,
) -> dict[str, Any]:

    result: dict[str, Any] = {
        "holder_owner_data_ok":
            False,
        "holder_owner_resolution_pct":
            0.0,
        "unique_top_owner_count":
            0,
        "top1_owner_pct":
            None,
        "top5_owners_pct":
            None,
        "top10_owners_pct":
            None,
        "top20_owners_pct":
            None,
    }

    addresses = [
        str(
            item.get(
                "address"
            )
            or ""
        )
        for item in holders
        if item.get(
            "address"
        )
    ]

    if (
        total_supply
        <= 0
        or not addresses
    ):
        return result

    accounts_result = rpc_call(
        rpc_url,
        "getMultipleAccounts",
        [
            addresses,
            {
                "encoding":
                    "jsonParsed",
                "commitment":
                    "confirmed",
            },
        ],
        timeout,
    )

    values = (
        (
            accounts_result
            or {}
        ).get(
            "value"
        )
        or []
    )

    owner_amounts: dict[
        str,
        int,
    ] = defaultdict(
        int
    )

    resolved = 0

    for (
        holder,
        account,
    ) in zip(
        holders,
        values,
    ):
        if not account:
            continue

        data = (
            account.get(
                "data"
            )
            or {}
        )

        parsed = (
            data.get(
                "parsed"
            )
            or {}
        )

        info = (
            parsed.get(
                "info"
            )
            or {}
        )

        owner = str(
            info.get(
                "owner"
            )
            or ""
        ).strip()

        if not owner:
            continue

        owner_amounts[
            owner
        ] += as_int(
            holder.get(
                "amount"
            ),
            0,
        )

        resolved += 1

    if not owner_amounts:
        return result

    ranked_amounts = sorted(
        owner_amounts.values(),
        reverse=True,
    )

    def owner_pct(
        count: int,
    ) -> float:
        return (
            100.0
            * sum(
                ranked_amounts[
                    :count
                ]
            )
            / total_supply
        )

    result.update(
        {
            "holder_owner_data_ok":
                True,
            "holder_owner_resolution_pct":
                100.0
                * resolved
                / max(
                    1,
                    len(
                        addresses
                    ),
                ),
            "unique_top_owner_count":
                len(
                    owner_amounts
                ),
            "top1_owner_pct":
                owner_pct(
                    1
                ),
            "top5_owners_pct":
                owner_pct(
                    5
                ),
            "top10_owners_pct":
                owner_pct(
                    10
                ),
            "top20_owners_pct":
                owner_pct(
                    20
                ),
        }
    )

    return result


def fetch_chain_facts(
    mint: str,
    rpc_url: str,
    timeout: int,
) -> dict[str, Any]:

    facts: dict[
        str,
        Any,
    ] = {
        "rpc_mint_ok":
            False,
        "holder_data_ok":
            False,
        "holder_owner_data_ok":
            False,
        "holder_owner_resolution_pct":
            0.0,
        "extension_data_ok":
            False,
        "mint_authority":
            None,
        "freeze_authority":
            None,
        "token_program":
            None,
        "is_token_2022":
            False,
        "mint_initialized":
            None,
        "decimals":
            None,
        "supply_raw":
            None,
        "mint_supply_raw":
            None,
        "supply_consistent":
            None,
        "extensions":
            [],
        "top1_account_pct":
            None,
        "top5_accounts_pct":
            None,
        "top10_accounts_pct":
            None,
        "top20_accounts_pct":
            None,
        "top1_owner_pct":
            None,
        "top5_owners_pct":
            None,
        "top10_owners_pct":
            None,
        "top20_owners_pct":
            None,
        "unique_top_owner_count":
            0,
    }

    try:
        result = rpc_call(
            rpc_url,
            "getAccountInfo",
            [
                mint,
                {
                    "encoding":
                        "jsonParsed",
                    "commitment":
                        "confirmed",
                },
            ],
            timeout,
        )

        value = (
            (
                result
                or {}
            ).get(
                "value"
            )
        )

        if value:
            facts[
                "token_program"
            ] = value.get(
                "owner"
            )

            facts[
                "is_token_2022"
            ] = (
                facts[
                    "token_program"
                ]
                == TOKEN_2022_PROGRAM
            )

            data = (
                value.get(
                    "data"
                )
                or {}
            )

            parsed = (
                data.get(
                    "parsed"
                )
                or {}
            )

            info = (
                parsed.get(
                    "info"
                )
                or {}
            )

            parsed_type = str(
                parsed.get(
                    "type"
                )
                or ""
            ).lower()

            facts[
                "mint_authority"
            ] = info.get(
                "mintAuthority"
            )

            facts[
                "freeze_authority"
            ] = info.get(
                "freezeAuthority"
            )

            facts[
                "decimals"
            ] = info.get(
                "decimals"
            )

            facts[
                "mint_supply_raw"
            ] = (
                as_int(
                    info.get(
                        "supply"
                    ),
                    0,
                )
                or None
            )

            if (
                "isInitialized"
                in info
            ):
                facts[
                    "mint_initialized"
                ] = bool(
                    info.get(
                        "isInitialized"
                    )
                )

            elif (
                parsed_type
                == "mint"
            ):
                facts[
                    "mint_initialized"
                ] = True

            extensions_raw = (
                info.get(
                    "extensions"
                )
            )

            if isinstance(
                extensions_raw,
                list,
            ):
                facts[
                    "extensions"
                ] = extensions_raw

                facts[
                    "extension_data_ok"
                ] = True

            elif not facts[
                "is_token_2022"
            ]:
                facts[
                    "extension_data_ok"
                ] = True

            facts[
                "rpc_mint_ok"
            ] = (
                parsed_type
                == "mint"
                and facts[
                    "token_program"
                ]
                in {
                    TOKEN_PROGRAM,
                    TOKEN_2022_PROGRAM,
                }
            )

    except Exception:
        pass

    try:
        supply_result = rpc_call(
            rpc_url,
            "getTokenSupply",
            [
                mint,
                {
                    "commitment":
                        "confirmed"
                },
            ],
            timeout,
        )

        supply_info = (
            (
                supply_result
                or {}
            ).get(
                "value"
            )
            or {}
        )

        total_supply = as_int(
            supply_info.get(
                "amount"
            ),
            0,
        )

        if total_supply > 0:
            facts[
                "supply_raw"
            ] = total_supply

        mint_supply = as_int(
            facts.get(
                "mint_supply_raw"
            ),
            0,
        )

        if (
            total_supply > 0
            and mint_supply > 0
        ):
            facts[
                "supply_consistent"
            ] = (
                total_supply
                == mint_supply
            )

        holders_result = rpc_call(
            rpc_url,
            "getTokenLargestAccounts",
            [
                mint,
                {
                    "commitment":
                        "confirmed"
                },
            ],
            timeout,
        )

        holders = (
            (
                holders_result
                or {}
            ).get(
                "value"
            )
            or []
        )

        if (
            total_supply > 0
            and holders
        ):
            amounts = [
                as_int(
                    item.get(
                        "amount"
                    ),
                    0,
                )
                for item
                in holders
            ]

            def account_pct(
                count: int,
            ) -> float:
                return (
                    100.0
                    * sum(
                        amounts[
                            :count
                        ]
                    )
                    / total_supply
                )

            facts[
                "top1_account_pct"
            ] = account_pct(
                1
            )

            facts[
                "top5_accounts_pct"
            ] = account_pct(
                5
            )

            facts[
                "top10_accounts_pct"
            ] = account_pct(
                10
            )

            facts[
                "top20_accounts_pct"
            ] = account_pct(
                20
            )

            facts[
                "holder_data_ok"
            ] = True

            try:
                facts.update(
                    aggregate_largest_account_owners(
                        holders,
                        total_supply,
                        rpc_url,
                        timeout,
                    )
                )

            except Exception:
                pass

    except Exception:
        pass

    return facts


def add_flag(
    flags: list[
        dict[str, Any]
    ],
    severity: str,
    code: str,
    message: str,
    points: int,
) -> None:

    flags.append(
        {
            "severity":
                severity,
            "code":
                code,
            "message":
                message,
            "points":
                points,
        }
    )


def add_token_2022_flags(
    chain: dict[str, Any],
    flags: list[
        dict[str, Any]
    ],
) -> None:

    if not chain.get(
        "is_token_2022"
    ):
        return

    seen: set[
        str
    ] = set()

    for extension in (
        chain.get(
            "extensions"
        )
        or []
    ):
        if not isinstance(
            extension,
            dict,
        ):
            continue

        raw_name = (
            extension.get(
                "extension"
            )
            or extension.get(
                "type"
            )
            or ""
        )

        name = (
            normalize_extension_name(
                raw_name
            )
        )

        if (
            not name
            or name in seen
        ):
            continue

        seen.add(
            name
        )

        dangerous = (
            DANGEROUS_TOKEN_2022_EXTENSIONS.get(
                name
            )
        )

        if dangerous:
            (
                severity,
                points,
                message,
            ) = dangerous

            add_flag(
                flags,
                severity,
                "TOKEN2022_"
                + name.upper(),
                message,
                points,
            )

        elif (
            name
            not in SAFE_TOKEN_2022_EXTENSIONS
        ):
            add_flag(
                flags,
                "HIGH",
                "TOKEN2022_UNRECOGNIZED_EXTENSION",
                (
                    "Unrecognized Token-2022 "
                    f"mint extension: {raw_name}."
                ),
                20,
            )


def risk_analysis(
    market: dict[str, Any] | None,
    chain: dict[str, Any],
    config: dict[str, Any],
) -> tuple[
    int,
    str,
    int,
    list[dict[str, Any]],
]:

    flags: list[
        dict[str, Any]
    ] = []

    coverage = 0

    # ============================================================
    # MARKET / POOL EVIDENCE = 40%
    # ============================================================

    if market:
        coverage += 40

        liquidity = num(
            market.get(
                "liquidity_usd"
            )
        )

        if liquidity < 5000:
            add_flag(
                flags,
                "CRITICAL",
                "VERY_LOW_LIQUIDITY",
                "Liquidity is below $5,000.",
                40,
            )

        elif liquidity < num(
            config.get(
                "min_liquidity_usd"
            ),
            20000,
        ):
            add_flag(
                flags,
                "HIGH",
                "LOW_LIQUIDITY",
                (
                    "Liquidity is only "
                    f"${liquidity:,.0f}."
                ),
                25,
            )

        elif liquidity < num(
            config.get(
                "preferred_liquidity_usd"
            ),
            50000,
        ):
            add_flag(
                flags,
                "MEDIUM",
                "THIN_LIQUIDITY",
                (
                    "Liquidity is "
                    f"${liquidity:,.0f}."
                ),
                8,
            )

        if (
            market.get(
                "price_usd"
            )
            is None
            or num(
                market.get(
                    "price_usd"
                )
            )
            <= 0
        ):
            add_flag(
                flags,
                "HIGH",
                "NO_RELIABLE_PRICE",
                (
                    "No positive USD "
                    "price was available."
                ),
                18,
            )

        if (
            market.get(
                "token_is_base",
                True,
            )
            is False
        ):
            add_flag(
                flags,
                "HIGH",
                "TOKEN_IS_QUOTE_SIDE",
                (
                    "The selected exact pair "
                    "does not expose the token "
                    "as the base asset."
                ),
                18,
            )

        fdv = num(
            market.get(
                "fdv"
            )
        )

        if (
            fdv > 0
            and liquidity > 0
        ):
            ratio = (
                fdv
                / liquidity
            )

            limit = num(
                config.get(
                    "max_fdv_liquidity_ratio"
                ),
                35,
            )

            if (
                ratio
                > limit * 2
            ):
                add_flag(
                    flags,
                    "CRITICAL",
                    "EXTREME_FDV_LIQUIDITY_IMBALANCE",
                    (
                        "FDV/liquidity is "
                        f"{ratio:.1f}x."
                    ),
                    32,
                )

            elif ratio > limit:
                add_flag(
                    flags,
                    "HIGH",
                    "FDV_LIQUIDITY_IMBALANCE",
                    (
                        "FDV/liquidity is "
                        f"{ratio:.1f}x."
                    ),
                    18,
                )

        market_cap = num(
            market.get(
                "market_cap"
            )
        )

        if (
            fdv > 0
            and market_cap > 0
            and fdv
            > market_cap * 3
        ):
            add_flag(
                flags,
                "MEDIUM",
                "FDV_MARKETCAP_GAP",
                (
                    "FDV is more than 3x "
                    "reported market cap."
                ),
                8,
            )

        move_5m = num(
            market.get(
                "change_5m_pct"
            )
        )

        extreme_5m = num(
            config.get(
                "extreme_5m_move_pct"
            ),
            70,
        )

        if (
            abs(
                move_5m
            )
            >= extreme_5m
        ):
            add_flag(
                flags,
                "HIGH",
                "EXTREME_5M_MOVE",
                (
                    "5m move is "
                    f"{move_5m:+.1f}%."
                ),
                18,
            )

        elif (
            abs(
                move_5m
            )
            >= 40
        ):
            add_flag(
                flags,
                "MEDIUM",
                "ELEVATED_5M_MOVE",
                (
                    "5m move is already "
                    f"{move_5m:+.1f}%."
                ),
                7,
            )

        move_1h = num(
            market.get(
                "change_1h_pct"
            )
        )

        if (
            abs(
                move_1h
            )
            >= num(
                config.get(
                    "extreme_1h_move_pct"
                ),
                180,
            )
        ):
            add_flag(
                flags,
                "HIGH",
                "EXTREME_1H_MOVE",
                (
                    "1h move is "
                    f"{move_1h:+.1f}%."
                ),
                18,
            )

        buys_5m = as_int(
            market.get(
                "buys_5m"
            )
        )

        sells_5m = as_int(
            market.get(
                "sells_5m"
            )
        )

        recent = (
            buys_5m
            + sells_5m
        )

        min_recent = as_int(
            config.get(
                "min_recent_transactions"
            ),
            10,
        )

        if recent < min_recent:
            add_flag(
                flags,
                "MEDIUM",
                "LOW_RECENT_ACTIVITY",
                (
                    "Only "
                    f"{recent} trades "
                    "were reported in 5m."
                ),
                6,
            )

        else:
            if (
                sells_5m
                >= max(
                    8,
                    buys_5m * 2,
                )
            ):
                add_flag(
                    flags,
                    "HIGH",
                    "SELL_PRESSURE",
                    (
                        "Recent sells are "
                        "at least 2x buys."
                    ),
                    14,
                )

            if (
                buys_5m >= 12
                and sells_5m == 0
            ):
                add_flag(
                    flags,
                    "HIGH",
                    "NO_RECENT_SELLS",
                    (
                        "There are many recent "
                        "buys but zero reported "
                        "sells."
                    ),
                    18,
                )

            buy_ratio = (
                buys_5m
                / max(
                    1,
                    recent,
                )
            )

            if (
                recent >= 20
                and buy_ratio > 0.92
            ):
                add_flag(
                    flags,
                    "HIGH",
                    "EXTREME_BUY_IMBALANCE",
                    (
                        "Recent flow is over "
                        "92% buys."
                    ),
                    14,
                )

        buys_1h = as_int(
            market.get(
                "buys_1h"
            )
        )

        sells_1h = as_int(
            market.get(
                "sells_1h"
            )
        )

        tx_1h = (
            buys_1h
            + sells_1h
        )

        if (
            tx_1h
            < as_int(
                config.get(
                    "min_1h_transactions"
                ),
                25,
            )
        ):
            add_flag(
                flags,
                "MEDIUM",
                "LOW_1H_TRADE_COUNT",
                (
                    "Only "
                    f"{tx_1h} trades "
                    "were reported in 1h."
                ),
                5,
            )

        elif (
            sells_1h
            >= max(
                15,
                buys_1h * 2,
            )
        ):
            add_flag(
                flags,
                "HIGH",
                "ONE_HOUR_SELL_PRESSURE",
                (
                    "1h sells are at least "
                    "2x buys."
                ),
                14,
            )

        volume_1h = num(
            market.get(
                "volume_1h_usd"
            )
        )

        if (
            volume_1h
            < num(
                config.get(
                    "min_1h_volume_usd"
                ),
                15000,
            )
        ):
            add_flag(
                flags,
                "MEDIUM",
                "LOW_1H_VOLUME",
                (
                    "1h volume is only "
                    f"${volume_1h:,.0f}."
                ),
                6,
            )

        if (
            liquidity > 0
            and volume_1h > 0
        ):
            vol_liq = (
                volume_1h
                / liquidity
            )

            max_ratio = num(
                config.get(
                    "max_1h_volume_liquidity_ratio"
                ),
                12,
            )

            if (
                vol_liq
                > max_ratio * 2
            ):
                add_flag(
                    flags,
                    "HIGH",
                    "EXTREME_VOLUME_LIQUIDITY_RATIO",
                    (
                        "1h volume/liquidity "
                        f"is {vol_liq:.1f}x."
                    ),
                    18,
                )

            elif (
                vol_liq
                > max_ratio
            ):
                add_flag(
                    flags,
                    "MEDIUM",
                    "HIGH_VOLUME_LIQUIDITY_RATIO",
                    (
                        "1h volume/liquidity "
                        f"is {vol_liq:.1f}x."
                    ),
                    8,
                )

        volume_5m = num(
            market.get(
                "volume_5m_usd"
            )
        )

        if (
            liquidity > 0
            and volume_5m > 0
        ):
            ratio_5m = (
                volume_5m
                / liquidity
            )

            if (
                ratio_5m
                > num(
                    config.get(
                        "max_5m_volume_liquidity_ratio"
                    ),
                    3,
                )
            ):
                add_flag(
                    flags,
                    "HIGH",
                    "EXTREME_5M_VOLUME_LIQUIDITY_RATIO",
                    (
                        "5m volume/liquidity "
                        f"is {ratio_5m:.1f}x."
                    ),
                    15,
                )

        age = market.get(
            "pair_age_minutes"
        )

        if age is None:
            add_flag(
                flags,
                "MEDIUM",
                "UNKNOWN_POOL_AGE",
                (
                    "Pool creation time "
                    "could not be verified."
                ),
                8,
            )

        elif age < 3:
            add_flag(
                flags,
                "HIGH",
                "EXTREMELY_NEW_POOL",
                (
                    "Pool age is only "
                    f"{age:.1f} minutes."
                ),
                12,
            )

        elif age < 5:
            add_flag(
                flags,
                "MEDIUM",
                "VERY_NEW_POOL",
                (
                    "Pool age is only "
                    f"{age:.1f} minutes."
                ),
                5,
            )

        pair_count = as_int(
            market.get(
                "pair_count"
            ),
            1,
        )

        primary_share = num(
            market.get(
                "primary_liquidity_share_pct"
            ),
            100,
        )

        if (
            pair_count >= 4
            and primary_share < 25
        ):
            add_flag(
                flags,
                "MEDIUM",
                "FRAGMENTED_LIQUIDITY",
                (
                    "Liquidity is highly "
                    "fragmented across pools."
                ),
                6,
            )

    else:
        add_flag(
            flags,
            "HIGH",
            "NO_EXACT_POOL",
            (
                "No exact-address Solana "
                "DEX pool was found."
            ),
            30,
        )

    # ============================================================
    # EXACT MINT VERIFICATION = 25%
    # ============================================================

    if chain.get(
        "rpc_mint_ok"
    ):
        coverage += 25

        if (
            chain.get(
                "mint_initialized"
            )
            is False
        ):
            add_flag(
                flags,
                "CRITICAL",
                "MINT_NOT_INITIALIZED",
                (
                    "Mint account is not "
                    "initialized."
                ),
                45,
            )

        if chain.get(
            "mint_authority"
        ):
            add_flag(
                flags,
                "HIGH",
                "MINT_AUTHORITY_ACTIVE",
                (
                    "Mint authority is "
                    "still active."
                ),
                30,
            )

        if chain.get(
            "freeze_authority"
        ):
            add_flag(
                flags,
                "HIGH",
                "FREEZE_AUTHORITY_ACTIVE",
                (
                    "Freeze authority is "
                    "still active."
                ),
                34,
            )

        if (
            as_int(
                chain.get(
                    "supply_raw"
                ),
                0,
            )
            <= 0
        ):
            add_flag(
                flags,
                "CRITICAL",
                "INVALID_SUPPLY",
                (
                    "Positive token supply "
                    "could not be verified."
                ),
                35,
            )

        if (
            chain.get(
                "supply_consistent"
            )
            is False
        ):
            add_flag(
                flags,
                "CRITICAL",
                "SUPPLY_MISMATCH",
                (
                    "Mint-account supply and "
                    "getTokenSupply disagree."
                ),
                35,
            )

        decimals = chain.get(
            "decimals"
        )

        if (
            decimals is not None
            and as_int(
                decimals
            )
            > 12
        ):
            add_flag(
                flags,
                "MEDIUM",
                "UNUSUAL_DECIMALS",
                (
                    "Token uses an unusually "
                    f"high {decimals} decimals."
                ),
                8,
            )

    else:
        add_flag(
            flags,
            "MEDIUM",
            "MINT_RPC_UNAVAILABLE",
            (
                "Mint identity/authority "
                "data could not be fully "
                "verified."
            ),
            10,
        )

    # ============================================================
    # TOKEN-2022 EXTENSIONS = 5%
    # ============================================================

    if chain.get(
        "is_token_2022"
    ):
        if chain.get(
            "extension_data_ok"
        ):
            coverage += 5

            add_token_2022_flags(
                chain,
                flags,
            )

        else:
            add_flag(
                flags,
                "HIGH",
                "TOKEN2022_EXTENSIONS_UNVERIFIED",
                (
                    "Token-2022 extensions "
                    "could not be verified."
                ),
                20,
            )

    else:
        coverage += 5

    # ============================================================
    # TOKEN-ACCOUNT CONCENTRATION = 20%
    # ============================================================

    if chain.get(
        "holder_data_ok"
    ):
        coverage += 20

        concentration_checks = [
            (
                "top1_account_pct",
                "max_top1_account_pct",
                20,
                "TOP1_CONCENTRATION",
                "Largest token account",
                20,
            ),
            (
                "top5_accounts_pct",
                "max_top5_accounts_pct",
                45,
                "TOP5_CONCENTRATION",
                "Top 5 token accounts",
                18,
            ),
            (
                "top10_accounts_pct",
                "max_top10_accounts_pct",
                60,
                "TOP10_CONCENTRATION",
                "Top 10 token accounts",
                18,
            ),
            (
                "top20_accounts_pct",
                "max_top20_accounts_pct",
                80,
                "TOP20_CONCENTRATION",
                "Top 20 token accounts",
                10,
            ),
        ]

        for (
            field,
            config_key,
            default_limit,
            code,
            label,
            points,
        ) in concentration_checks:

            value = chain.get(
                field
            )

            if (
                value is not None
                and num(
                    value
                )
                > num(
                    config.get(
                        config_key
                    ),
                    default_limit,
                )
            ):
                severity = (
                    "CRITICAL"
                    if (
                        field
                        == "top1_account_pct"
                        and num(
                            value
                        )
                        >= 60
                    )
                    else (
                        "MEDIUM"
                        if field
                        == "top20_accounts_pct"
                        else "HIGH"
                    )
                )

                add_flag(
                    flags,
                    severity,
                    code,
                    (
                        f"{label} hold "
                        f"{num(value):.1f}% "
                        "of supply."
                    ),
                    (
                        35
                        if severity
                        == "CRITICAL"
                        else points
                    ),
                )

    else:
        add_flag(
            flags,
            "MEDIUM",
            "HOLDER_RPC_UNAVAILABLE",
            (
                "Largest-account "
                "concentration could not "
                "be verified."
            ),
            8,
        )

    # ============================================================
    # OWNER AGGREGATION = 10%
    # ============================================================

    owner_resolution = num(
        chain.get(
            "holder_owner_resolution_pct"
        )
    )

    required_resolution = num(
        config.get(
            "min_holder_owner_resolution_pct"
        ),
        90,
    )

    if (
        "holder_owner_data_ok"
        not in chain
        and chain.get(
            "holder_data_ok"
        )
    ):
        owner_data_ok = True
        owner_resolution = 100.0

    else:
        owner_data_ok = bool(
            chain.get(
                "holder_owner_data_ok"
            )
        )

    if (
        owner_data_ok
        and owner_resolution
        >= required_resolution
    ):
        coverage += 10

        owner_checks = [
            (
                "top1_owner_pct",
                "max_top1_owner_pct",
                15,
                "TOP_OWNER_CONCENTRATION",
                "One resolved owner",
                24,
            ),
            (
                "top5_owners_pct",
                "max_top5_owners_pct",
                40,
                "TOP5_OWNER_CONCENTRATION",
                "Top 5 resolved owners",
                20,
            ),
            (
                "top10_owners_pct",
                "max_top10_owners_pct",
                60,
                "TOP10_OWNER_CONCENTRATION",
                "Top 10 resolved owners",
                18,
            ),
        ]

        for (
            field,
            config_key,
            default_limit,
            code,
            label,
            points,
        ) in owner_checks:

            value = chain.get(
                field
            )

            if (
                value is not None
                and num(
                    value
                )
                > num(
                    config.get(
                        config_key
                    ),
                    default_limit,
                )
            ):
                add_flag(
                    flags,
                    "HIGH",
                    code,
                    (
                        f"{label} control "
                        f"{num(value):.1f}% "
                        "of supply."
                    ),
                    points,
                )

        unique_owners = as_int(
            chain.get(
                "unique_top_owner_count"
            )
        )

        if (
            unique_owners > 0
            and unique_owners
            < as_int(
                config.get(
                    "min_unique_top_owners"
                ),
                8,
            )
        ):
            add_flag(
                flags,
                "HIGH",
                "FEW_LARGE_HOLDER_OWNERS",
                (
                    "Largest token accounts "
                    "resolve to only "
                    f"{unique_owners} owners."
                ),
                18,
            )

    else:
        add_flag(
            flags,
            "MEDIUM",
            "HOLDER_OWNERS_UNVERIFIED",
            (
                "Owners behind the largest "
                "token accounts were only "
                f"{owner_resolution:.0f}% "
                "resolved."
            ),
            10,
        )

    score = min(
        100,
        sum(
            as_int(
                flag.get(
                    "points"
                )
            )
            for flag in flags
        ),
    )

    if coverage < 100:
        level = (
            "INSUFFICIENT_DATA"
        )

    elif any(
        flag.get(
            "severity"
        )
        == "CRITICAL"
        for flag in flags
    ) or score >= 70:
        level = "CRITICAL"

    elif score >= 40:
        level = "HIGH"

    elif score >= 15:
        level = "MEDIUM"

    else:
        level = "LOW"

    return (
        score,
        level,
        coverage,
        flags,
    )


def setup_score(
    market: dict[str, Any] | None,
    risk_score: int,
    risk_level: str,
    data_coverage_pct: int = 100,
) -> tuple[
    int,
    str,
    list[str],
]:

    if not market:
        return (
            0,
            "SKIP",
            [
                "no exact market pair"
            ],
        )

    score = 50

    reasons: list[
        str
    ] = []

    liquidity = num(
        market.get(
            "liquidity_usd"
        )
    )

    if liquidity >= 150000:
        score += 20
        reasons.append(
            "very strong liquidity"
        )

    elif liquidity >= 75000:
        score += 16
        reasons.append(
            "strong liquidity"
        )

    elif liquidity >= 50000:
        score += 12
        reasons.append(
            "good liquidity"
        )

    elif liquidity >= 20000:
        score += 5
        reasons.append(
            "moderate liquidity"
        )

    else:
        score -= 20
        reasons.append(
            "weak liquidity"
        )

    volume_1h = num(
        market.get(
            "volume_1h_usd"
        )
    )

    if volume_1h >= 150000:
        score += 14
        reasons.append(
            "very high 1h volume"
        )

    elif volume_1h >= 50000:
        score += 10
        reasons.append(
            "healthy 1h volume"
        )

    elif volume_1h >= 15000:
        score += 6
        reasons.append(
            "adequate 1h volume"
        )

    else:
        score -= 6
        reasons.append(
            "low 1h volume"
        )

    buys_5m = as_int(
        market.get(
            "buys_5m"
        )
    )

    sells_5m = as_int(
        market.get(
            "sells_5m"
        )
    )

    recent = (
        buys_5m
        + sells_5m
    )

    if recent >= 20:
        buy_ratio = (
            buys_5m
            / max(
                1,
                recent,
            )
        )

        if (
            0.55
            <= buy_ratio
            <= 0.80
        ):
            score += 12

            reasons.append(
                "healthy recent buy/sell balance"
            )

        elif buy_ratio > 0.90:
            score -= 8

            reasons.append(
                "extremely one-sided buying"
            )

        elif buy_ratio < 0.40:
            score -= 12

            reasons.append(
                "sell-heavy recent flow"
            )

    move_5m = num(
        market.get(
            "change_5m_pct"
        )
    )

    if (
        2
        <= move_5m
        <= 25
    ):
        score += 10

        reasons.append(
            "positive but controlled 5m momentum"
        )

    elif (
        25
        < move_5m
        <= 40
    ):
        score += 4

        reasons.append(
            "strong elevated momentum"
        )

    elif move_5m > 70:
        score -= 18

        reasons.append(
            "extreme pump risk"
        )

    elif move_5m < -25:
        score -= 15

        reasons.append(
            "sharp drawdown"
        )

    age = market.get(
        "pair_age_minutes"
    )

    if age is not None:
        if (
            8
            <= age
            <= 60
        ):
            score += 7

            reasons.append(
                "fresh pool with some history"
            )

        elif (
            60
            < age
            <= 120
        ):
            score += 4

            reasons.append(
                "young pool"
            )

        elif age < 5:
            score -= 8

            reasons.append(
                "too new for enough evidence"
            )

    fdv = num(
        market.get(
            "fdv"
        )
    )

    if (
        fdv > 0
        and liquidity > 0
    ):
        ratio = (
            fdv
            / liquidity
        )

        if ratio <= 12:
            score += 7

            reasons.append(
                "reasonable FDV/liquidity ratio"
            )

        elif ratio > 35:
            score -= 12

            reasons.append(
                "high FDV/liquidity ratio"
            )

    score -= round(
        risk_score
        * 0.75
    )

    score = max(
        0,
        min(
            100,
            score,
        ),
    )

    # Safety cannot be compensated for by momentum.

    if data_coverage_pct < 100:
        score = min(
            score,
            39,
        )

        reasons.append(
            "critical evidence is incomplete"
        )

    elif (
        risk_level
        == "CRITICAL"
    ):
        score = min(
            score,
            15,
        )

    elif (
        risk_level
        == "HIGH"
    ):
        score = min(
            score,
            25,
        )

    elif (
        risk_level
        == "MEDIUM"
    ):
        score = min(
            score,
            39,
        )

    elif (
        risk_level
        == "LOW"
        and risk_score > 0
    ):
        score = min(
            score,
            49,
        )

    if (
        risk_level
        == "LOW"
        and risk_score == 0
        and data_coverage_pct
        == 100
    ):
        if score >= 99:
            label = (
                "STRONG SETUP"
            )

        elif score >= 80:
            label = (
                "GOOD SETUP"
            )

        elif score >= 55:
            label = "WATCH"

        else:
            label = "SKIP"

    else:
        label = "SKIP"

    return (
        score,
        label,
        reasons,
    )


def scan_token(
    mint: str,
    config: dict[str, Any],
) -> dict[str, Any]:

    timeout = as_int(
        config.get(
            "http_timeout_seconds"
        ),
        18,
    )

    raw_pairs = (
        fetch_token_pairs(
            mint,
            timeout,
        )
    )

    market = (
        select_primary_pair(
            raw_pairs,
            mint,
        )
    )

    chain = (
        fetch_chain_facts(
            mint,
            str(
                config.get(
                    "rpc_url"
                )
                or DEFAULT_RPC
            ),
            timeout,
        )
    )

    (
        risk_score,
        risk_level,
        coverage,
        flags,
    ) = risk_analysis(
        market,
        chain,
        config,
    )

    (
        score,
        label,
        reasons,
    ) = setup_score(
        market,
        risk_score,
        risk_level,
        coverage,
    )

    return {
        "analysis_version":
            "forensic-v0.5",
        "mint":
            mint,
        "name": (
            market[
                "name"
            ]
            if market
            else "Unknown"
        ),
        "symbol": (
            market[
                "symbol"
            ]
            if market
            else "?"
        ),
        "market":
            market,
        "chain":
            chain,
        "risk_score":
            risk_score,
        "risk_level":
            risk_level,
        "data_coverage_pct":
            coverage,
        "risk_flags":
            flags,
        "setup_score":
            score,
        "setup_label":
            label,
        "setup_reasons":
            reasons,
        "hard_vetoes": [
            flag[
                "code"
            ]
            for flag
            in flags
            if flag.get(
                "severity"
            )
            in {
                "CRITICAL",
                "HIGH",
            }
        ],
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


def fetch_discovery_candidates(
    timeout: int,
) -> list[
    dict[str, str]
]:

    sources = [
        (
            "profile",
            DEX_LATEST_PROFILES,
        ),
        (
            "boost",
            DEX_LATEST_BOOSTS,
        ),
        (
            "community_takeover",
            DEX_LATEST_CTO,
        ),
    ]

    found: dict[
        str,
        dict[str, str],
    ] = {}

    for (
        source,
        url,
    ) in sources:

        try:
            payload = http_json(
                url,
                timeout=timeout,
            )

        except Exception:
            continue

        if isinstance(
            payload,
            dict,
        ):
            payload = [
                payload
            ]

        if not isinstance(
            payload,
            list,
        ):
            continue

        for item in payload:
            if (
                not isinstance(
                    item,
                    dict,
                )
                or item.get(
                    "chainId"
                )
                != "solana"
            ):
                continue

            mint = str(
                item.get(
                    "tokenAddress"
                )
                or ""
            ).strip()

            if not mint:
                continue

            if (
                mint
                not in found
            ):
                found[
                    mint
                ] = {
                    "mint":
                        mint,
                    "source":
                        source,
                }

            elif (
                source
                not in found[
                    mint
                ][
                    "source"
                ]
            ):
                found[
                    mint
                ][
                    "source"
                ] += (
                    "+"
                    + source
                )

    return list(
        found.values()
    )


def report_rank(
    report: dict[str, Any],
) -> tuple[
    int,
    int,
    int,
    int,
    float,
]:

    market = (
        report.get(
            "market"
        )
        or {}
    )

    fully_clean = int(
        report.get(
            "data_coverage_pct"
        )
        == 100
        and report.get(
            "risk_level"
        )
        == "LOW"
        and as_int(
            report.get(
                "risk_score"
            )
        )
        == 0
    )

    full_coverage = int(
        report.get(
            "data_coverage_pct"
        )
        == 100
    )

    return (
        fully_clean,
        full_coverage,
        -as_int(
            report.get(
                "risk_score"
            )
        ),
        as_int(
            report.get(
                "setup_score"
            )
        ),
        num(
            market.get(
                "liquidity_usd"
            )
        ),
    )


def discover_reports(
    config: dict[str, Any],
) -> list[
    dict[str, Any]
]:

    timeout = as_int(
        config.get(
            "http_timeout_seconds"
        ),
        18,
    )

    max_age = num(
        config.get(
            "new_pair_max_age_minutes"
        ),
        120,
    )

    limit = max(
        1,
        as_int(
            config.get(
                "discover_limit"
            ),
            15,
        ),
    )

    reports: list[
        dict[str, Any]
    ] = []

    for candidate in (
        fetch_discovery_candidates(
            timeout
        )
    ):
        if (
            len(
                reports
            )
            >= limit
        ):
            break

        try:
            report = scan_token(
                candidate[
                    "mint"
                ],
                config,
            )

        except Exception:
            continue

        report[
            "discovery_source"
        ] = candidate[
            "source"
        ]

        age = (
            report.get(
                "market"
            )
            or {}
        ).get(
            "pair_age_minutes"
        )

        if (
            age is not None
            and age
            <= max_age
        ):
            reports.append(
                report
            )

    # Safety-first sorting.
    reports.sort(
        key=report_rank,
        reverse=True,
    )

    return reports


def format_report(
    report: dict[str, Any],
) -> str:

    market = (
        report.get(
            "market"
        )
        or {}
    )

    chain = (
        report.get(
            "chain"
        )
        or {}
    )

    lines = [
        "=" * 72,
        (
            f"{report['name']} "
            f"({report['symbol']})"
        ),
        (
            "Mint: "
            f"{report['mint']}"
        ),
        (
            "Setup: "
            f"{report['setup_label']} "
            f"{report['setup_score']}/100"
        ),
        (
            "Risk: "
            f"{report['risk_level']} "
            f"{report['risk_score']}/100"
        ),
        (
            "Evidence coverage: "
            f"{report['data_coverage_pct']}% "
            "(coverage, not a safety probability)"
        ),
    ]

    if market:
        lines.extend(
            [
                (
                    "Liquidity: "
                    f"${num(market.get('liquidity_usd')):,.0f}"
                ),
                (
                    "1h volume: "
                    f"${num(market.get('volume_1h_usd')):,.0f}"
                ),
                (
                    "5m buys/sells: "
                    f"{as_int(market.get('buys_5m'))}/"
                    f"{as_int(market.get('sells_5m'))}"
                ),
                (
                    "1h buys/sells: "
                    f"{as_int(market.get('buys_1h'))}/"
                    f"{as_int(market.get('sells_1h'))}"
                ),
                (
                    "5m move: "
                    f"{num(market.get('change_5m_pct')):+.2f}%"
                ),
                (
                    "1h move: "
                    f"{num(market.get('change_1h_pct')):+.2f}%"
                ),
                (
                    "Pool age: "
                    + (
                        f"{num(market.get('pair_age_minutes')):.1f} min"
                        if market.get(
                            "pair_age_minutes"
                        )
                        is not None
                        else "unknown"
                    )
                ),
                (
                    "Exact pairs: "
                    f"{as_int(market.get('pair_count'), 1)}"
                ),
                (
                    "DEX: "
                    f"{market.get('url') or 'unknown'}"
                ),
            ]
        )

    extension_names = [
        str(
            item.get(
                "extension"
            )
            or item.get(
                "type"
            )
            or "unknown"
        )
        for item
        in (
            chain.get(
                "extensions"
            )
            or []
        )
        if isinstance(
            item,
            dict,
        )
    ]

    lines.extend(
        [
            (
                "Token program: "
                f"{chain.get('token_program') or 'unknown'}"
            ),
            (
                "Token-2022: "
                f"{'YES' if chain.get('is_token_2022') else 'NO'}"
            ),
            (
                "Mint authority active: "
                f"{'YES' if chain.get('mint_authority') else 'NO/NULL'}"
            ),
            (
                "Freeze authority active: "
                f"{'YES' if chain.get('freeze_authority') else 'NO/NULL'}"
            ),
            (
                "Token-2022 extensions: "
                + (
                    ", ".join(
                        extension_names
                    )
                    if extension_names
                    else "none/legacy"
                )
            ),
            (
                "Largest token account: "
                + (
                    f"{num(chain.get('top1_account_pct')):.1f}%"
                    if chain.get(
                        "top1_account_pct"
                    )
                    is not None
                    else "unknown"
                )
            ),
            (
                "Top 10 token accounts: "
                + (
                    f"{num(chain.get('top10_accounts_pct')):.1f}%"
                    if chain.get(
                        "top10_accounts_pct"
                    )
                    is not None
                    else "unknown"
                )
            ),
            (
                "Largest resolved owner: "
                + (
                    f"{num(chain.get('top1_owner_pct')):.1f}%"
                    if chain.get(
                        "top1_owner_pct"
                    )
                    is not None
                    else "unknown"
                )
            ),
            (
                "Top 10 resolved owners: "
                + (
                    f"{num(chain.get('top10_owners_pct')):.1f}%"
                    if chain.get(
                        "top10_owners_pct"
                    )
                    is not None
                    else "unknown"
                )
            ),
            "Flags:",
        ]
    )

    if report[
        "risk_flags"
    ]:
        for flag in (
            report[
                "risk_flags"
            ]
        ):
            lines.append(
                (
                    f"- [{flag['severity']}] "
                    f"{flag['code']}: "
                    f"{flag['message']}"
                )
            )

    else:
        lines.append(
            "- No rule-based flags triggered."
        )

    lines.append(
        (
            "A clean scan does not prove "
            "that a token cannot rug, be "
            "manipulated, or lose value."
        )
    )

    return "\n".join(
        lines
    )


def main() -> int:

    parser = (
        argparse.ArgumentParser()
    )

    parser.add_argument(
        "--config",
        default="config.json",
    )

    parser.add_argument(
        "--mint",
    )

    parser.add_argument(
        "--discover",
        action="store_true",
    )

    parser.add_argument(
        "--json",
        action="store_true",
    )

    args = (
        parser.parse_args()
    )

    config = load_config(
        args.config
    )

    if args.mint:
        report = scan_token(
            args.mint.strip(),
            config,
        )

        print(
            json.dumps(
                report,
                indent=2,
            )
            if args.json
            else format_report(
                report
            )
        )

        return 0

    if args.discover:
        reports = (
            discover_reports(
                config
            )
        )

        if args.json:
            print(
                json.dumps(
                    reports,
                    indent=2,
                )
            )

        else:
            for report in reports:
                print(
                    format_report(
                        report
                    )
                )
                print()

        return 0

    parser.error(
        (
            "use --mint <address> "
            "or --discover"
        )
    )

    return 2


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
