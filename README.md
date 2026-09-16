# Solana Token Monitor

A small, dependency-free, open-source **read-only** market monitor for Solana pairs.

It uses the public DEX Screener API to search Solana pairs, filters results by liquidity and 24-hour volume, prints price/market information, and can optionally send Telegram alerts when the configured 5-minute price-change threshold is crossed.

> This project does **not** place trades, sign transactions, connect to a wallet, or promise profits.

## Features

- Search DEX Screener for Solana pairs
- Sort results by liquidity
- Filter by minimum liquidity and 24-hour volume
- Display USD price, liquidity, volume, 5-minute change, and DEX
- Configurable 5-minute movement alerts
- Optional Telegram notifications
- Continuous monitoring or one-shot mode
- No third-party Python packages required
- No wallet/private-key access

## Requirements

- Python 3.10 or newer
- Internet access

## Quick start

Clone or download the repository, then copy the example configuration:

```bash
cp config.example.json config.json
```

Run one check:

```bash
python monitor.py --once
```

Run continuously:

```bash
python monitor.py
```

Try another search without editing the config:

```bash
python monitor.py --once --query "BONK/USDC"
```

## Configuration

`config.example.json` contains:

```json
{
  "query": "SOL/USDC",
  "chain": "solana",
  "interval_seconds": 60,
  "top_n": 10,
  "min_liquidity_usd": 10000,
  "min_volume_24h_usd": 5000,
  "alert_abs_price_change_5m_pct": 5.0,
  "telegram_enabled": false
}
```

Copy it to `config.json` and edit your local copy. `config.json` is ignored by Git so personal settings are not committed accidentally.

## Telegram alerts

Telegram alerts are optional. Keep secrets out of source code.

1. Create a bot with Telegram's BotFather.
2. Obtain your chat ID.
3. Set these environment variables:

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

4. Set `"telegram_enabled": true` in `config.json`.

On Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="your-bot-token"
$env:TELEGRAM_CHAT_ID="your-chat-id"
```

Never commit a bot token, wallet seed phrase, or private key.

## What an alert means

An alert only means that a configured market-data threshold was crossed. It is **not a buy/sell signal** and should not be treated as a guarantee about future price movement.

## Data source

Market data comes from the DEX Screener public API. Availability, rate limits, and returned fields are controlled by DEX Screener.

## Roadmap

- Token-address watchlists
- Local CSV/JSON snapshots
- More configurable alert conditions
- Basic tests and CI
- Additional notification backends

## Contributing

Issues and pull requests are welcome. Please avoid committing API tokens, private keys, seed phrases, or other secrets.

## License

MIT
