#!/usr/bin/env python3
"""
Solana Meme Bot v0.5 - guarded paper/live engine + paper-learning tracker.

DEFAULT = PAPER MODE.

v0.5 does NOT loosen the v0.4 trading gates. It records every newly discovered
candidate, including rejected candidates, so later price performance can be
compared with the original decision.

Live trading still requires ALL of:
- TRADING_MODE=live
- ENABLE_LIVE_TRADING=YES
- I_UNDERSTAND_LIVE_TRADING=YES
- JUPITER_API_KEY
- BS58_PRIVATE_KEY
- Node.js + npm install

Never put a private key in this file.
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

    # v0.5 learning tracker
    "paper_learning_enabled": True,
    "paper_learning_file": "paper_learning.json",
    "paper_learning_max_records": 2000,
    "paper_learning_horizons_minutes": [5, 15, 30, 60, 360, 1440],
}

def num(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default

def as_int(value: Any, default: int = 0) -> int:
    try:
        return default if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return default

def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

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
        state["position"] = old_position

    state.setdefault("seen_mints", [])
    state.setdefault("paper_history", [])
    return state

def save_state(path: str, state: dict[str, Any]) -> None:
    state["seen_mints"] = list(dict.fromkeys(state.get("seen_mints", [])))[-3000:]
    state["paper_history"] = state.get("paper_history", [])[-500:]
    Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

# ---------------- v0.5 PAPER LEARNING ----------------

def learning_path(trading: dict[str, Any]) -> str:
    return str(trading.get("paper_learning_file") or "paper_learning.json")

def load_learning(trading: dict[str, Any]) -> dict[str, Any]:
    path = Path(learning_path(trading))
    if not path.exists():
        return {"version": "0.5", "records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("version", "0.5")
        data.setdefault("records", [])
        return data
    except Exception:
        return {"version": "0.5", "records": []}

def save_learning(trading: dict[str, Any], data: dict[str, Any]) -> None:
    limit = max(100, as_int(trading.get("paper_learning_max_records"), 2000))
    data["version"] = "0.5"
    data["updated_at"] = now_iso()
    data["records"] = data.get("records", [])[-limit:]
    Path(learning_path(trading)).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )

def market_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    market = report.get("market") or {}
    return {
        "captured_at": now_iso(),
        "price_usd": num(market.get("price_usd") or market.get("priceUsd"), 0.0),
        "market_cap_usd": num(
            market.get("market_cap_usd")
            or market.get("marketCap")
            or market.get("market_cap"),
            0.0,
        ),
        "fdv_usd": num(market.get("fdv_usd") or market.get("fdv"), 0.0),
        "liquidity_usd": num(
            market.get("liquidity_usd")
            or (market.get("liquidity") or {}).get("usd"),
            0.0,
        ),
        "volume_1h_usd": num(
            market.get("volume_1h_usd")
            or (market.get("volume") or {}).get("h1"),
            0.0,
        ),
        "change_5m_pct": num(
            market.get("change_5m_pct")
            or (market.get("priceChange") or {}).get("m5"),
            0.0,
        ),
        "pair_age_minutes": num(market.get("pair_age_minutes"), 0.0),
    }

def paper_return_pct(entry_price: float, current_price: float) -> float | None:
    if entry_price <= 0 or current_price <= 0:
        return None
    return ((current_price / entry_price) - 1.0) * 100.0

def learning_record(
    report: dict[str, Any],
    gate_passed: bool,
    gate_failures: list[str],
) -> dict[str, Any]:
    return {
        "mint": report.get("mint"),
        "name": report.get("name"),
        "symbol": report.get("symbol"),
        "detected_at": now_iso(),
        "source": report.get("source"),
        "entry": market_snapshot(report),
        "setup_score": as_int(report.get("setup_score")),
        "setup_label": report.get("setup_label"),
        "setup_reasons": report.get("setup_reasons") or [],
        "risk_score": as_int(report.get("risk_score")),
        "risk_level": report.get("risk_level"),
        "data_coverage_pct": as_int(report.get("data_coverage_pct")),
        "risk_flags": report.get("risk_flags") or [],
        "gate_passed": bool(gate_passed),
        "gate_failures": list(gate_failures),
        "route_checked": False,
        "route_passed": None,
        "route_reason": None,
        "snapshots": {},
    }

def add_learning_record(
    learning: dict[str, Any],
    report: dict[str, Any],
    gate_passed: bool,
    gate_failures: list[str],
) -> dict[str, Any] | None:
    mint = report.get("mint")
    if not mint:
        return None

    # One detection record per mint. This mirrors v0.4's seen-mint behavior.
    for record in learning.get("records", []):
        if record.get("mint") == mint:
            return record

    record = learning_record(report, gate_passed, gate_failures)
    learning.setdefault("records", []).append(record)
    return record

def update_route_learning(
    record: dict[str, Any] | None,
    passed: bool,
    route: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if record is None:
        return
    record["route_checked"] = True
    record["route_passed"] = bool(passed)
    if error:
        record["route_reason"] = error
    elif route and route.get("reason"):
        record["route_reason"] = str(route.get("reason"))
    else:
        record["route_reason"] = "passed" if passed else "failed"

    if route:
        record["route_metrics"] = {
            "immediate_value_usdc": route.get("immediate_value_usdc"),
            "buy_impact_pct": route.get("buy_impact_pct"),
            "sell_impact_pct": route.get("sell_impact_pct"),
        }

def refresh_learning_snapshots(
    config: dict[str, Any],
    learning: dict[str, Any],
) -> None:
    """
    Fill snapshots only when a horizon is due.

    This intentionally uses monitor.scan_token() for correctness and simplicity.
    On GitHub Actions, paper_learning.json must persist between runs for multi-hour
    learning to work; an ephemeral runner alone cannot preserve it.
    """
    trading = config["trading"]
    if not bool(trading.get("paper_learning_enabled", True)):
        return

    horizons = trading.get("paper_learning_horizons_minutes") or [5, 15, 30, 60, 360, 1440]
    horizons = sorted({max(1, as_int(x)) for x in horizons})
    now = datetime.now(timezone.utc)

    # Bound network work per loop.
    refreshed = 0
    max_refreshes = 8

    for record in reversed(learning.get("records", [])):
        if refreshed >= max_refreshes:
            break

        detected = parse_iso(record.get("detected_at", ""))
        mint = record.get("mint")
        if detected is None or not mint:
            continue

        age_min = (now - detected).total_seconds() / 60.0
        snapshots = record.setdefault("snapshots", [])

        if isinstance(snapshots, dict):
            # Migrate an early draft format safely.
            snapshots = [
                {"horizon_minutes": as_int(k.rstrip("m")), **(v or {})}
                for k, v in snapshots.items()
                if str(k).endswith("m")
            ]
            record["snapshots"] = snapshots

        completed = {as_int(x.get("horizon_minutes")) for x in snapshots if isinstance(x, dict)}
        due = [h for h in horizons if age_min >= h and h not in completed]
        if not due:
            continue

        # Capture once and apply it to the earliest due horizon. Later runs can
        # capture subsequent horizons; this avoids pretending one price was seen
        # at several different times.
        horizon = due[0]
        try:
            report = monitor.scan_token(mint, config)
            snap = market_snapshot(report)
            entry_price = num((record.get("entry") or {}).get("price_usd"), 0.0)
            snap["horizon_minutes"] = horizon
            snap["actual_age_minutes"] = round(age_min, 2)
            snap["paper_return_pct"] = paper_return_pct(
                entry_price, num(snap.get("price_usd"), 0.0)
            )
            snapshots.append(snap)
            refreshed += 1
        except Exception as error:
            # Record the observation failure without fabricating price data.
            snapshots.append({
                "horizon_minutes": horizon,
                "actual_age_minutes": round(age_min, 2),
                "captured_at": now_iso(),
                "error": str(error)[:300],
            })
            refreshed += 1

# ---------------- ORIGINAL v0.4 GATES / EXECUTION ----------------

def daily_gate(state: dict[str, Any], trading: dict[str, Any]) -> tuple[bool, str]:
    if state.get("position"):
        return False, "position already open"
    if as_int(state.get("trades_today")) >= as_int(trading.get("max_trades_per_day"), 3):
        return False, "daily trade limit reached"
    if as_int(state.get("consecutive_losses")) >= as_int(trading.get("max_consecutive_losses"), 3):
        return False, "consecutive-loss limit reached"
    if num(state.get("realized_pnl_usdc")) <= -abs(num(trading.get("daily_loss_limit_usdc"), 2.70)):
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

def jupiter_quote(input_mint: str, output_mint: str, amount: int, timeout: int = 15) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
    })
    request = urllib.request.Request(
        f"{JUPITER_ORDER}?{params}",
        headers={
            "Accept": "application/json",
            "x-api-key": jupiter_api_key(),
            "User-Agent": "SolanaMemeBot/0.5",
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
    if quote.get("priceImpact") not in (None, ""):
        value = abs(num(quote.get("priceImpact")))
        return value * 100 if value <= 1 else value
    value = abs(num(quote.get("priceImpactPct")))
    return value * 100 if value <= 1 else value

def pretrade_roundtrip(mint: str, trading: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    size_usdc = num(trading.get("trade_size_usdc"), 5.0)
    buy_amount = round(size_usdc * 10**USDC_DECIMALS)
    buy_quote = jupiter_quote(USDC_MINT, mint, buy_amount)
    token_out = as_int(buy_quote.get("outAmount"))
    if token_out <= 0:
        return False, {"reason": "buy quote returned zero token output"}

    buy_impact = quote_price_impact_pct(buy_quote)
    max_impact = num(trading.get("max_quote_price_impact_pct"), 1.0)
    if buy_impact > max_impact:
        return False, {"reason": f"buy quote price impact {buy_impact:.2f}% > {max_impact:.2f}%"}

    sell_quote = jupiter_quote(mint, USDC_MINT, token_out)
    sell_impact = quote_price_impact_pct(sell_quote)
    if sell_impact > max_impact:
        return False, {"reason": f"sell quote price impact {sell_impact:.2f}% > {max_impact:.2f}%"}

    immediate_value = as_int(sell_quote.get("outAmount")) / 10**USDC_DECIMALS
    min_roundtrip = num(trading.get("min_immediate_roundtrip_value_usdc"), 4.70)
    if immediate_value < min_roundtrip:
        return False, {
            "reason": f"immediate sell quote only ${immediate_value:.4f} < ${min_roundtrip:.2f}",
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

def call_live_swap(input_mint: str, output_mint: str, amount: int) -> dict[str, Any]:
    if not live_mode_enabled():
        raise RuntimeError("live trading safety interlock is not fully enabled")
    if not os.getenv("BS58_PRIVATE_KEY", "").strip():
        raise RuntimeError("BS58_PRIVATE_KEY is missing")
    if not os.getenv("JUPITER_API_KEY", "").strip():
        raise RuntimeError("JUPITER_API_KEY is missing")

    result = subprocess.run(
        ["node", "jupiter_live.mjs", "--input-mint", input_mint,
         "--output-mint", output_mint, "--amount", str(int(amount))],
        text=True, capture_output=True, timeout=45,
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
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message[:3900],
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except Exception:
        return False

def open_position(report, route, state, trading, mode) -> None:
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
        token_amount = as_int(route.get("token_out_amount"))
        signature = None

    state["position"] = {
        "mode": mode, "mint": mint, "name": report.get("name"),
        "symbol": report.get("symbol"), "opened_at": now_iso(),
        "entry_value_usdc": trade_size, "token_amount_raw": token_amount,
        "setup_score": report.get("setup_score"), "risk_score": report.get("risk_score"),
        "risk_level": report.get("risk_level"), "buy_signature": signature,
    }
    state["trades_today"] = as_int(state.get("trades_today")) + 1

    message = (
        f"{'LIVE' if mode == 'live' else 'PAPER'} BUY\n"
        f"{report.get('name')} ({report.get('symbol')})\nMint: {mint}\n"
        f"Entry: ${trade_size:.2f}\n"
        f"Hard stop trigger: ${num(trading.get('hard_stop_value_usdc'), 4.10):.2f}\n"
        f"Setup: {report.get('setup_score')}/100 | "
        f"Risk: {report.get('risk_level')} {report.get('risk_score')}/100"
    )
    print(message)
    if bool(trading.get("telegram_enabled")):
        send_telegram(message)

def position_age_minutes(position: dict[str, Any]) -> float:
    opened = parse_iso(position.get("opened_at", ""))
    if opened is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds() / 60)

def quote_position_value(position: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    quote = jupiter_quote(position["mint"], USDC_MINT, as_int(position["token_amount_raw"]))
    return as_int(quote.get("outAmount")) / 10**USDC_DECIMALS, quote

def close_position(state, trading, reason: str, quoted_value_usdc: float) -> None:
    position = state["position"]
    mode = position["mode"]

    if mode == "live":
        execution = call_live_swap(
            position["mint"], USDC_MINT, as_int(position["token_amount_raw"])
        )
        actual_value = as_int(execution.get("totalOutputAmount")) / 10**USDC_DECIMALS
        signature = execution.get("signature")
    else:
        actual_value = quoted_value_usdc
        signature = None

    pnl = actual_value - num(position["entry_value_usdc"])
    state["realized_pnl_usdc"] = num(state.get("realized_pnl_usdc")) + pnl
    state["consecutive_losses"] = (
        as_int(state.get("consecutive_losses")) + 1 if pnl < 0 else 0
    )
    state.setdefault("paper_history", []).append({
        **position, "closed_at": now_iso(), "exit_value_usdc": actual_value,
        "pnl_usdc": pnl, "exit_reason": reason, "sell_signature": signature,
    })
    state["position"] = None

    message = (
        f"{'LIVE' if mode == 'live' else 'PAPER'} EXIT\n"
        f"{position.get('name')} ({position.get('symbol')})\n"
        f"Reason: {reason}\nExit value: ${actual_value:.4f}\n"
        f"P/L: ${pnl:+.4f}\nDaily P/L: ${num(state.get('realized_pnl_usdc')):+.4f}"
    )
    print(message)
    if bool(trading.get("telegram_enabled")):
        send_telegram(message)

def manage_open_position(state, trading) -> None:
    position = state.get("position")
    if not position:
        return
    try:
        value, _ = quote_position_value(position)
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
    elif take_profit > 0 and value >= take_profit:
        close_position(state, trading, "take profit triggered", value)
    elif position_age_minutes(position) >= max_hold:
        close_position(state, trading, "maximum hold time reached", value)

def choose_candidate(config, state, learning) -> dict[str, Any] | None:
    trading = config["trading"]
    seen = set(state.get("seen_mints", []))

    for report in monitor.discover_reports(config):
        mint = report["mint"]
        if mint in seen:
            continue

        state.setdefault("seen_mints", []).append(mint)
        passed, failures = report_gate(report, trading)

        record = None
        if bool(trading.get("paper_learning_enabled", True)):
            record = add_learning_record(learning, report, passed, failures)

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
            update_route_learning(record, False, error=str(error))
            print(f"  Jupiter round-trip check failed: {error}")
            continue

        update_route_learning(record, route_ok, route=route)
        if not route_ok:
            print(f"  Route check failed: {route.get('reason')}")
            continue

        report["_route_check"] = route
        return report

    return None

def run_once(config, state, learning) -> None:
    trading = config["trading"]
    state_path = str(trading.get("state_file") or "bot_state.json")

    # Learning snapshots are observational only; they never change a trade gate.
    refresh_learning_snapshots(config, learning)

    if state.get("position"):
        manage_open_position(state, trading)
        save_state(state_path, state)
        save_learning(trading, learning)
        return

    allowed, reason = daily_gate(state, trading)
    if not allowed:
        print(f"NO NEW TRADE: {reason}")
        save_state(state_path, state)
        save_learning(trading, learning)
        return

    report = choose_candidate(config, state, learning)
    save_learning(trading, learning)

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

    open_position(report, report["_route_check"], state, trading, mode)
    save_state(state_path, state)

def print_safety_summary(trading: dict[str, Any]) -> None:
    size = num(trading.get("trade_size_usdc"), 5.0)
    stop = num(trading.get("hard_stop_value_usdc"), 4.10)
    planned = max(0.0, size - stop)
    pct = 100.0 * planned / size if size else 0.0

    print("Solana Meme Bot v0.5")
    print(f"Mode default: {trading.get('trading_mode')}")
    print(f"Trade size: ${size:.2f}")
    print(f"Hard stop trigger: ${stop:.2f}")
    print(f"Planned loss at trigger: ${planned:.2f} ({pct:.1f}%), before slippage/fees")
    print(f"Minimum setup score: {trading.get('min_setup_score')}/100")
    print(f"Daily loss limit: ${num(trading.get('daily_loss_limit_usdc'), 2.70):.2f}")
    print(f"Paper learning: {'ON' if trading.get('paper_learning_enabled', True) else 'OFF'}")
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
    learning = load_learning(trading)
    print_safety_summary(trading)

    if args.once:
        run_once(config, state, learning)
        return 0

    poll = max(5, as_int(trading.get("poll_seconds"), 10))
    discovery = max(poll, as_int(trading.get("discovery_seconds"), 45))
    last_discovery = 0.0

    while True:
        try:
            if state.get("position"):
                manage_open_position(state, trading)
                save_state(state_path, state)
                refresh_learning_snapshots(config, learning)
                save_learning(trading, learning)
                time.sleep(poll)
                continue

            now = time.time()
            if now - last_discovery >= discovery:
                run_once(config, state, learning)
                last_discovery = now
            time.sleep(poll)
        except KeyboardInterrupt:
            save_state(state_path, state)
            save_learning(trading, learning)
            print("\nStopped.")
            return 0
        except Exception as error:
            print(f"Bot loop error: {error}", file=sys.stderr)
            save_state(state_path, state)
            save_learning(trading, learning)
            time.sleep(poll)

if __name__ == "__main__":
    raise SystemExit(main())
