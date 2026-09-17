#!/usr/bin/env python3
"""
Solana Meme Bot v0.4 - guarded paper/live trade engine.

DEFAULT = PAPER MODE.

Live trading requires ALL of:
- TRADING_MODE=live
- ENABLE_LIVE_TRADING=YES
- I_UNDERSTAND_LIVE_TRADING=YES
- JUPITER_API_KEY
- BS58_PRIVATE_KEY
- Node.js + npm install

The live wallet should be a dedicated low-balance wallet, never a main wallet.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import monitor

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
JUPITER_ORDER = "https://api.jup.ag/swap/v2/order"

DEFAULT_TRADING: dict[str, Any] = {
    "trading_mode": "paper",
    "trade_size_usdc": 5.00,
    "hard_stop_value_usdc": 4.10,
    "take_profit_value_usdc": 0.0,
    "max_hold_minutes": 60,
    "min_setup_score": 99,
    "required_risk_level": "LOW",
    "required_data_coverage_pct": 100,
    "require_zero_risk_score": True,
    "one_position_at_a_time": True,
    "max_trades_per_day": 3,
    "max_consecutive_losses": 3,
    "daily_loss_limit_usdc": 2.70,
    "max_quote_price_impact_pct": 1.0,
    "min_immediate_roundtrip_value_usdc": 4.70,
    "poll_seconds": 10,
    "discovery_seconds": 45,
    "state_file": "bot_state.json",
    "telegram_enabled": False,
}

# The 5.00 -> 4.10 stop equals an 18% planned loss before fees/slippage.
PLANNED_STOP_LOSS_PCT = 18.0


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


def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_all_config(path: str) -> dict[str, Any]:
    config = monitor.load_config(path)
    trading = dict(DEFAULT_TRADING)

    p = Path(path)
    if p.exists():
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("trading"), dict):
            trading.update(loaded["trading"])

    config["trading"] = trading
    return config


def fresh_state() -> dict[str, Any]:
    return {
        "date_utc": utc_date(),
        "trades_today": 0,
        "consecutive_losses": 0,
        "realized_pnl_usdc": 0.0,
        "position": None,
        "seen_mints": [],
        "paper_history": [],
    }


def load_state(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return fresh_state()

    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return fresh_state()
    except Exception:
        return fresh_state()

    if state.get("date_utc") != utc_date():
        old_position = state.get("position")
        state = fresh_state()
        # Keep an existing position across midnight so it can still be exited.
        state["position"] = old_position

    state.setdefault("seen_mints", [])
    state.setdefault("paper_history", [])
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    state["seen_mints"] = list(dict.fromkeys(state.get("seen_mints", [])))[-3000:]
    state["paper_history"] = state.get("paper_history", [])[-500:]
    Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def daily_gate(state: dict[str, Any], trading: dict[str, Any]) -> tuple[bool, str]:
    if state.get("position"):
        return False, "position already open"

    if as_int(state.get("trades_today")) >= as_int(trading.get("max_trades_per_day"), 3):
        return False, "daily trade limit reached"

    if as_int(state.get("consecutive_losses")) >= as_int(trading.get("max_consecutive_losses"), 3):
        return False, "consecutive-loss limit reached"

    pnl = num(state.get("realized_pnl_usdc"))
    if pnl <= -abs(num(trading.get("daily_loss_limit_usdc"), 2.70)):
        return False, "daily loss limit reached"

    return True, "ok"


def report_gate(report: dict[str, Any], trading: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []

    if as_int(report.get("setup_score")) < as_int(trading.get("min_setup_score"), 99):
        failures.append(f"setup score below {trading.get('min_setup_score')}")

    if str(report.get("risk_level")) != str(trading.get("required_risk_level") or "LOW"):
        failures.append(f"risk level is {report.get('risk_level')}")

    if bool(trading.get("require_zero_risk_score", True)) and as_int(report.get("risk_score")) != 0:
        failures.append(f"risk score is {report.get('risk_score')}")

    if as_int(report.get("data_coverage_pct")) < as_int(trading.get("required_data_coverage_pct"), 100):
        failures.append(f"data coverage is {report.get('data_coverage_pct')}%")

    chain = report.get("chain") or {}
    if chain.get("mint_authority"):
        failures.append("mint authority active")
    if chain.get("freeze_authority"):
        failures.append("freeze authority active")

    return not failures, failures


def jupiter_api_key() -> str:
    key = os.getenv("JUPITER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JUPITER_API_KEY is required for Jupiter Swap V2 quotes")
    return key


def jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    timeout: int = 15,
) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
        }
    )
    request = urllib.request.Request(
        f"{JUPITER_ORDER}?{params}",
        headers={
            "Accept": "application/json",
            "x-api-key": jupiter_api_key(),
            "User-Agent": "SolanaMemeBot/0.4",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise RuntimeError("Jupiter returned a non-object quote")

    if payload.get("error") or payload.get("errorMessage"):
        raise RuntimeError(str(payload.get("error") or payload.get("errorMessage")))

    if not payload.get("outAmount"):
        raise RuntimeError("Jupiter quote has no outAmount")

    return payload


def quote_price_impact_pct(quote: dict[str, Any]) -> float:
    # Jupiter may expose either decimal fraction or percent-style strings depending
    # on route/API generation. Prefer priceImpact if present.
    if quote.get("priceImpact") not in (None, ""):
        value = abs(num(quote.get("priceImpact")))
        return value * 100 if value <= 1 else value

    value = abs(num(quote.get("priceImpactPct")))
    return value * 100 if value <= 1 else value


def pretrade_roundtrip(
    mint: str,
    trading: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    size_usdc = num(trading.get("trade_size_usdc"), 5.0)
    buy_amount = round(size_usdc * 10**USDC_DECIMALS)

    buy_quote = jupiter_quote(USDC_MINT, mint, buy_amount)
    token_out = as_int(buy_quote.get("outAmount"))
    if token_out <= 0:
        return False, {"reason": "buy quote returned zero token output"}

    buy_impact = quote_price_impact_pct(buy_quote)
    max_impact = num(trading.get("max_quote_price_impact_pct"), 1.0)
    if buy_impact > max_impact:
        return False, {
            "reason": f"buy quote price impact {buy_impact:.2f}% > {max_impact:.2f}%",
            "buy_quote": buy_quote,
        }

    sell_quote = jupiter_quote(mint, USDC_MINT, token_out)
    sell_impact = quote_price_impact_pct(sell_quote)
    if sell_impact > max_impact:
        return False, {
            "reason": f"sell quote price impact {sell_impact:.2f}% > {max_impact:.2f}%",
            "buy_quote": buy_quote,
            "sell_quote": sell_quote,
        }

    immediate_value = as_int(sell_quote.get("outAmount")) / 10**USDC_DECIMALS
    min_roundtrip = num(trading.get("min_immediate_roundtrip_value_usdc"), 4.70)
    if immediate_value < min_roundtrip:
        return False, {
            "reason": f"immediate sell quote only ${immediate_value:.4f} < ${min_roundtrip:.2f}",
            "buy_quote": buy_quote,
            "sell_quote": sell_quote,
            "immediate_value_usdc": immediate_value,
        }

    return True, {
        "buy_quote": buy_quote,
        "sell_quote": sell_quote,
        "token_out_amount": token_out,
        "immediate_value_usdc": immediate_value,
        "buy_impact_pct": buy_impact,
        "sell_impact_pct": sell_impact,
    }


def live_mode_enabled() -> bool:
    return (
        os.getenv("TRADING_MODE", "").strip().lower() == "live"
        and os.getenv("ENABLE_LIVE_TRADING", "").strip().upper() == "YES"
        and os.getenv("I_UNDERSTAND_LIVE_TRADING", "").strip().upper() == "YES"
    )


def call_live_swap(
    input_mint: str,
    output_mint: str,
    amount: int,
) -> dict[str, Any]:
    if not live_mode_enabled():
        raise RuntimeError("live trading safety interlock is not fully enabled")

    if not os.getenv("BS58_PRIVATE_KEY", "").strip():
        raise RuntimeError("BS58_PRIVATE_KEY is missing")
    if not os.getenv("JUPITER_API_KEY", "").strip():
        raise RuntimeError("JUPITER_API_KEY is missing")

    result = subprocess.run(
        [
            "node",
            "jupiter_live.mjs",
            "--input-mint",
            input_mint,
            "--output-mint",
            output_mint,
            "--amount",
            str(int(amount)),
        ],
        text=True,
        capture_output=True,
        timeout=45,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "live swap failed")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid live-swap response: {result.stdout[:500]}") from error

    if payload.get("status") != "Success":
        raise RuntimeError(f"Jupiter execute failed: {payload}")
    return payload


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message[:3900],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except Exception:
        return False


def open_position(
    report: dict[str, Any],
    route: dict[str, Any],
    state: dict[str, Any],
    trading: dict[str, Any],
    mode: str,
) -> None:
    mint = report["mint"]
    trade_size = num(trading.get("trade_size_usdc"), 5.0)
    input_amount = round(trade_size * 10**USDC_DECIMALS)

    if mode == "live":
        execution = call_live_swap(USDC_MINT, mint, input_amount)
        token_amount = as_int(execution.get("totalOutputAmount"))
        signature = execution.get("signature")
        if token_amount <= 0:
            raise RuntimeError("live buy succeeded but returned no token amount")
    else:
        execution = None
        token_amount = as_int(route.get("token_out_amount"))
        signature = None

    state["position"] = {
        "mode": mode,
        "mint": mint,
        "name": report.get("name"),
        "symbol": report.get("symbol"),
        "opened_at": now_iso(),
        "entry_value_usdc": trade_size,
        "token_amount_raw": token_amount,
        "setup_score": report.get("setup_score"),
        "risk_score": report.get("risk_score"),
        "risk_level": report.get("risk_level"),
        "buy_signature": signature,
    }
    state["trades_today"] = as_int(state.get("trades_today")) + 1

    message = (
        f"{'LIVE' if mode == 'live' else 'PAPER'} BUY\n"
        f"{report.get('name')} ({report.get('symbol')})\n"
        f"Mint: {mint}\n"
        f"Entry: ${trade_size:.2f}\n"
        f"Hard stop trigger: ${num(trading.get('hard_stop_value_usdc'), 4.10):.2f}\n"
        f"Setup: {report.get('setup_score')}/100 | Risk: {report.get('risk_level')} {report.get('risk_score')}/100"
    )
    print(message)
    if bool(trading.get("telegram_enabled")):
        send_telegram(message)


def position_age_minutes(position: dict[str, Any]) -> float:
    try:
        opened = datetime.fromisoformat(position["opened_at"].replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds() / 60)
    except Exception:
        return 0.0


def quote_position_value(position: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    quote = jupiter_quote(
        position["mint"],
        USDC_MINT,
        as_int(position["token_amount_raw"]),
    )
    value = as_int(quote.get("outAmount")) / 10**USDC_DECIMALS
    return value, quote


def close_position(
    state: dict[str, Any],
    trading: dict[str, Any],
    reason: str,
    quoted_value_usdc: float,
) -> None:
    position = state["position"]
    mode = position["mode"]

    if mode == "live":
        execution = call_live_swap(
            position["mint"],
            USDC_MINT,
            as_int(position["token_amount_raw"]),
        )
        actual_value = as_int(execution.get("totalOutputAmount")) / 10**USDC_DECIMALS
        signature = execution.get("signature")
    else:
        actual_value = quoted_value_usdc
        signature = None

    pnl = actual_value - num(position["entry_value_usdc"])
    state["realized_pnl_usdc"] = num(state.get("realized_pnl_usdc")) + pnl

    if pnl < 0:
        state["consecutive_losses"] = as_int(state.get("consecutive_losses")) + 1
    else:
        state["consecutive_losses"] = 0

    history_entry = {
        **position,
        "closed_at": now_iso(),
        "exit_value_usdc": actual_value,
        "pnl_usdc": pnl,
        "exit_reason": reason,
        "sell_signature": signature,
    }
    state.setdefault("paper_history", []).append(history_entry)
    state["position"] = None

    message = (
        f"{'LIVE' if mode == 'live' else 'PAPER'} EXIT\n"
        f"{position.get('name')} ({position.get('symbol')})\n"
        f"Reason: {reason}\n"
        f"Exit value: ${actual_value:.4f}\n"
        f"P/L: ${pnl:+.4f}\n"
        f"Daily P/L: ${num(state.get('realized_pnl_usdc')):+.4f}"
    )
    print(message)
    if bool(trading.get("telegram_enabled")):
        send_telegram(message)


def manage_open_position(
    state: dict[str, Any],
    trading: dict[str, Any],
) -> None:
    position = state.get("position")
    if not position:
        return

    try:
        value, quote = quote_position_value(position)
    except Exception as error:
        print(f"Position quote failed: {error}")
        return

    print(
        f"OPEN {position.get('symbol')} | quoted exit ${value:.4f} | "
        f"stop ${num(trading.get('hard_stop_value_usdc'), 4.10):.2f}"
    )

    stop = num(trading.get("hard_stop_value_usdc"), 4.10)
    take_profit = num(trading.get("take_profit_value_usdc"), 0.0)
    max_hold = num(trading.get("max_hold_minutes"), 60)

    if value <= stop:
        close_position(state, trading, "hard stop triggered", value)
        return

    if take_profit > 0 and value >= take_profit:
        close_position(state, trading, "take profit triggered", value)
        return

    if position_age_minutes(position) >= max_hold:
        close_position(state, trading, "maximum hold time reached", value)


def choose_candidate(
    config: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any] | None:
    trading = config["trading"]
    seen = set(state.get("seen_mints", []))

    for report in monitor.discover_reports(config):
        mint = report["mint"]
        if mint in seen:
            continue

        state.setdefault("seen_mints", []).append(mint)
        passed, failures = report_gate(report, trading)

        print(
            f"CANDIDATE {report.get('symbol')} | setup {report.get('setup_score')}/100 | "
            f"risk {report.get('risk_level')} {report.get('risk_score')}/100 | "
            f"{'PASS' if passed else 'SKIP'}"
        )
        if not passed:
            print("  " + "; ".join(failures))
            continue

        try:
            route_ok, route = pretrade_roundtrip(mint, trading)
        except Exception as error:
            print(f"  Jupiter round-trip check failed: {error}")
            continue

        if not route_ok:
            print(f"  Route check failed: {route.get('reason')}")
            continue

        report["_route_check"] = route
        return report

    return None


def run_once(config: dict[str, Any], state: dict[str, Any]) -> None:
    trading = config["trading"]
    state_path = str(trading.get("state_file") or "bot_state.json")

    if state.get("position"):
        manage_open_position(state, trading)
        save_state(state_path, state)
        return

    allowed, reason = daily_gate(state, trading)
    if not allowed:
        print(f"NO NEW TRADE: {reason}")
        save_state(state_path, state)
        return

    report = choose_candidate(config, state)
    if not report:
        print("No candidate passed every gate.")
        save_state(state_path, state)
        return

    configured_mode = str(trading.get("trading_mode") or "paper").lower()
    env_mode = os.getenv("TRADING_MODE", configured_mode).strip().lower()
    mode = "live" if env_mode == "live" else "paper"

    if mode == "live" and not live_mode_enabled():
        print("LIVE MODE REQUESTED BUT SAFETY INTERLOCKS ARE NOT COMPLETE; falling back to paper.")
        mode = "paper"

    open_position(
        report,
        report["_route_check"],
        state,
        trading,
        mode,
    )
    save_state(state_path, state)


def print_safety_summary(trading: dict[str, Any]) -> None:
    size = num(trading.get("trade_size_usdc"), 5.0)
    stop = num(trading.get("hard_stop_value_usdc"), 4.10)
    planned = max(0.0, size - stop)
    pct = 100.0 * planned / size if size else 0.0

    print("Solana Meme Bot v0.4")
    print(f"Mode default: {trading.get('trading_mode')}")
    print(f"Trade size: ${size:.2f}")
    print(f"Hard stop trigger: ${stop:.2f}")
    print(f"Planned loss at trigger: ${planned:.2f} ({pct:.1f}%), before slippage/fees")
    print(f"Minimum setup score: {trading.get('min_setup_score')}/100")
    print(f"Daily loss limit: ${num(trading.get('daily_loss_limit_usdc'), 2.70):.2f}")
    print("A stop trigger is NOT a guaranteed execution price on a fast or illiquid token.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        config = load_all_config(args.config)
    except Exception as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2

    trading = config["trading"]
    state_path = str(trading.get("state_file") or "bot_state.json")
    state = load_state(state_path)
    print_safety_summary(trading)

    if args.once:
        run_once(config, state)
        return 0

    poll = max(5, as_int(trading.get("poll_seconds"), 10))
    discovery = max(poll, as_int(trading.get("discovery_seconds"), 45))
    last_discovery = 0.0

    while True:
        try:
            if state.get("position"):
                manage_open_position(state, trading)
                save_state(state_path, state)
                time.sleep(poll)
                continue

            now = time.time()
            if now - last_discovery >= discovery:
                run_once(config, state)
                last_discovery = now
            time.sleep(poll)
        except KeyboardInterrupt:
            save_state(state_path, state)
            print("\nStopped.")
            return 0
        except Exception as error:
            print(f"Bot loop error: {error}", file=sys.stderr)
            save_state(state_path, state)
            time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
