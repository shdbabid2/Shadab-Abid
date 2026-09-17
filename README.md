# Solana Meme Bot v0.4

A conservative Solana new-token scanner plus **paper-trading-first** execution engine.

## Safety defaults

The bot ships in **paper mode**. It does not become a live trader merely because a wallet key exists.

A live buy requires all of the following:

- setup score **99 or 100**
- risk level exactly `LOW`
- risk score exactly `0`
- evidence coverage exactly `100%`
- mint authority absent
- freeze authority absent
- exact-address market data
- Jupiter buy quote available
- Jupiter sell quote available for the expected token output
- buy and sell quote price impact below the configured limit
- immediate round-trip quote at least **$4.70** from a **$5.00** entry
- daily limits not reached
- no other open position

The score is a rule score, **not** a probability of profit or a 99% guarantee.

## Position limits

Default position:

- Entry: **$5.00 USDC**
- Hard-stop trigger: **$4.10 quoted exit value**
- Planned loss at trigger: **$0.90 / 18%**, before slippage and fees
- One position at a time
- Maximum 3 trades/day
- Stop after 3 consecutive losses
- Daily realized loss limit: **$2.70**
- No averaging down
- Maximum hold: 60 minutes
- Take-profit is disabled by default

A $4.10 stop **cannot guarantee a $4.10 execution**. Fast meme-coin crashes, missing liquidity, transaction failure, slippage, RPC failure, or network congestion can produce a worse exit.

## Files

- `monitor.py` — exact-mint scanner and risk/setup scoring
- `trade_engine.py` — paper/live orchestration and risk limits
- `jupiter_live.mjs` — Jupiter Swap V2 signer/executor
- `config.example.json` — configuration
- `test_bot.py` — offline tests
- `.github/workflows/tests.yml` — automated tests
- `.github/workflows/paper-bot.yml` — manual paper-mode check only

## Data and routing

Scanner data:
- DEX Screener exact Solana token-pair endpoint
- Solana RPC `getAccountInfo`, `getTokenSupply`, and `getTokenLargestAccounts`

Trading quotes/execution:
- Jupiter Swap V2 `/order` and `/execute`

## Setup

Copy the configuration:

```bash
cp config.example.json config.json
```

For Jupiter quote checks, set:

```bash
export JUPITER_API_KEY="your-key"
```

Test one mint:

```bash
python monitor.py --mint TOKEN_MINT
```

Discover recent candidates:

```bash
python monitor.py --discover
```

Run one paper-trading cycle:

```bash
python trade_engine.py --once
```

Run paper mode continuously:

```bash
python trade_engine.py
```

## Live trading

**Do paper testing first.** The recommended activation rule is at least 20–50 logged paper trades before considering live mode.

For live execution install Node dependencies:

```bash
npm install
```

Use a **new, dedicated, low-balance Solana wallet**. Never use a main wallet.

Required environment variables:

```bash
export JUPITER_API_KEY="..."
export BS58_PRIVATE_KEY="..."
export TRADING_MODE="live"
export ENABLE_LIVE_TRADING="YES"
export I_UNDERSTAND_LIVE_TRADING="YES"
```

Optional:

```bash
export LIVE_SLIPPAGE_BPS="100"
```

Do not put private keys in `config.json`, GitHub source code, screenshots, or chat messages.

## GitHub Actions

The included GitHub Actions workflow runs only **paper mode / tests**. It intentionally does not store or use a private wallet key.

For a real stop-loss monitor, live mode should run continuously on a reliable machine/server. GitHub scheduled workflows can be delayed and are not appropriate for a time-sensitive meme-coin stop.

## Telegram

Set:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

Then set `"telegram_enabled": true` under `"trading"` in `config.json`.

## Accuracy

This project intentionally never prints "guaranteed safe" or "99.9% scam detection." On-chain checks can identify real warning signs, but they cannot know a developer's future actions or guarantee liquidity will remain available.

## Tests

```bash
python -m unittest -v test_bot.py
```

## License

MIT
