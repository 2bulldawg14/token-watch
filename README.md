# Token Watch

Checks every token on your list, finds new ones on its own, screens each for scam
red flags, and messages you on Telegram when there's a buy or sell setup. Every
buy call is stamped with its date and price so you can see how it turned out.

## Free live feed in 4 steps (runs in the cloud, your computer can be off)

**1. Make a Telegram bot (3 min, free)**
- Install Telegram and sign in.
- Search **@BotFather** (blue check mark), tap Start, send `/newbot`.
  Give it a name (e.g. "My Token Watch") and a username ending in `bot` (e.g. `jake_tokenwatch_bot`).
  BotFather replies with a **token** like `8123456789:AAH...`. Treat it like a password.
- Tap the link BotFather gives you to open your bot, and tap **Start**. (Bots can't message you until you do.)
- Get your **chat ID**: search **@userinfobot**, tap Start, and copy the number it shows as your Id.
  (Or open `https://api.telegram.org/bot<YOUR TOKEN>/getUpdates` and copy the number after `"chat":{"id":`.)

**2. Put this folder on GitHub (3 min, free)**
- Make an account at github.com, click **New repository**, name it `token-watch`, and choose **Public**.
  Public repos get unlimited free runs, which checking every 15 minutes needs (about 3,000-6,000 minutes a month).
  Your Telegram details stay hidden as secrets; your watchlist and track record would be visible.
  If you want it private, change the schedule to every 30-60 minutes to stay inside the free 2,000 minutes.
- Click **uploading an existing file** and drag in everything from this folder,
  including the `.github` folder. (If your computer hides it, turn on "show hidden files".)

**3. Add your Telegram details as secrets**
- In the repo: **Settings > Secrets and variables > Actions > New repository secret**.
- Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**4. Turn it on**
- Go to the **Actions** tab and enable workflows.
- Open **test-telegram** and click **Run workflow**. You should get "Token Watch is connected" on Telegram within a minute.
- Open **token-watch** and click **Run workflow**. After that it runs every 15 minutes by itself and
  messages you on Telegram only when something happens (new buy call, buy zone, RSI extreme, MACD cross,
  signal change, your price levels, flagged news, scam risk), plus a weekly track-record summary.

## Choosing what alerts you
- **Starred tokens always alert you.** Star with `*` in `watchlist.txt` (`*INJ`), in `starred` in `config.json`, or by messaging your bot `/star INJ`.
- **Discovered coins are muted on Telegram by default.** They still show on the dashboard and in exports. Turn on with `/discovered on` or `alerts.discovered` in `config.json`.
- **Other watchlist coins** alert by default; `/watchlist off` mutes them while starred ones keep coming.

Message your bot these any time (it reads them at the next check, within ~15 minutes):
`/star INJ`, `/unstar INJ`, `/add TAO`, `/remove ONDO`, `/discovered on|off`, `/watchlist on|off`,
`/report on|off`, `/pause`, `/resume`, `/status`, `/help`

## Adding tokens
Edit `watchlist.txt` on GitHub (pencil icon) and add one ticker per line. No limit, but the
free CoinGecko tier is slow (about 10 lookups a minute), so 30-40 tokens per run is comfortable.
For more, get a free Demo key at coingecko.com/en/api and paste it in `config.json`.

For tokens you've researched, add them to `watchlist` in `config.json` instead so you can record
`team_doxxed` (true/false), `audited`, your Token Grader letter, and price alerts.

## Ask for an assessment any time (from Telegram)
- `/check` gives a fresh read on all your starred coins.
- `/check TAO`, or just send `TAO` (capital letters) or `$tao`, assesses any coin, even one you don't track.
  You get the signal, buy/sell zones, scam risk, betting odds and the reasons. It won't stamp a buy call.
- After each scheduled check the script keeps listening for about 12 minutes (`listen_minutes`),
  so replies usually arrive within a minute. This uses more GitHub minutes, which is free on a public repo.

## Put it on your iPhone/iPad home screen (free)
1. In your repo: **Settings > Pages**. Under "Build and deployment" choose **Deploy from a branch**,
   branch **main**, folder **/docs**, then Save.
2. After the next run, your dashboard lives at `https://<your-username>.github.io/<repo-name>/`.
   It updates every 15 minutes. (On a public repo anyone with the link can view it.)
3. Open that link in **Safari**, tap **Share > Add to Home Screen**. It opens full-screen with its own icon, like an app.

## Timing notes
- GitHub runs scheduled jobs on a best-effort basis; at busy times a 15-minute check can arrive a few minutes late or occasionally be skipped.
- Chart indicators use daily candles with the live price, so every check sees the latest price. Discovery still runs every 12 hours.
- `export.json` and the dashboard refresh every 4 hours (`export_every_minutes`) to keep the repo small.
- To change the schedule, edit `cron` in `.github/workflows/token-watch.yml` (`*/30 * * * *` = every 30 minutes).

## Seeing results
- **Phone:** Telegram alerts, plus a weekly track-record summary.
- **Dashboard:** open `data/dashboard.html` (download it from GitHub) to see all signals and every past call.
- **Token Grader app:** open `data/export.json`, copy everything, and paste it into **Import** in the app.
  That updates prices, charts, scam checks and your track record there.

## Indicators (turn any off in `config.json` > `indicators`, re-weight in `weights`)
| Group (default weight) | What it looks at | Source |
|---|---|---|
| Technical (30%) | RSI, MACD, 50/200-day trend, buy/sell zones, Bollinger bands + squeeze, OBV volume divergence, RSI divergence, strength vs Bitcoin | Binance / CoinGecko |
| Flows (15%) | Buy vs sell pressure, order book, volume spikes, whale transfers*, TVL trend | Binance, DefiLlama, Whale Alert* |
| Derivatives (10%) | Funding rate (crowded longs/shorts), open interest vs price | Binance futures, Bybit backup |
| Market backdrop (10%) | Bitcoin bull/bear regime, Fear & Greed (contrarian), stablecoin money flowing in/out | Binance, alternative.me, DefiLlama |
| News (5%) | Headlines and votes* | CryptoPanic* |
| Betting markets (5%) | Polymarket odds | Polymarket |
| Fundamentals (25%) | Your Token Grader letter | You |

*needs a key. Groups with no data are skipped and the rest re-weighted. Futures APIs often block US
servers; if funding/open interest show "[skip]", that group is simply left out.

## Betting markets (Polymarket, free, no key)
Every hour the script searches Polymarket for live markets about each token: price targets
("Will Solana reach $300 by Dec 31?"), short-term "up or down" markets, and catalysts like ETF
approvals or hacks. It turns the odds into a score (10% of the signal), shows the top markets in
alerts, estimates the price the market gives ~50% odds of touching, and alerts you when any
market's odds move 15+ points. Most small tokens have no markets, so Bitcoin's odds are used as a
half-weight backdrop. Markets under $2,000 of liquidity are ignored because thin markets are easy to push around.
"Reach $X" odds are odds of touching that price at any point, not of closing there.

## What the scam screen checks (automatically)
Honeypots and sell taxes, owner powers (mint, blacklist, pause, change balances), unverified
contract code, top-10 wallet concentration, unlocked DEX liquidity (via GoPlus, free), missing
website or code, no recent development, very new projects, tiny market caps, suspicious volume,
and heavy future dilution.

**It can't verify a team.** Anonymous teams stay flagged until you check founders' real names,
LinkedIn history and past projects, then set `"team_doxxed": true` or `false` in `config.json`.
Discovered tokens always say "verify the team yourself".

## Other options
- Run on your own computer: install Python, then `python token_watch.py` (keeps running) or `--once`.
- Test offline first: `python token_watch.py --demo`.
- News alerts: free key from cryptopanic.com/developers/api into `cryptopanic_api_key`.
- Whale transfers: whale-alert.io key (paid) into `whale_alert_api_key`.

Signals are a checklist, not an autopilot. Indicators fail often in thin, news-driven markets. Not financial advice.
