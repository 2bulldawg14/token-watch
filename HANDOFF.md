# Token Watch: handoff notes for Claude

Read this first when you pick this project up in a new conversation. It describes the project, what's already set up, how the code is organized, and how to ship changes safely.

## What this is

A personal crypto signal bot for David. Every 15 minutes it:

- checks a watchlist of coins
- scans for new candidates
- screens each one for scams
- scores buy and sell setups
- tracks wallets he follows
- sends Telegram alerts
- publishes a phone dashboard

It also keeps an honest track record of every buy call and learns from its losing calls. It's not financial advice, and the code and dashboard say so.

## Where everything lives (already set up)

| Thing | Where |
|---|---|
| Code (public repo) | https://github.com/2bulldawg14/token-watch |
| Dashboard (GitHub Pages, from `/docs`) | https://2bulldawg14.github.io/token-watch/ |
| Runs | GitHub Actions: `.github/workflows/token-watch.yml` every 15 min, `add-coin.yml` (the dashboard's Add form) and `test-telegram.yml` (manual) |
| Alerts and commands | David's Telegram bot. Its username is looked up at runtime with `getMe`. |
| Secrets (repo Settings → Secrets → Actions) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COINGECKO_API_KEY` (Demo), `HELIUS_API_KEY`, `ETHERSCAN_API_KEY`. `CRYPTOPANIC_API_KEY` and `WHALE_ALERT_API_KEY` are supported but not set, because both are paid. |
| User settings | `config.json` (weights, indicators, discovery, alerts, `wallet_tracking`, `learning`) and `watchlist.txt` (one ticker per line, `*` = starred) |
| State the bot writes (pushed when the dashboard publishes) | `data/` (`calls.json`, `sells.json`, `coins.json` (the directory: ticker → CoinGecko ID, name, contract, first seen), `state.json`, `cache.json`, `export.json`, `wallet_trades.json`, `learning.json`, `dashboard.html`) and `docs/` (`index.html` and the PWA files) |

Telegram changes made with `/add`, `/star`, `/follow` and similar are stored in `data/state.json` under `_prefs`, not in `config.json`.

## How to ship a change

**Never paste secrets into files.** The repo is public. Keys are only read from environment variables, and the workflow passes GitHub Secrets into them.

1. Clone the public repo into the workspace (no auth needed to read).
2. Edit `token_watch.py`. It's a single file that uses only the standard library.
3. Test offline with `python3 token_watch.py --demo`. That writes `data-demo/dashboard.html` using fake data.
4. Screenshot the dashboard with Playwright at 390×844, in both dark and light mode. Check for JS `pageerror`s before shipping.
5. Upload through the browser, since the cloud workspace can't push:
   - open `https://github.com/2bulldawg14/token-watch/upload/main` (or `/upload/main/.github/workflows` for workflow files)
   - use `file_upload` on the hidden file input
   - type a commit message and click **Commit changes**
6. Verify by re-cloning and running `cmp` against the uploaded file.
7. Changes go live on the next scheduled run. Each run checks the coins, stays on about 12 minutes answering Telegram, then commits `data/` and `docs/`. You can also start a run with **Actions → token-watch → Run workflow**.

**Workflow action versions:** `checkout@v5`, `setup-python@v6`, `cache@v5`. These run on Node 24, which removed the Node 20 deprecation warning.

**Things David must do himself:** create accounts, paste API keys, and message the bot. Claude can open the right pages and fill in the secret *name*.

## Code map (`token_watch.py`)

**Data sources:**
- Binance (klines and depth). The `data-api.binance.vision` host works from US servers.
- CoinGecko (search, prices, coin details, markets and trending), with the Demo key sent as `x-cg-demo-api-key`.
- DefiLlama (TVL, stablecoin supply).
- alternative.me (Fear & Greed).
- Polymarket gamma API (betting odds).
- GoPlus (contract security, EVM and Solana).
- DexScreener (`/tokens/v1/{chain}/{addrs}`).
- Helius, for Solana wallets: `getSignaturesForAddress` costs 1 credit, and decoding with `/v0/transactions` costs 100 credits. It's budgeted under the free 1M credits a month.
- Etherscan V2 (Ethereum, Arbitrum and Polygon on the free tier).
- Blockscout, for Base (free, no key).
- Binance and Bybit futures return 403 from GitHub's US runners. This is expected, and that signal group is skipped.

**Core functions:**
- `zones()` builds buy zone 1 and the sell zone:
  - Buy zone 1 is the highest support below a ceiling, where support is the 30/90/180-day lows or the 50- or 200-day average. The zone runs from support − 0.25·ATR to support + 0.75·ATR.
  - The sell zone is the 90-day high ± ATR.
- `volume_nodes()` builds a volume-by-price profile over 180 days.
- `trade_plan()` adds buy zone 2 (deeper support), `strong` flags where heavy volume sits at support, a stop-loss and a take-profit.
- `analyse()` scores these signal groups: technical, flows, derivatives, macro, news, markets, fundamental and wallets. Each group is 0–100.
  - The groups are combined into one weighted score using `DEFAULT_W`, which `config.weights` overrides and learning multiplies.
  - The learned adjustment is then applied.
  - **Guardrails:** the signal is capped at HOLD if the price is in the sell zone, RSI is above 70, or a learned rule blocks it. It's forced to AVOID if the scam risk is HIGH.
- `is_buy()` decides what counts as a buy setup: a buy signal, or being inside zone 1 or zone 2 without being capped.

**Learning (`learn()`):**
- Every buy call stores a `feat` snapshot.
- Calls are graded after 7 days as a win (> +5%) or a loss (< −5%, or a drawdown worse than −15% while still negative).
- Losses get a post-mortem. A warning sign that lost money in 5 or more calls with a win rate of 35% or less becomes a penalty rule.
- Overlapping rules are dampened, and the total penalty is capped at −15.
- A rule blocks buy calls once it has 8 or more examples and a win rate of 25% or less.
- Weight multipliers (0.6–1.4) come from the correlation between each group's score and the 7-day return, after at least 10 calls.

**Followed wallets:**
- `sync_wallets()` pulls trades and filters dust and spam: under $500, or under $25K liquidity.
- The first sync is quiet (no alerts).
- Alerts go out for new buys. It's a 🔥 cluster buy when 2 or more wallets buy the same token within 48 hours.
- Each wallet gets a record, and its weight ranges from 0.5 to 1.6.
- `wallet_view()` feeds the "wallets" score group.

**Trades (buy calls → sell signals):**
- `sell_check()` logs a sell signal (to `sells.json`) when the signal turns TRIM, SELL or AVOID, the price enters the sell zone, or it falls below the stop.
- It fires on the transition, not while the condition simply stays true, and never on the first time a coin is seen.
- A sell closes that coin's open buy call (`open_call()`), and a Telegram "📉 SELL SIGNAL" reports the trade result.
- Coins with an open call that aren't on the watchlist (scanner finds) keep being checked as `source="tracked"` for up to 45 days.
- Dashboard track record: one card per coin (BUY/SELL/OPEN rows with date and time), total profit if $X went into every buy call (the amount is editable), and summary rows for your coins vs scanner finds.
- The top KPIs use trade returns: exit at the sell price, otherwise the current price.

**Instant updates:**
- While the bot is listening, `/add`, `/remove`, `/star` and `/unstar` (including from the website buttons) run `add_now()`, which checks the coin immediately and replies in Telegram.
- `publish_now()` then rebuilds the dashboard and git-pushes `data/` and `docs/` from inside the run, using the checkout's token. Pages shows it in about 1–2 minutes.
- Commands sent between runs wait for the next run to start.

**Adding coins from the dashboard:**
- On a phone, the Add box goes through Telegram with a deep link.
- On a computer it opens `.github/workflows/add-coin.yml`, a workflow_dispatch form with inputs `tickers` and `star`. That runs `token_watch.py --once --add "<tickers>" [--star]`, which appends to `watchlist.txt` and runs a full check.
- The form shares the `token-watch` concurrency group with `cancel-in-progress: true`, so it takes over from a running check straight away.
- Every run now publishes the dashboard as soon as its checks finish (`publish_now()` at the end of `run_once`), not after the 12-minute listening window.
- The watchlist is de-duplicated by real ticker, and `export` de-duplicates by CoinGecko id.

**Working memory between runs:**
- `save_snapshot()` copies the state files into `.hist-cache/data`, which the workflow's cache steps carry to the next run.
- `restore_snapshot()` uses that copy if it's newer (by `state._saved_t`) than the one in git.
- Each run pushes to git once, when the dashboard publishes, plus once per instant add. The final workflow step only pushes if `docs/` or `watchlist.txt` weren't already published.
- This keeps GitHub Pages under its soft limit of about 10 builds an hour.

**Signal guardrails:** a buy signal is capped at HOLD when:
- the price is in the sell zone
- RSI is above 70
- the price is more than 15% (or 2×ATR) above buy zone 1 (unless it's inside zone 2)
- a learned rule blocks it

**Formatting:**
- `px()` formats prices ($84,940 · $8.185 · $0.09397, never scientific notation).
- `usd()` shortens dollar amounts ($9.2M).
- Always use them in Telegram text.

**Trade grading (learning from real results):**
- `track_trades()` pairs each buy call with the first sell after it. It stores `exit` and `ret` (realized %), `held_days`, and `tmax`/`tmin` while the trade is open.
- Seven days after the sell it stores `after_sell` and `sell_verdict` (sold early / good sell / fine).
- `grade()`: a closed trade is a win above +3% and a loss below −3%. A trade still open after 14 days is graded at today's price.
- Rules and weight tuning use `gret`.
- Sell kinds are zone, signal, stop and target. If zone or signal sells are "sold early" at least half the time (5 or more reviewed, avg move after the sell above +8%), they go into `LEARN.sell_soft`. After that, a zone sell also needs RSI above 70, and a signal sell needs SELL, not TRIM.
- A trade also closes when price hits the take-profit `target` from the plan recorded with the call.
- Telegram sends the trade result at close, a 7-day "SELL REVIEW", and post-mortems that mention gains given back.
- The dashboard trade rows show When / Signal / Price · amount ($X → N coins, and N coins → $Y) / Gain/loss.

**Instant adds:**
- Form adds (`_form_added`) and Telegram adds made before listening starts are analyzed first, then merged into the live page with `quick_publish()`, which edits the `docs/index.html` data and pushes.
- The page shows a "⏳ analysis in progress" card (kept in localStorage) and polls every 20 seconds until the coin appears.

**Tickers vs names:**
- `resolve_id()` matches the ticker first, then falls back to the coin's name or id (CHAINLINK→LINK, CANTON→CC, AKASH→AKT).
- `fix_names()` rewrites names David added by name to real tickers and tells him on Telegram.
- Failed lookups are retried daily.

**Other modules:**
- **Telegram:** `handle_commands()`. Dashboard buttons deep-link as `t.me/<bot>?start=ADD_X`, `STAR_X`, `UNSTAR_X`, `REMOVE_X`, `CHECK_X` and `FOLLOW_<addr>`.
- **Dashboard:** the `DASH_HTML` template near the end of the file, filled by `write_dashboard()`. It has inline JS, SVG charts (price, 50/200-day averages, Bollinger bands, zones, stop and target, volume nodes, RSI, MACD), tabs, an Add-coin box, a Follow box, a wallets leaderboard, a track record split into "Your coins" and "Scanner found", and a learning section.
- **Links:** `token_links()` for the contract (preferring Solana, then ETH, Base, BSC, Arbitrum), CoinGecko and DexScreener. These appear on every buy suggestion and every call.

## Telegram commands

| Command | What it does |
|---|---|
| `/check [TICKER]`, or just send `TAO` / `$tao` | Fresh assessment |
| `/add` · `/remove` · `/star` · `/unstar` TICKER | Manage the watchlist |
| `/follow <address> <nickname>` · `/unfollow` · `/wallets` · `/wallets on\|off` | Followed wallets |
| `/record` · `/lessons` · `/status` | Track record, what the model learned, settings |
| `/discovered on\|off` · `/watchlist on\|off` · `/report on\|off` · `/pause` · `/resume` | Alert settings |

## David's preferences so far

- Explain things in plain English and give step-by-step clicks. He isn't a developer.
- He wants CoinGecko and DexScreener links, the ticker and the contract on every buy suggestion.
- Keep the check interval at 15 minutes. At that rate the CoinGecko Demo key uses roughly 4–4.5K of its 10K monthly calls.
- He prefers free data sources. CryptoPanic's API is paid now ($50/week and up), so it isn't used.
- Starred coins always alert him. Scanner finds are muted on Telegram by default.

## Ideas not built yet

- Free news from RSS feeds (CoinDesk, Cointelegraph) to replace CryptoPanic.
- A native iOS app. The PWA is the current approach.
