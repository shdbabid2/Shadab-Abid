#!/usr/bin/env python3
"""
Solana Meme Bot v0.4 - precision-first read-only scanner.

The scanner:
- discovers newly surfaced Solana tokens from DEX Screener feeds
- resolves each candidate by exact mint address
- checks exact DEX pair metrics
- reads mint/freeze authority and top-token-account concentration from Solana RPC
- produces a transparent 0-100 setup score and separate risk score

No score is a probability of profit or proof that a token is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEX = "https://api.dexscreener.com"
DEX_TOKEN_PAIRS = DEX + "/token-pairs/v1/solana/{mint}"
DEX_LATEST_PROFILES = DEX + "/token-profiles/latest/v1"
DEX_LATEST_BOOSTS = DEX + "/token-boosts/latest/v1"
DEX_LATEST_CTO = DEX + "/community-takeovers/latest/v1"

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

DEFAULT_CONFIG: dict[str, Any] = {
    "rpc_url": DEFAULT_RPC,
    "http_timeout_seconds": 18,
    "new_pair_max_age_minutes": 120,
    "discover_limit": 15,
    "min_liquidity_usd": 20000,
    "preferred_liquidity_usd": 50000,
    "min_1h_volume_usd": 15000,
    "max_fdv_liquidity_ratio": 35,
    "max_top1_account_pct": 20,
    "max_top10_accounts_pct": 60,
    "extreme_5m_move_pct": 70,
    "min_recent_transactions": 10,
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(path: str) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain a JSON object")
        config.update(loaded)
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
        "User-Agent": "SolanaMemeBot/0.4",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        method=method,
        data=body,
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def rpc_call(
    rpc_url: str,
    method: str,
    params: list[Any],
    timeout: int,
) -> Any:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")

    response = http_json(
        rpc_url,
        timeout=timeout,
        method="POST",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(f"RPC {method}: {response['error']}")
    return response.get("result") if isinstance(response, dict) else None


def fetch_token_pairs(mint: str, timeout: int) -> list[dict[str, Any]]:
    url = DEX_TOKEN_PAIRS.format(mint=urllib.parse.quote(mint, safe=""))
    payload = http_json(url, timeout=timeout)
    return payload if isinstance(payload, list) else []


def token_side(raw: dict[str, Any], mint: str) -> dict[str, Any]:
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    if base.get("address") == mint:
        return base
    if quote.get("address") == mint:
        return quote
    return {}


def pair_age_minutes(raw: dict[str, Any]) -> float | None:
    created = raw.get("pairCreatedAt")
    if created in (None, ""):
        return None
    try:
        return max(0.0, (time.time() * 1000 - float(created)) / 60000.0)
    except (TypeError, ValueError):
        return None


def parse_pair(raw: dict[str, Any], mint: str) -> dict[str, Any] | None:
    token = token_side(raw, mint)
    if not token:
        return None

    liquidity = raw.get("liquidity") or {}
    volume = raw.get("volume") or {}
    change = raw.get("priceChange") or {}
    txns = raw.get("txns") or {}
    tx5 = txns.get("m5") or {}
    tx1 = txns.get("h1") or {}

    return {
        "mint": mint,
        "name": str(token.get("name") or "Unknown"),
        "symbol": str(token.get("symbol") or "?"),
        "dex_id": str(raw.get("dexId") or "unknown"),
        "pair_address": str(raw.get("pairAddress") or ""),
        "url": str(raw.get("url") or ""),
        "price_usd": None if raw.get("priceUsd") in (None, "") else num(raw.get("priceUsd")),
        "liquidity_usd": num(liquidity.get("usd")),
        "market_cap": num(raw.get("marketCap")),
        "fdv": num(raw.get("fdv")),
        "pair_age_minutes": pair_age_minutes(raw),
        "volume_5m_usd": num(volume.get("m5")),
        "volume_1h_usd": num(volume.get("h1")),
        "volume_24h_usd": num(volume.get("h24")),
        "buys_5m": as_int(tx5.get("buys")),
        "sells_5m": as_int(tx5.get("sells")),
        "buys_1h": as_int(tx1.get("buys")),
        "sells_1h": as_int(tx1.get("sells")),
        "change_5m_pct": num(change.get("m5")),
        "change_1h_pct": num(change.get("h1")),
        "boosts_active": as_int((raw.get("boosts") or {}).get("active")),
    }


def select_primary_pair(
    pairs: list[dict[str, Any]],
    mint: str,
) -> dict[str, Any] | None:
    parsed = [parse_pair(pair, mint) for pair in pairs]
    parsed = [pair for pair in parsed if pair]
    if not parsed:
        return None
    return max(
        parsed,
        key=lambda pair: (
            pair["liquidity_usd"],
            pair["volume_24h_usd"],
        ),
    )


def fetch_chain_facts(
    mint: str,
    rpc_url: str,
    timeout: int,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "rpc_mint_ok": False,
        "holder_data_ok": False,
        "mint_authority": None,
        "freeze_authority": None,
        "token_program": None,
        "decimals": None,
        "supply_raw": None,
        "top1_account_pct": None,
        "top10_accounts_pct": None,
    }

    try:
        result = rpc_call(
            rpc_url,
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            timeout,
        )
        value = (result or {}).get("value")
        if value:
            facts["token_program"] = value.get("owner")
            data = value.get("data") or {}
            parsed = data.get("parsed") or {}
            info = parsed.get("info") or {}
            facts["mint_authority"] = info.get("mintAuthority")
            facts["freeze_authority"] = info.get("freezeAuthority")
            facts["decimals"] = info.get("decimals")
            facts["supply_raw"] = as_int(info.get("supply"), 0) or None
            facts["rpc_mint_ok"] = True
    except Exception:
        pass

    try:
        supply_result = rpc_call(
            rpc_url,
            "getTokenSupply",
            [mint, {"commitment": "confirmed"}],
            timeout,
        )
        supply_info = (supply_result or {}).get("value") or {}
        total_supply = as_int(supply_info.get("amount"), 0)

        holders_result = rpc_call(
            rpc_url,
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
            timeout,
        )
        holders = (holders_result or {}).get("value") or []

        if total_supply > 0 and holders:
            amounts = [as_int(item.get("amount"), 0) for item in holders]
            facts["supply_raw"] = total_supply
            facts["top1_account_pct"] = 100.0 * amounts[0] / total_supply
            facts["top10_accounts_pct"] = 100.0 * sum(amounts[:10]) / total_supply
            facts["holder_data_ok"] = True
    except Exception:
        pass

    return facts


def add_flag(
    flags: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    points: int,
) -> None:
    flags.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            "points": points,
        }
    )


def risk_analysis(
    market: dict[str, Any] | None,
    chain: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, str, int, list[dict[str, Any]]]:
    flags: list[dict[str, Any]] = []
    coverage = 0

    if market:
        coverage += 40
        liquidity = market["liquidity_usd"]
        if liquidity < 5000:
            add_flag(flags, "CRITICAL", "VERY_LOW_LIQUIDITY", "Liquidity is below $5,000.", 35)
        elif liquidity < num(config.get("min_liquidity_usd"), 20000):
            add_flag(flags, "HIGH", "LOW_LIQUIDITY", f"Liquidity is only ${liquidity:,.0f}.", 22)
        elif liquidity < num(config.get("preferred_liquidity_usd"), 50000):
            add_flag(flags, "MEDIUM", "THIN_LIQUIDITY", f"Liquidity is ${liquidity:,.0f}.", 8)

        if market["fdv"] > 0 and liquidity > 0:
            ratio = market["fdv"] / liquidity
            if ratio > num(config.get("max_fdv_liquidity_ratio"), 35):
                add_flag(flags, "HIGH", "FDV_LIQUIDITY_IMBALANCE", f"FDV/liquidity is {ratio:.1f}x.", 18)

        if abs(market["change_5m_pct"]) >= num(config.get("extreme_5m_move_pct"), 70):
            add_flag(flags, "HIGH", "EXTREME_5M_MOVE", f"5m move is {market['change_5m_pct']:+.1f}%.", 15)

        recent = market["buys_5m"] + market["sells_5m"]
        if recent >= as_int(config.get("min_recent_transactions"), 10):
            if market["sells_5m"] >= max(8, market["buys_5m"] * 2):
                add_flag(flags, "HIGH", "SELL_PRESSURE", "Recent sells are at least 2x buys.", 14)

        age = market["pair_age_minutes"]
        if age is not None and age < 3:
            add_flag(flags, "MEDIUM", "EXTREMELY_NEW_POOL", f"Pool age is only {age:.1f} minutes.", 6)
    else:
        add_flag(flags, "HIGH", "NO_EXACT_POOL", "No exact-address Solana DEX pool was found.", 25)

    if chain.get("rpc_mint_ok"):
        coverage += 30
        if chain.get("mint_authority"):
            add_flag(flags, "HIGH", "MINT_AUTHORITY_ACTIVE", "Mint authority is still active.", 26)
        if chain.get("freeze_authority"):
            add_flag(flags, "HIGH", "FREEZE_AUTHORITY_ACTIVE", "Freeze authority is still active.", 30)
    else:
        add_flag(flags, "MEDIUM", "MINT_RPC_UNAVAILABLE", "Mint authority data could not be verified.", 8)

    if chain.get("holder_data_ok"):
        coverage += 30
        top1 = chain.get("top1_account_pct")
        top10 = chain.get("top10_accounts_pct")

        if top1 is not None and top1 > num(config.get("max_top1_account_pct"), 20):
            severity = "CRITICAL" if top1 >= 60 else "HIGH"
            add_flag(flags, severity, "TOP1_CONCENTRATION", f"Largest token account holds {top1:.1f}% of supply.", 30 if severity == "CRITICAL" else 18)

        if top10 is not None and top10 > num(config.get("max_top10_accounts_pct"), 60):
            add_flag(flags, "HIGH", "TOP10_CONCENTRATION", f"Top 10 token accounts hold {top10:.1f}% of supply.", 18)
    else:
        add_flag(flags, "MEDIUM", "HOLDER_RPC_UNAVAILABLE", "Top-account concentration could not be verified.", 6)

    score = min(100, sum(flag["points"] for flag in flags))

    if coverage < 100:
        level = "INSUFFICIENT_DATA"
    elif any(flag["severity"] == "CRITICAL" for flag in flags) or score >= 70:
        level = "CRITICAL"
    elif score >= 40:
        level = "HIGH"
    elif score >= 15:
        level = "MEDIUM"
    else:
        level = "LOW"

    return score, level, coverage, flags


def setup_score(
    market: dict[str, Any] | None,
    risk_score: int,
    risk_level: str,
) -> tuple[int, str, list[str]]:
    if not market:
        return 0, "SKIP", ["no exact market pair"]

    score = 50
    reasons: list[str] = []

    liq = market["liquidity_usd"]
    if liq >= 150000:
        score += 20
        reasons.append("very strong liquidity")
    elif liq >= 75000:
        score += 16
        reasons.append("strong liquidity")
    elif liq >= 50000:
        score += 12
        reasons.append("good liquidity")
    elif liq >= 20000:
        score += 5
        reasons.append("moderate liquidity")
    else:
        score -= 20
        reasons.append("weak liquidity")

    vol = market["volume_1h_usd"]
    if vol >= 150000:
        score += 14
        reasons.append("very high 1h volume")
    elif vol >= 50000:
        score += 10
        reasons.append("healthy 1h volume")
    elif vol >= 15000:
        score += 6
        reasons.append("adequate 1h volume")
    else:
        score -= 6
        reasons.append("low 1h volume")

    recent = market["buys_5m"] + market["sells_5m"]
    if recent >= 20:
        buy_ratio = market["buys_5m"] / max(1, recent)
        if 0.55 <= buy_ratio <= 0.80:
            score += 12
            reasons.append("healthy recent buy/sell balance")
        elif buy_ratio > 0.90:
            score -= 4
            reasons.append("extremely one-sided buying")
        elif buy_ratio < 0.40:
            score -= 12
            reasons.append("sell-heavy recent flow")

    move = market["change_5m_pct"]
    if 2 <= move <= 25:
        score += 10
        reasons.append("positive but not extreme 5m momentum")
    elif 25 < move <= 50:
        score += 5
        reasons.append("strong elevated momentum")
    elif move > 70:
        score -= 15
        reasons.append("extreme pump risk")
    elif move < -25:
        score -= 15
        reasons.append("sharp drawdown")

    age = market["pair_age_minutes"]
    if age is not None:
        if 5 <= age <= 60:
            score += 7
            reasons.append("fresh pool with some history")
        elif 60 < age <= 240:
            score += 4
            reasons.append("young pool")
        elif age < 3:
            score -= 5
            reasons.append("too new for much evidence")

    if market["fdv"] > 0 and liq > 0:
        ratio = market["fdv"] / liq
        if ratio <= 12:
            score += 7
            reasons.append("reasonable FDV/liquidity ratio")
        elif ratio > 35:
            score -= 10
            reasons.append("high FDV/liquidity ratio")

    score -= round(risk_score * 0.65)
    score = max(0, min(100, score))

    if risk_level in {"CRITICAL", "HIGH", "INSUFFICIENT_DATA"}:
        label = "SKIP"
    elif score >= 99:
        label = "STRONG SETUP"
    elif score >= 80:
        label = "GOOD SETUP"
    elif score >= 55:
        label = "WATCH"
    else:
        label = "SKIP"

    return score, label, reasons


def scan_token(mint: str, config: dict[str, Any]) -> dict[str, Any]:
    timeout = as_int(config.get("http_timeout_seconds"), 18)
    market = select_primary_pair(fetch_token_pairs(mint, timeout), mint)
    chain = fetch_chain_facts(
        mint,
        str(config.get("rpc_url") or DEFAULT_RPC),
        timeout,
    )

    risk_score, risk_level, coverage, flags = risk_analysis(market, chain, config)
    score, label, reasons = setup_score(market, risk_score, risk_level)

    return {
        "mint": mint,
        "name": market["name"] if market else "Unknown",
        "symbol": market["symbol"] if market else "?",
        "market": market,
        "chain": chain,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "data_coverage_pct": coverage,
        "risk_flags": flags,
        "setup_score": score,
        "setup_label": label,
        "setup_reasons": reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_discovery_candidates(timeout: int) -> list[dict[str, str]]:
    sources = [
        ("profile", DEX_LATEST_PROFILES),
        ("boost", DEX_LATEST_BOOSTS),
        ("community_takeover", DEX_LATEST_CTO),
    ]
    found: dict[str, dict[str, str]] = {}

    for source, url in sources:
        try:
            payload = http_json(url, timeout=timeout)
        except Exception:
            continue

        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            continue

        for item in payload:
            if not isinstance(item, dict) or item.get("chainId") != "solana":
                continue
            mint = str(item.get("tokenAddress") or "").strip()
            if not mint:
                continue
            if mint not in found:
                found[mint] = {"mint": mint, "source": source}
            elif source not in found[mint]["source"]:
                found[mint]["source"] += "+" + source

    return list(found.values())


def discover_reports(config: dict[str, Any]) -> list[dict[str, Any]]:
    timeout = as_int(config.get("http_timeout_seconds"), 18)
    max_age = num(config.get("new_pair_max_age_minutes"), 120)
    limit = max(1, as_int(config.get("discover_limit"), 15))

    reports: list[dict[str, Any]] = []
    for candidate in fetch_discovery_candidates(timeout):
        if len(reports) >= limit:
            break
        try:
            report = scan_token(candidate["mint"], config)
        except Exception:
            continue

        report["discovery_source"] = candidate["source"]
        age = (report.get("market") or {}).get("pair_age_minutes")
        if age is not None and age <= max_age:
            reports.append(report)

    reports.sort(
        key=lambda report: (report.get("market") or {}).get("pair_age_minutes", 999999)
    )
    return reports


def format_report(report: dict[str, Any]) -> str:
    market = report.get("market") or {}
    chain = report.get("chain") or {}

    lines = [
        "=" * 72,
        f"{report['name']} ({report['symbol']})",
        f"Mint: {report['mint']}",
        f"Setup: {report['setup_label']} {report['setup_score']}/100",
        f"Risk: {report['risk_level']} {report['risk_score']}/100",
        f"Evidence coverage: {report['data_coverage_pct']}% (coverage, not safety probability)",
    ]

    if market:
        lines.extend(
            [
                f"Liquidity: ${market['liquidity_usd']:,.0f}",
                f"1h volume: ${market['volume_1h_usd']:,.0f}",
                f"5m buys/sells: {market['buys_5m']}/{market['sells_5m']}",
                f"5m move: {market['change_5m_pct']:+.2f}%",
                f"Pool age: {market['pair_age_minutes']:.1f} min" if market["pair_age_minutes"] is not None else "Pool age: unknown",
                f"DEX: {market['url']}",
            ]
        )

    lines.extend(
        [
            f"Mint authority active: {'YES' if chain.get('mint_authority') else 'NO/NULL'}",
            f"Freeze authority active: {'YES' if chain.get('freeze_authority') else 'NO/NULL'}",
            f"Top account: {chain.get('top1_account_pct') if chain.get('top1_account_pct') is not None else 'unknown'}%",
            f"Top 10 accounts: {chain.get('top10_accounts_pct') if chain.get('top10_accounts_pct') is not None else 'unknown'}%",
            "Flags:",
        ]
    )

    if report["risk_flags"]:
        for flag in report["risk_flags"]:
            lines.append(f"- [{flag['severity']}] {flag['code']}: {flag['message']}")
    else:
        lines.append("- No rule-based flags triggered.")

    lines.append("A clean scan does not prove that a token cannot rug or lose value.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--mint")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.mint:
        report = scan_token(args.mint.strip(), config)
        print(json.dumps(report, indent=2) if args.json else format_report(report))
        return 0

    if args.discover:
        reports = discover_reports(config)
        if args.json:
            print(json.dumps(reports, indent=2))
        else:
            for report in reports:
                print(format_report(report))
                print()
        return 0

    parser.error("use --mint <address> or --discover")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
