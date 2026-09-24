#!/usr/bin/env python3
"""
Token Watch v2 - tracks a watchlist, finds new candidates, screens for scams,
alerts on buy/sell setups, and keeps an honest track record of every buy call.

  python token_watch.py --demo     offline test with fake data
  python token_watch.py --once     one full check, then exit (use this with GitHub Actions / cron)
  python token_watch.py            keep running, checks every N minutes
  python token_watch.py --report   print the track record of past buy calls

Outputs go to ./data/: dashboard.html (open in a browser), export.json (import into
Token Grader), calls.json (track record), state.json and cache.json.
Standard library only. Not financial advice.
"""
import argparse, json, math, os, random, subprocess, sys, time, urllib.parse, urllib.request, html
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
UA = {"User-Agent": "token-watch/2.0", "Accept": "application/json"}
DAY = 86400
BUY_SIGNALS = ("STRONG BUY ZONE", "ACCUMULATE")
STABLES = {"usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usds", "pyusd", "busd", "usdd", "frax", "eurc", "wbtc", "weth", "steth", "wsteth"}
DEMO = False

# ================================================================ helpers
def now(): return time.time()
def iso(t=None): return datetime.fromtimestamp(t or now(), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
def load(name, default):
    try:
        with open(os.path.join(DATA, name)) as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default
STATE_FILES = ("state.json", "cache.json", "calls.json", "sells.json", "coins.json", "learning.json", "wallet_trades.json")
SNAP = os.path.join(HERE, ".hist-cache", "data")     # restored/saved by the workflow's cache steps
def restore_snapshot():
    """Use the bot's working memory from the previous run's cache if it's newer than the copy in git."""
    try:
        snap = json.load(open(os.path.join(SNAP, "state.json"))).get("_saved_t", 0)
        cur = (load("state.json", {}) or {}).get("_saved_t", 0)
        if snap > cur:
            for n in STATE_FILES:
                src = os.path.join(SNAP, n)
                if os.path.exists(src): os.makedirs(DATA, exist_ok=True); open(os.path.join(DATA, n), "w").write(open(src).read())
            print(f"Restored working memory from the previous run ({int((now() - snap) / 60)} min old).")
    except Exception: pass
def save_snapshot():
    try:
        os.makedirs(SNAP, exist_ok=True)
        for n in STATE_FILES:
            src = os.path.join(DATA, n)
            if os.path.exists(src): open(os.path.join(SNAP, n), "w").write(open(src).read())
    except Exception as e: print(f"  [skip] snapshot: {e}")
def save(name, obj):
    if name == "state.json" and isinstance(obj, dict): obj["_saved_t"] = now()
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".tmp")
    with open(tmp, "w") as f: json.dump(obj, f, indent=1)
    os.replace(tmp, os.path.join(DATA, name))

_last_cg = [0.0]
def get_json(url, headers=None, timeout=20, cg=False):
    if cg:  # stay under CoinGecko's free rate limit
        wait = CFG.get("coingecko_min_seconds", 6 if not CFG.get("coingecko_api_key") else 2.2) - (now() - _last_cg[0])
        if wait > 0: time.sleep(wait)
        _last_cg[0] = now()
        if CFG.get("coingecko_api_key"): headers = {**(headers or {}), "x-cg-demo-api-key": CFG["coingecko_api_key"]}
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2: time.sleep(30 * (attempt + 1)); continue
            raise
ERRORS = []   # problems worth telling you about (sent to Telegram once per hour at most)
CRITICAL = {"live prices", "logos", "market backdrop"}
def try_get(label, fn):
    try: return fn()
    except Exception as e:
        print(f"  [skip] {label}: {e}")
        if label in CRITICAL: ERRORS.append(f"{label}: {str(e)[:120]}")
        return None

def report_errors(state):
    """One Telegram message per hour at most, listing problems that need attention."""
    if not ERRORS: return
    msgs = list(dict.fromkeys(ERRORS)); ERRORS.clear()
    key = "|".join(sorted(m.split(":")[0] for m in msgs))
    last = state.setdefault("_err_alert", {})
    if now() - last.get(key, 0) < 3600: return
    last[key] = now()
    run = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID") else ""
    send("⚠️ Token Watch needs attention:\n" + "\n".join("• " + m for m in msgs[:6])
         + ("\nDetails: " + run if run else "") + "\nI'll keep retrying automatically; tell Claude if this keeps happening.", "system")

CG = "https://api.coingecko.com/api/v3"
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]  # 2nd works from the US

# ================================================================ data sources
def resolve_id(sym, cache):
    """Ticker -> CoinGecko id (largest market cap with that exact ticker)."""
    key = "id:" + sym.lower()
    if cache.get(key): return cache[key]
    if key in cache and now() - cache.get("idt:" + sym.lower(), 0) < DAY: return None     # retry failed lookups daily
    d = get_json(f"{CG}/search?query={urllib.parse.quote(sym)}", cg=True)
    coins = d.get("coins", []); rank = lambda c: c.get("market_cap_rank") or 10**9
    hits = sorted([c for c in coins if c.get("symbol", "").lower() == sym.lower()], key=rank)
    if not hits:   # typed a name instead of a ticker (CHAINLINK, CANTON, AKASH...): match the name/id instead
        s_ = sym.lower().replace(" ", "")
        hits = sorted([c for c in coins if c.get("name", "").lower().replace(" ", "") == s_ or c.get("id", "") == s_], key=rank) or \
               sorted([c for c in coins if (c.get("name", "").lower().startswith(sym.lower()) or c.get("id", "").startswith(s_)) and rank(c) < 1000], key=rank)
        if hits: cache["tick:" + sym.lower()] = (hits[0].get("symbol") or sym).upper()
    cache[key] = hits[0]["id"] if hits else None; cache["idt:" + sym.lower()] = now()
    return cache[key]

def fix_names(cache):
    """Coins added by name (CANTON, CHAINLINK) get switched to their real ticker (CC, LINK), once, with a Telegram note."""
    moved = []
    for lst in ("added", "starred"):
        out = []
        for x in PREFS.get(lst, []):
            real = cache.get("tick:" + x.lower())
            if not real and cache.get("id:" + x.lower()) is None and x.lower() and ("id:" + x.lower()) not in cache:
                try_get("lookup", lambda x=x: resolve_id(x, cache)); real = cache.get("tick:" + x.lower())
            if real and real != x:
                if lst == "added": moved.append(f"{x} → {real}")
                x = real
            if x not in out: out.append(x)
        PREFS[lst] = out
    if moved: reply("Switched coins you added by name to their tickers: " + ", ".join(moved) + ". Tip: use tickers like LINK, UNI, AKT.")

def binance_daily(pair):
    for host in BINANCE_HOSTS:
        try:
            d = get_json(f"{host}/api/v3/klines?symbol={pair}&interval=1d&limit=365")
            return {"close": [float(k[4]) for k in d], "high": [float(k[2]) for k in d], "low": [float(k[3]) for k in d],
                    "volume": [float(k[5]) for k in d], "taker_buy": [float(k[9]) for k in d], "host": host, "src": "Binance"}
        except urllib.error.HTTPError as e:
            if e.code == 400: return None   # pair doesn't exist
        except Exception: pass
    return None

def binance_depth(host, pair, price):
    d = get_json(f"{host}/api/v3/depth?symbol={pair}&limit=1000")
    bids = sum(float(p) * float(q) for p, q in d["bids"] if float(p) >= price * 0.98)
    asks = sum(float(p) * float(q) for p, q in d["asks"] if float(p) <= price * 1.02)
    return {"bid_usd": bids, "ask_usd": asks}

def coingecko_daily(cg_id):
    d = get_json(f"{CG}/coins/{cg_id}/market_chart?vs_currency=usd&days=365&interval=daily", cg=True)
    return {"close": [p[1] for p in d["prices"]], "high": None, "low": None,
            "volume": [v[1] for v in d["total_volumes"]], "taker_buy": None, "src": "CoinGecko"}

def simple_prices(ids):
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        d = get_json(f"{CG}/simple/price?ids={','.join(chunk)}&vs_currencies=usd", cg=True)
        out.update({k: v.get("usd") for k, v in d.items()})
    return out

NEWS_FLAGS = {"partner": 1, "integrat": 1, "etf": 1, "approv": 1, "listing": 1, "lists ": 1, "launch": 1,
              "upgrade": 1, "mainnet": 1, "institution": 1, "buyback": 1, "burn": 1, "acquire": 1,
              "hack": -2, "exploit": -2, "rug": -2, "scam": -2, "delist": -2, "lawsuit": -2, "charges": -2,
              "unlock": -1, "outage": -1, "halt": -1, "reject": -1, "delay": -1, "investigation": -1, "dump": -1}

def cryptopanic_news(symbol):
    base = CFG.get("cryptopanic_url", "https://cryptopanic.com/api/developer/v2/posts/")
    d = get_json(f"{base}?auth_token={CFG['cryptopanic_api_key']}&currencies={symbol}&public=true")
    items = []
    for p in d.get("results", [])[:30]:
        t = p.get("title", ""); low = t.lower(); v = p.get("votes") or {}
        items.append({"id": str(p.get("id") or t), "title": t, "url": p.get("url") or p.get("original_url") or "",
                      "flag": sum(x for k, x in NEWS_FLAGS.items() if k in low),
                      "pos": v.get("positive", 0) + v.get("liked", 0), "neg": v.get("negative", 0) + v.get("disliked", 0) + v.get("toxic", 0)})
    return items

def whale_flows(symbol):
    url = (f"https://api.whale-alert.io/v1/transactions?api_key={CFG['whale_alert_api_key']}"
           f"&min_value={int(CFG.get('whale_min_usd', 500000))}&start={int(now()) - DAY}&currency={symbol.lower()}")
    to_ex = from_ex = 0.0
    for t in get_json(url).get("transactions", []) or []:
        amt = float(t.get("amount_usd", 0))
        if (t.get("to") or {}).get("owner_type") == "exchange": to_ex += amt
        if (t.get("from") or {}).get("owner_type") == "exchange": from_ex += amt
    return {"net_outflow": from_ex - to_ex}

# ================================================================ scam / due-diligence screen
GOPLUS_CHAINS = {"ethereum": "1", "binance-smart-chain": "56", "polygon-pos": "137", "arbitrum-one": "42161",
                 "base": "8453", "optimistic-ethereum": "10", "avalanche": "43114"}

def due_diligence(cg_id, override, cache):
    """Returns {'level','points','flags','checks','facts'}. Cached for 3 days."""
    key = "dd:" + cg_id
    hit = cache.get(key)
    if hit and now() - hit.get("t", 0) < 3 * DAY and not override: return hit["v"]
    flags, checks, pts = [], {}, 0.0
    def flag(p, msg, check=None, ok=None):
        nonlocal pts
        pts += p
        if msg: flags.append((p, msg))
        if check: checks[check] = ok
    c = get_json(f"{CG}/coins/{cg_id}?localization=false&tickers=false&market_data=true&community_data=true&developer_data=true&sparkline=false", cg=True)
    md = c.get("market_data") or {}
    mcap = (md.get("market_cap") or {}).get("usd") or 0
    fdv = (md.get("fully_diluted_valuation") or {}).get("usd") or 0
    vol = (md.get("total_volume") or {}).get("usd") or 0
    links = c.get("links") or {}
    homepage = [h for h in (links.get("homepage") or []) if h]
    github = [g for g in ((links.get("repos_url") or {}).get("github") or []) if g]
    commits = (c.get("developer_data") or {}).get("commit_count_4_weeks")
    genesis = c.get("genesis_date")
    facts = {"name": c.get("name"), "market_cap": mcap, "fdv": fdv, "volume": vol, "website": homepage[:1],
             "github": github[:1], "twitter": links.get("twitter_screen_name"), "genesis": genesis,
             "categories": (c.get("categories") or [])[:4], "platforms": {k: v for k, v in (c.get("platforms") or {}).items() if k and v}}

    # Team: can't be verified automatically. You set it in config after checking.
    team = (override or {}).get("team_doxxed")
    if team is None and cg_id in ("bitcoin", "litecoin", "bitcoin-cash", "monero", "ethereum-classic", "dogecoin"): team = True   # no company/team to verify
    if team is False: flag(2, "Team is anonymous (red flag)", "team", False)
    elif team is None: flag(1, "Team not verified yet - check founders' real names, LinkedIn, past projects", "team", None)
    else: checks["team"] = True
    audit = (override or {}).get("audited")
    if audit is False: flag(1, "No audit from a known firm", "audit", False)
    elif audit: checks["audit"] = True

    if not homepage: flag(2, "No official website listed")
    if not github: flag(1, "No public code repository", "code", False)
    elif commits == 0: flag(1, "No code commits in the last 4 weeks")
    else: checks["code"] = True
    if genesis:
        try:
            age = (datetime.now() - datetime.strptime(genesis, "%Y-%m-%d")).days
            ok = age >= 180; checks["age"] = ok
            if not ok: flag(1, f"Very new project ({age} days old)")
        except ValueError: pass
    if mcap and mcap < CFG.get("min_market_cap_usd", 5e6): flag(1.5, f"Tiny market cap (${mcap/1e6:.1f}M) - easy to manipulate")
    if mcap and vol / mcap > 1.5: flag(1, f"Volume is {vol/mcap:.1f}x market cap - possible wash trading or pump")
    if mcap and fdv and fdv / mcap > 4: flag(1, f"FDV is {fdv/mcap:.1f}x market cap - heavy future dilution")
    if not mcap: flag(1, "No market cap reported")

    # Contract checks via GoPlus (free, no key) on the first supported chain
    evm = [(ch, addr) for ch, addr in facts["platforms"].items() if ch in GOPLUS_CHAINS]
    if evm:
        ch, addr = evm[0]
        g = try_get("contract security", lambda: get_json(
            f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_CHAINS[ch]}?contract_addresses={addr}"))
        r = ((g or {}).get("result") or {}).get(addr.lower()) if g else None
        if r:
            one = lambda k: str(r.get(k, "0")) == "1"
            if one("is_honeypot") or one("cannot_sell_all"): flag(10, "HONEYPOT - holders can't sell", "tax", False)
            try:
                st, bt = float(r.get("sell_tax") or 0), float(r.get("buy_tax") or 0)
                if st > 0.1 or bt > 0.1: flag(4, f"High trading tax (buy {bt*100:.0f}% / sell {st*100:.0f}%)", "tax", False)
                elif "tax" not in checks: checks["tax"] = True
            except ValueError: pass
            if str(r.get("is_open_source", "1")) == "0": flag(2, "Contract source code not verified", "code", False)
            powers = [n for k, n in (("is_mintable", "mint new tokens"), ("owner_change_balance", "change balances"),
                      ("hidden_owner", "hidden owner"), ("can_take_back_ownership", "reclaim ownership"),
                      ("transfer_pausable", "pause transfers"), ("is_blacklisted", "blacklist wallets")) if one(k)]
            if one("owner_change_balance") or one("hidden_owner"): flag(5, "Owner can " + ", ".join(powers), "owner", False)
            elif powers: flag(1.5, "Owner can " + ", ".join(powers), "owner", False)
            else: checks["owner"] = True
            if one("is_proxy"): flag(0.5, "Upgradeable contract (rules can change)")
            try:
                top = sum(float(h.get("percent", 0)) for h in (r.get("holders") or [])[:10] if str(h.get("is_locked")) != "1")
                if top > 0.5: flag(2, f"Top 10 wallets hold {top*100:.0f}% (may include exchanges)", "holders", False)
                elif r.get("holders"): checks["holders"] = True
            except ValueError: pass
            lp = r.get("lp_holders") or []
            if lp:
                locked = sum(float(h.get("percent", 0)) for h in lp if str(h.get("is_locked")) == "1")
                if locked < 0.5 and mcap < 2e8: flag(2, f"Only {locked*100:.0f}% of DEX liquidity is locked", "liquidity", False)
                else: checks["liquidity"] = True
    elif mcap > 3e8:
        checks["liquidity"] = True
    level = "HIGH" if pts >= 5 or checks.get("tax") is False else "MEDIUM" if pts >= 2 else "LOW"
    flags = [m for _, m in sorted(flags, key=lambda x: -x[0])]
    v = {"level": level, "points": round(pts, 1), "flags": flags, "checks": checks, "facts": facts}
    cache[key] = {"t": now(), "v": v}
    return v

# ================================================================ prediction markets (Polymarket, free, no key)
import re
PM = "https://gamma-api.polymarket.com/public-search"
UP_WORDS = ("reach", "hit", "above", "over", "exceed", "higher than", "greater than", "at least", "surpass")
DOWN_WORDS = ("dip", "below", "under", "fall", "drop", "less than", "lower than", "crash")
CAT_POS = ("etf", "approv", "list", "launch", "partner", "mainnet", "upgrade", "adopt", "reserve")
CAT_NEG = ("hack", "exploit", "delist", "lawsuit", "ban", "depeg", "bankrupt")

def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def polymarket(sym, name):
    """Active Polymarket markets that mention this token. Cached between runs."""
    pmc = CFG.get("prediction_markets") or {}
    key = "pm:" + sym
    hit = HIST.get(key)
    if hit and now() - hit["t"] < pmc.get("cache_minutes", 60) * 60: return hit["v"]
    terms = sorted({t for t in ((name or "").lower(), sym.lower() if len(sym) >= 3 else "") if t})
    min_liq = pmc.get("min_liquidity_usd", 2000)
    seen, out = set(), {"thresholds": [], "updown": [], "catalysts": []}
    for q in terms:
        d = get_json(f"{PM}?q={urllib.parse.quote(q)}&events_status=active")
        for ev in d.get("events", []) or []:
            for m in ev.get("markets", []) or []:
                mid = m.get("id") or m.get("conditionId") or m.get("question")
                if mid in seen or m.get("closed") or m.get("active") is False: continue
                seen.add(mid)
                qt = (m.get("question") or "").strip(); ql = qt.lower()
                if not any(re.search(r"\b" + re.escape(t) + r"\b", ql) for t in terms): continue
                liq = _num(m.get("liquidityNum")) or _num(m.get("liquidity")) or 0
                if liq < min_liq: continue
                try:
                    outs = m["outcomes"]; outs = json.loads(outs) if isinstance(outs, str) else outs
                    prs = m["outcomePrices"]; prs = [float(x) for x in (json.loads(prs) if isinstance(prs, str) else prs)]
                except Exception: continue
                if len(outs) != 2 or len(prs) != 2: continue
                end = (m.get("endDate") or ev.get("endDate") or "")[:10]
                o0 = str(outs[0]).lower()
                if o0 == "up":
                    out["updown"].append({"p": prs[0], "q": qt, "end": end, "liq": liq}); continue
                if o0 != "yes": continue
                p = prs[0]
                mt = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*([kKmMbB])?\b", qt)
                if mt and "between" not in ql:
                    k = float(mt.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6, "b": 1e9}.get((mt.group(2) or "").lower(), 1)
                    dirn = "down" if any(w in ql for w in DOWN_WORDS) else "up" if any(w in ql for w in UP_WORDS) else None
                    if dirn and k > 0:
                        out["thresholds"].append({"dir": dirn, "k": k, "p": p, "q": qt, "end": end, "liq": liq}); continue
                sign = -1 if any(w in ql for w in CAT_NEG) else 1 if any(w in ql for w in CAT_POS) else 0
                if sign: out["catalysts"].append({"sign": sign, "p": p, "q": qt, "end": end, "liq": liq})
    HIST[key] = {"t": now(), "v": out}
    return out

def market_view(pm, price):
    """Turn betting odds into a 0-100 score (50 = neutral) plus readable lines."""
    if not pm or not (pm["thresholds"] or pm["updown"] or pm["catalysts"]): return None
    parts = []
    near = lambda t: abs(math.log(t["k"] / price)) <= 0.5
    ups = [t for t in pm["thresholds"] if t["dir"] == "up" and t["k"] > price and near(t)]
    dns = [t for t in pm["thresholds"] if t["dir"] == "down" and t["k"] < price and near(t)]
    if ups and dns:   # upside odds vs downside odds at similar distances
        parts.append((sum(t["p"] for t in ups) / len(ups) - sum(t["p"] for t in dns) / len(dns), 1.0))
    if pm["updown"]:  # short-term "up or down" markets
        parts.append(((sum(u["p"] for u in pm["updown"]) / len(pm["updown"]) - 0.5) * 2, 0.7))
    for c in pm["catalysts"]:  # ETF approvals, listings, hacks...
        parts.append((c["sign"] * c["p"] * (0.3 if c["sign"] > 0 else 0.6), 0.5))
    score = 50 + 50 * (sum(v * w for v, w in parts) / sum(w for _, w in parts)) if parts else 50
    score = max(0.0, min(100.0, score))
    implied = None   # where "reach $K" odds cross 50% for the most common deadline
    allup = [t for t in pm["thresholds"] if t["dir"] == "up"]
    if len(allup) >= 2:
        ends = {}
        for t in allup: ends[t["end"]] = ends.get(t["end"], 0) + 1
        e = max(ends, key=ends.get)
        pts = sorted((t["k"], t["p"]) for t in allup if t["end"] == e)
        for (k1, p1), (k2, p2) in zip(pts, pts[1:]):
            if p1 >= 0.5 > p2:
                f = (p1 - 0.5) / (p1 - p2)
                implied = {"level": math.exp(math.log(k1) + f * (math.log(k2) - math.log(k1))), "by": e}; break
    best = sorted(pm["thresholds"] + pm["catalysts"] + pm["updown"], key=lambda t: -t["liq"])[:4]
    return {"score": score, "lines": [f"{t['p']*100:.0f}% odds: {t['q']}" for t in best], "implied": implied,
            "n": len(pm["thresholds"]) + len(pm["updown"]) + len(pm["catalysts"])}

# ================================================================ indicators
def sma(xs, n): return sum(xs[-n:]) / n if len(xs) >= n else None
def ema_series(xs, n):
    if len(xs) < n: return []
    k, out = 2 / (n + 1), [sum(xs[:n]) / n]
    for x in xs[n:]: out.append(x * k + out[-1] * (1 - k))
    return out
def rsi(xs, n=14):
    if len(xs) <= n: return None
    g = [max(xs[i] - xs[i-1], 0) for i in range(1, len(xs))]; l = [max(xs[i-1] - xs[i], 0) for i in range(1, len(xs))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for a, b in zip(g[n:], l[n:]): ag, al = (ag*(n-1)+a)/n, (al*(n-1)+b)/n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
def macd(xs):
    e26 = ema_series(xs, 26)
    if not e26: return None
    e12 = ema_series(xs, 12)[-len(e26):]
    line = [a - b for a, b in zip(e12, e26)]; sig = ema_series(line, 9)
    if len(sig) < 2: return None
    h = [a - b for a, b in zip(line[-len(sig):], sig)]
    return {"hist": h[-1], "prev": h[-2], "cross": "up" if h[-2] <= 0 < h[-1] else "down" if h[-2] >= 0 > h[-1] else None}
def atr(c, h=None, l=None, n=14):
    if len(c) <= n: return None
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))] if h and l else [abs(c[i]-c[i-1]) for i in range(1, len(c))]
    return sum(tr[-n:]) / n
def zones(c, lows, highs, a):
    price = c[-1]; lo = lows or c
    cands = [min(lo[-w:]) for w in (30, 90, 180) if len(lo) >= w] + [x for x in (sma(c, 50), sma(c, 200)) if x]
    hi = max((highs or c)[-90:])
    ceiling = min(price * 0.995, hi - 1.5 * a) if a else price * 0.995   # support must sit well below the recent high
    below = [x for x in cands if x < ceiling]
    bz = {"low": max(below) - 0.25*a, "high": min(max(below) + 0.75*a, hi - a)} if below and a else None
    sz = {"low": hi - 0.75*a, "high": hi + 0.25*a} if a else None
    return bz, sz
def volume_nodes(c, v, lows=None, highs=None, days=180, bins=48):
    """Volume-by-price: where the most coins actually changed hands. Returns heavy-volume price levels (strongest first)."""
    if not v or len(v) < 30: return []
    n = min(days, len(c), len(v)); c, v = c[-n:], v[-n:]
    lo_s = (lows or c)[-n:]; hi_s = (highs or c)[-n:]
    lo, hi = min(lo_s), max(hi_s)
    if hi <= lo: return []
    step = (hi - lo) / bins; prof = [0.0] * bins
    for i in range(n):                                   # spread each day's volume across that day's range
        a_, b_ = int((lo_s[i] - lo) / step), int((hi_s[i] - lo) / step)
        a_, b_ = max(0, min(bins - 1, a_)), max(0, min(bins - 1, b_))
        for k in range(a_, b_ + 1): prof[k] += v[i] / (b_ - a_ + 1)
    avg = sum(prof) / bins
    nodes = [(lo + (k + 0.5) * step, prof[k] / avg) for k in range(bins)
             if prof[k] > 1.3 * avg and prof[k] >= max(prof[max(0, k-2):k+3])]
    return [{"price": p_, "strength": round(w, 2)} for p_, w in sorted(nodes, key=lambda x: -x[1])[:6]]

def trade_plan(c, lows, highs, a, bz, sz, nodes):
    """Two buy zones (scale in), a stop-loss and a take-profit level, using supports + heavy-volume levels."""
    if not bz or not a: return {}
    price = c[-1]; lo = lows or c
    near = [n for n in nodes if abs(n["price"] - (bz["low"] + bz["high"]) / 2) <= 1.2 * a]
    z1 = dict(bz); z1["strong"] = bool(near)
    if near:                                             # heavy trading right at support = stronger floor
        z1["vol_level"] = near[0]["price"]
    # deeper support for zone 2: older lows, 200-day average and heavy-volume levels well below zone 1
    cands = [min(lo[-w:]) for w in (90, 180, 365) if len(lo) >= w] + [x for x in (sma(c, 200),) if x] + [n["price"] for n in nodes]
    deeper = [x for x in cands if x < z1["low"] - 0.75 * a and x > price * 0.35]
    z2 = None
    if deeper:
        s2 = max(deeper)
        z2 = {"low": s2 - 0.25 * a, "high": min(s2 + 0.5 * a, z1["low"] - 0.25 * a)}
        vn = [n for n in nodes if abs(n["price"] - s2) <= 1.2 * a]
        z2["strong"] = bool(vn)
        if z2["high"] <= z2["low"]: z2 = None
    floor = (z2 or z1)["low"]
    under = [n["price"] for n in nodes if n["price"] < floor and n["price"] > floor - 2 * a]
    stop = (min(under) if under else floor) - 0.5 * a
    above = sorted(n["price"] for n in nodes if n["price"] > price * 1.02)
    first_res = [x for x in above if not sz or x < sz["low"]]           # first heavy-volume ceiling before the sell zone
    target = first_res[0] if first_res else ((sz["low"] + sz["high"]) / 2 if sz and sz["low"] > price else None)
    return {"zone1": z1, "zone2": z2, "stop": stop, "target": target,
            "resistance": [x for x in above[:3]], "support_levels": sorted([n["price"] for n in nodes if n["price"] < price], reverse=True)[:3]}

def px(v):
    """Human-friendly price: $84,940 · $8.19 · $0.4309 · $0.00001234 (never scientific notation)."""
    if v is None: return "n/a"
    a_ = abs(v)
    if a_ >= 1000: return f"${v:,.0f}"
    if a_ >= 100: return f"${v:,.2f}"
    if a_ >= 1e-4: return f"${v:.4g}"                   # 4 significant digits, no scientific notation in this range
    return "$" + (f"{v:.12f}".rstrip("0"))[:14]
def usd(v):
    """Short dollar amounts: $950 · $168K · $9.2M · $1.3B."""
    a_ = abs(v or 0)
    return f"${v/1e9:.1f}B" if a_ >= 1e9 else f"${v/1e6:.1f}M" if a_ >= 1e6 else f"${v/1e3:.0f}K" if a_ >= 1e3 else f"${v:,.0f}"
def lin(x, bad, good): return max(0.0, min(100.0, (x - bad) / (good - bad) * 100))

GRADE_SCORE = {"A": 90, "B": 75, "C": 55, "D": 40, "F": 20}

def analyse(sym, data, depth, news, whales, grade, dd, mv=None, backdrop=None, ex=None):
    ex = ex or {}
    c = data["close"]; price = c[-1]
    r, m, a = rsi(c), macd(c), atr(c, data.get("high"), data.get("low"))
    s50, s200 = sma(c, 50), sma(c, 200)
    bz, sz = zones(c, data.get("low"), data.get("high"), a)
    parts, why, t, f = {}, [], [], []
    if r is not None:
        t.append(90 if r < 30 else 70 if r < 45 else 50 if r < 60 else 30 if r < 70 else 10)
        why.append(f"RSI {r:.0f}" + (" (oversold)" if r < 30 else " (overbought)" if r > 70 else ""))
    if m:
        t.append(85 if m["cross"] == "up" else 15 if m["cross"] == "down" else 65 if m["hist"] > m["prev"] else 35)
        why.append("MACD bullish cross" if m["cross"] == "up" else "MACD bearish cross" if m["cross"] == "down" else "MACD " + ("improving" if m["hist"] > m["prev"] else "fading"))
    if s50 and s200:
        t.append(70 if price > s50 > s200 else 55 if price > s200 else 40 if s50 > s200 else 30)
        why.append(f"{'Above' if price > s200 else 'Below'} 200-day avg {px(s200)}")
    nodes = volume_nodes(c, data.get("volume"), data.get("low"), data.get("high"))
    plan = trade_plan(c, data.get("low"), data.get("high"), a, bz, sz, nodes)
    z2 = plan.get("zone2")
    in_zone = bool(bz and bz["low"] <= price <= bz["high"])
    in_zone2 = bool(z2 and z2["low"] <= price <= z2["high"])
    if bz:
        if in_zone: t.append(92 if plan["zone1"].get("strong") else 88); why.append("Inside buy zone 1" + (" (heavy-volume support)" if plan["zone1"].get("strong") else ""))
        elif in_zone2: t.append(90 if z2.get("strong") else 86); why.append("Inside deep buy zone 2" + (" (heavy-volume support)" if z2.get("strong") else ""))
        elif plan.get("stop") and price < plan["stop"]: t.append(25); why.append(f"Below the stop-loss level {px(plan['stop'])} - support failed")
        elif price < bz["low"]: t.append(50); why.append("Below buy zone 1" + (f"; next support is zone 2 {px(z2['low'])}-{px(z2['high'])}" if z2 else ""))
        else: t.append(max(20, 80 - (price - bz["high"]) / (a or 1) * 15))
    if sz and price >= sz["low"]: t.append(15); why.append("Near 90-day high (sell zone)")
    if ind("bollinger"):
        bb = bollinger(c)
        if bb: ex["_pb"] = bb["pb"]
        if bb:
            t.append(80 if bb["pb"] < 0.05 else 65 if bb["pb"] < 0.2 else 25 if bb["pb"] > 0.95 else 40 if bb["pb"] > 0.8 else 50)
            if bb["pb"] < 0.05: why.append("At the lower Bollinger band (stretched down)")
            elif bb["pb"] > 0.95: why.append("At the upper Bollinger band (stretched up)")
            if bb["squeeze"]: why.append("Bollinger squeeze: volatility is unusually low, a big move often follows")
    if ind("obv"):
        ob = obv_div(c, data.get("volume"))
        if ob:
            if ob["obv_chg"] > 0.15 and ob["price_chg"] <= 0.02: t.append(78); why.append("Volume is accumulating while price stalls (OBV bullish divergence)")
            elif ob["obv_chg"] < -0.15 and ob["price_chg"] >= -0.02: t.append(25); why.append("Volume is leaving while price holds (OBV bearish divergence)")
    if ind("rsi_divergence"):
        dv = rsi_divergence(c, rsi_series(c))
        if dv == "bullish": t.append(82); why.append("Bullish RSI divergence: lower price low, higher RSI low")
        elif dv == "bearish": t.append(20); why.append("Bearish RSI divergence: higher price high, lower RSI high")
    if ind("relative_strength") and MACRO.get("btc_close") and sym != "BTC" and len(c) >= 31:
        b = MACRO["btc_close"]; rs30 = (c[-1] / c[-31] - b[-1] / b[-31]) * 100; ex["_rs30"] = rs30
        t.append(lin(rs30, -25, 25)); why.append(f"{'Outperforming' if rs30 > 0 else 'Underperforming'} Bitcoin by {abs(rs30):.0f}% over 30 days")
    parts["technical"] = sum(t) / len(t) if t else None
    ratio = None
    if data.get("taker_buy"):
        v7 = sum(data["volume"][-7:]); ratio = sum(data["taker_buy"][-7:]) / v7 if v7 else 0.5
        f.append(lin(ratio, 0.44, 0.56)); why.append(f"Buy pressure {ratio*100:.0f}% of 7d volume")
    if len(data["volume"]) >= 31:
        avg = sum(data["volume"][-31:-1]) / 30; sp = data["volume"][-1] / avg if avg else 1
        if sp > 2: up = c[-1] > c[-2]; f.append(80 if up else 20); why.append(f"Volume {sp:.1f}x normal ({'up' if up else 'down'} day)")
    if depth and depth["bid_usd"] + depth["ask_usd"]:
        f.append(lin(depth["bid_usd"] / (depth["bid_usd"] + depth["ask_usd"]), 0.35, 0.65))
        why.append(f"Order book 2%: {usd(depth['bid_usd'])} bids / {usd(depth['ask_usd'])} asks")
    if whales and whales["net_outflow"]:
        f.append(75 if whales["net_outflow"] > 0 else 25)
        why.append(f"Whales ${abs(whales['net_outflow'])/1e6:.1f}M net {'off' if whales['net_outflow'] > 0 else 'onto'} exchanges")
    tv = ex.get("tvl")
    if tv: f.append(lin(tv["chg"], -20, 20)); why.append(f"{tv['what']} {tv['chg']:+.0f}% over {tv['days']} days")
    parts["flows"] = sum(f) / len(f) if f else None
    dv = ex.get("deriv"); dp = []
    if dv:
        fr = dv["funding"]
        dp.append(lin(fr, 0.0006, -0.0003))
        why.append(f"Funding {fr*100:+.3f}% per 8h" + (" (longs crowded, squeeze-down risk)" if fr > 0.0005 else " (shorts crowded, squeeze-up potential)" if fr < -0.0002 else ""))
        if dv.get("oi_chg") is not None and len(c) >= 8:
            oc, pc = dv["oi_chg"], c[-1] / c[-8] - 1
            dp.append(65 if oc > 0.05 and pc > 0 else 35 if oc > 0.05 and pc < 0 else 55 if oc < -0.05 and pc < 0 else 50)
            why.append(f"Open interest {oc*100:+.0f}% in 7 days while price {pc*100:+.0f}%" + (" (new money confirming the move)" if oc > 0.05 and pc > 0 else " (shorts piling in)" if oc > 0.05 and pc < 0 else ""))
    parts["derivatives"] = sum(dp) / len(dp) if dp else None
    wv = ex.get("wallets")
    parts["wallets"] = wv["score"] if wv else None                 # followed wallets ("smart money"); only counts when they traded it
    if wv: why.append(wv["why"])
    parts["macro"] = MACRO.get("score")
    if news:
        flag = sum(n["flag"] for n in news[:15]); pos = sum(n["pos"] for n in news); neg = sum(n["neg"] for n in news)
        parts["news"] = max(0, min(100, 0.5 * (lin(pos/(pos+neg), 0.3, 0.8) if pos+neg else 50) + 0.5 * (50 + flag * 8)))
    else: parts["news"] = None
    parts["fundamental"] = GRADE_SCORE.get((grade or "").upper())
    if mv:
        parts["markets"] = mv["score"]
        why.append(f"Betting markets lean {'up' if mv['score'] > 55 else 'down' if mv['score'] < 45 else 'neutral'} ({mv['score']:.0f}/100, {mv['n']} markets)")
        if mv["implied"]: why.append(f"Betting odds put ~50% on touching {px(mv['implied']['level'])} by {mv['implied']['by']}")
    elif backdrop:
        parts["markets"] = 50 + (backdrop["score"] - 50) / 2
        why.append(f"No betting markets for {sym}; Bitcoin odds used as backdrop ({backdrop['score']:.0f}/100)")
    else: parts["markets"] = None
    W = {**DEFAULT_W, **(CFG.get("weights") or {})}
    W = {k: v * (LEARN.get("mult") or {}).get(k, 1.0) for k, v in W.items()}      # tuned by learning from past calls
    have = {k: v for k, v in parts.items() if v is not None and W.get(k, 0) > 0}; ws = sum(W[k] for k in have)
    score = sum(v * W[k] for k, v in have.items()) / ws if ws else None
    m_ = m
    feat_ = {"rsi": r, "zone": "in" if in_zone else ("above" if bz and price > bz["high"] else "below" if bz and price < bz["low"] else None),
             "pb": ex.get("_pb"), "rs30": ex.get("_rs30"), "macd": None if not m_ else m_["cross"] or ("improving" if m_["hist"] > m_["prev"] else "fading"),
             "vs200": (price / s200 - 1) if s200 else None, "chg7": (price / c[-8] - 1) if len(c) >= 8 else None,
             "macro": MACRO.get("score"), "btc_bull": MACRO.get("btc_bull"), "buy_ratio": ratio, "wallets": parts.get("wallets"),
             "risk": (dd or {}).get("level"), "source": ex.get("_source")}
    adj, lwhy, blocked = learned_adjust(feat_) if score is not None else (0, [], False)
    if adj: score = max(0, min(100, score + adj)); why += lwhy
    signal = ("STRONG BUY ZONE" if score >= 72 else "ACCUMULATE" if score >= 60 else "HOLD" if score >= 45
              else "TRIM" if score >= 33 else "SELL / AVOID") if score is not None else "NO DATA"
    # Guardrails: never call it a buy while it's in the sell zone or overbought - wait for the pullback
    in_sell = bool(sz and price >= sz["low"])
    if signal in BUY_SIGNALS and (in_sell or (r is not None and r > 70)):
        signal = "HOLD"; why.insert(0, "Capped at HOLD: " + ("price is in the sell zone" if in_sell else f"RSI {r:.0f} is overbought") + " - wait for a pullback toward the buy zone")
    far = bz and a and price > bz["high"] and (price - bz["high"] > 2 * a or price > bz["high"] * 1.15) and not in_zone2
    if signal in BUY_SIGNALS and far:
        signal = "HOLD"; why.insert(0, f"Capped at HOLD: price is {(price / bz['high'] - 1) * 100:.0f}% above buy zone 1 ({px(bz['low'])}-{px(bz['high'])}) - wait for a pullback")
    if signal in BUY_SIGNALS and blocked:
        signal = "HOLD"; why.insert(0, "Capped at HOLD: this setup has lost money repeatedly in past calls (see Learned)")
    if dd and dd["level"] == "HIGH":
        signal = "AVOID (SCAM RISK)"; why.insert(0, "High scam risk: " + "; ".join(dd["flags"][:3]))
    return {"symbol": sym, "price": price, "rsi": r, "macd": m, "atr": a, "sma50": s50, "sma200": s200, "buy_zone": bz,
            "sell_zone": sz, "in_zone": in_zone, "parts": parts, "score": score, "signal": signal, "why": why,
            "buy_ratio": ratio, "src": data.get("src"), "markets": mv, "pb": ex.get("_pb"), "rs30": ex.get("_rs30"),
            "in_sell": in_sell, "blocked": blocked, "plan": plan, "in_zone2": in_zone2, "vnodes": nodes}

# ================================================================ extra indicators (all free, all optional)
DEFAULT_IND = {"bollinger": True, "obv": True, "rsi_divergence": True, "relative_strength": True,
               "derivatives": True, "fear_greed": True, "market_regime": True, "stablecoin_liquidity": True,
               "tvl_trend": True, "betting_markets": True, "wallets": True}
DEFAULT_W = {"technical": .30, "flows": .15, "derivatives": .10, "macro": .10, "news": .05, "markets": .05, "fundamental": .25, "wallets": .10}
def ind(k): return (CFG.get("indicators") or {}).get(k, DEFAULT_IND[k])
MACRO = {}

def rsi_series(xs, n=14):
    if len(xs) <= n: return []
    out = [None] * n
    g = [max(xs[i] - xs[i-1], 0) for i in range(1, len(xs))]; l = [max(xs[i-1] - xs[i], 0) for i in range(1, len(xs))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out.append(100 - 100 / (1 + ag / al) if al else 100)
    for a, b in zip(g[n:], l[n:]):
        ag, al = (ag*(n-1)+a)/n, (al*(n-1)+b)/n
        out.append(100 - 100 / (1 + ag / al) if al else 100)
    return out

def bollinger(c, n=20, k=2):
    if len(c) < 120: return None
    def band(xs):
        m = sum(xs) / n; sd = (sum((x - m) ** 2 for x in xs) / n) ** 0.5
        return m, sd
    m, sd = band(c[-n:])
    pb = (c[-1] - (m - k*sd)) / (2*k*sd) if sd else 0.5
    widths = [band(c[i-n:i])[1] / band(c[i-n:i])[0] for i in range(len(c) - 100, len(c) + 1)]
    squeeze = widths[-1] <= sorted(widths)[10]
    return {"pb": pb, "squeeze": squeeze}

def obv_div(c, v, n=20):
    if len(c) < n + 2 or not v: return None
    obv = [0.0]
    for i in range(1, len(c)): obv.append(obv[-1] + (v[i] if c[i] > c[i-1] else -v[i] if c[i] < c[i-1] else 0))
    po = (c[-1] / c[-n] - 1); oo = obv[-1] - obv[-n]; scale = sum(v[-n:]) or 1
    return {"price_chg": po, "obv_chg": oo / scale}

def rsi_divergence(c, rs):
    if len(c) < 60 or len(rs) < 60 or rs[-60] is None: return None
    a, b = slice(-60, -30), slice(-30, None)
    pa, pb = c[a], c[b]; ra, rb = rs[a], rs[b]
    ia, ib = pa.index(min(pa)), pb.index(min(pb))
    if pb[ib] < pa[ia] and rb[ib] > ra[ia] + 3: return "bullish"
    ia, ib = pa.index(max(pa)), pb.index(max(pb))
    if pb[ib] > pa[ia] and rb[ib] < ra[ia] - 3: return "bearish"
    return None

FUT_BINANCE = "https://fapi.binance.com"
def derivatives(sym):
    """Funding rate + 7-day open-interest change. Binance futures, then Bybit."""
    pair = sym + "USDT"
    try:
        f = float(get_json(f"{FUT_BINANCE}/fapi/v1/premiumIndex?symbol={pair}")["lastFundingRate"])
        oi = get_json(f"{FUT_BINANCE}/futures/data/openInterestHist?symbol={pair}&period=1d&limit=8")
        vals = [float(x["sumOpenInterestValue"]) for x in oi]
        return {"funding": f, "oi_chg": vals[-1] / vals[0] - 1 if len(vals) > 1 and vals[0] else None, "src": "Binance"}
    except Exception: pass
    t = get_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={pair}")["result"]["list"][0]
    oi = get_json(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={pair}&intervalTime=1d&limit=8")["result"]["list"]
    vals = [float(x["openInterest"]) for x in oi][::-1]   # Bybit returns newest first
    return {"funding": float(t["fundingRate"]), "oi_chg": vals[-1] / vals[0] - 1 if len(vals) > 1 and vals[0] else None, "src": "Bybit"}

def llama_maps():
    h = HIST.get("llama_maps")
    if h and now() - h["t"] < 12 * 3600: return h["v"]
    chains = {c["gecko_id"]: c["name"] for c in get_json("https://api.llama.fi/v2/chains") if c.get("gecko_id")}
    protos = {p["gecko_id"]: {"slug": p.get("slug"), "c7": p.get("change_7d"), "tvl": p.get("tvl")}
              for p in get_json("https://api.llama.fi/protocols", timeout=60) if p.get("gecko_id")}
    v = {"chains": chains, "protos": protos}; HIST["llama_maps"] = {"t": now(), "v": v}
    return v

def tvl_trend(cg_id):
    mp = llama_maps()
    if cg_id in mp["chains"]:
        name = mp["chains"][cg_id]; key = "tvl:" + name; h = HIST.get(key)
        if not h or now() - h["t"] > 6 * 3600:
            hist = get_json(f"https://api.llama.fi/v2/historicalChainTvl/{urllib.parse.quote(name)}")
            h = HIST[key] = {"t": now(), "v": [x["tvl"] for x in hist[-31:]]}
        s = h["v"]
        return {"chg": (s[-1] / s[0] - 1) * 100, "days": 30, "what": f"{name} chain TVL"} if len(s) > 1 and s[0] else None
    p = mp["protos"].get(cg_id)
    if p and p.get("c7") is not None: return {"chg": p["c7"], "days": 7, "what": "protocol TVL"}
    return None

def load_macro():
    """Market-wide context, computed once per run and shared by every token."""
    MACRO.clear(); parts, why = [], []
    if ind("market_regime"):
        btc = binance_daily("BTCUSDT") or try_get("BTC history", lambda: coingecko_daily("bitcoin"))
        if btc:
            c = btc["close"]; MACRO["btc_close"] = c; s50, s200 = sma(c, 50), sma(c, 200)
            if s50 and s200:
                sc = 75 if c[-1] > s50 > s200 else 55 if c[-1] > s200 else 40 if s50 > s200 else 25
                MACRO["btc_bull"] = c[-1] > s200
                parts.append(sc); why.append(f"Market regime: BTC {'above' if c[-1] > s200 else 'below'} its 200-day average" + (" (bull trend)" if sc == 75 else " (bear trend)" if sc == 25 else ""))
    if ind("fear_greed"):
        fg = try_get("Fear & Greed", lambda: get_json("https://api.alternative.me/fng/?limit=1")["data"][0])
        if fg:
            v = int(fg["value"]); parts.append(lin(v, 90, 10))   # contrarian: fear = opportunity
            why.append(f"Fear & Greed {v} ({fg.get('value_classification', '')})")
    if ind("stablecoin_liquidity"):
        def stable():
            h = HIST.get("stables")
            if h and now() - h["t"] < 6 * 3600: return h["v"]
            d = get_json("https://stablecoins.llama.fi/stablecoincharts/all", timeout=60)
            s = [float((x.get("totalCirculatingUSD") or {}).get("peggedUSD", 0)) for x in d[-31:]]
            v = (s[-1] / s[0] - 1) * 100 if s and s[0] else None
            HIST["stables"] = {"t": now(), "v": v}; return v
        sc = try_get("stablecoin supply", stable)
        if sc is not None:
            parts.append(lin(sc, -2, 3)); why.append(f"Stablecoin supply {sc:+.1f}% in 30 days ({'money flowing in' if sc > 0 else 'money leaving'})")
    MACRO["score"] = sum(parts) / len(parts) if parts else None; MACRO["why"] = why
    if why: print("Market backdrop: " + "; ".join(why))

# ================================================================ track record
def stamp_call(calls, res, cg_id, dd, source):
    if not is_buy(res): return None
    if res["rsi"] is not None and res["rsi"] > 70: return None   # don't stamp buys into overbought moves
    if dd and dd["level"] == "HIGH": return None
    cool = CFG.get("call_cooldown_days", 7) * DAY
    if any(c["symbol"] == res["symbol"] and now() - c["t"] < cool for c in calls): return None
    call = {"id": f"{res['symbol']}-{int(now())}", "symbol": res["symbol"], "cg_id": cg_id, "t": int(now()), "date": iso(),
            "entry": res["price"], "signal": res["signal"] if res["signal"] in BUY_SIGNALS else "IN BUY ZONE",
            "score": round(res["score"] or 0), "buy_zone": res["buy_zone"], "risk": dd["level"] if dd else None, "plan": res.get("plan"),
            "source": source, "last": res["price"], "max": res["price"], "min": res["price"], "checkpoints": {}}
    calls.append(call); return call

def update_calls(calls, prices):
    for c in calls:
        p = prices.get(c["symbol"]) or prices.get(c.get("cg_id") or "")
        if not p: continue
        c["last"], c["max"], c["min"] = p, max(c["max"], p), min(c["min"], p)
        c["updated"] = iso()
        age = (now() - c["t"]) / DAY
        for d in (7, 30, 90, 180):
            if age >= d and str(d) not in c["checkpoints"]:
                c["checkpoints"][str(d)] = round((p / c["entry"] - 1) * 100, 2)

def ret(c): return (c["last"] / c["entry"] - 1) * 100

def report_text(calls):
    if not calls: return "No buy calls recorded yet."
    mine = [c for c in calls if c.get("source") != "discovery"]; found = [c for c in calls if c.get("source") == "discovery"]
    out = []
    for title, grp in (("⭐ YOUR COINS", mine), ("🔎 COINS THE SCANNER FOUND", found)):
        out.append(title + "\n" + (report_block(grp) if grp else "No calls yet."))
    return "\n\n".join(out) + ("\n\n" + lessons_text() if LEARN.get("graded") or (LEARN.get("scorecard") or {}).get("closed") else "")

def report_block(calls):
    rs = [ret(c) for c in calls]; wins = sum(1 for x in rs if x > 0)
    lines = [f"Track record: {len(calls)} buy calls, {wins} in profit ({wins/len(calls)*100:.0f}%), average {sum(rs)/len(rs):+.1f}%"]
    for d in ("7", "30", "90"):
        cp = [c["checkpoints"][d] for c in calls if d in c["checkpoints"]]
        if cp: lines.append(f"  After {d} days: avg {sum(cp)/len(cp):+.1f}% across {len(cp)} calls, {sum(1 for x in cp if x > 0)} up")
    for c in sorted(calls, key=lambda c: -c["t"])[:25]:
        lines.append(f"  {c['date'][:10]} {c['symbol']:<8} {c['signal']:<15} entry {px(c['entry'])} now {px(c['last'])} "
                     f"{ret(c):+6.1f}%  (best {(c['max']/c['entry']-1)*100:+.0f}%, worst {(c['min']/c['entry']-1)*100:+.0f}%)")
    return "\n".join(lines)

# ================================================================ alerts
PREFS = {}   # alert settings, from config.json plus anything you change by Telegram command

def tg_creds():
    tg = CFG.get("telegram") or {}
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token"), str(os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id") or ""))

def alert_allowed(kind, sym=None):
    """kind: 'token' (watchlist/starred), 'discovery', 'report', 'system'."""
    a = PREFS["alerts"]
    if kind == "system": return True
    if PREFS.get("paused"): return False
    if kind == "discovery": return a["discovered"]
    if kind == "report": return a["weekly_report"]
    if kind == "wallet" and not (sym and sym in PREFS["starred"]): return a.get("wallets", True)
    if sym and sym in PREFS["starred"]: return True          # starred: always
    return a["watchlist"]

def send(text, kind="system", sym=None):
    print("\n" + text + "\n")
    if not alert_allowed(kind, sym):
        print("  (Telegram muted for this kind of alert)"); return
    if sym and sym in PREFS.get("starred", []): text = "⭐ " + text
    tok, chat = tg_creds()
    if tok and chat and not DEMO:
        try:
            body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers=UA), timeout=15)
        except Exception as e: print(f"  [telegram failed] {e}")

BACKDROP = {}

def odds_alerts(sym, mv, state):
    """Alert when a betting market's odds move 15+ points since the last check."""
    st = state.setdefault(sym, {"seen_news": []}); old = st.get("odds", {}); new, ev = {}, []
    for line in mv["lines"]:
        pct, q = line.split("% odds: ", 1); new[q] = int(pct)
        if q in old and abs(new[q] - old[q]) >= (CFG.get("prediction_markets") or {}).get("alert_move_points", 15):
            ev.append(f"Betting odds moved {old[q]}% -> {new[q]}%: {q}")
    st["odds"] = new
    if ev: send(f"ODDS ALERT {sym}\n" + "\n".join("* " + e for e in ev), "token", sym)

# ---------------------------------------------------------------- contract + links (CoinGecko, DexScreener)
DEX_CHAINS = {"ethereum": "ethereum", "binance-smart-chain": "bsc", "solana": "solana", "base": "base",
              "arbitrum-one": "arbitrum", "polygon-pos": "polygon", "avalanche": "avalanche", "optimistic-ethereum": "optimism",
              "sui": "sui", "aptos": "aptos", "tron": "tron", "the-open-network": "ton", "sonic": "sonic", "linea": "linea",
              "blast": "blast", "mantle": "mantle", "zksync": "zksync", "cronos": "cronos", "fantom": "fantom", "osmosis": "osmosis",
              "injective": "injective", "sei-v2": "seiv2", "hyperevm": "hyperevm", "berachain": "berachain", "abstract": "abstract"}
CHAIN_PREF = ("solana", "ethereum", "base", "binance-smart-chain", "arbitrum-one")
def token_links(sym, cg_id, dd):
    """Best contract + CoinGecko and DexScreener links for a token."""
    plats = (((dd or {}).get("facts") or {}).get("platforms") or {})
    plats = {k: v for k, v in plats.items() if k and v}
    order = sorted(plats, key=lambda k: (k not in CHAIN_PREF, CHAIN_PREF.index(k) if k in CHAIN_PREF else 0, k not in DEX_CHAINS))
    chain = order[0] if order else None; addr = plats.get(chain) if chain else None
    dex = (f"https://dexscreener.com/{DEX_CHAINS[chain]}/{addr}" if chain in DEX_CHAINS
           else "https://dexscreener.com/search?q=" + urllib.parse.quote(addr or sym))
    return {"chain": chain, "contract": addr, "contracts": [{"chain": k, "address": plats[k]} for k in order][:4],
            "coingecko": f"https://www.coingecko.com/en/coins/{cg_id}" if cg_id else "https://www.coingecko.com/en/search?query=" + urllib.parse.quote(sym),
            "dexscreener": dex}
def links_text(res, dd):
    L = token_links(res["symbol"], res.get("cg_id"), dd)
    name = ((dd or {}).get("facts") or {}).get("name")
    s = f"\nTicker: {res['symbol']}" + (f" ({name})" if name else "")
    s += f"\nContract ({L['chain']}): {L['contract']}" if L["contract"] else "\nContract: none (native coin)"
    return s + f"\nCoinGecko: {L['coingecko']}\nDexScreener: {L['dexscreener']}"
def is_buy(res): return res["signal"] in BUY_SIGNALS or ((res.get("in_zone") or res.get("in_zone2")) and not res.get("in_sell") and not res.get("blocked") and res["signal"] not in ("SELL / AVOID", "AVOID (SCAM RISK)", "TRIM"))

def z(zn): return f"{px(zn['low'])}-{px(zn['high'])}" if zn else "n/a"
def summary(res, dd=None, links=False):
    s = f"{res['symbol']} {px(res['price'])} -> {res['signal']}" + (f" ({res['score']:.0f}/100)" if res['score'] is not None else "")
    s += f"\nBuy zone {z(res['buy_zone'])} | Sell zone {z(res['sell_zone'])}"
    pl = res.get("plan") or {}
    if pl.get("zone1"):
        s += (f"\nPlan: buy ½ in zone 1 {z(pl['zone1'])}{' (strong)' if pl['zone1'].get('strong') else ''}"
              + (f", ½ in zone 2 {z(pl['zone2'])}{' (strong)' if pl['zone2'].get('strong') else ''}" if pl.get("zone2") else "")
              + (f"; stop below {px(pl['stop'])}" if pl.get("stop") else "") + (f"; take profit near {px(pl['target'])}" if pl.get("target") else ""))
    if res.get("markets"): s += "\nBetting markets:\n" + "\n".join("   " + l for l in res["markets"]["lines"][:3])
    if dd: s += f"\nScam risk {dd['level']}" + (": " + "; ".join(dd["flags"][:4]) if dd["flags"] else "")
    s += "\n" + "\n".join(" - " + w for w in res["why"]) + ("\nMarket: " + "; ".join(MACRO.get("why", [])) if MACRO.get("why") else "")
    if is_buy(res) or links: s += "\n" + links_text(res, dd)
    return s

def check_alerts(tok, res, news, dd, state, call):
    st = state.setdefault(res["symbol"], {"seen_news": []}); ev = []; p = res["price"]
    if st.get("signal") and st["signal"] != res["signal"]: ev.append(f"Signal changed {st['signal']} -> {res['signal']}")
    if call: ev.append(f"BUY CALL stamped at {px(call['entry'])} (tracked in your record)")
    if res["in_zone"] and not st.get("in_zone") and not call: ev.append(f"Entered buy zone {z(res['buy_zone'])}")
    pl = res.get("plan") or {}
    if res.get("in_zone2") and not st.get("in_zone2"): ev.append(f"Entered deep buy zone 2 {z(pl.get('zone2'))} - second half of the plan")
    st["in_zone2"] = res.get("in_zone2")
    if pl.get("stop") and p < pl["stop"] and not st.get("below_stop"): ev.append(f"Fell below the stop-loss level {px(pl['stop'])} - the setup failed")
    st["below_stop"] = bool(pl.get("stop") and p < pl["stop"])
    if res["buy_zone"] and p < res["buy_zone"]["low"] and st.get("in_zone") and not res.get("in_zone2"): ev.append("Fell through buy zone 1" + (f" - next support is zone 2 {z(pl['zone2'])}" if pl.get("zone2") else " - support broke"))
    st["in_zone"] = res["in_zone"]
    if res["rsi"] is not None:
        band = "low" if res["rsi"] < 30 else "high" if res["rsi"] > 70 else "mid"
        if band != st.get("rsi_band") and band != "mid": ev.append(f"RSI {'oversold' if band == 'low' else 'overbought'} ({res['rsi']:.0f})")
        st["rsi_band"] = band
    today = iso()[:10]
    if res["macd"] and res["macd"]["cross"] and st.get("macd_day") != today:
        ev.append(f"MACD {'bullish' if res['macd']['cross'] == 'up' else 'bearish'} crossover"); st["macd_day"] = today
    for k, hit in (("below", lambda x: p <= x), ("above", lambda x: p >= x)):
        x = (tok.get("alerts") or {}).get(k)
        if x is not None:
            h = hit(x)
            if h and not st.get("hit_" + k): ev.append(f"Price {k} your ${x} level")
            st["hit_" + k] = h
    if dd and dd["level"] == "HIGH" and st.get("risk") != "HIGH": ev.append("Scam risk is HIGH - see flags")
    st["risk"] = dd["level"] if dd else None
    for n in news or []:
        if n["id"] in st["seen_news"]: continue
        st["seen_news"] = (st["seen_news"] + [n["id"]])[-300:]
        if n["flag"]: ev.append(f"News ({'+' if n['flag'] > 0 else '-'}): {n['title']}\n   {n['url']}")
    st["signal"] = res["signal"]
    if ev: send(f"ALERT {res['symbol']}\n" + "\n".join("* " + e for e in ev) + "\n\n" + summary(res, dd), "token", res["symbol"])

# ================================================================ watchlist + discovery
def norm(tok):
    if isinstance(tok, str): tok = {"symbol": tok}
    tok["symbol"] = tok["symbol"].upper().strip(); return tok

HIST, LIVE = {}, {}   # daily-history cache (kept between runs) and this run's live prices

def get_data(tok, cg_id):
    pair = tok.get("binance_pair") or tok["symbol"] + "USDT"
    data = binance_daily(pair)          # Binance: live candles every run, generous free limits
    depth = None
    if data and len(data["close"]) < 90 and cg_id:  # newly listed on Binance: too little history there, use CoinGecko's longer history
        live = data["close"][-1]; data = None
        LIVE.setdefault(cg_id, live)
    if data:
        depth = try_get("order book", lambda: binance_depth(data["host"], pair, data["close"][-1]))
    elif cg_id:
        # CoinGecko: daily history refreshed every few hours, live price patched in each run
        h = HIST.get(cg_id)
        if not h or now() - h["t"] > CFG.get("history_refresh_hours", 6) * 3600:
            fresh = try_get("CoinGecko prices", lambda: coingecko_daily(cg_id))
            if fresh: h = HIST[cg_id] = {"t": now(), "d": fresh}
        if h:
            data = {k: (list(v) if isinstance(v, list) else v) for k, v in h["d"].items()}
            if LIVE.get(cg_id): data["close"][-1] = LIVE[cg_id]
    return data, depth

REG = {}     # every coin ever added or called: ticker -> CoinGecko ID, name, contract (data/coins.json)

def register(sym, cg_id, dd, source):
    e = REG.setdefault(sym, {"first_seen": iso(), "source": source})
    L = token_links(sym, cg_id, dd); f = (dd or {}).get("facts") or {}
    e.update({k: v for k, v in {"cg_id": cg_id, "name": f.get("name"), "chain": L.get("chain"), "contract": L.get("contract")}.items() if v})
    if source == "watchlist": e["source"] = "watchlist"
    e["last_seen"] = iso()

LOGOS = {}    # CoinGecko id -> logo image URL
SELLS = []   # sell signals (data/sells.json); buys are the buy calls in calls.json

def delete_trades(ids, calls=None):
    """Remove buy calls (trades) by their ID (the call's timestamp). Also used by the dashboard's Delete button."""
    want = {x for x in re.split(r"[\s,]+", str(ids)) if x.isdigit()}
    own = calls is None
    if own: calls = load("calls.json", [])
    before = len(calls); calls[:] = [c for c in calls if str(int(c["t"])) not in want]
    if own: save("calls.json", calls)
    return before - len(calls)

def open_call(sym, calls):
    """The buy call for this coin that no sell signal has closed yet (or None)."""
    last_sell = max([e["t"] for e in SELLS if e["symbol"] == sym] or [0])
    op = [c for c in calls if c["symbol"] == sym and c["t"] > last_sell]
    return min(op, key=lambda c: c["t"]) if op else None

def sell_check(res, state, sym, source, cg_id, dd, calls):
    """Log a sell signal when the signal turns TRIM/SELL, the price enters the sell zone, or it drops below the stop."""
    st = state.setdefault("_sellst", {}).setdefault(sym, {})
    first = not st.get("seen"); st["seen"] = True
    soft = LEARN.get("sell_soft", [])
    bad = res["signal"] in (("SELL / AVOID", "AVOID (SCAM RISK)") if "signal" in soft else ("TRIM", "SELL / AVOID", "AVOID (SCAM RISK)"))
    ins = bool(res.get("in_sell")) and ("zone" not in soft or (res.get("rsi") or 0) > 70)
    stop = (res.get("plan") or {}).get("stop"); below = bool(stop and res["price"] < stop)
    oc0 = open_call(sym, calls); tgt = ((oc0 or {}).get("plan") or {}).get("target")
    hit_t = bool(tgt and res["price"] >= tgt)
    why = []
    if not first:
        if bad and not st.get("bad"): why.append(f"Signal turned {res['signal']}")
        if ins and not st.get("in_sell"): why.append(f"Entered sell zone {z(res['sell_zone'])}" + (f" · RSI {res['rsi']:.0f}" if res.get("rsi") else ""))
        if below and not st.get("below"): why.append(f"Fell below stop-loss {px(stop)}")
        if hit_t and not st.get("hit_t") and oc0: why.append(f"Hit the take-profit target {px(tgt)}")
    st.update(bad=bad, in_sell=ins, below=below, hit_t=hit_t)
    if not why: return
    oc = open_call(sym, calls)
    if not oc: return                                    # nothing to close - don't log noise
    ev = {"symbol": sym, "cg_id": cg_id, "t": int(now()), "date": iso(), "price": res["price"], "why": " · ".join(why),
          "source": source, "starred": sym in PREFS.get("starred", []), "closes": oc["id"] if oc else None}
    SELLS.append(ev)
    if oc:                                               # a buy call is open: tell you how that trade turned out
        r = (res["price"] / oc["entry"] - 1) * 100
        best = (max(oc.get("tmax", oc["entry"]), res["price"]) / oc["entry"] - 1) * 100
        send(f"{'✅' if r > 0 else '📉'} SELL SIGNAL {sym}: {ev['why']}\nTrade closed: bought {datetime.fromtimestamp(oc['t'], timezone.utc):%b %d} at {px(oc['entry'])} → sold at {px(res['price'])} = "
             f"{r:+.1f}% ({'profit' if r > 0 else 'loss'}) after {(now() - oc['t']) / DAY:.0f} days. Best point during the trade: {best:+.0f}%.\n"
             "I'll check in 7 days whether selling here was well timed.\n" + links_text(res, dd), "token", sym)

def check_token(tok, cache, state, calls, source="watchlist", grade=None):
    sym = tok["symbol"]; print(f"\n{sym} ...")
    cg_id = tok.get("coingecko_id") or (REG.get(sym) or {}).get("cg_id") or try_get("lookup", lambda: resolve_id(sym, cache))
    if not cg_id: print("  Not found on CoinGecko."); return None
    dd = try_get("due diligence", lambda: due_diligence(cg_id, tok, cache)) if CFG.get("scam_screen", True) else None
    data, depth = get_data(tok, cg_id)
    if not data or len(data["close"]) < 30: print("  Not enough price history."); return None
    news = try_get("news", lambda: cryptopanic_news(sym)) if CFG.get("cryptopanic_api_key") else None
    whales = try_get("whales", lambda: whale_flows(sym)) if CFG.get("whale_alert_api_key") else None
    mv = None
    ex = {}
    if ind("derivatives"): ex["deriv"] = try_get("funding/open interest", lambda: derivatives(sym))
    if ind("tvl_trend"): ex["tvl"] = try_get("TVL", lambda: tvl_trend(cg_id))
    if ind("wallets"):
        plats = list((((dd or {}).get("facts") or {}).get("platforms") or {}).values())
        ex["wallets"] = wallet_view(sym, plats)
    if ind("betting_markets") and (CFG.get("prediction_markets") or {}).get("enabled", True):
        name = ((dd or {}).get("facts") or {}).get("name") or tok.get("name") or ""
        pm = try_get("betting markets", lambda: polymarket(sym, name))
        mv = market_view(pm, data["close"][-1]) if pm else None
    ex["_source"] = source
    res = analyse(sym, data, depth, news, whales, tok.get("fundamental_grade") or grade, dd, mv, None if sym == "BTC" else BACKDROP.get("btc"), ex)
    res["cg_id"] = cg_id
    if source in ("watchlist", "tracked", "discovery"): register(sym, cg_id, dd, source)
    call = None if source in ("adhoc", "tracked") else stamp_call(calls, res, cg_id, dd, source)
    if source != "adhoc": try_get("sell check", lambda: sell_check(res, state, sym, source, cg_id, dd, calls))
    if call:
        call["feat"] = features(res, data, source, dd); call["starred"] = sym in PREFS.get("starred", [])
        call["links"] = token_links(sym, cg_id, dd)
    print(summary(res, dd))
    if source == "watchlist": check_alerts(tok, res, news, dd, state, call)
    if source == "watchlist" and mv: odds_alerts(sym, mv, state)
    return {"tok": tok, "cg_id": cg_id, "res": res, "dd": dd, "call": call, "closes": data["close"][-365:], "vols": (data.get("volume") or [])[-365:], "source": source}

def discover(cache, state, calls, skip):
    d = CFG.get("discovery") or {}
    if not d.get("enabled", True): return []
    last = state.get("_discovery_t", 0)
    if now() - last < d.get("every_hours", 12) * 3600: return []
    state["_discovery_t"] = now()
    print("\n=== Discovery scan ===")
    cands = {}
    mk = try_get("market list", lambda: get_json(f"{CG}/coins/markets?vs_currency=usd&order=volume_desc&per_page=250&page=1&price_change_percentage=7d", cg=True)) or []
    tr = try_get("trending", lambda: get_json(f"{CG}/search/trending", cg=True)) or {}
    lo, hi = d.get("min_market_cap_usd", 50e6), d.get("max_market_cap_usd", 5e9)
    for c in mk:
        if c["symbol"].lower() in STABLES or c["symbol"].upper() in skip: continue
        mc = c.get("market_cap") or 0
        if lo <= mc <= hi and (c.get("total_volume") or 0) >= d.get("min_volume_usd", 5e6):
            cands[c["id"]] = {"symbol": c["symbol"].upper(), "coingecko_id": c["id"], "mc": mc,
                              "chg7": c.get("price_change_percentage_7d_in_currency") or 0}
    for it in tr.get("coins", []):
        i = it.get("item", {})
        if i.get("symbol", "").upper() not in skip and i.get("id"):
            cands.setdefault(i["id"], {"symbol": i["symbol"].upper(), "coingecko_id": i["id"], "mc": 0, "chg7": 0, "trending": True})
    # prefer coins that pulled back this week (buy-zone candidates) and trending ones
    ranked = sorted(cands.values(), key=lambda c: (not c.get("trending"), c["chg7"]))[: d.get("max_checks_per_scan", 12)]
    found = []
    for c in ranked:
        r = check_token(c, cache, state, calls, source="discovery")
        if not r: continue
        ok_risk = r["dd"] and r["dd"]["level"] in (("LOW",) if d.get("require_low_risk", True) else ("LOW", "MEDIUM"))
        if ok_risk and is_buy(r["res"]):
            seen = state.setdefault("_disc_seen", {})
            if now() - seen.get(c["symbol"], 0) > 7 * DAY:
                seen[c["symbol"]] = now()
                send(f"NEW CANDIDATE (reply /add {c['symbol']} to watch it)\n" + summary(r["res"], r["dd"]) +
                     "\n\nAutomated checks passed, but still verify the team and audit yourself before buying.", "discovery")
            found.append(r)
    return found

# ================================================================ learning from its own calls
# Every buy call saves a snapshot of what the model saw. After `learning.grade_days` (7) the call is graded.
# Losers get a post-mortem (which warning signs were there at entry) sent to Telegram. Across all graded calls the
# model then (1) tunes the weight of each signal group toward what has actually predicted returns and (2) learns
# rules like "entries with RSI above 65 lost 6 of 8 times" that lower future scores or block the buy call.
# Everything is bounded and needs a minimum number of examples, so a few unlucky trades can't wreck the model.
LEARN = {}
WARN_SIGNS = [   # key, test on the entry snapshot, plain-English description
    ("rsi_high", lambda f: (f.get("rsi") or 0) > 65, "RSI was already high (>65) - buying after a run-up"),
    ("above_zone", lambda f: f.get("zone") == "above", "Price was above the buy zone - chasing instead of waiting for a pullback"),
    ("macd_weak", lambda f: f.get("macd") in ("down", "fading"), "MACD momentum was fading or turning down"),
    ("below_200", lambda f: f.get("vs200") is not None and f["vs200"] < 0, "Price was below its 200-day average (longer-term downtrend)"),
    ("weak_market", lambda f: f.get("macro") is not None and f["macro"] < 45, "Weak market backdrop (Bitcoin trend, Fear & Greed, stablecoin flows)"),
    ("btc_bear", lambda f: f.get("btc_bull") is False, "Bitcoin was below its 200-day average (bear regime)"),
    ("upper_band", lambda f: (f.get("pb") or 0) > 0.8, "Price was near the upper Bollinger band (stretched)"),
    ("ran_up", lambda f: (f.get("chg7") or 0) > 0.2, "Had already jumped more than 20% in 7 days"),
    ("lagging_btc", lambda f: f.get("rs30") is not None and f["rs30"] < -10, "Lagging Bitcoin by more than 10% over 30 days"),
    ("sell_pressure", lambda f: f.get("buy_ratio") is not None and f["buy_ratio"] < 0.48, "More selling than buying in the last 7 days"),
    ("scam_medium", lambda f: f.get("risk") in ("MEDIUM", "HIGH"), "Scam check was MEDIUM risk"),
    ("wallets_selling", lambda f: f.get("wallets") is not None and f["wallets"] < 45, "Followed wallets were selling it"),
    ("found_coin", lambda f: f.get("source") == "discovery", "A coin the scanner found (not one you picked)"),
]

def features(res, data, source, dd):
    """Snapshot of what the model saw at a buy call (used later to learn)."""
    c = data["close"]; p = c[-1]; bz = res.get("buy_zone")
    zone = "in" if res.get("in_zone") else ("above" if bz and p > bz["high"] else "below" if bz and p < bz["low"] else None)
    m = res.get("macd")
    return {"rsi": res.get("rsi"), "zone": zone, "pb": res.get("pb"), "rs30": res.get("rs30"),
            "macd": None if not m else m["cross"] or ("improving" if m["hist"] > m["prev"] else "fading"),
            "vs200": (p / res["sma200"] - 1) if res.get("sma200") else None, "vs50": (p / res["sma50"] - 1) if res.get("sma50") else None,
            "chg7": (p / c[-8] - 1) if len(c) >= 8 else None, "chg30": (p / c[-31] - 1) if len(c) >= 31 else None,
            "macro": MACRO.get("score"), "btc_bull": MACRO.get("btc_bull"), "buy_ratio": res.get("buy_ratio"),
            "wallets": (res.get("parts") or {}).get("wallets"), "risk": (dd or {}).get("level"), "source": source,
            "parts": {k: (round(v, 1) if v is not None else None) for k, v in (res.get("parts") or {}).items()},
            "btc": LIVE.get("bitcoin")}

def learned_adjust(f):
    """Score change (+/-) and reasons from rules learned on past calls. Returns (points, [reasons], blocked)."""
    hits, block = [], False
    for r in LEARN.get("rules", []):
        test = next((t for k, t, _ in WARN_SIGNS if k == r["key"]), None)
        if test and test(f): hits.append(r); block = block or r.get("block", False)
    if not hits: return 0.0, [], False
    hits.sort(key=lambda r: r["points"])                     # overlapping signs (e.g. high RSI + big run-up) count only partly
    pts = max(-(CFG.get("learning") or {}).get("max_penalty", 15), hits[0]["points"] + 0.25 * sum(r["points"] for r in hits[1:]))
    why = [f"Learned from past calls ({pts:+.0f} pts): " + "; ".join(f"{r['text'].split(' - ')[0]} won {r['wins']}/{r['n']}" for r in hits[:3])]
    return pts, why, block

def trade_exit(c):
    """The first sell signal for this coin after the buy call closes that trade."""
    xs = [e for e in SELLS if e["symbol"] == c["symbol"] and e["t"] > c["t"]]
    return min(xs, key=lambda e: e["t"]) if xs else None

def sell_kind(why):
    w = (why or "").lower()
    return "stop" if "stop-loss" in w else "target" if "take-profit" in w else "zone" if "sell zone" in w else "signal" if "signal turned" in w else "other"
SELL_KIND_TEXT = {"zone": "entered the sell zone", "signal": "signal turned TRIM/SELL", "stop": "fell below the stop-loss",
                  "target": "hit the take-profit target", "other": "other"}

def track_trades(calls):
    """Close each buy call at its sell signal: realized result, days held, best/worst while open, and what price did after the sell."""
    for c in calls:
        x = trade_exit(c); last = c.get("last") or c["entry"]
        if x:
            if not c.get("exit"):
                c["exit"] = {"t": x["t"], "date": x.get("date"), "price": x["price"], "why": x["why"], "kind": sell_kind(x["why"])}
                c["ret"] = round((x["price"] / c["entry"] - 1) * 100, 2); c["held_days"] = round((x["t"] - c["t"]) / DAY, 1)
                c["tmax"] = max(c.get("tmax", c["entry"]), x["price"]); c["tmin"] = min(c.get("tmin", c["entry"]), x["price"])
            if "after_sell" not in c and now() - x["t"] >= 7 * DAY:        # did selling help? (price 7 days later vs sell price)
                a_ = round((last / x["price"] - 1) * 100, 1); c["after_sell"] = a_
                c["sell_verdict"] = "sold early" if a_ > 10 else "good sell" if a_ < -10 else "fine"
        else:
            c["tmax"] = max(c.get("tmax", c["entry"]), last); c["tmin"] = min(c.get("tmin", c["entry"]), last)
            c["ret_open"] = round((last / c["entry"] - 1) * 100, 2)

def grade(c):
    """Closed trades are graded on their real result (buy price -> sell price); trades still open after 14 days on today's price."""
    if c.get("exit"):
        r = c["ret"]; c["gbasis"] = "closed"
        g = "win" if r > 3 else "loss" if r < -3 else "flat"
    elif now() - c["t"] >= 14 * DAY:
        r = c.get("ret_open", 0); dd = (c.get("tmin", c["entry"]) / c["entry"] - 1) * 100; c["gbasis"] = "open 14d"
        g = "loss" if r < -5 or (dd < -15 and r < 0) else "win" if r > 5 else "flat"
    else: return None
    c["gret"] = r; return g

def learn(calls, state):
    """Grade finished calls, write post-mortems for losers, rebuild the learned rules and weights."""
    lc = CFG.get("learning") or {}
    if not lc.get("enabled", True): LEARN.clear(); return
    track_trades(calls)
    fresh = []
    for c in calls:
        if not c.get("feat") or c.get("outcome"): continue
        g = grade(c)
        if not g: continue
        c["outcome"] = g; fresh.append(c)
        if g == "loss":
            f = c["feat"]; signs = [d for k, t, d in WARN_SIGNS if k != "found_coin" and t(f)]
            btc_now = LIVE.get("bitcoin"); btc_chg = (btc_now / f["btc"] - 1) * 100 if btc_now and f.get("btc") else None
            best = (c.get("tmax", c["entry"]) / c["entry"] - 1) * 100
            c["postmortem"] = {"signs": signs, "btc_chg": btc_chg, "r7": c["gret"], "dd": (c.get("tmin", c["min"]) / c["entry"] - 1) * 100,
                               "best": best, "basis": c["gbasis"], "gave_back": best >= 10}
    for c in calls:                                          # older graded calls: result = 7-day return
        if c.get("outcome") and "gret" not in c and "7" in (c.get("checkpoints") or {}): c["gret"] = c["checkpoints"]["7"]
    graded = [c for c in calls if c.get("outcome") and c.get("feat") and c.get("gret") is not None]
    n_min = lc.get("min_examples", 5)
    # 1) rules from warning signs
    rules = []
    for k, t, d in WARN_SIGNS:
        hit = [c for c in graded if t(c["feat"])]
        if len(hit) < n_min: continue
        rets = [c["gret"] for c in hit]; wins = sum(c["outcome"] == "win" for c in hit)
        wr, avg = wins / len(hit), sum(rets) / len(rets)
        if wr <= 0.35 and avg < 0:
            pts = -min(12, 4 + (0.35 - wr) * 30 + min(4, -avg / 3))
            rules.append({"key": k, "text": d, "n": len(hit), "wins": wins, "avg": avg, "points": round(pts, 1),
                          "block": len(hit) >= lc.get("block_after", 8) and wr <= 0.25})
        elif wr >= 0.65 and avg > 0 and k not in ("rsi_high", "above_zone", "ran_up"):
            continue                                        # a "warning sign" that keeps working isn't rewarded, just not punished
    # 2) weight tuning: does each signal group's score at entry line up with the 7-day return?
    mult = {}
    for part in DEFAULT_W:
        xs = [(c["feat"]["parts"].get(part), c["gret"]) for c in graded if (c["feat"].get("parts") or {}).get(part) is not None]
        if len(xs) < lc.get("min_examples_weights", 10): continue
        mx, my = sum(x for x, _ in xs) / len(xs), sum(y for _, y in xs) / len(xs)
        sx = sum((x - mx) ** 2 for x, _ in xs) ** .5; sy = sum((y - my) ** 2 for _, y in xs) ** .5
        corr = sum((x - mx) * (y - my) for x, y in xs) / (sx * sy) if sx and sy else 0
        mult[part] = round(max(0.6, min(1.4, 1 + corr * 0.6)), 2)
    # 3) closed-trade scorecard + sell review: were the sells well timed?
    closed = [c for c in calls if c.get("exit")]
    sc = {"closed": len(closed), "wins": sum(c["ret"] > 0 for c in closed),
          "avg": round(sum(c["ret"] for c in closed) / len(closed), 2) if closed else None,
          "held": round(sum(c["held_days"] for c in closed) / len(closed), 1) if closed else None,
          "gave_back": sum((c["tmax"] / c["entry"] - 1) * 100 >= 10 and c["ret"] < 2 for c in closed)}
    sells, soft = {}, []
    for c in closed:
        k = c["exit"]["kind"]; e = sells.setdefault(k, {"n": 0, "reviewed": 0, "early": 0, "good": 0, "after": 0.0, "ret": 0.0, "text": SELL_KIND_TEXT.get(k, k)})
        e["n"] += 1; e["ret"] += c["ret"]
        if "after_sell" in c:
            e["reviewed"] += 1; e["after"] += c["after_sell"]; e["early"] += c["sell_verdict"] == "sold early"; e["good"] += c["sell_verdict"] == "good sell"
    for k, e in sells.items():
        e["ret"] = round(e["ret"] / e["n"], 2); e["after"] = round(e["after"] / e["reviewed"], 1) if e["reviewed"] else None
        if k in ("zone", "signal") and e["reviewed"] >= n_min and e["after"] > 8 and e["early"] / e["reviewed"] >= 0.5: soft.append(k)   # these sells keep coming too early
    old = LEARN.get("rules", []); old_soft = LEARN.get("sell_soft", [])
    LEARN.clear(); LEARN.update({"rules": rules, "mult": mult, "graded": len(graded), "t": iso(), "scorecard": sc, "sells": sells, "sell_soft": soft,
                                 "wins": sum(c["outcome"] == "win" for c in graded), "losses": sum(c["outcome"] == "loss" for c in graded)})
    for k in soft:
        if k not in old_soft:
            send(f"🧠 Sell review: exits when the coin {SELL_KIND_TEXT[k]} were usually too early - price rose {sells[k]['after']:+.0f}% on average in the 7 days after "
                 f"({sells[k]['early']}/{sells[k]['reviewed']} trades). From now on that alone won't close a trade: "
                 + ("it also needs RSI above 70." if k == "zone" else "the signal must fall to SELL, not just TRIM."), "report")
    for c in calls:                                          # one-time note when a sell gets its 7-day review
        if "after_sell" in c and not c.get("sell_reviewed"):
            c["sell_reviewed"] = True
            send(f"🔍 SELL REVIEW {c['symbol']}: sold {c['exit']['date'][:10] if c['exit'].get('date') else ''} at {px(c['exit']['price'])} "
                 f"({c['ret']:+.1f}% on the trade). 7 days later it's {c['after_sell']:+.1f}% from there - {c['sell_verdict']}.", "report", c["symbol"])
    save("learning.json", LEARN)
    new_rules = [r for r in rules if r["key"] not in {o["key"] for o in old}]
    for c in fresh:
        if c["outcome"] != "loss": continue
        pm = c["postmortem"]
        msg = (f"📉 POST-MORTEM {c['symbol']} ({'found by scanner' if c.get('source') == 'discovery' else 'your coin'})\n"
               + (f"Buy call {c['date'][:10]} at {px(c['entry'])} closed at the sell signal ({c['exit']['why']}) after {c['held_days']:.0f} days: {pm['r7']:+.1f}%." if pm.get("basis") == "closed"
                  else f"Buy call {c['date'][:10]} at {px(c['entry'])} is still open after 14 days: {pm['r7']:+.1f}%.")
               + (f" It was up {pm['best']:+.0f}% at best - the gain was given back, so the exit came too late." if pm.get("gave_back") else f" Worst point {pm['dd']:+.0f}%.") + "\n"
               + ("Warning signs that were already there:\n" + "\n".join("• " + s for s in pm["signs"]) if pm["signs"] else "No obvious warning signs at entry - likely news or market-driven.")
               + (f"\nBitcoin moved {pm['btc_chg']:+.1f}% over the same time" + (" - the whole market fell, which the model can't fully avoid." if pm["btc_chg"] < -8 else ".") if pm["btc_chg"] is not None else ""))
        send(msg, "report", c["symbol"])
    if new_rules:
        send("🧠 The model learned from its past calls:\n" + "\n".join(f"• {r['text']}: {r['wins']}/{r['n']} won, avg {r['avg']:+.1f}% → {r['points']:+.0f} pts" + (" and no more buy calls when this shows up" if r["block"] else "") for r in new_rules), "report")

def lessons_text_sc():
    L_ = dict(LEARN); sc = L_.get("scorecard") or {}
    return (f"Closed trades: {sc['closed']}, {sc['wins']} profitable, avg {sc['avg']:+.1f}%. Not enough graded trades yet to change the model.")

def lessons_text():
    if not LEARN.get("graded") and not (LEARN.get("scorecard") or {}).get("closed"): return "No graded trades yet. A trade is graded when a sell signal closes it (or after 14 days if still open); the model starts adjusting after 5 similar examples."
    if not LEARN.get("graded"): return lessons_text_sc()
    s = f"🧠 Learned from {LEARN['graded']} graded calls ({LEARN['wins']} wins, {LEARN['losses']} losses):"
    s += "".join(f"\n• {r['text']}: {r['wins']}/{r['n']} won, avg {r['avg']:+.1f}% → {r['points']:+.0f} pts" + (" (blocks buy calls)" if r["block"] else "") for r in LEARN.get("rules", [])) or "\nNo rules yet - no warning sign has lost often enough to act on."
    if LEARN.get("mult"): s += "\nWeight tuning: " + ", ".join(f"{k} ×{v}" for k, v in LEARN["mult"].items())
    sc = LEARN.get("scorecard") or {}
    if sc.get("closed"):
        s += (f"\n\nClosed trades: {sc['closed']}, {sc['wins']} profitable ({sc['wins']/sc['closed']*100:.0f}%), avg {sc['avg']:+.1f}%, held {sc['held']:.0f} days on average"
              + (f", {sc['gave_back']} gave back a 10%+ gain" if sc["gave_back"] else ""))
        for k, e in (LEARN.get("sells") or {}).items():
            s += f"\n• Sells when it {e['text']}: {e['n']} trades, avg {e['ret']:+.1f}%" + (f"; price moved {e['after']:+.1f}% in the 7 days after ({e['early']} sold early, {e['good']} good sells)" if e.get("after") is not None else "")
    return s

# ================================================================ followed wallets ("smart money")
# Solana: Helius (free key). Ethereum/Arbitrum/Polygon: Etherscan V2 (free key). Base: Blockscout (free, no key).
# Prices, liquidity and links: DexScreener (free, no key). Contract safety: GoPlus (free, no key).
QUOTE_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
               "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"}
EVM_QUOTES = {"WETH", "ETH", "USDC", "USDC.E", "USDBC", "USDT", "USDT0", "DAI", "WBTC", "CBBTC", "WMATIC", "WPOL", "POL", "FDUSD",
              "USDE", "USDS", "PYUSD", "STETH", "WSTETH", "CBETH", "RETH", "WEETH", "FRAX", "LUSD", "GHO", "CRVUSD", "USD0", "EURC"}
EVM_CHAINS = [("ethereum", "1"), ("arbitrum", "42161"), ("polygon", "137"), ("base", "base")]   # base -> Blockscout
BLOCKSCOUT = {"base": "https://base.blockscout.com/api"}
GOPLUS_NUM = {"ethereum": "1", "arbitrum": "42161", "polygon": "137", "base": "8453", "bsc": "56"}
WTRADES = []      # recent trades by followed wallets (data/wallet_trades.json)
WSTATS = {}       # address -> track record of that wallet's buys
DEXS = {}         # this run's DexScreener lookups: "chain:address" -> info

def wkind(a):
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", a or ""): return "evm"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a or ""): return "sol"
    return None

def followed():
    """Wallets from config.json ("wallets") plus ones added with /follow, minus /unfollow."""
    out, seen = [], set()
    for w in (CFG.get("wallets") or []) + PREFS.get("wallets", []):
        if isinstance(w, str): w = {"address": w}
        a = (w.get("address") or "").strip(); k = wkind(a)
        key = a.lower() if k == "evm" else a
        if not k or key in seen or key in {x.lower() if wkind(x) == "evm" else x for x in PREFS.get("unfollowed", [])}: continue
        seen.add(key); out.append({"address": a, "name": w.get("name") or f"Wallet {a[:4]}…{a[-4:]}", "kind": k})
    return out

def post_json(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode())

def dex_info(chain, addrs):
    """DexScreener: best-liquidity pair for each token -> symbol, name, price, liquidity, fdv, link. Up to 30 per call."""
    need = [a for a in dict.fromkeys(addrs) if f"{chain}:{a.lower() if chain != 'solana' else a}" not in DEXS]
    for i in range(0, len(need), 30):
        chunk = need[i:i+30]
        pairs = try_get("DexScreener", lambda: get_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{','.join(chunk)}")) or []
        best = {}
        for p in pairs if isinstance(pairs, list) else []:
            b = p.get("baseToken") or {}; ad = b.get("address") or ""
            k = ad if chain == "solana" else ad.lower()
            liq = ((p.get("liquidity") or {}).get("usd")) or 0
            if k and (k not in best or liq > best[k]["liquidity"]):
                best[k] = {"symbol": (b.get("symbol") or "?").upper(), "name": b.get("name") or "", "price": float(p.get("priceUsd") or 0),
                           "liquidity": liq, "fdv": p.get("fdv") or p.get("marketCap") or 0, "url": p.get("url"),
                           "chg24": ((p.get("priceChange") or {}).get("h24")), "created": p.get("pairCreatedAt")}
        for a in chunk:
            k = a if chain == "solana" else a.lower()
            DEXS[f"{chain}:{k}"] = best.get(k)
    return {a: DEXS.get(f"{chain}:{a if chain == 'solana' else a.lower()}") for a in addrs}

def helius_budget(state, cost):
    """Stay inside the free 1M credits a month (stops at 'helius_monthly_credits', default 900k)."""
    m = datetime.now(timezone.utc).strftime("%Y-%m"); h = state.setdefault("_helius", {"month": m, "used": 0})
    if h["month"] != m: h.update(month=m, used=0)
    if h["used"] + cost > (CFG.get("wallet_tracking") or {}).get("helius_monthly_credits", 900000): return False
    h["used"] += cost; return True

def sol_trades(w, st, state, first):
    key = CFG.get("helius_api_key")
    if not key: return []
    if not helius_budget(state, 1): return []
    q = {"limit": 50}
    if st.get("sig") and not first: q["until"] = st["sig"]
    r = post_json(f"https://mainnet.helius-rpc.com/?api-key={key}", {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [w["address"], q]})
    sigs = [x["signature"] for x in (r.get("result") or []) if not x.get("err")]
    if not sigs: return []
    if first: sigs = sigs[:25]
    every = (CFG.get("wallet_tracking") or {}).get("solana_decode_every_minutes", 30) * 60
    if not first and now() - st.get("decoded_t", 0) < every: return []       # decode later (saves credits); nothing is lost
    if not helius_budget(state, 100): print("  Helius monthly budget reached; Solana wallets paused until next month"); return []
    txs = None
    for base in ("https://api-mainnet.helius-rpc.com", "https://api.helius.xyz"):
        try: txs = post_json(f"{base}/v0/transactions/?api-key={key}", {"transactions": sigs[:100]}); break
        except Exception as e: err = e
    if txs is None: raise err
    st["sig"] = sigs[0]; st["decoded_t"] = now()
    out = []
    for tx in txs or []:
        if tx.get("transactionError"): continue
        legs = {}
        for tt in tx.get("tokenTransfers") or []:
            m = tt.get("mint"); amt = float(tt.get("tokenAmount") or 0)
            if not m or m in QUOTE_MINTS or not amt: continue
            if tt.get("toUserAccount") == w["address"]: legs[m] = legs.get(m, 0) + amt
            elif tt.get("fromUserAccount") == w["address"]: legs[m] = legs.get(m, 0) - amt
        for m, amt in legs.items():
            out.append({"chain": "solana", "token": m, "amount": abs(amt), "side": "buy" if amt > 0 else "sell",
                        "t": tx.get("timestamp") or int(now()), "tx": tx.get("signature")})
    return out

def evm_trades(w, st, state, first):
    out, a = [], w["address"].lower()
    probe = first or not st.get("active") or now() - st.get("probe_t", 0) > 6 * 3600
    if probe: st["probe_t"] = now()
    for chain, cid in EVM_CHAINS:
        if not probe and chain not in st.get("active", []): continue
        cs = st.setdefault("blocks", {})
        start = int(cs.get(chain, 0)) + 1 if cs.get(chain) else 0
        if chain in BLOCKSCOUT: url = f"{BLOCKSCOUT[chain]}?module=account&action=tokentx&address={a}&sort=desc&page=1&offset=100&startblock={start}"
        else:
            key = CFG.get("etherscan_api_key")
            if not key: continue
            url = f"https://api.etherscan.io/v2/api?chainid={cid}&module=account&action=tokentx&address={a}&sort=desc&page=1&offset=100&startblock={start}&apikey={key}"
            time.sleep(0.4)                                 # free tier: 3 calls/second
        d = try_get(f"{chain} transfers", lambda url=url: get_json(url))
        rows = (d or {}).get("result")
        if not isinstance(rows, list) or not rows: continue
        st.setdefault("active", []); chain in st["active"] or st["active"].append(chain)
        cs[chain] = max(int(r.get("blockNumber") or 0) for r in rows)
        if first: rows = [r for r in rows if now() - int(r.get("timeStamp") or 0) < 3 * DAY]
        by = {}
        for r in rows:
            sym = (r.get("tokenSymbol") or "").upper()
            if sym in EVM_QUOTES: continue
            try: amt = int(r.get("value") or 0) / 10 ** int(r.get("tokenDecimal") or 18)
            except ValueError: continue
            k = (r.get("hash"), r.get("contractAddress", "").lower())
            sign = 1 if r.get("to", "").lower() == a else -1 if r.get("from", "").lower() == a else 0
            if sign: by[k] = (by.get(k, (0, r))[0] + sign * amt, r)
        for (h, ca), (amt, r) in by.items():
            if amt: out.append({"chain": chain, "token": ca, "amount": abs(amt), "side": "buy" if amt > 0 else "sell",
                                "t": int(r.get("timeStamp") or now()), "tx": h, "sym_hint": (r.get("tokenSymbol") or "").upper()})
    return out

def token_risk(chain, addr):
    """Quick GoPlus contract check for wallet-trade alerts. Returns a list of red flags (cached 3 days)."""
    key = f"gp:{chain}:{addr}"; c = CTX.get("cache") if CTX else None
    if c is not None and key in c and now() - c[key]["t"] < 3 * DAY: return c[key]["v"]
    flags = []
    if chain == "solana":
        g = get_json(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={addr}")
        r = ((g or {}).get("result") or {}).get(addr) or {}
        st = lambda k: str((r.get(k) or {}).get("status", "0")) == "1" if isinstance(r.get(k), dict) else False
        if st("mintable"): flags.append("Mint authority still active (more tokens can be created)")
        if st("freezable"): flags.append("Can freeze holders' tokens")
        if st("balance_mutable_authority"): flags.append("Owner can change balances")
        if st("non_transferable"): flags.append("Token can't be transferred")
    elif chain in GOPLUS_NUM:
        g = get_json(f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_NUM[chain]}?contract_addresses={addr}")
        r = ((g or {}).get("result") or {}).get(addr.lower()) or {}
        one = lambda k: str(r.get(k, "0")) == "1"
        if one("is_honeypot") or one("cannot_sell_all"): flags.append("HONEYPOT - holders can't sell")
        try:
            if float(r.get("sell_tax") or 0) > 0.1: flags.append(f"Sell tax {float(r['sell_tax'])*100:.0f}%")
        except ValueError: pass
        if one("owner_change_balance") or one("hidden_owner"): flags.append("Owner can change balances / hidden owner")
        if one("is_mintable"): flags.append("Owner can mint new tokens")
    if c is not None: c[key] = {"t": now(), "v": flags}
    return flags

def wallet_stats():
    """Each wallet's record: how its tracked buys have done since (price now vs price when bought)."""
    WSTATS.clear()
    for tr in WTRADES:
        if tr["side"] != "buy" or not tr.get("price"): continue
        s = WSTATS.setdefault(tr["wallet"], {"buys": 0, "wins": 0, "sum": 0.0, "name": tr["wname"]})
        s["name"] = tr["wname"]
        cur = tr.get("last") or tr["price"]; r = (cur / tr["price"] - 1) * 100
        if now() - tr["t"] < DAY: continue                   # judge a buy after at least a day
        s["buys"] += 1; s["sum"] += r; s["wins"] += r > 0
    for s in WSTATS.values():
        s["win"] = (s["wins"] + 1) / (s["buys"] + 2)        # shrunk toward 50% until there's a real record
        s["avg"] = s["sum"] / s["buys"] if s["buys"] else 0.0
        s["weight"] = max(0.5, min(1.6, 0.4 + 1.2 * s["win"] + max(-0.2, min(0.2, s["avg"] / 200))))

def wline(addr):
    s = WSTATS.get(addr)
    if not s or not s["buys"]: return "Wallet record: new (no finished buys tracked yet)"
    return f"Wallet record: {s['buys']} buys tracked, {s['wins']/s['buys']*100:.0f}% in profit, avg {s['avg']:+.0f}%"

def sync_wallets(state):
    """Pull new trades for every followed wallet, price them, alert, and keep a track record per wallet."""
    WTRADES[:] = load("wallet_trades.json", [])
    ws = followed()
    if not ws: wallet_stats(); return
    wt = CFG.get("wallet_tracking") or {}
    min_usd, min_liq, look = wt.get("min_trade_usd", 500), wt.get("min_liquidity_usd", 25000), wt.get("cluster_hours", 48) * 3600
    have = {tr["id"] for tr in WTRADES}; new = []
    for w in ws:
        st = state.setdefault("_wallets", {}).setdefault(w["address"], {})
        first = not st.get("synced")
        try: raw = (sol_trades if w["kind"] == "sol" else evm_trades)(w, st, state, first)
        except Exception as e: print(f"  [skip] wallet {w['name']}: {e}"); continue
        st["synced"] = True
        for chain in {r["chain"] for r in raw}: dex_info(chain, [r["token"] for r in raw if r["chain"] == chain])
        for r in raw:
            info = DEXS.get(f"{r['chain']}:{r['token'] if r['chain'] == 'solana' else r['token'].lower()}")
            if not info or not info["price"] or info["liquidity"] < min_liq: continue    # skips spam airdrops and dust
            usd = r["amount"] * info["price"]
            if usd < min_usd: continue
            tid = f"{r['tx']}:{r['token']}"
            if tid in have: continue
            have.add(tid)
            tr = {"id": tid, "wallet": w["address"], "wname": w["name"], "chain": r["chain"], "token": r["token"],
                  "symbol": info["symbol"], "name": info["name"], "side": r["side"], "usd": round(usd), "price": info["price"],
                  "last": info["price"], "t": int(r["t"]), "url": info["url"], "quiet": first}
            WTRADES.append(tr); new.append(tr)
    # refresh prices of recent buys (for each wallet's track record)
    recent = [tr for tr in WTRADES if tr["side"] == "buy" and now() - tr["t"] < 45 * DAY]
    for chain in {tr["chain"] for tr in recent}:
        info = dex_info(chain, [tr["token"] for tr in recent if tr["chain"] == chain])
        for tr in recent:
            i = info.get(tr["token"]) if tr["chain"] == chain else None
            if i and i["price"]: tr["last"] = i["price"]
    WTRADES[:] = sorted([tr for tr in WTRADES if now() - tr["t"] < 60 * DAY], key=lambda x: x["t"])[-3000:]
    wallet_stats()
    for tr in sorted(new, key=lambda x: x["t"]):
        if tr.get("quiet") or now() - tr["t"] > look: continue
        wallet_alert(tr, look)
    for tr in WTRADES: tr.pop("quiet", None)
    save("wallet_trades.json", WTRADES)

def cluster(chain, token, look, side="buy"):
    k = token if chain == "solana" else token.lower()
    hits = [tr for tr in WTRADES if tr["side"] == side and tr["chain"] == chain and (tr["token"] if chain == "solana" else tr["token"].lower()) == k and now() - tr["t"] < look]
    names = {}
    for tr in hits: names[tr["wallet"]] = tr["wname"]
    return names

def wallet_alert(tr, look):
    info = DEXS.get(f"{tr['chain']}:{tr['token'] if tr['chain'] == 'solana' else tr['token'].lower()}") or {}
    who = cluster(tr["chain"], tr["token"], look, tr["side"])
    if tr["side"] == "sell":
        mine = [b for b in WTRADES if b["side"] == "buy" and b["wallet"] == tr["wallet"] and b["token"] == tr["token"]]
        if not mine: return                                  # only report sells of something we saw them buy
        r = (tr["price"] / mine[-1]["price"] - 1) * 100
        send(f"🐋 {tr['wname']} SOLD {tr['symbol']} (${tr['usd']:,})\nBought {datetime.fromtimestamp(mine[-1]['t'], timezone.utc):%b %d} at {px(mine[-1]['price'])}, now {px(tr['price'])} ({r:+.0f}%)"
             + (f"\n{len(who)} of your wallets sold it in the last {look//3600}h: {', '.join(who.values())}" if len(who) > 1 else "")
             + f"\nDexScreener: {tr['url']}", "wallet", tr["symbol"])
        return
    try: flags = token_risk(tr["chain"], tr["token"])
    except Exception: flags = None
    age = ""
    if info.get("created"):
        days = (now() - info["created"] / 1000) / DAY; age = f" · pair {days:.0f}d old" if days >= 1 else f" · pair {days*24:.0f}h old"
    head = "🔥 CLUSTER BUY" if len(who) > 1 else "🐋 Wallet buy"
    L = {"coingecko": "https://www.coingecko.com/en/search?query=" + urllib.parse.quote(tr["symbol"]), "dexscreener": tr["url"]}
    msg = (f"{head}: {tr['wname']} bought ${tr['usd']:,} of {tr['symbol']}" + (f" ({tr['name']})" if tr["name"] else "") + f" on {tr['chain']}"
           + f"\nPrice {px(tr['price'])} · Liquidity ${info.get('liquidity', 0)/1e3:,.0f}K · FDV ${(info.get('fdv') or 0)/1e6:,.1f}M"
           + (f" · 24h {info['chg24']:+.0f}%" if info.get("chg24") is not None else "") + age
           + f"\n{wline(tr['wallet'])}"
           + (f"\n{len(who)} of your wallets bought it in the last {look//3600}h: {', '.join(who.values())}" if len(who) > 1 else "")
           + ("\nScam check: " + ("; ".join(flags) if flags else "no red flags found in the contract") if flags is not None else "")
           + f"\n\nTicker: {tr['symbol']}\nContract ({tr['chain']}): {tr['token']}\nCoinGecko: {L['coingecko']}\nDexScreener: {L['dexscreener']}"
           + "\n\nCopying wallets is risky: they may sell before the next check. Verify before buying.")
    send(msg, "wallet", tr["symbol"])

def wallet_view(sym, contracts):
    """Score (0-100) from followed wallets' recent net buying of this token, weighted by each wallet's record."""
    if not WTRADES: return None
    look = (CFG.get("wallet_tracking") or {}).get("cluster_hours", 48) * 3600
    cs = {c.lower() for c in contracts if c} | {c for c in contracts if c}
    hits = [tr for tr in WTRADES if now() - tr["t"] < look and (tr["token"] in cs or tr["token"].lower() in cs or (not contracts and tr["symbol"] == sym))]
    if not hits: return None
    net = {}
    for tr in hits: net[tr["wallet"]] = net.get(tr["wallet"], 0) + (tr["usd"] if tr["side"] == "buy" else -tr["usd"])
    b = [a for a, v in net.items() if v > 0]; s = [a for a, v in net.items() if v < 0]
    wgt = lambda a: (WSTATS.get(a) or {}).get("weight", 1.0)
    score = max(0, min(100, 50 + 16 * sum(wgt(a) for a in b) - 16 * sum(wgt(a) for a in s)))
    nm = lambda xs: ", ".join((WSTATS.get(a) or {}).get("name") or next(tr["wname"] for tr in hits if tr["wallet"] == a) for a in xs[:3])
    why = []
    if b: why.append(f"🐋 {len(b)} followed wallet{'s' if len(b) > 1 else ''} bought in {look//3600}h ({nm(b)})")
    if s: why.append(f"🐋 {len(s)} followed wallet{'s' if len(s) > 1 else ''} sold in {look//3600}h ({nm(s)})")
    return {"score": score, "why": "; ".join(why), "buyers": len(b), "sellers": len(s)}

def wallets_text():
    ws = followed()
    if not ws: return "You're not following any wallets. Send /follow <address> <nickname>. Find good ones on the Fomo leaderboard, DexScreener's Top Traders tab, or GMGN."
    rows = sorted(ws, key=lambda w: -((WSTATS.get(w["address"]) or {}).get("weight", 1)))
    out = ["Wallets you follow (best record first):"]
    for w in rows:
        s = WSTATS.get(w["address"]) or {}
        out.append(f"• {w['name']} ({'Solana' if w['kind'] == 'sol' else 'EVM'}): " + (f"{s['buys']} buys, {s['wins']/s['buys']*100:.0f}% in profit, avg {s['avg']:+.0f}%" if s.get("buys") else "no finished buys yet"))
    last = [tr for tr in reversed(WTRADES) if tr["side"] == "buy"][:5]
    if last: out += ["", "Latest buys:"] + [f"• {tr['wname']}: {tr['symbol']} ${tr['usd']:,} ({(tr['last']/tr['price']-1)*100:+.0f}% since)" for tr in last]
    return "\n".join(out)

def wallets_export():
    board = []
    for w in followed():
        s = WSTATS.get(w["address"]) or {}
        board.append({"name": w["name"], "address": w["address"], "kind": w["kind"], "buys": s.get("buys", 0),
                      "win": (s["wins"] / s["buys"] * 100) if s.get("buys") else None, "avg": s.get("avg") if s.get("buys") else None,
                      "weight": s.get("weight", 1.0)})
    board.sort(key=lambda b: -b["weight"])
    look = (CFG.get("wallet_tracking") or {}).get("cluster_hours", 48) * 3600
    recent = []
    for tr in list(reversed(WTRADES))[:40]:
        recent.append({**{k: tr[k] for k in ("wname", "chain", "token", "symbol", "name", "side", "usd", "price", "last", "t", "url")},
                       "cluster": len(cluster(tr["chain"], tr["token"], look, tr["side"])) if tr["side"] == "buy" else 0})
    return {"board": board, "recent": recent}

# ================================================================ outputs
def export(results, calls, full=True):
    out = {"generated": iso(), "tokens": [], "calls": calls}
    ids_ = set()
    for r in results:
        k_ = r.get("cg_id") or r["res"]["symbol"]
        if k_ in ids_: continue
        ids_.add(k_)
        out["tokens"].append(token_entry(r))
    out["macro"] = {"score": MACRO.get("score"), "why": MACRO.get("why", [])}
    out["wallets"] = wallets_export()
    out["coins"] = REG; out["logos"] = LOGOS
    out["sells"] = [e for e in SELLS if now() - e["t"] < 120 * DAY]
    out["learning"] = {"rules": LEARN.get("rules", []), "mult": LEARN.get("mult", {}), "graded": LEARN.get("graded", 0),
                       "wins": LEARN.get("wins", 0), "losses": LEARN.get("losses", 0), "scorecard": LEARN.get("scorecard"), "sells": LEARN.get("sells"), "sell_soft": LEARN.get("sell_soft", [])}
    if full: save("export.json", out)
    write_dashboard(out)

def token_entry(r):
        res, dd = r["res"], r["dd"] or {}
        pr = res["buy_ratio"]
        return ({
            "symbol": res["symbol"], "name": (dd.get("facts") or {}).get("name") or "", "source": r["source"],
            "price": res["price"], "prices": "\n".join(f"{x:.8g}" for x in r["closes"]),
            "signal": res["signal"], "score": res["score"], "buy_zone": res["buy_zone"], "sell_zone": res["sell_zone"],
            "plan": res.get("plan"), "vnodes": res.get("vnodes"), "in_zone2": res.get("in_zone2"),
            "why": res["why"], "risk": dd.get("level"), "risk_flags": dd.get("flags", []), "checks": dd.get("checks", {}),
            "pressure": None if pr is None else (-2 if pr < .46 else -1 if pr < .49 else 0 if pr < .51 else 1 if pr < .54 else 2),
            "market_cap": (dd.get("facts") or {}).get("market_cap"),
            "markets": res.get("markets"), "parts": res["parts"], "rsi": res.get("rsi"), "cg_id": r.get("cg_id"),
            "links": token_links(res["symbol"], r.get("cg_id"), dd), "fdv": (dd.get("facts") or {}).get("fdv"),
            "volume_24h": (dd.get("facts") or {}).get("volume"), "is_buy": res["signal"] in BUY_SIGNALS, "in_zone_only": bool(is_buy(res) and res["signal"] not in BUY_SIGNALS), "starred": False, "logo": LOGOS.get(r.get("cg_id") or ""),
            "chart": {"c": [round(x, 10) for x in r["closes"]], "v": [round(x) for x in (r.get("vols") or [])]}})

def write_dashboard(out):
    """Phone-friendly dashboard with charts. Also published to docs/ so GitHub Pages can host it as a home-screen app."""
    data = dict(out); data["starred"] = sorted(PREFS.get("starred", []))
    data["tokens"] = [{k: v for k, v in t.items() if k != "prices"} for t in out["tokens"]]
    data["repo"] = os.environ.get("GITHUB_REPOSITORY", "")
    tok, _ = tg_creds()
    if tok and not DEMO:
        me = try_get("bot name", lambda: get_json(f"https://api.telegram.org/bot{tok}/getMe"))
        data["bot"] = ((me or {}).get("result") or {}).get("username")
    js = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    doc = DASH_HTML.replace("__DATA__", js)
    with open(os.path.join(DATA, "dashboard.html"), "w") as f: f.write(doc)
    if DEMO: return
    docs = os.path.join(HERE, "docs"); os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "index.html"), "w") as f: f.write(doc)
    with open(os.path.join(docs, "manifest.webmanifest"), "w") as f:
        json.dump({"name": "Token Watch", "short_name": "Token Watch", "start_url": ".", "display": "standalone",
                   "background_color": "#12171E", "theme_color": "#2F4B7C", "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}, f)
    icon = os.path.join(docs, "icon.png")
    if not os.path.exists(icon):
        with open(icon, "wb") as f: f.write(make_icon())

DASH_HTML = r'''<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes><meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content="Token Watch"><meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content="#0E1320">
<link rel=apple-touch-icon href=icon.png><link rel=manifest href=manifest.webmanifest>
<title>Token Watch</title>
<style>
:root{--bg:#0E1320;--bg2:#141B2B;--card:#182033;--card2:#1E2840;--ink:#E8EDF5;--mut:#8C98AE;--line:#27324A;
--up:#22C08A;--up-bg:rgba(34,192,138,.14);--dn:#F0605D;--dn-bg:rgba(240,96,93,.14);--warn:#F2B544;--acc:#6C8CFF;
--price:#E8EDF5;--s50:#F2B544;--s200:#B07CFF;--bb:rgba(108,140,255,.13);--bbl:rgba(108,140,255,.55);--rsi:#6C8CFF;--grid:#232D43}
@media(prefers-color-scheme:light){:root{--bg:#EEF1F6;--bg2:#E4E8F0;--card:#FFFFFF;--card2:#F4F6FA;--ink:#141A26;--mut:#5D6880;--line:#DCE1EA;
--up:#11946A;--up-bg:rgba(17,148,106,.11);--dn:#D6423F;--dn-bg:rgba(214,66,63,.10);--warn:#B97D0B;--acc:#3F5FE0;
--price:#141A26;--s50:#C98A10;--s200:#8B4FE0;--bb:rgba(63,95,224,.09);--bbl:rgba(63,95,224,.45);--rsi:#3F5FE0;--grid:#E6EAF1}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
padding:calc(env(safe-area-inset-top) + 14px) 14px calc(env(safe-area-inset-bottom) + 28px);max-width:760px;margin:0 auto}
a{color:inherit}small,.mut{color:var(--mut)}
header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
h1{font-size:1.5rem;margin:0;letter-spacing:-.02em}h1 span{color:var(--acc)}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:22px 2px 8px;font-weight:700}
.upd{font-size:.75rem;color:var(--mut);text-align:right}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px}
.kpi b{display:block;font-size:1.35rem;letter-spacing:-.02em}.kpi small{font-size:.74rem}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{background:var(--card);border:1px solid var(--line);border-radius:99px;padding:4px 10px;font-size:.78rem;color:var(--mut)}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:14px 0 4px;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}
.tab{border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:99px;padding:6px 13px;font:inherit;font-size:.85rem;white-space:nowrap;cursor:pointer}
.tab.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.tok{background:var(--card);border:1px solid var(--line);border-radius:16px;margin:10px 0;overflow:hidden}
.tok.buy{border-color:color-mix(in srgb,var(--up) 55%,var(--line));box-shadow:0 0 0 1px color-mix(in srgb,var(--up) 25%,transparent)}
.row{display:grid;grid-template-columns:38px minmax(0,1fr) 60px auto;align-items:center;gap:10px;padding:12px 12px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-weight:800;font-size:.8rem;color:#fff}
.logo{position:relative;overflow:hidden}
.logo.img{background:#0F1624;border-radius:50%}.logo .lgf{color:#8C98AE;font-weight:800;font-size:.72rem}
.logo img{position:absolute;inset:3px;width:calc(100% - 6px);height:calc(100% - 6px);object-fit:contain}
.nm b{font-size:1.02rem}.nm .sub{font-size:.78rem;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nm .px{font-weight:600;font-variant-numeric:tabular-nums}.chg{font-size:.78rem;font-weight:600;margin-left:6px}
.spark{width:60px;height:30px}
.rt{text-align:right}.pill{display:inline-block;border-radius:8px;padding:3px 7px;font-size:.68rem;font-weight:800;letter-spacing:.02em;white-space:nowrap}
.score{font-size:.74rem;color:var(--mut);margin-top:3px}
.badge.zone{color:var(--up)}
.badge{font-size:.66rem;border:1px solid var(--line);border-radius:6px;padding:0 5px;color:var(--mut);margin-left:4px;vertical-align:2px}
.body{display:none;padding:0 14px 14px;border-top:1px solid var(--line)}.tok.open .body{display:block}
.chev{transition:transform .2s;color:var(--mut)}.tok.open .chev{transform:rotate(180deg)}
.buybox{margin:14px 0 4px;background:var(--up-bg);border:1px solid color-mix(in srgb,var(--up) 40%,transparent);border-radius:14px;padding:12px}
.buybox h3{margin:0 0 8px;font-size:.95rem;color:var(--up)}
.infobox{margin:14px 0 4px;background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:12px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:.86rem}.kv span:nth-child(odd){color:var(--mut)}
.addr{display:flex;align-items:center;gap:8px;margin-top:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.addr code{font:12px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;flex:1}
.addr .ch{font-size:.68rem;font-weight:700;color:var(--mut);text-transform:uppercase;white-space:nowrap}
button.copy{border:0;background:var(--acc);color:#fff;border-radius:8px;padding:6px 10px;font:inherit;font-size:.78rem;font-weight:600;cursor:pointer}
.links{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.lbtn{display:flex;align-items:center;justify-content:center;gap:7px;text-decoration:none;border-radius:11px;padding:10px;font-weight:700;font-size:.88rem;border:1px solid var(--line);background:var(--card)}
.lbtn i{width:18px;height:18px;border-radius:50%;display:inline-block}
.ranges{display:flex;gap:4px;justify-content:flex-end;margin:12px 0 4px}
.rg{border:1px solid var(--line);background:transparent;color:var(--mut);border-radius:8px;padding:3px 9px;font:inherit;font-size:.75rem;cursor:pointer}.rg.on{background:var(--card2);color:var(--ink);font-weight:700}
.chart{position:relative}.chart svg{display:block;width:100%;touch-action:pan-y}
.tip{position:absolute;top:4px;left:8px;font-size:.74rem;background:color-mix(in srgb,var(--card) 88%,transparent);border:1px solid var(--line);border-radius:8px;padding:3px 7px;pointer-events:none;font-variant-numeric:tabular-nums;display:none}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:.72rem;color:var(--mut);margin:6px 0 2px}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px}
.legend i.box{height:9px;border-radius:2px}
.plabel{font-size:.7rem;font-weight:700;color:var(--mut);margin:10px 0 0;display:flex;justify-content:space-between}
.checks{list-style:none;padding:0;margin:8px 0 0}.checks li{display:flex;gap:9px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--line);font-size:.87rem}
.checks li:last-child{border:0}.dot{flex:0 0 20px;height:20px;border-radius:6px;display:grid;place-items:center;font-size:.72rem;font-weight:800}
.dot.g{background:var(--up-bg);color:var(--up)}.dot.r{background:var(--dn-bg);color:var(--dn)}.dot.n{background:var(--card2);color:var(--mut)}
.bars{display:grid;gap:7px;margin-top:8px}.bar{display:grid;grid-template-columns:92px 1fr 32px;gap:8px;align-items:center;font-size:.8rem}
.bar .tr{height:7px;border-radius:9px;background:var(--card2);overflow:hidden}.bar .fl{height:100%;border-radius:9px}
.sect{font-weight:700;font-size:.85rem;margin:16px 0 2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;font-size:.86rem}
td,th{padding:9px 8px;white-space:nowrap;border-bottom:1px solid var(--line);text-align:left;font-variant-numeric:tabular-nums}th{font-size:.72rem;color:var(--mut);font-weight:600}
tr:last-child td{border:0}
.foot{font-size:.74rem;color:var(--mut);margin-top:18px;text-align:center}
.addbox{margin:14px 0 0;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px}
.addrow{display:flex;gap:6px}.addrow input{flex:1;min-width:0;background:var(--card2);border:1px solid var(--line);border-radius:10px;color:var(--ink);font:inherit;font-size:16px;padding:8px 10px;text-transform:uppercase}.addrow input::placeholder{text-transform:none}
.abtn{border:0;background:var(--acc);color:#fff;border-radius:10px;padding:8px 12px;font:inherit;font-size:.88rem;font-weight:700;cursor:pointer;white-space:nowrap}
.abtn.alt{background:var(--card2);color:var(--ink);border:1px solid var(--line)}.addnote{font-size:.76rem;color:var(--mut);margin-top:6px}
.acts{display:flex;gap:8px;margin-top:10px}.acts a{flex:1;text-align:center;text-decoration:none;font-size:.82rem;font-weight:600;border:1px solid var(--line);border-radius:10px;padding:7px;background:var(--card)}
.plan{margin-top:10px;font-size:.84rem;line-height:1.45;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.mini{display:inline-block;font-size:.68rem;font-weight:700;text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin:3px 3px 0 0;background:var(--card2)}
.krow{grid-column:1/-1;font-size:.78rem;font-weight:700;color:var(--mut);margin:4px 2px -2px}
.calls td,.calls th{padding:8px 5px;font-size:.8rem}.crow{cursor:pointer}
.amt{display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;font-size:.85rem}
.amt input{width:90px;background:var(--card2);border:1px solid var(--line);border-radius:8px;color:var(--ink);font:inherit;font-size:16px;padding:5px 8px}
.k4{grid-template-columns:repeat(4,1fr);gap:6px}.k4 .kpi{padding:8px 9px}.k4 b{font-size:1.05rem}.k4 small{font-size:.68rem}
.coin{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:8px 0;overflow:hidden}
.ch{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;padding:10px 12px;cursor:pointer}.ch .logo{width:34px;height:34px;border-radius:10px;font-size:.72rem}
.coin .chev{display:inline-block}.coin.open .chev{transform:rotate(180deg)}
.pl{text-align:right;font-weight:800}.pl small{display:block;font-weight:500}
.tbody{display:none;border-top:1px solid var(--line);padding:8px 10px 12px}.coin.open .tbody{display:block}
.tg{display:inline-block;border-radius:6px;padding:1px 6px;font-size:.68rem;font-weight:800}.tg.b{background:var(--up-bg);color:var(--up)}.tg.s{background:var(--dn-bg);color:var(--dn)}.tg.o{background:rgba(108,140,255,.16);color:var(--acc)}
.why{font-size:.72rem;color:var(--mut);white-space:normal!important;padding-top:0!important}.res{font-weight:700;text-align:right}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.filters select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:6px 8px;font:inherit;font-size:.82rem}
.chk{display:flex;align-items:center;gap:5px;font-size:.8rem;color:var(--mut)}
.tapk{cursor:pointer}.tapk:active{opacity:.7}
.trade{border:1px solid var(--line);border-radius:10px;margin:6px 0;background:var(--card2);overflow:hidden}
.trade[open]{border-color:var(--acc)}
.trsum{list-style:none;position:relative;overflow:hidden;cursor:pointer}
.trsum::-webkit-details-marker{display:none}
.trslide{display:grid;grid-template-columns:auto auto 1fr auto;align-items:center;gap:6px;padding:8px 8px;background:var(--card2);position:relative;z-index:1;transition:transform .18s ease;touch-action:pan-y;will-change:transform}
.tractions{position:absolute;top:0;right:0;bottom:0;display:flex;z-index:0}
.swact{border:0;color:#fff;font:inherit;font-size:.78rem;font-weight:700;min-width:66px;cursor:pointer}
.swact[data-a=hide]{background:#5b6472}.swact.del{background:var(--dn)}
.trbtns{display:inline-flex;gap:4px}
.trbtn{border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:7px;padding:3px 6px;font:inherit;font-size:.64rem;font-weight:600;cursor:pointer;white-space:nowrap}
.trbtn.del{color:var(--dn)}.trbtn:active{opacity:.6}
.tg2{border-radius:6px;padding:1px 5px;font-size:.62rem;font-weight:800;white-space:nowrap}
.trwhen{color:var(--mut);font-size:.72rem;white-space:nowrap}.trres{font-weight:700;text-align:right;white-space:nowrap;font-size:.86rem}.trres small{font-size:.72rem}.trres small{font-weight:500;color:var(--mut)}
.trbody{padding:0 6px 8px}.trbody table{width:100%}
.acts2{display:flex;gap:6px;margin-top:6px;justify-content:flex-end}.mini2{border:1px solid var(--line);background:var(--card2);color:var(--mut);border-radius:7px;padding:1px 7px;font:inherit;font-size:.7rem;cursor:pointer}.mini2.del{color:var(--dn)}
tr.hid td{opacity:.55}
.rehint{font-size:.72rem;color:var(--mut);margin:2px 2px 8px}
.tok.dragging{opacity:.95;transform:scale(1.03);box-shadow:0 10px 26px rgba(0,0,0,.45);position:relative;z-index:5}
.reordering{cursor:grabbing;user-select:none}.reordering .tok:not(.dragging){transition:transform .12s ease}
.hdr{display:flex;align-items:center;gap:10px}
.refbtn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;width:38px;height:38px;font-size:1.2rem;cursor:pointer;line-height:1;display:grid;place-items:center}
.refbtn:active{background:var(--card2)}.refbtn.spin{animation:spin 1s linear infinite;color:var(--acc)}
@keyframes spin{to{transform:rotate(360deg)}}
.toast{position:fixed;left:50%;bottom:calc(env(safe-area-inset-bottom) + 20px);transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:10px;font-size:.85rem;opacity:0;transition:opacity .2s;pointer-events:none}
</style></head><body>
<header><div><h1>Token <span>Watch</span></h1></div><div class=hdr><button id=refresh class=refbtn title="Refresh data">↻</button><div class=upd id=upd></div></div></header>
<div class="kpis k4" id=kpis></div>
<div class=addbox><div class=addrow><input id=addin placeholder="Ticker, e.g. TAO" autocapitalize=characters autocomplete=off spellcheck=false maxlength=15>
<button class="abtn" id=addbtn>Add</button><button class="abtn alt" id=addstar>Add ⭐</button></div><div class=addnote id=addnote></div></div>
<div id=macro></div>
<div class=tabs id=tabs></div>
<div class=rehint>↕ Press and hold a coin to drag it into any order · <a href="#" id=forcechk style="display:none;color:var(--acc)">force a fresh check</a></div>
<div id=pending></div>
<div id=list></div>
<h2>Wallets you follow</h2><div id=wallets></div>
<h2>Coin directory</h2><div id=coindir></div>
<h2>Track record</h2><div id=calls></div>
<h2>What the model has learned</h2><div id=learn></div>
<p class=foot>Not financial advice. Signals describe the past and are often wrong. Always check the team and contract yourself.</p>
<div class=toast id=toast>Copied</div>
<script>
const D=__DATA__;
const $=s=>document.querySelector(s), el=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt=p=>p==null?"–":p>=1000?"$"+p.toLocaleString(undefined,{maximumFractionDigits:0}):p>=100?"$"+p.toFixed(2):p>=1e-4?"$"+String(Number(p.toPrecision(4))):"$"+p.toFixed(12).replace(/0+$/,"");
const big=n=>!n?"–":n>=1e9?"$"+(n/1e9).toFixed(2)+"B":n>=1e6?"$"+(n/1e6).toFixed(1)+"M":"$"+(n/1e3).toFixed(0)+"K";
const SIG={"STRONG BUY ZONE":["var(--up)","#fff"],"ACCUMULATE":["var(--up-bg)","var(--up)"],"HOLD":["rgba(242,181,68,.16)","var(--warn)"],
"TRIM":["var(--dn-bg)","var(--dn)"],"SELL / AVOID":["var(--dn)","#fff"],"AVOID (SCAM RISK)":["#7A1F1F","#fff"],"NO DATA":["var(--card2)","var(--mut)"]};
const SHORT={"STRONG BUY ZONE":"STRONG BUY","AVOID (SCAM RISK)":"SCAM RISK","SELL / AVOID":"SELL"};
const PART={wallets:"Smart wallets",technical:"Technicals",fundamental:"Fundamentals",flows:"Flows",derivatives:"Derivatives",macro:"Market",news:"News",markets:"Betting odds"};
function logoHTML(sym,url){const l=esc(sym.slice(0,4));return url?`<div class="logo img"><span class=lgf>${l}</span><img src="${esc(url)}" alt="" loading=lazy onerror="this.remove()"></div>`:`<div class=logo style="background:hsl(${hue(sym)} 55% 42%)">${l}</div>`}
const hue=s=>{let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))%360;return h};
// ---------- indicator math
const sma=(a,n)=>a.map((_,i)=>i<n-1?null:a.slice(i-n+1,i+1).reduce((x,y)=>x+y,0)/n);
const ema=(a,n)=>{const k=2/(n+1),o=[];let p=null;a.forEach((x,i)=>{if(x==null){o.push(null);return}if(p==null){if(i>=n-1){const w=a.slice(i-n+1,i+1);if(w.every(v=>v!=null)){p=w.reduce((s,v)=>s+v,0)/n}}o.push(p);return}p=x*k+p*(1-k);o.push(p)});return o};
function rsiS(c,n=14){const o=Array(c.length).fill(null);if(c.length<=n)return o;let g=0,l=0;for(let i=1;i<=n;i++){const d=c[i]-c[i-1];g+=Math.max(d,0);l+=Math.max(-d,0)}g/=n;l/=n;o[n]=l?100-100/(1+g/l):100;
for(let i=n+1;i<c.length;i++){const d=c[i]-c[i-1];g=(g*(n-1)+Math.max(d,0))/n;l=(l*(n-1)+Math.max(-d,0))/n;o[i]=l?100-100/(1+g/l):100}return o}
function macdS(c){const e12=ema(c,12),e26=ema(c,26),line=c.map((_,i)=>e12[i]!=null&&e26[i]!=null?e12[i]-e26[i]:null);const sig=ema(line,9);return{line,sig,hist:line.map((v,i)=>v!=null&&sig[i]!=null?v-sig[i]:null)}}
function bollS(c,n=20,k=2){const m=sma(c,n);return c.map((_,i)=>{if(m[i]==null)return null;const w=c.slice(i-n+1,i+1),sd=Math.sqrt(w.reduce((s,x)=>s+(x-m[i])**2,0)/n);return[m[i]-k*sd,m[i]+k*sd]})}
// ---------- header
const star=new Set(D.starred||[]);
D.tokens.forEach(t=>{t.starred=star.has(t.symbol);t.c=(t.chart&&t.chart.c&&t.chart.c.length)?t.chart.c:String(t.prices||"").split("\n").map(Number).filter(x=>x>0)});
$("#refresh").onclick=function(){const b=this;b.classList.add("spin");
 fetch(location.pathname+"?t="+Date.now(),{cache:"no-store"}).then(r=>r.text()).then(h=>{const m=h.match(/\"generated\":\"([^\"]+)\"/);
  if(m&&m[1]!==D.generated){location.reload();return;}
  b.classList.remove("spin");const x=$("#toast");x.textContent="Already showing the newest data";x.style.opacity=1;setTimeout(()=>{x.style.opacity=0;x.textContent="Copied";},2200);
  const f=$("#forcechk");if(f)f.style.display="";
 }).catch(()=>location.reload());};
(function(){const g=Date.parse(D.generated.replace(" UTC","Z").replace(" ","T")),upd=()=>{const m=Math.round((Date.now()-g)/60000);
$("#upd").innerHTML=`Updated<br><b style="color:${m>45?"var(--dn)":"inherit"}">${isNaN(m)?esc(D.generated):m<1?"just now":m<60?m+" min ago":Math.round(m/60)+" h ago"}</b>`+(m>45?"<br><small style=\"color:var(--dn)\">checks are delayed</small>":"")};upd();setInterval(upd,60000)})();
const calls=[...(D.calls||[])].sort((a,b)=>b.t-a.t),SELLS=D.sells||[];
// a buy call's trade closes at the next sell signal for that coin; otherwise it's open at today's price
const exitOf=c=>SELLS.filter(e=>e.symbol==c.symbol&&e.t>c.t).sort((a,b)=>a.t-b.t)[0]||null;
const tradeRet=c=>{const x=exitOf(c);return ((x?x.price:c.last)/c.entry-1)*100};
const rets=calls.map(tradeRet);
const buys=D.tokens.filter(t=>t.is_buy&&!/AVOID/.test(t.signal));
// two rows at the top: your starred coins, then coins the scanner found
const DELK=(()=>{try{return new Set(JSON.parse(localStorage.getItem("tw_deleted")||"[]"))}catch(e){return new Set()}})();
const pct=v=>v==null?"–":(v>=0?"+":"")+v.toFixed(1)+"%",col=v=>v==null?"inherit":v>=0?"var(--up)":"var(--dn)";
const krow=(title,toks,cl0)=>{const cl=cl0.filter(c=>!DELK.has(String(Math.floor(c.t)))),r=cl.map(tradeRet),avg=r.length?r.reduce((a,b)=>a+b,0)/r.length:null,nb=toks.filter(t=>t.is_buy&&!/AVOID/.test(t.signal)).length;
const op=cl.filter(c=>!exitOf(c)).map(c=>(c.last/c.entry-1)*100),oavg=op.length?op.reduce((a,b)=>a+b,0)/op.length:null;
return `<div class=krow>${title}</div><div class=kpi><b>${nb}</b><small>buy setups now</small></div><div class=kpi><b>${r.length?Math.round(r.filter(x=>x>0).length/r.length*100)+"%":"–"}</b><small>calls in profit${r.length?" ("+r.length+")":""}</small></div><div class=kpi><b style="color:${col(avg)}">${pct(avg)}</b><small>avg call return</small></div><div class=kpi><b style="color:${col(oavg)}">${pct(oavg)}</b><small>open calls now${op.length?" ("+op.length+")":""}</small></div>`};
$("#kpis").innerHTML=krow("⭐ Your starred coins",D.tokens.filter(t=>t.starred),calls.filter(c=>c.starred||star.has(c.symbol)))+
krow("🔎 Coins the scanner found",D.tokens.filter(t=>t.source=="discovery"),calls.filter(c=>c.source=="discovery"));
if((D.macro||{}).why&&D.macro.why.length){const m=$("#macro");m.innerHTML=`<h2>Market backdrop${D.macro.score!=null?" · "+Math.round(D.macro.score)+"/100":""}</h2>`;const ch=el("div","chips");D.macro.why.forEach(w=>ch.append(el("span","chip",esc(w))));m.append(ch)}
// ---------- add coins (opens your Telegram bot with the command ready; the next check picks it up)
const botURL=(act,sym)=>D.bot?`https://t.me/${D.bot}?start=${act}_${encodeURIComponent(sym)}`:null;
const ghURL=D.repo?`https://github.com/${D.repo}/edit/main/watchlist.txt`:null;
const formURL=D.repo?`https://github.com/${D.repo}/actions/workflows/add-coin.yml`:null;
const mobile=/iPhone|iPad|Android|Mobile/i.test(navigator.userAgent);
$("#addnote").innerHTML=mobile&&D.bot?"Opens your Telegram bot — tap <b>Start</b>. It's checked right away and shows here in a few minutes.":
 "Opens a short GitHub form: click <b>Run workflow</b>, check the ticker, click the green <b>Run workflow</b>. It shows here in about 3–5 minutes."+(D.bot?` On your phone this goes through Telegram instead.`:"");
const PK="tw_pending",getP=()=>{try{return JSON.parse(localStorage.getItem(PK)||"[]")}catch(e){return[]}},setP=v=>{try{localStorage.setItem(PK,JSON.stringify(v))}catch(e){}};
const have=new Set(D.tokens.map(t=>t.symbol));let PEND=getP().filter(p=>!have.has(p.sym)&&Date.now()-p.t<25*60000);setP(PEND);
function showPending(){const box=$("#pending");if(!box)return;box.innerHTML="";PEND.forEach(p=>{const m=Math.max(0,Math.round((Date.now()-p.t)/60000));
box.append(el("div","tok",`<div class=row style="grid-template-columns:38px 1fr"><div class=logo style="background:var(--card2);color:var(--mut)">⏳</div><div class=nm><b>${esc(p.sym)}</b><div class=sub style="white-space:normal">Analysis in progress… added ${m<1?"just now":m+" min ago"}. It appears here automatically — usually within 1–3 minutes${p.via=="form"?" after you click Run workflow on GitHub":""}.</div></div></div>`))})}
async function pollPending(){if(!PEND.length)return;try{const h=await (await fetch(location.pathname+"?t="+Date.now(),{cache:"no-store"})).text();
if(PEND.some(p=>h.includes(`"symbol":"${p.sym}"`)))location.reload()}catch(e){}showPending()}
setInterval(pollPending,20000);
function addCoin(star){const v=$("#addin").value.trim().toUpperCase().replace(/^\$/,"");if(!/^[A-Z0-9]{1,20}$/.test(v)){$("#addin").focus();return}
const cmd=(star?"/star ":"/add ")+v;PEND=PEND.filter(p=>p.sym!=v).concat([{sym:v,t:Date.now(),via:mobile&&D.bot?"tg":"form"}]);setP(PEND);setTimeout(showPending,50);try{navigator.clipboard&&navigator.clipboard.writeText(mobile?cmd:v).catch(()=>{})}catch(e){}
if(mobile&&D.bot){window.open(botURL(star?"STAR":"ADD",v),"_blank");$("#addnote").innerHTML=`Telegram opened — tap <b>Start</b>. If nothing was sent, paste <b>${cmd}</b> (already copied) into your bot.`}
else if(formURL){window.open(formURL,"_blank");$("#addnote").innerHTML=`On the GitHub page: click <b>Run workflow</b> → paste <b>${v}</b> (already copied)${star?" and tick <b>Star it</b>":""} → click the green <b>Run workflow</b>. ${v} shows here in about 3–5 minutes (refresh the page).`}
else $("#addnote").innerHTML=`Send <b>${cmd}</b> to your Telegram bot.`;$("#addin").value=""}
$("#addbtn").onclick=()=>addCoin(false);$("#addstar").onclick=()=>addCoin(true);$("#addin").addEventListener("keydown",e=>{if(e.key=="Enter")addCoin(false)});
// ---------- tabs
const TABS=[["all","All"],["buy","Buy setups"],["star","⭐ Starred"],["watch","Watchlist"],["found","Found by scanner"]];let cur="all";
const tabs=$("#tabs");TABS.forEach(([k,n])=>{const b=el("button","tab"+(k==cur?" on":""),n);b.onclick=()=>{cur=k;[...tabs.children].forEach(x=>x.classList.toggle("on",x==b));render()};tabs.append(b)});
const pass=t=>cur=="all"||(cur=="buy"&&t.is_buy&&!/AVOID/.test(t.signal))||(cur=="star"&&t.starred)||(cur=="watch"&&t.source=="watchlist")||(cur=="found"&&t.source!="watchlist");
// ---------- sparkline
function spark(c){const a=c.slice(-30),mn=Math.min(...a),mx=Math.max(...a),W=60,H=30,up=a[a.length-1]>=a[0];
const pts=a.map((v,i)=>[i/(a.length-1)*W,H-3-(v-mn)/((mx-mn)||1)*(H-6)]);const d=pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join("");
const col=up?"var(--up)":"var(--dn)";return `<svg class=spark viewBox="0 0 ${W} ${H}"><path d="${d} L${W} ${H} L0 ${H}Z" fill="${col}" opacity=".12"/><path d="${d}" fill=none stroke="${col}" stroke-width="1.6" stroke-linejoin=round/></svg>`}
// ---------- list
let ORDER=[];try{ORDER=JSON.parse(localStorage.getItem("tw_order")||"[]")}catch(e){}
const oIdx=sym=>{const i=ORDER.indexOf(sym);return i<0?1e6:i};
function render(){const L=$("#list");L.innerHTML="";const toks=D.tokens.filter(pass).sort((a,b)=>{const ia=oIdx(a.symbol),ib=oIdx(b.symbol);return ia!=ib?ia-ib:(b.starred-a.starred)||(b.is_buy-a.is_buy)||((b.score||0)-(a.score||0));});
if(!toks.length){L.innerHTML="<p class=mut>Nothing here yet.</p>";return}toks.forEach(t=>L.append(card(t)));enableReorder(L)}
function enableReorder(L){const cards=()=>[...L.querySelectorAll(".tok")];
 cards().forEach(w=>{const row=w.querySelector(".row");let timer=null,sy=0;
  const cancel=()=>{if(timer){clearTimeout(timer);timer=null;}};
  row.addEventListener("pointerdown",e=>{if(e.target.closest("a,button"))return;sy=e.clientY;timer=setTimeout(()=>{timer=null;startDrag(L,w,e);},420);});
  row.addEventListener("pointermove",e=>{if(timer&&Math.abs(e.clientY-sy)>10)cancel();});
  row.addEventListener("pointerup",cancel);row.addEventListener("pointercancel",cancel);});}
function startDrag(L,w,e){w.classList.add("dragging");L.classList.add("reordering");w.dataset.drag="1";if(navigator.vibrate)navigator.vibrate(15);
 const move=ev=>{ev.preventDefault();const y=ev.clientY,others=[...L.querySelectorAll(".tok")].filter(x=>x!=w);let placed=false;
  for(const o of others){const r=o.getBoundingClientRect();if(y<r.top+r.height/2){L.insertBefore(w,o);placed=true;break;}}
  if(!placed)L.appendChild(w);};
 const up=()=>{document.removeEventListener("pointermove",move);document.removeEventListener("pointerup",up);
  w.classList.remove("dragging");L.classList.remove("reordering");
  const seq=[...L.querySelectorAll(".tok")].map(x=>x.dataset.sym);ORDER=seq.concat(ORDER.filter(x=>!seq.includes(x)));
  try{localStorage.setItem("tw_order",JSON.stringify(ORDER))}catch(_){}
  setTimeout(()=>{delete w.dataset.drag;render();},20);};
 document.addEventListener("pointermove",move,{passive:false});document.addEventListener("pointerup",up);}
function card(t){const c=t.c,chg=c.length>1?(c[c.length-1]/c[c.length-2]-1)*100:0,[bg,fg]=SIG[t.signal]||SIG["NO DATA"];
const w=el("div","tok"+(t.is_buy&&!/AVOID/.test(t.signal)?" buy":""));w.dataset.sym=t.symbol;
const row=el("div","row",`${logoHTML(t.symbol,t.logo)}
<div class=nm><div><b>${t.starred?"⭐ ":""}${esc(t.symbol)}</b>${t.source=="discovery"?"<span class=badge>found</span>":""}${t.in_zone_only?"<span class='badge zone'>in buy zone</span>":""}</div>
<div><span class=px>${fmt(t.price)}</span><span class=chg style="color:${chg>=0?"var(--up)":"var(--dn)"}">${chg>=0?"+":""}${chg.toFixed(1)}%</span></div>
<div class=sub>${esc(t.name||"")}</div></div>${spark(c)}
<div class=rt><span class=pill style="background:${bg};color:${fg}">${esc(SHORT[t.signal]||t.signal)}</span><div class=score>${t.score!=null?Math.round(t.score)+"/100":""} <span class=chev>▾</span></div></div>`);
const body=el("div","body");let built=false;
row.onclick=()=>{if(w.dataset.drag)return;w.classList.toggle("open");if(!built){built=true;fill(body,t)}};w.append(row,body);return w}
function copy(txt){(navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).catch(()=>{const a=el("textarea");a.value=txt;document.body.append(a);a.select();document.execCommand("copy");a.remove()}).finally(()=>{const x=$("#toast");x.style.opacity=1;setTimeout(()=>x.style.opacity=0,1100)})}
function fill(b,t){const Lk=t.links||{},isB=t.is_buy&&!/AVOID/.test(t.signal),bz=t.buy_zone,sz=t.sell_zone;
const box=el("div",isB?"buybox":"infobox");
box.innerHTML=(isB?`<h3>Suggested buy · ${esc(t.symbol)}</h3>`:`<div class=sect style="margin:0 0 8px">Token info</div>`)+
`<div class=kv><span>Ticker</span><b>${esc(t.symbol)}${t.name?" · "+esc(t.name):""}</b><span>CoinGecko ID</span><b style="font-family:ui-monospace,Menlo,monospace;font-size:.82rem">${esc(t.cg_id||"–")}</b>
<span>Price</span><b>${fmt(t.price)}</b><span>Buy zone ${(t.plan||{}).zone2?"1":""}</span><b>${bz?fmt(bz.low)+" – "+fmt(bz.high):"n/a"}${(t.plan||{}).zone1&&t.plan.zone1.strong?" 💪":""}</b>
${(t.plan||{}).zone2?`<span>Buy zone 2</span><b>${fmt(t.plan.zone2.low)+" – "+fmt(t.plan.zone2.high)}${t.plan.zone2.strong?" 💪":""}</b>`:""}
${(t.plan||{}).stop?`<span>Stop-loss</span><b style="color:var(--dn)">below ${fmt(t.plan.stop)}</b>`:""}
${(t.plan||{}).target?`<span>Take profit</span><b style="color:var(--up)">near ${fmt(t.plan.target)}</b>`:""}
<span>Sell zone</span><b>${sz?fmt(sz.low)+" – "+fmt(sz.high):"n/a"}</b><span>Market cap</span><b>${big(t.market_cap)}</b><span>Scam risk</span><b style="color:${t.risk=="LOW"?"var(--up)":t.risk=="HIGH"?"var(--dn)":"var(--warn)"}">${esc(t.risk||"?")}</b></div>`;
const TP=t.plan||{};if(TP.zone1){box.append(el("div","plan",`<b>Plan:</b> buy ${TP.zone2?"½":"your position"} in zone 1 (${fmt(TP.zone1.low)}–${fmt(TP.zone1.high)})${TP.zone2?`, ½ in zone 2 (${fmt(TP.zone2.low)}–${fmt(TP.zone2.high)})`:""}${TP.stop?`; exit if it closes below ${fmt(TP.stop)}`:""}${TP.target?`; take profit near ${fmt(TP.target)}`:""}. <span class=mut>💪 = heavy trading happened at that price (stronger support).</span>`))}
const cs=(Lk.contracts&&Lk.contracts.length)?Lk.contracts:[];
if(cs.length)cs.forEach(x=>{const a=el("div","addr",`<span class=ch>${esc(x.chain.replace(/-/g," "))}</span><code>${esc(x.address)}</code>`);const bt=el("button","copy","Copy");bt.onclick=e=>{e.stopPropagation();copy(x.address)};a.append(bt);box.append(a)});
else box.append(el("div","addr",`<span class=ch>Contract</span><code>None — native coin of its own chain</code>`));
const ln=el("div","links",`<a class=lbtn target=_blank rel=noopener href="${esc(Lk.coingecko||"https://www.coingecko.com/en/search?query="+t.symbol)}"><i style="background:#8DC63F"></i>CoinGecko</a>
<a class=lbtn target=_blank rel=noopener href="${esc(Lk.dexscreener||"https://dexscreener.com/search?q="+t.symbol)}"><i style="background:linear-gradient(135deg,#222,#777)"></i>DexScreener</a>`);box.append(ln);b.append(box);
if(D.bot){const a=el("div","acts",`<a href="${botURL(t.starred?"UNSTAR":"STAR",t.symbol)}">${t.starred?"☆ Unstar":"⭐ Star"}</a><a href="${botURL("CHECK",t.symbol)}">↻ Fresh check</a><a href="${botURL("REMOVE",t.symbol)}" style="color:var(--dn)">Remove</a>`);b.append(a)}
// chart
const rg=el("div","ranges");const holder=el("div");let range=180;[["3M",90],["6M",180],["1Y",365]].forEach(([n,d])=>{const x=el("button","rg"+(d==range?" on":""),n);x.onclick=()=>{range=d;[...rg.children].forEach(y=>y.classList.toggle("on",y==x));draw(holder,t,range)};rg.append(x)});
b.append(rg,holder);draw(holder,t,range);
// signals checklist
b.append(el("div","sect","What the signal is based on"));const ul=el("ul","checks");
(t.why||[]).forEach(w=>{const bad=/overbought|bearish|Below|fading|sell zone|Broke|leaving|Under|crowded, squeeze-down|money leaving|upper Bollinger|High scam/i.test(w),good=/oversold|bullish|Above|improving|Inside buy|accumulat|Outperform|squeeze-up|flowing in|lower Bollinger|confirming/i.test(w);
ul.append(el("li","",`<span class="dot ${bad?"r":good?"g":"n"}">${bad?"–":good?"+":"•"}</span><span>${esc(w)}</span>`))});b.append(ul);
// breakdown
const P=t.parts||{},ks=Object.keys(PART).filter(k=>P[k]!=null);if(ks.length){b.append(el("div","sect","Score breakdown"));const bs=el("div","bars");
ks.forEach(k=>{const v=Math.round(P[k]),col=v>=60?"var(--up)":v>=45?"var(--warn)":"var(--dn)";bs.append(el("div","bar",`<span class=mut>${PART[k]}</span><div class=tr><div class=fl style="width:${v}%;background:${col}"></div></div><b>${v}</b>`))});b.append(bs)}
const mk=((t.markets||{}).lines)||[];if(mk.length){b.append(el("div","sect","Betting markets"));const u=el("ul","checks");mk.slice(0,4).forEach(l=>u.append(el("li","",`<span class="dot n">%</span><span>${esc(l)}</span>`)));b.append(u)}
if((t.risk_flags||[]).length){b.append(el("div","sect","Scam-check flags"));const u=el("ul","checks");t.risk_flags.slice(0,6).forEach(f=>u.append(el("li","",`<span class="dot r">!</span><span>${esc(f)}</span>`)));b.append(u)}}
// ---------- the chart
function draw(h,t,range){h.innerHTML="";const all=t.c,n=all.length,st=Math.max(0,n-range);
const c=all.slice(st),s50=sma(all,50).slice(st),s200=sma(all,200).slice(st),bb=bollS(all).slice(st),rs=rsiS(all).slice(st),M=macdS(all),mh=M.hist.slice(st),ml=M.line.slice(st),msg=M.sig.slice(st);
const vol=((t.chart||{}).v||[]).slice(-n).slice(st);
const W=Math.max(300,h.clientWidth||340),PH=210,RH=74,MH=70,gap=16,H=PH+RH+MH+gap*2+18,padR=46,pw=W-padR,N=c.length;
const x=i=>i/(N-1)*pw;
let lo=Math.min(...c),hi=Math.max(...c);bb.forEach(v=>{if(v){lo=Math.min(lo,v[0]);hi=Math.max(hi,v[1])}});
[t.buy_zone,t.sell_zone,(t.plan||{}).zone2].forEach(z=>{if(z){lo=Math.min(lo,z.low);hi=Math.max(hi,z.high)}});if((t.plan||{}).stop)lo=Math.min(lo,t.plan.stop*.99);const pad=(hi-lo)*.06;lo-=pad;hi+=pad;
const y=v=>8+(hi-v)/(hi-lo)*(PH-16);
const path=(a,f)=>{let d="",on=false;a.forEach((v,i)=>{if(v==null){on=false;return}d+=(on?"L":"M")+x(i).toFixed(1)+" "+f(v).toFixed(1);on=true});return d};
let s=`<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
// grid + y labels
for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4,yy=y(v);s+=`<line x1=0 x2=${pw} y1=${yy} y2=${yy} stroke="var(--grid)"/><text x=${pw+5} y=${yy+3} font-size=9.5 fill="var(--mut)">${fmt(v).replace("$","")}</text>`}
// zones
const band=(z,col,lab)=>{if(!z)return"";const a=y(Math.min(z.high,hi)),b2=y(Math.max(z.low,lo));return `<rect x=0 y=${a} width=${pw} height=${Math.max(2,b2-a)} fill="${col}"/><text x=6 y=${a+11} font-size=9.5 font-weight=700 fill="${col.replace(/,[.\d]+\)$/,",1)")}">${lab}</text>`};
const PL=t.plan||{};s+=band(t.sell_zone,"rgba(240,96,93,.16)","SELL ZONE")+band(t.buy_zone,"rgba(34,192,138,.18)",PL.zone2?"BUY ZONE 1":"BUY ZONE")+(PL.zone2?band(PL.zone2,"rgba(34,192,138,.10)","BUY ZONE 2"):"");
// volume-by-price bars (right side) - where the most coins changed hands
(t.vnodes||[]).forEach(n=>{if(n.price<lo||n.price>hi)return;const yy=y(n.price),w=Math.min(pw*.28,pw*.07*n.strength);s+=`<rect x=${pw-w} y=${yy-3} width=${w} height=6 rx=2 fill="var(--warn)" opacity=.35><title>Heavy trading near ${fmt(n.price)}</title></rect>`});
const hl=(v,col,lab,dash)=>{if(!v||v<lo||v>hi)return"";const yy=y(v);return `<line x1=0 x2=${pw} y1=${yy} y2=${yy} stroke="${col}" stroke-width=1.2 stroke-dasharray="${dash}"/><text x=${pw-4} y=${yy-3} text-anchor=end font-size=9 font-weight=700 fill="${col}">${lab} ${fmt(v)}</text>`};
if(PL.stop){lo=Math.min(lo,PL.stop*0.98)}
// bollinger
const up=bb.map(v=>v&&v[1]),dn=bb.map(v=>v&&v[0]);const f0=bb.findIndex(v=>v);
if(f0>=0){let d="M"+x(f0)+" "+y(up[f0]);for(let i=f0+1;i<N;i++)d+="L"+x(i).toFixed(1)+" "+y(up[i]).toFixed(1);for(let i=N-1;i>=f0;i--)d+="L"+x(i).toFixed(1)+" "+y(dn[i]).toFixed(1);s+=`<path d="${d}Z" fill="var(--bb)"/>`;
s+=`<path d="${path(up,y)}" fill=none stroke="var(--bbl)" stroke-width=.8 stroke-dasharray="3 3"/><path d="${path(dn,y)}" fill=none stroke="var(--bbl)" stroke-width=.8 stroke-dasharray="3 3"/>`}
// volume
if(vol.length==N){const vm=Math.max(...vol)||1;vol.forEach((v,i)=>{const hh=v/vm*38;s+=`<rect x=${(x(i)-pw/N*.4).toFixed(1)} y=${PH-hh} width=${Math.max(.8,pw/N*.8).toFixed(1)} height=${hh} fill="${i&&c[i]>=c[i-1]?"var(--up)":"var(--dn)"}" opacity=.18 />`})}
// averages + price
s+=`<path d="${path(s200,y)}" fill=none stroke="var(--s200)" stroke-width=1.3 /><path d="${path(s50,y)}" fill=none stroke="var(--s50)" stroke-width=1.3 />`;
const pd=path(c,y);s+=`<path d="${pd} L${x(N-1)} ${PH} L0 ${PH}Z" fill="var(--acc)" opacity=".07"/><path d="${pd}" fill=none stroke="var(--price)" stroke-width=1.9 stroke-linejoin=round />`;
// overbought/oversold markers on price
rs.forEach((r,i)=>{if(r==null)return;if(r>70)s+=`<circle cx=${x(i)} cy=${y(c[i])} r=2.3 fill="var(--dn)"/>`;else if(r<30)s+=`<circle cx=${x(i)} cy=${y(c[i])} r=2.3 fill="var(--up)"/>`});
// buy calls
const t0=Date.now()/1000-(N-1)*86400;(D.calls||[]).filter(k=>k.symbol==t.symbol).forEach(k=>{const i=Math.round((k.t-t0)/86400);if(i<0||i>=N)return;const xx=x(i),yy=y(k.entry)+14;
s+=`<path d="M${xx} ${yy-9} l6 9 h-12z" fill="var(--up)" stroke="var(--card)" stroke-width=1.2><title>Buy call ${esc(k.date)} at ${fmt(k.entry)}</title></path>`});
s+=hl(PL.stop,"var(--dn)","STOP","5 3")+hl(PL.target,"var(--up)","TARGET","2 3");
// last price tag
const ly=y(c[N-1]);s+=`<rect x=${pw+1} y=${ly-8} width=${padR-2} height=16 rx=4 fill="var(--acc)"/><text x=${pw+5} y=${ly+4} font-size=10 font-weight=700 fill="#fff">${fmt(c[N-1]).replace("$","")}</text>`;
// RSI panel
const r0=PH+gap,ry=v=>r0+(100-v)/100*RH;
s+=`<text x=0 y=${r0-4} font-size=9.5 font-weight=700 fill="var(--mut)">RSI 14 ${rs[N-1]!=null?"· "+Math.round(rs[N-1])+(rs[N-1]>70?" OVERBOUGHT":rs[N-1]<30?" OVERSOLD":""):""}</text>`;
s+=`<rect x=0 y=${ry(100)} width=${pw} height=${ry(70)-ry(100)} fill="rgba(240,96,93,.12)"/><rect x=0 y=${ry(30)} width=${pw} height=${ry(0)-ry(30)} fill="rgba(34,192,138,.12)"/>`;
[30,50,70].forEach(v=>s+=`<line x1=0 x2=${pw} y1=${ry(v)} y2=${ry(v)} stroke="var(--grid)" ${v==50?'stroke-dasharray="2 3"':""}/><text x=${pw+5} y=${ry(v)+3} font-size=9 fill="var(--mut)">${v}</text>`);
s+=`<path d="${path(rs,ry)}" fill=none stroke="var(--rsi)" stroke-width=1.5 />`;
// MACD panel
const m0=r0+RH+gap+10;let mm=0;[...mh,...ml,...msg].forEach(v=>{if(v!=null)mm=Math.max(mm,Math.abs(v))});mm=mm||1;const my=v=>m0+MH/2-v/mm*(MH/2-2);
s+=`<text x=0 y=${m0-4} font-size=9.5 font-weight=700 fill="var(--mut)">MACD 12/26/9</text><line x1=0 x2=${pw} y1=${my(0)} y2=${my(0)} stroke="var(--grid)"/>`;
mh.forEach((v,i)=>{if(v==null)return;const a=my(Math.max(v,0)),b2=my(Math.min(v,0));s+=`<rect x=${(x(i)-pw/N*.4).toFixed(1)} y=${a.toFixed(1)} width=${Math.max(.8,pw/N*.8).toFixed(1)} height=${Math.max(.5,b2-a).toFixed(1)} fill="${v>=0?"var(--up)":"var(--dn)"}" opacity=.55 />`});
s+=`<path d="${path(ml,my)}" fill=none stroke="var(--acc)" stroke-width=1.3 /><path d="${path(msg,my)}" fill=none stroke="var(--s50)" stroke-width=1.1 />`;
// crosshair
s+=`<line id=cx x1=0 x2=0 y1=0 y2=${H} stroke="var(--mut)" stroke-width=.8 stroke-dasharray="2 2" opacity=0 /><rect id=hit x=0 y=0 width=${pw} height=${H} fill=transparent /></svg>`;
const box=el("div","chart",s);const tip=el("div","tip");box.append(tip);h.append(box);
h.append(el("div","legend",`<span><i style="background:var(--price)"></i>Price</span><span><i style="background:var(--s50)"></i>50-day avg</span><span><i style="background:var(--s200)"></i>200-day avg</span>
<span><i class=box style="background:var(--bb);border:1px dashed var(--bbl)"></i>Bollinger bands</span><span><i class=box style="background:rgba(34,192,138,.35)"></i>Buy zone 1</span><span><i class=box style="background:rgba(34,192,138,.18)"></i>Buy zone 2</span><span><i style="background:var(--dn)"></i>Stop</span><span><i style="background:var(--up)"></i>Target</span><span><i class=box style="background:var(--warn);opacity:.5"></i>Heavy volume</span><span><i class=box style="background:rgba(240,96,93,.3)"></i>Sell zone</span>
<span><i class=box style="background:var(--dn);width:7px;height:7px;border-radius:50%"></i>RSI overbought</span><span><i class=box style="background:var(--up);width:7px;height:7px;border-radius:50%"></i>RSI oversold</span><span>▲ Buy call</span>`));
const sv=box.querySelector("svg"),cxl=sv.querySelector("#cx");
const mv=e=>{const r=sv.getBoundingClientRect(),p=(e.touches?e.touches[0]:e),px=(p.clientX-r.left)/r.width*W;if(px<0||px>pw)return;const i=Math.max(0,Math.min(N-1,Math.round(px/pw*(N-1))));
cxl.setAttribute("x1",x(i));cxl.setAttribute("x2",x(i));cxl.setAttribute("opacity",1);const dd=new Date((t0+i*86400)*1000);
tip.style.display="block";tip.innerHTML=`<b>${dd.toLocaleDateString(undefined,{month:"short",day:"numeric"})}</b> ${fmt(c[i])}${rs[i]!=null?" · RSI "+Math.round(rs[i]):""}${s50[i]?" · 50d "+fmt(s50[i]):""}`};
const out=()=>{cxl.setAttribute("opacity",0);tip.style.display="none"};
sv.addEventListener("mousemove",mv);sv.addEventListener("touchmove",mv,{passive:true});sv.addEventListener("touchstart",mv,{passive:true});sv.addEventListener("mouseleave",out);sv.addEventListener("touchend",()=>setTimeout(out,1500))}
// ---------- track record: every buy call and sell signal, per coin, with profit
(function(){const C=$("#calls"),tk=Object.fromEntries(D.tokens.map(t=>[t.symbol,t]));let AMT=100;try{AMT=+localStorage.getItem("tw_amt")||100}catch(e){}
const money=v=>(v>=0?"+$":"−$")+Math.abs(v).toFixed(2);
const when=t=>{const d=new Date(t*1000);return[d.toLocaleDateString(undefined,{month:"2-digit",day:"2-digit"}),d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"})]};
const OUT={win:"WIN",loss:"LOSS",flat:"FLAT"};
// group buy calls into coins; each call = one trade
const coins={};calls.slice().sort((a,b)=>a.t-b.t).forEach(c=>{const k=c.symbol;(coins[k]=coins[k]||{sym:k,calls:[],star:false,found:false,links:null}).calls.push(c);
 const g=coins[k];g.star=g.star||!!c.starred||star.has(k);g.found=g.found||c.source=="discovery";g.links=g.links||c.links||(tk[k]||{}).links;g.name=g.name||(tk[k]||{}).name||""});
let F={coin:"",range:"all",status:"all",hidden:false};try{F=Object.assign(F,JSON.parse(localStorage.getItem("tw_f")||"{}"))}catch(e){}
const getSet=k=>{try{return new Set(JSON.parse(localStorage.getItem(k)||"[]"))}catch(e){return new Set()}},putSet=(k,v)=>{try{localStorage.setItem(k,JSON.stringify([...v]))}catch(e){}};
const HID=getSet("tw_hidden"),DEL=getSet("tw_deleted"),ids=new Set(calls.map(c=>String(Math.floor(c.t))));[...DEL].forEach(i=>{if(!ids.has(i))DEL.delete(i)});putSet("tw_deleted",DEL);
const idOf=c=>String(Math.floor(c.t)),DAYS={today:0,"7d":7,"30d":30,"90d":90};
const keep=c=>{const id=idOf(c);if(DEL.has(id))return false;if(HID.has(id)&&!F.hidden)return false;if(F.coin&&c.symbol!=F.coin)return false;
 if(F.range!="all"){const since=F.range=="today"?new Date().setHours(0,0,0,0)/1000:Date.now()/1000-DAYS[F.range]*86400;if(c.t<since)return false}
 if(F.status!="all"){const x=exitOf(c),r=tradeRet(c);if(F.status=="open"&&x)return false;if(F.status=="closed"&&!x)return false;if(F.status=="win"&&!(x&&r>0))return false;if(F.status=="loss"&&!(x&&r<=0))return false}return true};
const trades=g=>g.calls.filter(keep).map(c=>{const x=exitOf(c);return{c,x,exit:x?x.price:c.last,r:((x?x.price:c.last)/c.entry-1)}});
C.innerHTML=`<div class=amt>If you'd put $<input id=amt type=number min=1 inputmode=decimal value="${AMT}"> into every buy call…</div><div id=tsum></div><div class=tabs id=ttabs></div><div class=filters id=tfil></div><div id=tlist></div>
<p class=mut style="font-size:.74rem;margin-top:10px">A trade opens at a buy call and closes at the next sell signal for that coin (signal turns TRIM or SELL, price enters the sell zone, or falls below the stop-loss). Trades with no sell signal yet are <b>OPEN</b> at today's price. Fees and slippage aren't included. Times are in your time zone.</p>`;
const OPENC=new Set();let cur="all";const tt=$("#ttabs");[["all","All"],["star","⭐ Your coins"],["found","🔎 Scanner found"]].forEach(([k,n])=>{const b=el("button","tab"+(k==cur?" on":""),n);b.onclick=()=>{cur=k;[...tt.children].forEach(x=>x.classList.toggle("on",x==b));draw()};tt.append(b)});
function stat(gs){let pl=0,n=0,w=0,op=0;gs.forEach(g=>trades(g).forEach(t=>{pl+=AMT*t.r;n++;if(t.x){if(t.r>0)w++}else op++}));return{pl,n,w,op,inv:n*AMT,closed:n-op}}
function srow(title,gs){const s=stat(gs);return `<div class=krow>${title}</div><div class="kpis k4"><div class=kpi><b style="color:${s.pl>=0?"var(--up)":"var(--dn)"}">${s.n?money(s.pl):"–"}</b><small>total profit</small></div><div class=kpi><b>${s.inv?(s.pl/s.inv*100).toFixed(1)+"%":"–"}</b><small>return on $${s.inv.toLocaleString()}</small></div><div class="kpi tapk" data-fs=closed><b>${s.closed?Math.round(s.w/s.closed*100)+"%":"–"}</b><small>closed trades won (${s.closed}) ›</small></div><div class="kpi tapk" data-fs=open><b>${s.op}</b><small>open trades ›</small></div></div>`}
const fil=$("#tfil");fil.innerHTML=`<select id=fcoin><option value="">All coins</option>${Object.keys(coins).sort().map(k=>`<option ${F.coin==k?"selected":""}>${esc(k)}</option>`).join("")}</select>
<select id=frange>${[["all","Any date"],["today","Today"],["7d","Last 7 days"],["30d","Last 30 days"],["90d","Last 90 days"]].map(([k,n])=>`<option value=${k} ${F.range==k?"selected":""}>${n}</option>`).join("")}</select>
<select id=fstat>${[["all","All trades"],["open","Open"],["closed","Closed"],["win","Closed in profit"],["loss","Closed at a loss"]].map(([k,n])=>`<option value=${k} ${F.status==k?"selected":""}>${n}</option>`).join("")}</select>
<label class=chk><input type=checkbox id=fhid ${F.hidden?"checked":""}> Show hidden (${HID.size})</label>`;
const setF=()=>{F={coin:$("#fcoin").value,range:$("#frange").value,status:$("#fstat").value,hidden:$("#fhid").checked};try{localStorage.setItem("tw_f",JSON.stringify(F))}catch(e){}draw()};
["#fcoin","#frange","#fstat","#fhid"].forEach(q=>$(q).onchange=setF);
function act(e){e.stopPropagation();e.preventDefault();const b=e.currentTarget,id=b.dataset.id,sym=b.dataset.sym;
 if(b.dataset.a=="hide"){HID.has(id)?HID.delete(id):HID.add(id);putSet("tw_hidden",HID);$("#fhid").parentNode.lastChild.textContent=` Show hidden (${HID.size})`;draw();return}
 if(!confirm(`Delete this ${sym} trade permanently from your track record? (It's removed for everyone and from the model's learning.)`))return;
 DEL.add(id);putSet("tw_deleted",DEL);draw();const mob=/iPhone|iPad|Android|Mobile/i.test(navigator.userAgent);
 if(mob&&D.bot)window.open(`https://t.me/${D.bot}?start=DELTRADE_${id}`,"_blank");
 else if(D.repo){try{navigator.clipboard&&navigator.clipboard.writeText(id).catch(()=>{})}catch(e){}window.open(`https://github.com/${D.repo}/actions/workflows/delete-trade.yml`,"_blank");
  alert(`On the GitHub page: click "Run workflow", paste the trade ID ${id} (already copied), then click the green "Run workflow". It's hidden here already.`)}}
function draw(){const Gall=Object.values(coins);$("#tsum").innerHTML=srow("⭐ Your coins",Gall.filter(g=>!g.found))+srow("🔎 Coins the scanner found",Gall.filter(g=>g.found));
$("#tsum").querySelectorAll(".tapk").forEach(k=>k.onclick=()=>{$("#fstat").value=k.dataset.fs;setF();$("#tfil").scrollIntoView({behavior:"smooth",block:"start"})});
const G=Object.values(coins).filter(g=>trades(g).length);
const L=$("#tlist");L.innerHTML="";const list=G.filter(g=>cur=="all"||(cur=="star"&&!g.found)||(cur=="found"&&g.found)).sort((a,b)=>Math.max(...b.calls.map(c=>c.t))-Math.max(...a.calls.map(c=>c.t)));
if(!list.length){L.append(el("p","mut",calls.length?"No trades match these filters.":"No buy calls here yet. Every buy call and the sell signal that closes it will show up here."));return}
list.forEach(g=>{const T=trades(g);let pl=0;T.forEach(t=>pl+=AMT*t.r);const nopen=T.filter(t=>!t.x).length,Lk=g.links||{};
const w=el("div","coin");const qf=q=>q>=1000?Math.round(q).toLocaleString():q>=1?q.toFixed(2):Number(q.toPrecision(3));
// each trade is its own expandable block: tap the summary line to see the full buy/sell detail
const blocks=T.slice().sort((a,b)=>b.c.t-a.c.t).map(t=>{const c=t.c,[d1,t1]=when(c.t),pm=c.postmortem,col=t.r>=0?"var(--up)":"var(--dn)";
 const status=t.x?(t.r>0?"WIN":"LOSS"):"OPEN",sbg=status=="WIN"?"tg b":status=="LOSS"?"tg s":"tg o";
 const sum=`<summary class=trsum><div class=tractions><button class=swact data-a=hide data-id="${idOf(c)}" data-sym="${esc(g.sym)}">${HID.has(idOf(c))?"Unhide":"Hide"}</button><button class="swact del" data-a=del data-id="${idOf(c)}" data-sym="${esc(g.sym)}">Delete</button></div><div class=trslide><span class="tg2 ${sbg}">${status}</span><span class=trwhen>${d1} → ${t.x?when(t.x.t)[0]:"now"}</span><span class=trres style="color:${col}">${t.r>=0?"+":""}${(t.r*100).toFixed(1)}%<small> ${money(AMT*t.r)}</small></span><span class=trbtns><button class=trbtn data-a=hide data-id="${idOf(c)}" data-sym="${esc(g.sym)}">${HID.has(idOf(c))?"Unhide":"Hide"}</button><button class="trbtn del" data-a=del data-id="${idOf(c)}" data-sym="${esc(g.sym)}">Delete</button></span></div></summary>`;
 const buyrow=`<tr><td>${d1}<br><small>${t1}</small></td><td><span class="tg b">BUY</span></td><td>${fmt(c.entry)}<br><small>$${AMT.toFixed(2)} → ${qf(AMT/c.entry)} ${esc(g.sym)}</small></td><td class=res>${c.outcome?`<small style="color:${c.outcome=="win"?"var(--up)":c.outcome=="loss"?"var(--dn)":"var(--mut)"};font-weight:700">${OUT[c.outcome]}</small>`:""}</td></tr>
 <tr><td colspan=4 class=why>↳ ${esc(c.signal||"Buy call")}${c.score?" · score "+c.score:""}${c.source=="discovery"?" · found by scanner":""}${c.buy_zone?` · buy zone ${fmt(c.buy_zone.low)}–${fmt(c.buy_zone.high)}`:""}${(c.plan||{}).stop?` · stop ${fmt(c.plan.stop)}`:""}${(c.plan||{}).target?` · target ${fmt(c.plan.target)}`:""}${pm?`<br>📉 Post-mortem: ${pm.signs.length?pm.signs.map(esc).join("; "):"no obvious warning signs"}${pm.btc_chg!=null?` (BTC ${pm.btc_chg.toFixed(1)}%)`:""}`:""}</td></tr>`;
 const res=`<td class=res style="color:${col}">${t.r>=0?"+":""}${(t.r*100).toFixed(1)}%<br><small style="color:${col}">${money(AMT*t.r)}</small></td>`;
 let sellrow;if(t.x){const [d2,t2]=when(t.x.t),held=(t.x.t-c.t)/86400;sellrow=`<tr><td>${d2}<br><small>${t2}</small></td><td><span class="tg s">SELL</span></td><td>${fmt(t.x.price)}<br><small>${qf(AMT/c.entry)} ${esc(g.sym)} → $${(AMT*(1+t.r)).toFixed(2)}</small></td>${res}</tr><tr><td colspan=4 class=why>↳ Sell signal: ${esc(t.x.why)} · held ${held<1?Math.round(held*24)+"h":held.toFixed(0)+"d"}${c.tmax?` · best ${((c.tmax/c.entry-1)*100).toFixed(0)}%`:""}${c.sell_verdict?` · 7d later ${c.after_sell>=0?"+":""}${c.after_sell}% (${esc(c.sell_verdict)})`:""}</td></tr>`;}
 else sellrow=`<tr><td><small>now</small></td><td><span class="tg o">OPEN</span></td><td>${fmt(t.exit)}<br><small>worth $${(AMT*(1+t.r)).toFixed(2)}</small></td>${res}</tr><tr><td colspan=4 class=why>↳ Waiting for a sell signal (sell zone, TRIM/SELL, stop-loss or take-profit target)</td></tr>`;
 return `<details class=trade${HID.has(idOf(c))?' hid':''}>${sum}<div class=trbody><table class=calls>${buyrow}${sellrow}</table><div class=acts2><button class=mini2 data-a=hide data-id="${idOf(c)}" data-sym="${esc(g.sym)}">${HID.has(idOf(c))?"Unhide":"Hide"}</button><button class="mini2 del" data-a=del data-id="${idOf(c)}" data-sym="${esc(g.sym)}">Delete this trade</button></div></div></details>`}).join("");
w.innerHTML=`<div class=ch>${logoHTML(g.sym,(D.logos||{})[(g.calls[0]||{}).cg_id]||(tk[g.sym]||{}).logo)}<div><b>${g.star&&!g.found?"⭐ ":""}${esc(g.sym)}</b> <small>${esc(g.name||"")}</small><br><small>${T.length} trade${T.length==1?"":"s"}${nopen?" · "+nopen+" open":""} · tap to open <span class=chev>▾</span></small></div><div class=pl style="color:${pl>=0?"var(--up)":"var(--dn)"}">${money(pl)}<small>${(pl/(AMT*T.length)*100).toFixed(1)}%</small></div></div>
<div class=tbody>${blocks}
${Lk.contract?`<div class=addr style="margin-top:8px"><span class=ch>${esc((Lk.chain||"").replace(/-/g," "))}</span><code>${esc(Lk.contract)}</code><button class=copy data-a="${esc(Lk.contract)}">Copy</button></div>`:""}
<div class=links><a class=lbtn target=_blank rel=noopener href="${esc(Lk.coingecko||"https://www.coingecko.com/en/search?query="+encodeURIComponent(g.sym))}"><i style="background:#8DC63F"></i>CoinGecko</a><a class=lbtn target=_blank rel=noopener href="${esc(Lk.dexscreener||"https://dexscreener.com/search?q="+encodeURIComponent(g.sym))}"><i style="background:linear-gradient(135deg,#222,#777)"></i>DexScreener</a></div></div>`;
w.querySelector(".ch").onclick=()=>{w.classList.toggle("open");OPENC.has(g.sym)?OPENC.delete(g.sym):OPENC.add(g.sym)};if(OPENC.has(g.sym))w.classList.add("open");
w.querySelectorAll("button.mini2,button.swact,button.trbtn").forEach(b=>b.onclick=act);
w.querySelectorAll("button.copy").forEach(bt=>bt.onclick=e=>{e.stopPropagation();copy(bt.dataset.a)});
w.querySelectorAll(".trade").forEach(det=>{const slide=det.querySelector(".trslide"),sum=det.querySelector(".trsum"),OPENW=132;
 let x0=null,y0=null,last=null,moved=false;const setT=px=>{slide.style.transform="translateX("+px+"px)";};
 slide.addEventListener("pointerdown",e=>{x0=e.clientX;y0=e.clientY;last=null;moved=false;slide.style.transition="none";});
 slide.addEventListener("pointermove",e=>{if(x0==null)return;const mx=e.clientX-x0,my=e.clientY-y0;
  if(!moved){if(Math.abs(mx)<6)return;if(Math.abs(my)>=Math.abs(mx)){x0=null;return;}moved=true;det.dataset.sw="1";try{slide.setPointerCapture(e.pointerId)}catch(_){}}
  const base=det.classList.contains("swopen")?-OPENW:0;last=Math.max(-OPENW,Math.min(0,base+mx));setT(last);e.preventDefault();});
 const end=()=>{if(x0==null)return;x0=null;slide.style.transition="";
  if(last==null){return;}if(last<-OPENW/2){setT(-OPENW);det.classList.add("swopen");}else{setT(0);det.classList.remove("swopen");}
  setTimeout(()=>{det.dataset.sw="";},30);};
 slide.addEventListener("pointerup",end);slide.addEventListener("pointercancel",end);
 sum.addEventListener("click",e=>{if(det.dataset.sw){e.preventDefault();e.stopImmediatePropagation();return;}if(det.classList.contains("swopen")){e.preventDefault();setT(0);det.classList.remove("swopen");}},true);});
L.append(w)})}
$("#amt").oninput=()=>{AMT=+$("#amt").value||100;try{localStorage.setItem("tw_amt",AMT)}catch(e){}draw()};draw();
})();
(function(){// learning
const Lr=D.learning||{};const L=$("#learn");
if(!Lr.graded&&!(Lr.scorecard||{}).closed){L.append(el("p","mut","Each trade is graded when a sell signal closes it (or after 14 days if it's still open). Losing trades get a post-mortem, every sell is reviewed 7 days later to see if it was well timed, and once a pattern repeats in 5+ trades the model adjusts. Nothing graded yet."));return}
L.append(el("div","kpis",`<div class=kpi><b>${Lr.graded}</b><small>calls graded</small></div><div class=kpi><b>${Lr.wins}</b><small>wins</small></div><div class=kpi><b>${Lr.losses}</b><small>losses</small></div>`));
const u=el("ul","checks");(Lr.rules||[]).forEach(r=>u.append(el("li","",`<span class="dot r">${Math.round(r.points)}</span><span>${esc(r.text)} — ${r.wins}/${r.n} won, avg ${r.avg.toFixed(1)}%${r.block?" · <b>no more buy calls in this setup</b>":""}</span>`)));
if(!(Lr.rules||[]).length)u.append(el("li","",`<span class="dot n">•</span><span>No warning sign has lost often enough yet to change the model.</span>`));L.append(u);
const SC=Lr.scorecard||{};if(SC.closed){L.append(el("div","sect","Closed trades"));L.append(el("div","kpis",`<div class=kpi><b>${SC.closed}</b><small>trades closed by a sell signal</small></div><div class=kpi><b>${Math.round(SC.wins/SC.closed*100)}%</b><small>profitable</small></div><div class=kpi><b style="color:${SC.avg>=0?"var(--up)":"var(--dn)"}">${SC.avg>=0?"+":""}${SC.avg}%</b><small>avg per trade · held ${SC.held}d</small></div>`));
const us=el("ul","checks");Object.values(Lr.sells||{}).forEach(e=>us.append(el("li","",`<span class="dot ${e.after!=null&&e.after>8?"r":e.after!=null&&e.after<-8?"g":"n"}">↗</span><span>Sold when it ${esc(e.text)}: ${e.n} trades, avg ${e.ret>=0?"+":""}${e.ret}%${e.after!=null?` · price moved ${e.after>=0?"+":""}${e.after}% in the 7 days after (${e.early} sold early, ${e.good} good sells)`:""}</span>`)));
if(SC.gave_back)us.append(el("li","",`<span class="dot r">!</span><span>${SC.gave_back} trade(s) were up 10%+ but gave it back before the sell signal.</span>`));
(Lr.sell_soft||[]).forEach(k=>us.append(el("li","",`<span class="dot n">🧠</span><span>Adjusted: ${k=="zone"?"entering the sell zone now also needs RSI above 70 to close a trade":"a TRIM signal no longer closes a trade — it needs SELL"}</span>`)));L.append(us)}
const M=Object.entries(Lr.mult||{});if(M.length)L.append(el("p","mut","Weight tuning: "+M.map(([k,v])=>(PART[k]||k)+" ×"+v).join(", ")))})();
// ---------- followed wallets
(function(){const W=D.wallets||{board:[],recent:[]},C=$("#wallets"),ago=t=>{const h=(Date.now()/1000-t)/3600;return h<1?Math.round(h*60)+"m":h<48?Math.round(h)+"h":Math.round(h/24)+"d"};
const f=el("div","addbox",`<div class=addrow><input id=win placeholder="Wallet address to follow" autocomplete=off spellcheck=false style="text-transform:none"><button class=abtn id=wbtn>Follow</button></div>
<div class=addnote>Paste a Solana or 0x wallet (from the Fomo leaderboard, DexScreener "Top traders", GMGN…). Rename it in Telegram with /follow &lt;address&gt; &lt;nickname&gt;.</div>`);
C.append(f);f.querySelector("#wbtn").onclick=()=>{const v=f.querySelector("#win").value.trim();if(!/^(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$/.test(v)){f.querySelector("#win").focus();return}
const u=D.bot?`https://t.me/${D.bot}?start=FOLLOW_${v}`:null;if(u)location.href=u;else alert("Send this to your bot: /follow "+v)};
if(!W.board.length){C.append(el("p","mut","Not following any wallets yet. Followed wallets' buys feed the score (weighted by how well each wallet's buys have done) and send you alerts."));return}
C.append(el("div","",`<table style="margin-top:10px"><tr><th>#</th><th>Wallet</th><th>Buys</th><th>In profit</th><th>Avg</th></tr>${W.board.map((b,i)=>`<tr><td>${i+1}</td><td><b>${esc(b.name)}</b><br><small>${b.kind=="sol"?"Solana":"EVM"} · ${esc(b.address.slice(0,4))}…${esc(b.address.slice(-4))}</small></td><td>${b.buys}</td><td>${b.win==null?"–":Math.round(b.win)+"%"}</td><td style="color:${(b.avg||0)>=0?"var(--up)":"var(--dn)"};font-weight:700">${b.avg==null?"–":(b.avg>=0?"+":"")+b.avg.toFixed(0)+"%"}</td></tr>`).join("")}</table>`));
if(W.recent.length){C.append(el("div","sect","Latest wallet trades"));const u=el("ul","checks");W.recent.slice(0,15).forEach(r=>{const ch=(r.last/r.price-1)*100;
u.append(el("li","",`<span class="dot ${r.side=="buy"?"g":"r"}">${r.side=="buy"?"B":"S"}</span><span style="flex:1"><b>${esc(r.wname)}</b> ${r.side=="buy"?"bought":"sold"} <a href="${esc(r.url)}" target=_blank rel=noopener><b>${esc(r.symbol)}</b></a> $${Number(r.usd).toLocaleString()} <small>· ${esc(r.chain)} · ${ago(r.t)} ago</small>${r.cluster>1?` <span class=pill style="background:var(--warn);color:#111">🔥 ${r.cluster} wallets</span>`:""}</span><small style="color:${ch>=0?"var(--up)":"var(--dn)"};font-weight:700">${ch>=0?"+":""}${ch.toFixed(0)}%</small>`))});C.append(u)}})();
// ---------- coin directory: every coin ever added, with its ticker and CoinGecko ID
(function(){const R=D.coins||{},C=$("#coindir"),ks=Object.keys(R).sort();if(!ks.length){C.append(el("p","mut","Coins appear here the first time they're checked."));return}
const d=el("details","tok");d.innerHTML=`<summary class=row style="grid-template-columns:1fr auto"><b>${ks.length} coins tracked</b><span class=mut>show ▾</span></summary>
<div style="padding:0 10px 10px"><table class=calls><tr><th>Ticker</th><th>Name</th><th>CoinGecko ID</th><th>Added</th></tr>${ks.map(k=>{const r=R[k];return `<tr><td><b>${esc(k)}</b></td><td style="white-space:normal">${esc(r.name||"")}</td><td><a href="https://www.coingecko.com/en/coins/${esc(r.cg_id||"")}" target=_blank rel=noopener style="font-family:ui-monospace,Menlo,monospace;font-size:.75rem">${esc(r.cg_id||"–")}</a></td><td>${esc((r.first_seen||"").slice(5,10))}</td></tr>`}).join("")}</table></div>`;C.append(d)})();
render();showPending();
{const f=$("#forcechk");if(f)f.onclick=e=>{e.preventDefault();if(D.repo)window.open("https://github.com/"+D.repo+"/actions/workflows/refresh.yml","_blank");};}
let rt;addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(()=>document.querySelectorAll(".tok.open").forEach(w=>{const r=w.querySelector(".rg.on");r&&r.click()}),250)});
setTimeout(()=>location.reload(),15*60*1000);
</script></body></html>
'''

def make_icon(n=180):
    """A simple app icon (navy with a teal rising bar chart), built without any image library."""
    import zlib, struct
    rows = []
    bars = [(40, 70, 100), (80, 110, 70), (120, 150, 40)]   # x0, x1, top
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            px = (47, 75, 124)
            for x0, x1, top in bars:
                if x0 <= x < x1 and top <= y < 140: px = (79, 179, 172)
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(rows)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

# ================================================================ run
HELP = """Token Watch commands:
/check - fresh assessment of all your starred coins
/check TAO - assess any coin (or just send TAO or $tao)
/star INJ - always alert me about INJ
/unstar INJ - stop forcing alerts for INJ
/add INJ - add to the watchlist
/remove INJ - remove from the watchlist
/discovered on|off - alerts for coins the scanner finds
/watchlist on|off - alerts for non-starred watchlist coins
/report on|off - weekly track-record message
/follow <address> <nickname> - follow a wallet (Solana or 0x)
/unfollow <address or nickname> - stop following
/wallets - the wallets you follow, their records, latest buys
/wallets on|off - wallet-buy alerts
/record - track record (your coins vs scanner finds)
/lessons - what the model has learned from its past calls
/pause - mute everything except replies
/resume - unmute
/status - show settings
Replies usually arrive within a minute."""

CTX = {}
SIG_EMOJI = {"STRONG BUY ZONE": "🟢", "ACCUMULATE": "🟢", "HOLD": "🟡", "TRIM": "🟠", "SELL / AVOID": "🔴", "AVOID (SCAM RISK)": "⛔"}

def assess(syms):
    """On-demand check requested from Telegram. Doesn't stamp buy calls."""
    syms = [x for x in dict.fromkeys(syms) if x][:10]
    if not syms: reply("You have no starred coins yet. Send /star INJ, or /check INJ."); return
    if not CTX: reply("Still starting up, try again in a minute."); return
    reply("Checking " + ", ".join(syms) + " …")
    known = {t["symbol"]: t for t in CFG_WATCH}
    for sym in syms:
        tok = dict(known.get(sym) or {"symbol": sym})
        try:
            r = check_token(tok, CTX["cache"], CTX["state"], CTX["calls"], source="adhoc")
        except Exception as e:
            r = None; print(f"  [error] {sym}: {e}")
        if not r: reply(f"Couldn't assess {sym}: not found on CoinGecko or not enough price history."); continue
        res = r["res"]
        note = "" if sym in known or sym in PREFS["added"] else f"\n\nNot on your watchlist. Reply /add {sym} to track it, or /star {sym} to always get alerts."
        reply(f"{SIG_EMOJI.get(res['signal'], '⚪')} " + ("⭐ " if sym in PREFS["starred"] else "") + summary(res, r["dd"], links=True) + note)

def add_now(sym, quiet=False):
    """Check a newly added coin right away and put it on the dashboard (published within a couple of minutes)."""
    if not CTX: reply(f"Added {sym}. It shows up after the next check."); return
    try: r = check_token({"symbol": sym}, CTX["cache"], CTX["state"], CTX["calls"], source="watchlist")
    except Exception as e: r = None; print(f"  [error] {sym}: {e}")
    if not r:
        reply(f"Added {sym}, but I couldn't find it on CoinGecko (or it has too little price history). Check the ticker, e.g. LINK not CHAINLINK."); return
    res = CTX.setdefault("results", [])
    res[:] = [x for x in res if x["res"]["symbol"] != r["res"]["symbol"]] + [r]
    CTX["dirty"] = True
    if not CTX.get("listening"): quick_publish([r])        # run still starting up: don't wait for the other coins
    if not quiet:
        reply(f"✅ Added {r['res']['symbol']}" + (f" ({(REG.get(r['res']['symbol']) or {}).get('name')}, CoinGecko ID: {r['cg_id']})" if r.get("cg_id") else "") + " to your watchlist - it'll be on your dashboard in about a minute.\n\n" + summary(r["res"], r["dd"], links=True)
              + f"\n\nReply /star {r['res']['symbol']} to always get its alerts.")

def quick_publish(rs):
    """Put just-added coins on the live page right away: merge them into the current dashboard data and publish."""
    try:
        cur = open(os.path.join(HERE, "docs", "index.html")).read()
        m = re.search(r"const D=(\{.*?\});\n", cur, re.S); D_ = json.loads(m.group(1).replace("<\\/", "</"))
    except Exception as e: print(f"  [skip] quick publish: {e}"); return False
    new = [token_entry(r) for r in rs]; keys = {(t.get("cg_id") or t["symbol"]) for t in new}
    D_["calls"] = CTX.get("calls", D_.get("calls", []))
    D_["tokens"] = new + [t for t in D_.get("tokens", []) if (t.get("cg_id") or t["symbol"]) not in keys]
    D_["generated"] = iso(); D_["coins"] = REG; D_["starred"] = sorted(PREFS.get("starred", []))
    D_["tokens"] = [{k: v for k, v in t.items() if k != "prices"} for t in D_["tokens"]]
    doc = DASH_HTML.replace("__DATA__", json.dumps(D_, separators=(",", ":")).replace("</", "<\\/"))
    for pth in (os.path.join(HERE, "docs", "index.html"), os.path.join(DATA, "dashboard.html")):
        with open(pth, "w") as f: f.write(doc)
    save("coins.json", REG); save_prefs(CTX["state"], None); save("state.json", CTX["state"]); save("cache.json", CTX["cache"]); save("calls.json", CTX["calls"])
    git_push(); print("  Quick-published: " + ", ".join(r["res"]["symbol"] for r in rs)); return True

def publish_now(rebuild=True):
    """Rebuild the dashboard and push it to GitHub right away (only on GitHub Actions)."""
    if not CTX.get("dirty") or CTX.get("results") is None: return
    CTX["dirty"] = False
    if rebuild:
        save_prefs(CTX["state"], None); save("state.json", CTX["state"]); save("calls.json", CTX["calls"]); save("cache.json", CTX["cache"])
        save("coins.json", REG); save("sells.json", SELLS[-2000:])
        export(CTX["results"], CTX["calls"], True)
    git_push()

def git_push():
    if not os.environ.get("GITHUB_ACTIONS") or DEMO: return
    g = lambda *a: subprocess.run(["git", "-c", "user.name=token-watch", "-c", "user.email=token-watch@users.noreply.github.com", *a],
                                  cwd=HERE, capture_output=True, text=True, timeout=60)
    try:
        g("add", "--", *[x for x in ("data", "docs", "watchlist.txt") if os.path.exists(os.path.join(HERE, x))])
        if g("diff", "--cached", "--quiet").returncode == 0: return
        g("commit", "-m", f"Live update {iso()}")
        for i in range(4):                               # GitHub occasionally answers "Internal Server Error": try again
            if g("pull", "--rebase", "-X", "theirs").returncode != 0: g("rebase", "--abort")
            out = g("push")
            if out.returncode == 0: print("  Published dashboard now."); return
            print(f"  [retry {i + 1}] publish: {out.stderr.strip()[:160]}"); time.sleep(10 * (i + 1))
        print("  [skip] publish failed - the workflow's last step will push it")
        ERRORS.append("publishing the dashboard to GitHub failed 4 times (GitHub error) - the end-of-run step will try again")
    except Exception as e: print(f"  [skip] publish: {e}")

def listen(minutes):
    """After the scheduled check, keep answering Telegram messages until the next run starts."""
    tok, chat = tg_creds()
    if not (tok and chat) or minutes <= 0: return
    end = now() + minutes * 60; CTX["listening"] = True
    print(f"Listening for Telegram messages for {minutes} min …")
    while now() < end - 5:
        handle_commands(CTX["state"], wait=int(min(25, max(1, end - now() - 5))))
        publish_now()                                      # coins you just added/removed/starred go live right away
        save_prefs(CTX["state"], None); save("state.json", CTX["state"]); save("calls.json", CTX["calls"]); save("cache.json", CTX["cache"])
    save_hist()

def load_prefs(state):
    cfg_alerts = CFG.get("alerts") or {}
    saved = state.get("_prefs") or {}
    starred = set(s.upper() for s in CFG.get("starred", []))
    starred |= {t["symbol"] for t in CFG_WATCH if t.get("starred")}
    PREFS.clear()
    PREFS.update({
        "starred": sorted((starred | set(saved.get("star_add", []))) - set(saved.get("star_del", []))),
        "alerts": {"watchlist": cfg_alerts.get("watchlist", True), "discovered": cfg_alerts.get("discovered", False),
                   "weekly_report": cfg_alerts.get("weekly_report", True), "wallets": cfg_alerts.get("wallets", True), **saved.get("alerts", {})},
        "paused": saved.get("paused", False), "added": saved.get("added", []), "removed": saved.get("removed", []),
        "wallets": saved.get("wallets", []), "unfollowed": saved.get("unfollowed", []),
    })

def save_prefs(state, base):
    base_star = set(s.upper() for s in CFG.get("starred", [])) | {t["symbol"] for t in CFG_WATCH if t.get("starred")}
    cur = set(PREFS["starred"])
    state["_prefs"] = {"star_add": sorted(cur - base_star), "star_del": sorted(base_star - cur),
                       "alerts": PREFS["alerts"], "paused": PREFS["paused"], "added": PREFS["added"], "removed": PREFS["removed"],
                       "wallets": PREFS.get("wallets", []), "unfollowed": PREFS.get("unfollowed", [])}

def reply(text):
    tok, chat = tg_creds()
    if not (tok and chat) or DEMO: print("[reply] " + text); return
    try:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers=UA), timeout=15)
    except Exception as e: print(f"  [telegram failed] {e}")

def handle_commands(state, texts=None, wait=0):
    """Reads messages you sent the bot since the last check and applies them."""
    if texts is None:
        tok, chat = tg_creds()
        if not (tok and chat): return
        try:
            d = get_json(f"https://api.telegram.org/bot{tok}/getUpdates?offset={state.get('_tg_offset', 0)}&timeout={wait}", timeout=wait + 15)
        except Exception as e: print(f"  [skip] Telegram commands: {e}"); return
        texts = []
        for u in d.get("result", []):
            state["_tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == chat and msg.get("text"): texts.append(msg["text"])
    for t in texts:
        m = re.fullmatch(r"/start(?:@\w+)?\s+(ADD|STAR|UNSTAR|REMOVE|CHECK)_([A-Za-z0-9]{1,15})", t.strip(), re.I)
        if m: t = f"/{m.group(1).lower()} {m.group(2).upper()}"      # buttons on the dashboard open the bot with these
        m = re.fullmatch(r"/start(?:@\w+)?\s+DELTRADE_([0-9]{6,12})", t.strip(), re.I)
        if m: t = f"/deletetrade {m.group(1)}"
        m = re.fullmatch(r"/start(?:@\w+)?\s+(FOLLOW|UNFOLLOW)_([A-Za-z0-9]{32,44})", t.strip(), re.I)
        if m: t = f"/{m.group(1).lower()} {m.group(2)}"                # wallet addresses keep their exact letters
        parts = t.strip().split(); cmd = parts[0].lower().split("@")[0]; arg = parts[1].upper() if len(parts) > 1 else ""
        onoff = {"ON": True, "OFF": False}.get(arg)
        raw = parts[1] if len(parts) > 1 else ""
        if cmd == "/follow":
            if not wkind(raw): reply("Send /follow <wallet address> <nickname>, e.g. /follow 7xKX…abc Sniper1 (Solana or 0x… EVM address)."); continue
            name = " ".join(parts[2:])[:30] or f"Wallet {raw[:4]}…{raw[-4:]}"
            PREFS["wallets"] = [w for w in PREFS.get("wallets", []) if w["address"].lower() != raw.lower()] + [{"address": raw, "name": name}]
            PREFS["unfollowed"] = [x for x in PREFS.get("unfollowed", []) if x.lower() != raw.lower()]
            need = "HELIUS_API_KEY" if wkind(raw) == "sol" else "ETHERSCAN_API_KEY"
            reply(f"🐋 Following {name}. I'll alert you when it buys (from the next check) and track how its buys do."
                  + ("" if CFG.get(need.lower()) or wkind(raw) == "evm" and CFG.get("etherscan_api_key") else f"\n⚠️ Add the {need} secret on GitHub so I can read this wallet." if need == "HELIUS_API_KEY" else ""))
            continue
        if cmd == "/unfollow" and raw:
            hit = [w for w in followed() if raw.lower() in (w["address"].lower(), w["name"].lower())]
            if not hit: reply("Not following that wallet. Send /wallets to see the list."); continue
            for w in hit:
                PREFS["wallets"] = [x for x in PREFS.get("wallets", []) if x["address"].lower() != w["address"].lower()]
                PREFS.setdefault("unfollowed", []).append(w["address"])
            reply(f"Unfollowed {', '.join(w['name'] for w in hit)}."); continue
        if cmd == "/wallets" and onoff is None: reply(wallets_text()); continue
        if cmd in ("/lessons", "/learned"): reply(lessons_text()); continue
        if cmd == "/deletetrade" and raw:
            n = delete_trades(raw, CTX.get("calls") if CTX else None)
            reply(f"🗑 Deleted {n} trade(s) from your track record." if n else "No trade with that ID."); CTX["dirty"] = bool(n) or CTX.get("dirty", False)
            if n and not CTX.get("listening") and CTX.get("results"): quick_publish([])
            continue
        if cmd == "/record": reply(report_text(CTX.get("calls") or load("calls.json", []))); continue
        if cmd == "/check":
            assess([a.upper().lstrip("$") for a in parts[1:]] or PREFS["starred"])
        elif not cmd.startswith("/") and len(parts) <= 5 and all(re.fullmatch(r"\$[A-Za-z0-9]{2,10}|[A-Z0-9]{2,10}", x) for x in parts):
            assess([x.upper().lstrip("$") for x in parts])     # just typing "TAO" or "SOL LINK" checks them
        elif cmd in ("/start", "/help"): reply(HELP)
        elif cmd == "/status":
            a = PREFS["alerts"]
            reply("Starred (always alert): " + (", ".join(PREFS["starred"]) or "none") +
                  f"\nOther watchlist alerts: {'on' if a['watchlist'] else 'off'}\nDiscovered-coin alerts: {'on' if a['discovered'] else 'off'}"
                  f"\nWeekly report: {'on' if a['weekly_report'] else 'off'}\nPaused: {'yes' if PREFS['paused'] else 'no'}"
                  + ("\nAdded by command: " + ", ".join(PREFS["added"]) if PREFS["added"] else "")
                  + f"\nWallet-buy alerts: {'on' if a.get('wallets', True) else 'off'} ({len(followed())} wallets followed)")
        elif cmd in ("/star", "/unstar") and arg:
            st = set(PREFS["starred"]); (st.add if cmd == "/star" else st.discard)(arg); PREFS["starred"] = sorted(st)
            if cmd == "/star" and arg not in PREFS["added"] and arg not in {w["symbol"] for w in CFG_WATCH}: PREFS["added"].append(arg)
            if arg in PREFS["removed"]: PREFS["removed"].remove(arg)
            reply(f"{'⭐ Starred' if cmd == '/star' else 'Unstarred'} {arg}.")
            if cmd == "/star" and CTX.get("results") is not None and arg not in {r["res"]["symbol"] for r in CTX["results"]}: add_now(arg, quiet=True)
            CTX["dirty"] = True
        elif cmd == "/add" and arg:
            if arg not in PREFS["added"]: PREFS["added"].append(arg)
            if arg in PREFS["removed"]: PREFS["removed"].remove(arg)
            add_now(arg)
        elif cmd == "/remove" and arg:
            if arg in PREFS["added"]: PREFS["added"].remove(arg)
            if arg not in PREFS["removed"]: PREFS["removed"].append(arg)
            PREFS["starred"] = [x for x in PREFS["starred"] if x != arg]
            reply(f"Removed {arg}.")
            if CTX.get("results") is not None: CTX["results"][:] = [r for r in CTX["results"] if r["res"]["symbol"] != arg]
            CTX["dirty"] = True
        elif cmd in ("/discovered", "/watchlist", "/report", "/wallets") and onoff is not None:
            PREFS["alerts"][{"/discovered": "discovered", "/watchlist": "watchlist", "/report": "weekly_report", "/wallets": "wallets"}[cmd]] = onoff
            reply(f"{cmd[1:].capitalize()} alerts turned {'on' if onoff else 'off'}.")
        elif cmd == "/pause": PREFS["paused"] = True; reply("Paused. Reply /resume to turn alerts back on.")
        elif cmd == "/resume": PREFS["paused"] = False; reply("Alerts are back on.")
        else: reply("I didn't understand that. " + HELP)

CFG_WATCH = []

def run_once():
    cache, state, calls = load("cache.json", {}), load("state.json", {}), load("calls.json", [])
    wl = [norm(t) for t in CFG.get("watchlist", [])]
    extra = os.path.join(HERE, "watchlist.txt")
    if os.path.exists(extra) and not DEMO:
        have = {t["symbol"] for t in wl}
        for line in open(extra):
            s = line.split("#")[0].strip()
            star = s.startswith("*"); s = s.lstrip("*").strip()     # "*INJ" = starred
            if not s: continue
            if s.upper() in have:
                if star: next(t for t in wl if t["symbol"] == s.upper())["starred"] = True
                continue
            t = norm(s); t["starred"] = star; wl.append(t); have.add(s.upper())
    CFG_WATCH[:] = wl
    load_prefs(state)
    HIST.clear(); HIST.update(load_hist())
    CTX.clear(); CTX.update({"cache": cache, "state": state, "calls": calls})
    SELLS[:] = load("sells.json", [])
    REG.clear(); REG.update(load("coins.json", {}))
    LEARN.clear(); LEARN.update(load("learning.json", {}))
    have = {t["symbol"] for t in wl}
    wl = [t for t in wl if t["symbol"] not in PREFS["removed"]] + [norm(x) for x in PREFS["added"] if x not in have]
    save_prefs(state, None)
    ids = [t.get("coingecko_id") or try_get("lookup", lambda t=t: resolve_id(t["symbol"], cache)) for t in wl]
    ids += [c.get("cg_id") for c in calls] + ["bitcoin"]
    ids = sorted({i for i in ids if i})
    LIVE.clear()
    if ids: LIVE.update({k: v for k, v in (try_get("live prices", lambda: simple_prices(ids)) or {}).items() if v})
    need = [i for i in ids if not cache.get("logo:" + i)][:250]
    if need:
        mk = try_get("logos", lambda: get_json(f"{CG}/coins/markets?vs_currency=usd&ids={','.join(need)}&per_page=250", cg=True)) or []
        for m_ in mk if isinstance(mk, list) else []:
            if m_.get("id") and m_.get("image"): cache["logo:" + m_["id"]] = m_["image"].replace("/large/", "/small/")
    LOGOS.clear(); LOGOS.update({k[5:]: v for k, v in cache.items() if k.startswith("logo:")})
    try_get("market backdrop", load_macro)
    BACKDROP.clear()
    if ind("betting_markets") and (CFG.get("prediction_markets") or {}).get("enabled", True):
        pm = try_get("Bitcoin betting markets", lambda: polymarket("BTC", "Bitcoin"))
        btc_px = LIVE.get("bitcoin")
        if pm and btc_px:
            bv = market_view(pm, btc_px)
            if bv: BACKDROP["btc"] = bv; print(f"Bitcoin betting backdrop {bv['score']:.0f}/100 from {bv['n']} markets")
    handle_commands(state)   # messages sent since the last run (applies /star, /check, etc.)
    if CFG.get("_deleted"): reply(f"🗑 Deleted {CFG['_deleted']} trade(s) from your track record (from the dashboard).")
    if CFG.get("_form_added"): reply("⏳ Adding " + ", ".join(CFG["_form_added"]) + " from your dashboard - analyzing now, it'll be on the page in about a minute.")
    fix_names(cache)
    if ind("wallets"): try_get("followed wallets", lambda: sync_wallets(state))
    wl = [t for t in wl if t["symbol"] not in PREFS["removed"]] + [norm(x) for x in PREFS["added"] if x not in {t["symbol"] for t in wl}]
    seen_ = set(); wl2 = []                              # one entry per coin (CHAINLINK and LINK are the same coin)
    for t in wl:
        t["symbol"] = cache.get("tick:" + t["symbol"].lower()) or t["symbol"]
        if t["symbol"] not in seen_ and t["symbol"] not in PREFS["removed"]: seen_.add(t["symbol"]); wl2.append(t)
    wl = wl2
    results = []
    if CFG.get("_form_added"):
        first = [t for t in wl if t["symbol"] in {cache.get("tick:" + x.lower()) or x for x in CFG["_form_added"]}]
        for tok in first:
            try:
                r = check_token(tok, cache, state, calls)
                if r:
                    results.append(r)
                    reply(f"✅ {r['res']['symbol']} is on your dashboard now" + (f" ({(REG.get(r['res']['symbol']) or {}).get('name')}, CoinGecko ID: {r['cg_id']})" if r.get("cg_id") else "") + ":\n\n" + summary(r["res"], r["dd"], links=True))
                else: reply(f"Couldn't find {tok['symbol']} on CoinGecko (or it has too little price history). Check the ticker.")
            except Exception as ex: print(f"  [error] {tok['symbol']}: {ex}")
        if results: quick_publish(results)
    done_ = {r["res"]["symbol"] for r in results}
    for tok in wl:
        if tok["symbol"] in done_: continue
        try:
            r = check_token(tok, cache, state, calls)
            if r: results.append(r)
        except Exception as ex: print(f"  [error] {tok['symbol']}: {ex}")
    have = {t["symbol"] for t in wl}
    for sym in sorted({c["symbol"] for c in calls if c["symbol"] not in have and now() - c["t"] < 45 * DAY})[:10]:
        if not open_call(sym, calls): continue
        c0 = open_call(sym, calls)
        try:
            r = check_token({"symbol": sym, "coingecko_id": c0.get("cg_id")}, cache, state, calls, source="tracked")
            if r: results.append(r)
        except Exception as ex: print(f"  [error] {sym}: {ex}")
    try: results += discover(cache, state, calls, have | {r["res"]["symbol"] for r in results})
    except Exception as ex: print(f"  [discovery error] {ex}")
    prices = dict(LIVE); prices.update({r["res"]["symbol"]: r["res"]["price"] for r in results})
    update_calls(calls, prices)
    for cl in calls:                                     # older calls: add CoinGecko/DexScreener links
        if not cl.get("links"): cl["links"] = token_links(cl["symbol"], cl.get("cg_id"), (cache.get("dd:" + (cl.get("cg_id") or "")) or {}).get("v"))
    try_get("learning", lambda: learn(calls, state))
    wk = datetime.now(timezone.utc).strftime("%G-W%V")
    if calls and state.get("_report_week") != wk:
        state["_report_week"] = wk; send(report_text(calls), "report")
    save_prefs(state, None)
    save("cache.json", cache); save("state.json", state); save("calls.json", calls); save("sells.json", SELLS[-2000:]); save("coins.json", REG); save_hist()
    # export.json / dashboard are big; rewrite them every few hours, not every 15 minutes
    full = DEMO or now() - state.get("_export_t", 0) >= CFG.get("export_every_minutes", 240) * 60
    try:
        bad = []
        if not results: bad.append("no coins were checked this run")
        for r in results:
            rr = r["res"]
            if rr.get("price") in (None, 0): bad.append(f"{rr['symbol']}: price is {rr.get('price')}")
            if rr["signal"] in BUY_SIGNALS and (rr.get("in_sell") or (rr.get("rsi") or 0) > 71): bad.append(f"{rr['symbol']}: buy signal while overbought/in sell zone")
        if bad: ERRORS.append("dashboard self-check: " + "; ".join(bad[:5]))
    except Exception as e: print(f"  [skip] self-check: {e}")
    export(results, calls, full)
    CTX["results"] = results; CTX["dirty"] = False
    if os.environ.get("GITHUB_ACTIONS") and not DEMO:  # put the fresh dashboard online now, not after the listening window
        CTX["dirty"] = True; publish_now(rebuild=False)
    if full: state["_export_t"] = now(); save("state.json", state)
    print(f"\nDashboard: {os.path.join(DATA, 'dashboard.html')}")

HIST_PATH = os.path.join(HERE, ".hist-cache", "hist.json")   # not committed; GitHub keeps it in its cache
def load_hist():
    try:
        with open(HIST_PATH) as f: return json.load(f)
    except Exception: return {}
def save_hist():
    try:
        os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
        with open(HIST_PATH, "w") as f: json.dump(HIST, f)
    except Exception as e: print(f"  [skip] history cache: {e}")

# ---------------------------------------------------------------- demo (offline)
def install_demo():
    global get_json, binance_daily, binance_depth
    random.seed(3)
    def series(start, n=365, turn=250):
        p, c = start, []
        for i in range(n):
            p *= math.exp((-0.004 if i < turn else 0.005) + random.gauss(0, 0.035)); c.append(p)
        return c
    SER = {"BTCUSDT": series(60000, turn=200), "INJUSDT": series(20), "GOODUSDT": series(3, turn=330), "RUGUSDT": series(0.5)}
    def fake_klines(pair):
        if pair not in SER: return None
        c = SER[pair]; v = [random.uniform(5e6, 2e7) for _ in c]
        return {"close": c, "high": [x*1.02 for x in c], "low": [x*0.98 for x in c], "volume": v,
                "taker_buy": [x*random.uniform(.47, .57) for x in v], "host": "demo", "src": "demo"}
    def fake_json(url, headers=None, timeout=20, cg=False):
        if "/search/trending" in url: return {"coins": [{"item": {"id": "rug-coin", "symbol": "RUG"}}]}
        if "/search?" in url:
            q = url.split("query=")[1].lower(); return {"coins": [{"id": {"inj": "injective-protocol"}.get(q, q + "-coin"), "symbol": q, "market_cap_rank": 50}]}
        if "/coins/markets" in url: return [{"id": "good-coin", "symbol": "good", "market_cap": 3e8, "total_volume": 4e7, "price_change_percentage_7d_in_currency": -12}]
        if "/simple/price" in url: return {"bitcoin": {"usd": 81000}} if "bitcoin" in url else {}
        if "/coins/" in url:
            cid = url.split("/coins/")[1].split("?")[0]
            rug = cid == "rug-coin"
            return {"name": {"injective-protocol": "Injective"}.get(cid, cid), "links": {"homepage": [] if rug else ["https://x.io"], "repos_url": {"github": [] if rug else ["https://github.com/x"]}},
                    "genesis_date": "2026-08-01" if rug else "2021-01-01", "developer_data": {"commit_count_4_weeks": 30},
                    "platforms": {"ethereum": "0xabc"} if rug else {},
                    "market_data": {"market_cap": {"usd": 3e6 if rug else 7.7e8}, "fully_diluted_valuation": {"usd": 3e7 if rug else 7.7e8}, "total_volume": {"usd": 9e6 if rug else 1e8}}}
        if "alternative.me" in url: return {"data": [{"value": "28", "value_classification": "Fear"}]}
        if "stablecoincharts" in url: return [{"totalCirculatingUSD": {"peggedUSD": 2.60e11 * (1 + i * 0.0012)}} for i in range(40)]
        if "llama.fi/v2/chains" in url: return [{"gecko_id": "injective-protocol", "name": "Injective"}]
        if "llama.fi/protocols" in url: return []
        if "historicalChainTvl" in url: return [{"date": i, "tvl": 1.0e8 * (1 + i * 0.006)} for i in range(40)]
        if "premiumIndex" in url: return {"lastFundingRate": "-0.00025"}
        if "openInterestHist" in url: return [{"sumOpenInterestValue": str(4e7 * (1 + i * 0.02))} for i in range(8)]
        if "polymarket" in url:
            q = urllib.parse.unquote(url.split("q=")[1].split("&")[0])
            mk = lambda qt, o, p, liq=50000, end="2026-12-31": {"id": qt, "question": qt, "outcomes": json.dumps(o), "outcomePrices": json.dumps([str(p), str(round(1-p, 3))]), "liquidityNum": liq, "endDate": end, "active": True, "closed": False}
            if q == "bitcoin":
                return {"events": [{"markets": [mk("Bitcoin Up or Down on September 23?", ["Up", "Down"], 0.58),
                    mk("Will Bitcoin reach $90,000 by December 31?", ["Yes", "No"], 0.62), mk("Will Bitcoin reach $110,000 by December 31?", ["Yes", "No"], 0.31),
                    mk("Will Bitcoin dip to $65,000 by December 31?", ["Yes", "No"], 0.28)]}]}
            if q == "injective":
                return {"events": [{"markets": [mk("Injective ETF approved in 2026?", ["Yes", "No"], 0.41, 12000)]}]}
            return {"events": []}
        if "gopluslabs" in url:
            return {"result": {"0xabc": {"is_honeypot": "0", "sell_tax": "0.25", "buy_tax": "0.05", "is_open_source": "0", "is_mintable": "1",
                    "holders": [{"percent": "0.4"}, {"percent": "0.2"}], "lp_holders": [{"percent": "1", "is_locked": "0"}]}}}
        raise RuntimeError("no demo data for " + url)
    get_json = fake_json; binance_daily = fake_klines
    binance_depth = lambda host, pair, price: {"bid_usd": 420e3, "ask_usd": 380e3}

def main():
    global CFG, DEMO, DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true"); ap.add_argument("--demo", action="store_true"); ap.add_argument("--report", action="store_true")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    ap.add_argument("--add", default="", help="tickers to add to watchlist.txt first (used by the Add-a-coin form)")
    ap.add_argument("--star", action="store_true", help="star the --add tickers")
    ap.add_argument("--delete-trades", default="", help="trade IDs to delete from the track record (dashboard Delete button)")
    a = ap.parse_args()
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path): path = os.path.join(HERE, "config.example.json")
    CFG = json.load(open(path))
    for _k in ("coingecko_api_key", "cryptopanic_api_key", "whale_alert_api_key", "helius_api_key", "etherscan_api_key"):  # keys from GitHub Secrets override config.json
        if os.environ.get(_k.upper()): CFG[_k] = os.environ[_k.upper()]
    if a.demo:
        DEMO = True; DATA = os.path.join(HERE, "data-demo"); CFG["coingecko_min_seconds"] = 0; install_demo()
        CFG["watchlist"] = [{"symbol": "INJ", "team_doxxed": True, "audited": True, "fundamental_grade": "B", "alerts": {"below": 6.5}}]
    if a.report: print(report_text(load("calls.json", []))); return
    if a.demo:
        CFG["watchlist"][0]["starred"] = True
        CFG["alerts"] = {"watchlist": True, "discovered": False, "weekly_report": True}
    if (a.add.strip() or a.delete_trades.strip()) and os.environ.get("GITHUB_ACTIONS"): restore_snapshot()
    if a.delete_trades.strip():
        n = delete_trades(a.delete_trades); print(f"Deleted {n} trade(s)"); CFG["_deleted"] = n
    if a.add.strip():
        adds = [x.upper().lstrip("$") for x in re.split(r"[\s,]+", a.add.strip()) if re.fullmatch(r"\$?[A-Za-z0-9]{1,20}", x)][:10]
        wpath = os.path.join(HERE, "watchlist.txt"); lines = open(wpath).read().splitlines() if os.path.exists(wpath) else []
        have = {l.split("#")[0].strip().lstrip("*").upper() for l in lines}
        for x in adds:
            if x in have:
                if a.star: lines = [("*" + l.lstrip("*")) if l.split("#")[0].strip().lstrip("*").upper() == x else l for l in lines]
            else: lines.append(("*" if a.star else "") + x)
        open(wpath, "w").write("\n".join(lines) + "\n")
        st_ = load("state.json", {}); pr = st_.get("_prefs") or {}
        pr["removed"] = [x for x in pr.get("removed", []) if x not in adds]; st_["_prefs"] = pr; save("state.json", st_)
        print("Added from the form: " + ", ".join(adds))
        if adds: CFG["_form_added"] = adds
    if a.test_telegram:
        tg = CFG.get("telegram") or {}
        if not ((os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")) and (os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id"))):
            sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets (or fill them in config.json).")
        load_prefs({}); send(f"Token Watch is connected. You'll get alerts here. ({iso()})\n\n" + HELP); return
    if os.environ.get("GITHUB_ACTIONS") and not DEMO: restore_snapshot()
    while True:
        run_once()
        if a.once and not a.demo: listen(CFG.get("listen_minutes", 0) if os.environ.get("GITHUB_ACTIONS") else 0)
        if CTX.get("state") is not None: report_errors(CTX["state"]); save("state.json", CTX["state"])
        if os.environ.get("GITHUB_ACTIONS") and not DEMO: save_snapshot()
        if a.once or a.demo: break
        mins = CFG.get("check_every_minutes", 15); print(f"Next check in {mins} min. Ctrl+C to stop."); time.sleep(mins * 60)

CFG = {}
if __name__ == "__main__": main()
