# Token Watch: handoff notes for Claude

Read this first when you pick this project up in a new conversation. It describes the project, what's already set up, how the code is organized, and how to ship changes safely.

## What this is

A personal signal bot for David, covering **crypto and US stocks**. Every 15 minutes it:

- checks a watchlist of coins and stocks
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
| Runs | GitHub Actions: `.github/workflows/token-watch.yml` every 15 min, `add-coin.yml` (the dashboard's Add form), `delete-trade.yml` (its Delete button), `refresh.yml` (its Refresh button) and `test-telegram.yml` (manual) |
| Alerts and commands | David's Telegram bot. Its username is looked up at runtime with `getMe`. |
| Secrets (repo Settings → Secrets → Actions) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COINGECKO_API_KEY` (Demo), `HELIUS_API_KEY`, `ETHERSCAN_API_KEY`, `FINNHUB_API_KEY`. `CRYPTOPANIC_API_KEY` and `WHALE_ALERT_API_KEY` are supported but not set, because both are paid. |
| User settings | `config.json` (weights, indicators, discovery, alerts, `wallet_tracking`, `learning`) and `watchlist.txt` (one ticker per line, `*` = starred) |
| State the bot writes (pushed when the dashboard publishes) | `data/` (`calls.json`, `sells.json`, `coins.json` (the directory: ticker → CoinGecko ID, name, contract, first seen), `state.json`, `cache.json`, `export.json`, `wallet_trades.json`, `learning.json`, `dashboard.html`) and `docs/` (`index.html` and the PWA files) |

Telegram changes made with `/add`, `/star`, `/follow` and similar are stored in `data/state.json` under `_prefs`, not in `config.json`.

## How to ship a change

**Never paste secrets into files.** The repo is public. Keys are only read from environment variables, and the workflow passes GitHub Secrets into them.

1. Clone the public repo into the workspace (no auth needed to read).
2. Edit `token_watch.py`. It's a single file that uses only the standard library.
3. Test offline with `python3 token_watch.py --demo`. That writes `data-demo/dashboard.html` using fake data.
4. Screenshot the dashboard with Playwright at 390×844, in both dark and light mode. Check for JS `pageerror`s before shipping. `--demo` includes a fake stock (AAPL) and fake people (David, Ian), so the stock and person tabs are exercised offline.
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

**Track record controls:**
- The page has filters for coin, date range and status. Hide/Unhide is kept per device in localStorage (`tw_hidden`).
- Delete is permanent. On a phone it goes through a Telegram deep link (`DELTRADE_<id>`, then `/deletetrade <id>`). On a computer it uses the `delete-trade.yml` form, which runs `--delete-trades`.
- A trade ID is the buy call's unix time `t`.
- Sell signals are only logged when they close an open buy call. This stopped the noisy repeats from coins hovering at their sell-zone edge.
- Top stats show buy setups, calls in profit, average call return, and open calls now (average live P/L of open trades), for both starred coins and scanner finds.

**Track record drill-down:**
- The two "closed trades won" / "open trades" tiles in each top summary row are tappable (`.tapk`): they set the status filter and scroll to the list.
- Each trade renders as its own `<details class=trade>` block — a one-line summary (WIN/LOSS/OPEN, dates, result) that expands to the full BUY/SELL detail with Hide/Delete.
- Coin logos are transparent-background circular badges (`.logo.img`), with a muted-letter fallback under the image (`onerror` removes the img). No white box.

**Reorder / swipe / refresh (dashboard):**
- Token cards: press-and-hold (~420ms) then drag to reorder. Order saved per device in `tw_order`; `render()` sorts by it first, default sort for the rest.
- Trade rows: swipe left on the summary to reveal Hide / Delete (`.trslide` translateX; `.tractions` behind). In-panel Hide/Delete buttons remain too.
- "Found by scanner" tab = `source != "watchlist"` (includes `tracked` coins that have an open call).
- ↻ Refresh button (header): re-fetches `index.html`; reloads if newer, otherwise shows a toast and reveals an optional `#forcechk` link to `refresh.yml`. It never navigates away on its own.
- Each trade row has always-visible Hide / Delete buttons (`.trbtn`); swipe-left is a bonus, not the only way.
- Logos sit on a fixed dark circle in BOTH themes — several CoinGecko logos are white symbols (e.g. `xrp-symbol-white-128.png`, Litecoin) and were invisible on the light theme without it.
- The green "buy" ring and the "buy setups" counts use `is_buy` = signal is ACCUMULATE/STRONG BUY ZONE. A coin merely sitting in its buy zone gets an `in_zone_only` flag and a small "in buy zone" badge instead.
- `run_once` ends with a self-check that pushes anomalies (zero prices, buy signal while overbought) into `ERRORS` so they reach Telegram.

**Error alerts:**
- Critical `try_get` failures (live prices, logos, market backdrop) and failed publishes are collected in `ERRORS` and sent to Telegram at the end of a run, at most once an hour per error type (`report_errors`).
- Each workflow has an `if: failure()` step that sends a Telegram message with the run link.
- Coin logos come from CoinGecko `/coins/markets`, are cached as `logo:<id>`, and are shown with a letter fallback.

**Tickers vs names:**
- `resolve_id()` matches the ticker first, then falls back to the coin's name or id (CHAINLINK→LINK, CANTON→CC, AKASH→AKT).
- `fix_names()` rewrites names David added by name to real tickers and tells him on Telegram.
- Failed lookups are retried daily.

**Other modules:**
- **Telegram:** `handle_commands()`. Dashboard buttons deep-link as `t.me/<bot>?start=ADD_X`, `STAR_X`, `UNSTAR_X`, `REMOVE_X`, `CHECK_X` and `FOLLOW_<addr>`.
- **Dashboard:** the `DASH_HTML` template near the end of the file, filled by `write_dashboard()`. It has inline JS, SVG charts (price, 50/200-day averages, Bollinger bands, zones, stop and target, volume nodes, RSI, MACD), tabs, an Add-coin box, a Follow box, a wallets leaderboard, a track record split into "Your coins" and "Scanner found", and a learning section.
- **Links:** `token_links()` for the contract (preferring Solana, then ETH, Base, BSC, Arbitrum), CoinGecko and DexScreener. These appear on every buy suggestion and every call.

## Stocks

Stocks live alongside coins in the same watchlist, scoring and track record.

- **Data:** daily OHLCV from **Stooq** (free, no key, `stooq_daily`); live quote, company profile, metrics, earnings date and news from **Finnhub** (`FINNHUB_API_KEY`, free tier, 60 calls/min). Finnhub's free plan does **not** include `/stock/candle`, which is why Stooq supplies the history.
- **Which is it?** `norm()` sets `tok["kind"]` to `crypto` or `stock`. A ticker is a stock if it's in `PREFS["stocks"]` (set by `/stock`, the dashboard's Stock radio, or `--kind stock`) or `REG[sym]["kind"] == "stock"`. `classify()` looks a new ticker up on both CoinGecko and Finnhub; when both match, the bot asks in Telegram ("Reply /stock AAPL or /coin AAPL") and adds nothing until you answer.
- **Pipeline:** `check_token()` hands stocks to `check_stock()`, which uses `stock_data()`, `stock_quality()` (the stand-in for the scam screen: small cap, unprofitable, high debt, shrinking revenue, very high P/E, earnings within 7 days) and `stock_news()`. Crypto-only groups (derivatives, TVL, wallets, order book, betting markets) are simply absent, so the weights redistribute.
- **Differences inside `analyse()`** (all keyed off `ex["_kind"] == "stock"`): relative strength is measured against the S&P 500 instead of Bitcoin; `parts["macro"]` uses `MACRO["stock_score"]` (S&P 500 trend, S&P RSI, VIX — see `stock_macro()`); a HIGH quality score caps the signal at HOLD instead of calling it a scam; the `fundamental` group comes from `stock_grade()` (margin, ROE, revenue and earnings growth, P/E, debt/equity → A–F).
- **Market hours:** `market_open()` (weekday 9:30am–4pm Eastern, with a real DST rule; holidays aren't checked). Stock alerts are held back while the market is closed unless a new buy call was stamped.
- **Links:** Yahoo Finance and TradingView replace CoinGecko and DexScreener.
- **On the dashboard:** a `stock` badge, 📈 Stocks / 🪙 Crypto tabs, a "Your stocks" KPI row and track-record row, a Company numbers panel (P/E, margin, growth, ROE, debt/equity, 52-week range, dividend, next earnings) and a US stock backdrop section.

### Insider trading (stocks)

`insider_view(sym)` reads two free Finnhub endpoints and becomes its own score group (`insiders`, weight 0.15; `None` for coins so the other weights absorb it):

- `/stock/insider-transactions` - SEC Form 4 filings over `insider_days` (180). Only open-market buys (`transactionCode` `P`) and sales (`S`) count; option grants and tax withholding are ignored. It totals dollars bought and sold, counts distinct people on each side, and keeps the 12 most recent rows for the dashboard.
- `/stock/insider-sentiment` - Finnhub's monthly MSPR score (-100 to 100), averaged over the last 3 months.
- **Cluster buying** (`insider_cluster`, default 3 different buyers in the last `insider_recent_days` with sales under a quarter of buys) scores 92 and fires its own Telegram alert, deduplicated per set of names in `state[sym]["_ins"]`. The mirror case, three or more sellers and almost no buyers, scores 12.
- `short_view(sym)` calls `/stock/short-interest`, which is **premium on Finnhub**. On the free plan it returns nothing and the `shorts` group stays `None` - the code is there so it switches on by itself if the plan is ever upgraded. Institutional 13F positions are premium too and aren't used.
- The dashboard shows an "Insider trading" panel inside the stock's card (bought vs sold, the reasons, and a table of who traded what) plus a 🔥 insiders buying / insiders selling badge on the card itself.

### Congressional trading (stocks)

`congress_view(sym, industry)` scores trades that members of Congress must disclose under the STOCK Act (group `congress`, weight 0.08).

- **Data, free, no key, tried in order:** the House and Senate Stock Watcher bulk JSON files (`CONGRESS_SRC`), fetched at most once a day into the history cache and indexed by ticker; if neither answers, `congress_ticker()` falls back to bargo.ai's free per-ticker endpoint (optional `congress_api_key`). `_crow()` normalises both shapes, keeping only real purchases and sales; `money_range()` turns "$1,001 - $15,000" into a low/high pair, and the midpoint is what gets totalled.
- **Committee oversight:** `committees()` pulls `committee-membership-current.json` and `committees-current.json` from unitedstates/congress-legislators (free, refreshed weekly). `_cname()` matches people across sources on last name plus first initial. `oversees()` matches the member's committees against the company's Finnhub industry through `COMMITTEE_SECTORS`. A purchase by someone who oversees the industry scores 94 and gets its own Telegram alert; three or more members buying with few sales scores 88.
- **Confidence:** one member trading barely moves the score - the buy/sell ratio is scaled by how many different people traded, so a single sale lands near 40 rather than 0.
- Disclosures are filed up to 45 days after the trade, so this is slow information. The dashboard panel and the alert both say so.

## Distribution list (other people)

- `PREFS["members"]` = `[{chat_id, name, mode, added}]`, where `mode` is `all`, `mine` or `off`. Kept in `data/state.json` under `_prefs`.
- **Owner only** (the chat in `TELEGRAM_CHAT_ID`): `/invite <name>` returns a `t.me/<bot>?start=JOIN_<Name>-<sig>` link, where `sig` is an HMAC of the name keyed by the bot token (`invite_code`/`invite_check`). Nothing is stored, so the link survives lost state, works more than once, and can't be forged without the bot token - an earlier version kept one-time codes in `_prefs`, and they were lost whenever a run failed to commit. The owner tapping their own link gets told to forward it instead; `/people` lists everyone; `/kick <name>` removes someone. Every admin command (`/star`, `/remove`, `/follow`, `/pause`, `/deletetrade`, `/discovered`, …) is refused for anyone else.
- **Members** can add and check tickers, see `/record`, and control their own alerts with `/mine on|off`, `/mute`, `/unmute`, `/leave`.
- `handle_commands()` now reads every chat, not just the owner's. `CUR` holds who sent the message being handled; `reply()` answers them, `tell_owner()` messages David. Unknown chats get a polite refusal unless they present a valid invite code.
- **Attribution:** `PREFS["added_by"]` maps ticker → person. It's written by `/add`, `/stock`, `/coin` and `/star`, copied onto `REG[sym]["by"]`, every buy call, and each dashboard token. The dashboard builds a 👤 tab per person, shows an "Added by" badge and row, and adds a person dropdown to the track-record filters. `send()` uses it to honour a member's `mine` mode.
- System and error messages (`kind="system"`, `owner_only=True`) never go to members.

## Telegram commands

| Command | What it does |
|---|---|
| `/check [TICKER]`, or just send `TAO` / `$tao` | Fresh assessment |
| `/add` · `/remove` · `/star` · `/unstar` TICKER | Manage the watchlist |
| `/stock AAPL` · `/coin LINK` · `/stocks` | Add a stock, add a coin when the ticker means both, list stocks |
| `/invite <name>` · `/people` · `/kick <name>` | The distribution list (owner only) |
| `/mine` · `/mine on\|off` · `/mute` · `/unmute` · `/leave` | A member's own settings |
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

## Run timing (important)

**The schedule is every 20 minutes, not 15, and that is deliberate.** A run costs about 12 minutes of wall clock (2-3 for checkout, Python setup and cache restore, then the script, then saving the cache and publishing). Against a 15-minute cron that left the lane roughly 80% occupied, so runs queued, started 10+ minutes late, had no working time left, and did only the minimum. At 20 minutes the lane has real headroom: runs start on time and get through the whole watchlist. The refresh you actually get went from "a few coins every two hours" to "everything every twenty minutes" by making the schedule *less* frequent.

GitHub keeps at most one run of a workflow waiting in line. If a run is still going when the next two come due, GitHub cancels the running one with *"Canceling since a higher priority waiting request for token-watch exists"* - and nothing gets published. That happened once when the checks plus a 12-minute Telegram window ran past the 15-minute cron.

So every run must finish inside the check interval:

- `listen()` takes the tighter of two ceilings: how long until the next run is due on the 15-minute grid (minus `run_margin_seconds`, 180, for saving the cache and the final publish step) and `run_budget_minutes` (8) from when Python started. Because scheduled runs land on a fixed grid, the clock ceiling is what actually keeps runs from overlapping - a budget measured from the start of the run misses the minutes GitHub spends on checkout, Python setup and cache restore before the script even begins, which is how a run once came to 14m02s against a 13-minute cap.
- `listen_minutes` (8) is the most it will ever listen for.
- `time_left()` is the single source of truth, and **the checks obey it too**: when under `publish_reserve_minutes` (1.5) remains, the run stops checking coins and goes straight to publishing, leaving the rest for the next run. `state["_wl_cursor"]` remembers where it stopped so the tail of the watchlist isn't perpetually skipped. Discovery only runs with `discovery_reserve_minutes` (3) to spare. That means **every run publishes**, however slow the data sources are that day.
- `time_left()` returns 999 off GitHub Actions, so local and `--demo` runs are never cut short.
- **A partial run must not empty the page.** `export()` merges `prev_tokens()` (parsed back out of the published `docs/index.html`) into its results: coins refreshed this run replace their old entry, coins there wasn't time for keep their last reading marked `stale`, and coins in `PREFS["removed"]` drop out. `min_coins_per_run` (4) also guarantees some progress even when a run starts with almost no time left - without it, a run that queued for 13 minutes once published a dashboard with zero coins on it.
- `timeout-minutes: 13` on each workflow, so a stuck run dies quickly instead of blocking the lane.
- The dashboard is published *before* the listening window, so a cancelled run still leaves the page updated.

If the dashboard stops updating, check **Actions → token-watch** for runs marked *cancelled* - that's this failure, not a crash, and it won't show up as a failed run or trigger the failure alert.
