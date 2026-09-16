#!/usr/bin/env python3
"""
Solana Token Monitor

Read-only market monitor using DEX Screener's public API.
It does NOT place trades, sign transactions, or access a wallet.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEFAULT_CONFIG = {
    "query": "SOL/USDC",
    "chain": "solana",
    "interval_seconds": 60,
    "top_n": 10,
    "min_liquidity_usd": 10000,
    "min_volume_24h_usd": 5000,
    "alert_abs_price_change_5m_pct": 5.0,
    "telegram_enabled": False,
}


@dataclass
class Pair:
    name: str
    symbol: str
    price_usd: float | None
    liquidity_usd: float
    volume_24h_usd: float
    change_5m_pct: float
    dex_id: str
    url: str
    pair_address: str


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_config(path: str) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    file_path = PathLike(path)
    if file_path.exists():
        with file_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain a JSON object")
        config.update(loaded)
    return config


class PathLike:
    """Tiny wrapper so this script stays dependency-free."""
    def __init__(self, value: str):
        self.value = value

    def exists(self) -> bool:
        return os.path.isfile(self.value)

    def open(self, *args: Any, **kwargs: Any):
        return open(self.value, *args, **kwargs)


def fetch_pairs(query: str, chain: str = "solana", timeout: int = 15) -> list[Pair]:
    params = urllib.parse.urlencode({"q": query})
    url = f"{DEXSCREENER_SEARCH}?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SolanaTokenMonitor/0.2",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    pairs: list[Pair] = []
    for raw in payload.get("pairs") or []:
        if raw.get("chainId") != chain:
            continue

        base = raw.get("baseToken") or {}
        liquidity = raw.get("liquidity") or {}
        volume = raw.get("volume") or {}
        price_change = raw.get("priceChange") or {}

        price_raw = raw.get("priceUsd")
        price = None if price_raw in (None, "") else _to_float(price_raw)

        pairs.append(
            Pair(
                name=str(base.get("name") or "Unknown"),
                symbol=str(base.get("symbol") or "?"),
                price_usd=price,
                liquidity_usd=_to_float(liquidity.get("usd")),
                volume_24h_usd=_to_float(volume.get("h24")),
                change_5m_pct=_to_float(price_change.get("m5")),
                dex_id=str(raw.get("dexId") or "unknown"),
                url=str(raw.get("url") or ""),
                pair_address=str(raw.get("pairAddress") or ""),
            )
        )

    pairs.sort(key=lambda item: item.liquidity_usd, reverse=True)
    return pairs


def eligible_pairs(pairs: list[Pair], config: dict[str, Any]) -> list[Pair]:
    min_liquidity = _to_float(config.get("min_liquidity_usd"))
    min_volume = _to_float(config.get("min_volume_24h_usd"))

    return [
        pair
        for pair in pairs
        if pair.liquidity_usd >= min_liquidity
        and pair.volume_24h_usd >= min_volume
    ]


def alert_reason(pair: Pair, config: dict[str, Any]) -> str | None:
    threshold = abs(_to_float(config.get("alert_abs_price_change_5m_pct"), 5.0))
    if threshold <= 0:
        return None

    if abs(pair.change_5m_pct) >= threshold:
        direction = "up" if pair.change_5m_pct > 0 else "down"
        return f"5m price moved {direction} {abs(pair.change_5m_pct):.2f}%"
    return None


def format_pair(pair: Pair) -> str:
    price = "N/A" if pair.price_usd is None else f"${pair.price_usd:.10g}"
    return (
        f"{pair.name} ({pair.symbol}) | {price} | "
        f"Liq ${pair.liquidity_usd:,.0f} | "
        f"Vol24h ${pair.volume_24h_usd:,.0f} | "
        f"5m {pair.change_5m_pct:+.2f}% | {pair.dex_id}"
    )


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("Telegram alert skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except Exception as error:
        print(f"Telegram error: {error}")
        return False


def run_once(config: dict[str, Any]) -> int:
    query = str(config.get("query") or "SOL/USDC")
    chain = str(config.get("chain") or "solana")
    top_n = max(1, int(config.get("top_n") or 10))

    print(f"\nSolana Token Monitor | query={query!r}")
    print("=" * 72)

    pairs = fetch_pairs(query=query, chain=chain)
    filtered = eligible_pairs(pairs, config)

    if not filtered:
        print("No pairs matched the configured liquidity/volume filters.")
        return 0

    alerts: list[str] = []
    for pair in filtered[:top_n]:
        print(format_pair(pair))
        reason = alert_reason(pair, config)
        if reason:
            alerts.append(
                f"ALERT: {pair.name} ({pair.symbol}) — {reason}\n"
                f"{format_pair(pair)}\n{pair.url}"
            )

    if alerts:
        print(f"\n{len(alerts)} alert(s) triggered.")
        if bool(config.get("telegram_enabled")):
            send_telegram("\n\n".join(alerts))
    else:
        print("\nNo alert thresholds triggered.")

    return len(alerts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Solana market monitor using DEX Screener."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument(
        "--query",
        help="Override the configured DEX Screener search query, e.g. BONK/USDC",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit instead of looping.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2

    if args.query:
        config["query"] = args.query

    if args.once:
        try:
            run_once(config)
            return 0
        except Exception as error:
            print(f"Monitor error: {error}", file=sys.stderr)
            return 1

    interval = max(15, int(config.get("interval_seconds") or 60))
    print(f"Starting monitor. Refresh interval: {interval}s. Press Ctrl+C to stop.")

    while True:
        try:
            run_once(config)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as error:
            print(f"Monitor error: {error}", file=sys.stderr)

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
