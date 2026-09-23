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
import argparse, json, math, os, random, sys, time, urllib.parse, urllib.request, html
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
def save(name, obj):
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
def try_get(label, fn):
    try: return fn()
    except Exception as e:
        print(f"  [skip] {label}: {e}"); return None

CG = "https://api.coingecko.com/api/v3"
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]  # 2nd works from the US

# ================================================================ data sources
def resolve_id(sym, cache):
    """Ticker -> CoinGecko id (largest market cap with that exact ticker)."""
    key = "id:" + sym.lower()
    if key in cache: return cache[key]
    d = get_json(f"{CG}/search?query={urllib.parse.quote(sym)}", cg=True)
    hits = [c for c in d.get("coins", []) if c.get("symbol", "").lower() == sym.lower()]
    hits.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
    cache[key] = hits[0]["id"] if hits else None
    return cache[key]

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
        why.append(f"{'Above' if price > s200 else 'Below'} 200-day avg ${s200:.4g}")
    in_zone = bool(bz and bz["low"] <= price <= bz["high"])
    if bz:
        if in_zone: t.append(90); why.append("Inside buy zone")
        elif price < bz["low"]: t.append(45); why.append("Broke below buy zone")
        else: t.append(max(20, 80 - (price - bz["high"]) / (a or 1) * 15))
    if sz and price >= sz["low"]: t.append(15); why.append("Near 90-day high (sell zone)")
    if ind("bollinger"):
        bb = bollinger(c)
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
        b = MACRO["btc_close"]; rs30 = (c[-1] / c[-31] - b[-1] / b[-31]) * 100
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
        why.append(f"Order book 2%: ${depth['bid_usd']/1e3:.0f}K bids / ${depth['ask_usd']/1e3:.0f}K asks")
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
    parts["macro"] = MACRO.get("score")
    if news:
        flag = sum(n["flag"] for n in news[:15]); pos = sum(n["pos"] for n in news); neg = sum(n["neg"] for n in news)
        parts["news"] = max(0, min(100, 0.5 * (lin(pos/(pos+neg), 0.3, 0.8) if pos+neg else 50) + 0.5 * (50 + flag * 8)))
    else: parts["news"] = None
    parts["fundamental"] = GRADE_SCORE.get((grade or "").upper())
    if mv:
        parts["markets"] = mv["score"]
        why.append(f"Betting markets lean {'up' if mv['score'] > 55 else 'down' if mv['score'] < 45 else 'neutral'} ({mv['score']:.0f}/100, {mv['n']} markets)")
        if mv["implied"]: why.append(f"Betting odds put ~50% on touching ${mv['implied']['level']:.4g} by {mv['implied']['by']}")
    elif backdrop:
        parts["markets"] = 50 + (backdrop["score"] - 50) / 2
        why.append(f"No betting markets for {sym}; Bitcoin odds used as backdrop ({backdrop['score']:.0f}/100)")
    else: parts["markets"] = None
    W = {**DEFAULT_W, **(CFG.get("weights") or {})}
    have = {k: v for k, v in parts.items() if v is not None and W.get(k, 0) > 0}; ws = sum(W[k] for k in have)
    score = sum(v * W[k] for k, v in have.items()) / ws if ws else None
    signal = ("STRONG BUY ZONE" if score >= 72 else "ACCUMULATE" if score >= 60 else "HOLD" if score >= 45
              else "TRIM" if score >= 33 else "SELL / AVOID") if score is not None else "NO DATA"
    if dd and dd["level"] == "HIGH":
        signal = "AVOID (SCAM RISK)"; why.insert(0, "High scam risk: " + "; ".join(dd["flags"][:3]))
    return {"symbol": sym, "price": price, "rsi": r, "macd": m, "atr": a, "sma50": s50, "sma200": s200, "buy_zone": bz,
            "sell_zone": sz, "in_zone": in_zone, "parts": parts, "score": score, "signal": signal, "why": why,
            "buy_ratio": ratio, "src": data.get("src"), "markets": mv}

# ================================================================ extra indicators (all free, all optional)
DEFAULT_IND = {"bollinger": True, "obv": True, "rsi_divergence": True, "relative_strength": True,
               "derivatives": True, "fear_greed": True, "market_regime": True, "stablecoin_liquidity": True,
               "tvl_trend": True, "betting_markets": True}
DEFAULT_W = {"technical": .30, "flows": .15, "derivatives": .10, "macro": .10, "news": .05, "markets": .05, "fundamental": .25}
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
    if res["signal"] not in BUY_SIGNALS and not res["in_zone"]: return None
    if res["rsi"] is not None and res["rsi"] > 70: return None   # don't stamp buys into overbought moves
    if dd and dd["level"] == "HIGH": return None
    cool = CFG.get("call_cooldown_days", 7) * DAY
    if any(c["symbol"] == res["symbol"] and now() - c["t"] < cool for c in calls): return None
    call = {"id": f"{res['symbol']}-{int(now())}", "symbol": res["symbol"], "cg_id": cg_id, "t": int(now()), "date": iso(),
            "entry": res["price"], "signal": res["signal"] if res["signal"] in BUY_SIGNALS else "IN BUY ZONE",
            "score": round(res["score"] or 0), "buy_zone": res["buy_zone"], "risk": dd["level"] if dd else None,
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
    rs = [ret(c) for c in calls]; wins = sum(1 for x in rs if x > 0)
    lines = [f"Track record: {len(calls)} buy calls, {wins} in profit ({wins/len(calls)*100:.0f}%), average {sum(rs)/len(rs):+.1f}%"]
    for d in ("7", "30", "90"):
        cp = [c["checkpoints"][d] for c in calls if d in c["checkpoints"]]
        if cp: lines.append(f"  After {d} days: avg {sum(cp)/len(cp):+.1f}% across {len(cp)} calls, {sum(1 for x in cp if x > 0)} up")
    for c in sorted(calls, key=lambda c: -c["t"])[:25]:
        lines.append(f"  {c['date'][:10]} {c['symbol']:<8} {c['signal']:<15} entry ${c['entry']:.4g} now ${c['last']:.4g} "
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

def z(zn): return f"${zn['low']:.4g}-${zn['high']:.4g}" if zn else "n/a"
def summary(res, dd=None):
    s = f"{res['symbol']} ${res['price']:.4g} -> {res['signal']}" + (f" ({res['score']:.0f}/100)" if res['score'] is not None else "")
    s += f"\nBuy zone {z(res['buy_zone'])} | Sell zone {z(res['sell_zone'])}"
    if res.get("markets"): s += "\nBetting markets:\n" + "\n".join("   " + l for l in res["markets"]["lines"][:3])
    if dd: s += f"\nScam risk {dd['level']}" + (": " + "; ".join(dd["flags"][:4]) if dd["flags"] else "")
    return s + "\n" + "\n".join(" - " + w for w in res["why"]) + ("\nMarket: " + "; ".join(MACRO.get("why", [])) if MACRO.get("why") else "")

def check_alerts(tok, res, news, dd, state, call):
    st = state.setdefault(res["symbol"], {"seen_news": []}); ev = []; p = res["price"]
    if st.get("signal") and st["signal"] != res["signal"]: ev.append(f"Signal changed {st['signal']} -> {res['signal']}")
    if call: ev.append(f"BUY CALL stamped at ${call['entry']:.4g} (tracked in your record)")
    if res["in_zone"] and not st.get("in_zone") and not call: ev.append(f"Entered buy zone {z(res['buy_zone'])}")
    if res["buy_zone"] and p < res["buy_zone"]["low"] and st.get("in_zone"): ev.append("Fell through the buy zone - support broke")
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

def check_token(tok, cache, state, calls, source="watchlist", grade=None):
    sym = tok["symbol"]; print(f"\n{sym} ...")
    cg_id = tok.get("coingecko_id") or try_get("lookup", lambda: resolve_id(sym, cache))
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
    if ind("betting_markets") and (CFG.get("prediction_markets") or {}).get("enabled", True):
        name = ((dd or {}).get("facts") or {}).get("name") or tok.get("name") or ""
        pm = try_get("betting markets", lambda: polymarket(sym, name))
        mv = market_view(pm, data["close"][-1]) if pm else None
    res = analyse(sym, data, depth, news, whales, tok.get("fundamental_grade") or grade, dd, mv, None if sym == "BTC" else BACKDROP.get("btc"), ex)
    call = None if source == "adhoc" else stamp_call(calls, res, cg_id, dd, source)
    print(summary(res, dd))
    if source == "watchlist": check_alerts(tok, res, news, dd, state, call)
    if source == "watchlist" and mv: odds_alerts(sym, mv, state)
    return {"tok": tok, "cg_id": cg_id, "res": res, "dd": dd, "call": call, "closes": data["close"][-365:], "source": source}

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
        if ok_risk and (r["res"]["signal"] in BUY_SIGNALS or r["res"]["in_zone"]):
            seen = state.setdefault("_disc_seen", {})
            if now() - seen.get(c["symbol"], 0) > 7 * DAY:
                seen[c["symbol"]] = now()
                send(f"NEW CANDIDATE (reply /add {c['symbol']} to watch it)\n" + summary(r["res"], r["dd"]) +
                     "\n\nAutomated checks passed, but still verify the team and audit yourself before buying.", "discovery")
            found.append(r)
    return found

# ================================================================ outputs
def export(results, calls, full=True):
    out = {"generated": iso(), "tokens": [], "calls": calls}
    for r in results:
        res, dd = r["res"], r["dd"] or {}
        pr = res["buy_ratio"]
        out["tokens"].append({
            "symbol": res["symbol"], "name": (dd.get("facts") or {}).get("name") or "", "source": r["source"],
            "price": res["price"], "prices": "\n".join(f"{x:.8g}" for x in r["closes"]),
            "signal": res["signal"], "score": res["score"], "buy_zone": res["buy_zone"], "sell_zone": res["sell_zone"],
            "why": res["why"], "risk": dd.get("level"), "risk_flags": dd.get("flags", []), "checks": dd.get("checks", {}),
            "pressure": None if pr is None else (-2 if pr < .46 else -1 if pr < .49 else 0 if pr < .51 else 1 if pr < .54 else 2),
            "market_cap": (dd.get("facts") or {}).get("market_cap"),
            "markets": res.get("markets"), "parts": res["parts"]})
    out["macro"] = {"score": MACRO.get("score"), "why": MACRO.get("why", [])}
    if full: save("export.json", out)
    write_dashboard(out)

def write_dashboard(out):
    """Phone-friendly dashboard. Also published to docs/ so GitHub Pages can host it as a home-screen app."""
    e = lambda s: html.escape(str(s if s is not None else ""))
    col = {"STRONG BUY ZONE": "#1F6F6B", "ACCUMULATE": "#5E8A35", "HOLD": "#B8831A", "TRIM": "#C2552E"}
    star = set(PREFS.get("starred", []))
    toks = sorted(out["tokens"], key=lambda t: (t["symbol"] not in star, t["source"] != "watchlist", -(t["score"] or 0)))
    def card(t):
        c = col.get(t["signal"], "#8E2C2C"); bz = t["buy_zone"]
        why = "".join(f"<li>{e(w)}</li>" for w in t["why"][:10])
        odds = "".join(f"<li>{e(l)}</li>" for l in ((t.get("markets") or {}).get("lines") or [])[:3])
        return (f"<details class=card><summary><div><b class=sym>{'⭐ ' if t['symbol'] in star else ''}{e(t['symbol'])}</b>"
                f"{' <span class=tag>found</span>' if t['source'] == 'discovery' else ''}<br><span class=px>${t['price']:.4g}</span></div>"
                f"<div class=r><span class=pill style='background:{c}'>{e(t['signal'])}</span><br><small>{(t['score'] or 0):.0f}/100 · risk {e(t['risk'] or '?')}</small></div></summary>"
                f"<p><small>Buy zone {'$%.4g–$%.4g' % (bz['low'], bz['high']) if bz else 'n/a'}</small></p><ul>{why}</ul>"
                + (f"<p><b>Betting markets</b></p><ul>{odds}</ul>" if odds else "")
                + (f"<p><b>Scam flags</b></p><ul>{''.join(f'<li>{e(f)}</li>' for f in t['risk_flags'][:5])}</ul>" if t['risk_flags'] else "")
                + "</details>")
    calls = sorted(out["calls"], key=lambda c: -c["t"])
    rs = [ret(c) for c in calls]
    stats = (f"<div class=stats><div><b>{len(calls)}</b><small>buy calls</small></div>"
             f"<div><b>{(sum(1 for x in rs if x > 0) / len(rs) * 100 if rs else 0):.0f}%</b><small>in profit</small></div>"
             f"<div><b>{(sum(rs) / len(rs) if rs else 0):+.1f}%</b><small>average</small></div></div>")
    crow = "".join(f"<tr><td>{e(c['date'][5:10])}</td><td><b>{e(c['symbol'])}</b></td><td>${c['entry']:.4g}</td>"
                   f"<td style='color:{'#1F6F6B' if ret(c) >= 0 else '#C2552E'}'><b>{ret(c):+.1f}%</b></td></tr>" for c in calls[:40])
    macro = "".join(f"<li>{e(w)}</li>" for w in (out.get("macro") or {}).get("why", []))
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes><meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content="Token Watch"><meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<link rel=apple-touch-icon href=icon.png><link rel=manifest href=manifest.webmanifest><meta http-equiv=refresh content=300>
<title>Token Watch</title><style>
:root{{--bg:#EDF0F3;--card:#fff;--ink:#18212C;--mut:#5C6978;--line:#D3D9E0}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12171E;--card:#1A212B;--ink:#E6EBF1;--mut:#95A2B2;--line:#2C3643}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;
padding:calc(env(safe-area-inset-top) + 12px) 14px calc(env(safe-area-inset-bottom) + 20px)}}
h1{{font-size:1.4rem;margin:0}}h2{{font-size:1.05rem;margin:18px 0 8px}}small{{color:var(--mut)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:8px 0;padding:10px 12px}}
summary{{list-style:none;display:flex;justify-content:space-between;gap:10px;cursor:pointer}}summary::-webkit-details-marker{{display:none}}
.sym{{font-size:1.1rem}}.px{{font-weight:600}}.r{{text-align:right}}.pill{{color:#fff;border-radius:99px;padding:2px 9px;font-size:.8rem;font-weight:700}}
.tag{{font-size:.7rem;border:1px solid var(--line);border-radius:99px;padding:0 6px;color:var(--mut)}}
ul{{padding-left:1.1rem;margin:6px 0}}li{{margin:2px 0;font-size:.9rem}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.stats div{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}}
.stats b{{display:block;font-size:1.3rem}}table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px}}
td{{padding:8px;border-bottom:1px solid var(--line)}}.box{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 12px}}
</style></head><body><h1>Token Watch</h1><small>Updated {e(out['generated'])} · refreshes every 15 min · tap a coin for details</small>
{f"<h2>Market</h2><div class=box><ul>{macro}</ul></div>" if macro else ""}
<h2>Signals</h2>{''.join(card(t) for t in toks) or '<p>No tokens checked yet.</p>'}
<h2>Track record</h2>{stats}<table>{crow}</table>
<p><small>Not financial advice. Signals describe the past and are often wrong.</small></p></body></html>"""
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
        reply(f"{SIG_EMOJI.get(res['signal'], '⚪')} " + ("⭐ " if sym in PREFS["starred"] else "") + summary(res, r["dd"]) + note)

def listen(minutes):
    """After the scheduled check, keep answering Telegram messages until the next run starts."""
    tok, chat = tg_creds()
    if not (tok and chat) or minutes <= 0: return
    end = now() + minutes * 60
    print(f"Listening for Telegram messages for {minutes} min …")
    while now() < end - 5:
        handle_commands(CTX["state"], wait=int(min(25, max(1, end - now() - 5))))
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
                   "weekly_report": cfg_alerts.get("weekly_report", True), **saved.get("alerts", {})},
        "paused": saved.get("paused", False), "added": saved.get("added", []), "removed": saved.get("removed", []),
    })

def save_prefs(state, base):
    base_star = set(s.upper() for s in CFG.get("starred", [])) | {t["symbol"] for t in CFG_WATCH if t.get("starred")}
    cur = set(PREFS["starred"])
    state["_prefs"] = {"star_add": sorted(cur - base_star), "star_del": sorted(base_star - cur),
                       "alerts": PREFS["alerts"], "paused": PREFS["paused"], "added": PREFS["added"], "removed": PREFS["removed"]}

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
        parts = t.strip().split(); cmd = parts[0].lower().split("@")[0]; arg = parts[1].upper() if len(parts) > 1 else ""
        onoff = {"ON": True, "OFF": False}.get(arg)
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
                  + ("\nAdded by command: " + ", ".join(PREFS["added"]) if PREFS["added"] else ""))
        elif cmd in ("/star", "/unstar") and arg:
            st = set(PREFS["starred"]); (st.add if cmd == "/star" else st.discard)(arg); PREFS["starred"] = sorted(st)
            if cmd == "/star" and arg not in PREFS["added"] and arg not in {w["symbol"] for w in CFG_WATCH}: PREFS["added"].append(arg)
            if arg in PREFS["removed"]: PREFS["removed"].remove(arg)
            reply(f"{'⭐ Starred' if cmd == '/star' else 'Unstarred'} {arg}.")
        elif cmd == "/add" and arg:
            if arg not in PREFS["added"]: PREFS["added"].append(arg)
            if arg in PREFS["removed"]: PREFS["removed"].remove(arg)
            reply(f"Added {arg} to your watchlist. Reply /star {arg} to always get its alerts.")
        elif cmd == "/remove" and arg:
            if arg in PREFS["added"]: PREFS["added"].remove(arg)
            if arg not in PREFS["removed"]: PREFS["removed"].append(arg)
            PREFS["starred"] = [x for x in PREFS["starred"] if x != arg]
            reply(f"Removed {arg}.")
        elif cmd in ("/discovered", "/watchlist", "/report") and onoff is not None:
            PREFS["alerts"][{"/discovered": "discovered", "/watchlist": "watchlist", "/report": "weekly_report"}[cmd]] = onoff
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
    have = {t["symbol"] for t in wl}
    wl = [t for t in wl if t["symbol"] not in PREFS["removed"]] + [norm(x) for x in PREFS["added"] if x not in have]
    save_prefs(state, None)
    ids = [t.get("coingecko_id") or try_get("lookup", lambda t=t: resolve_id(t["symbol"], cache)) for t in wl]
    ids += [c.get("cg_id") for c in calls] + ["bitcoin"]
    ids = sorted({i for i in ids if i})
    LIVE.clear()
    if ids: LIVE.update({k: v for k, v in (try_get("live prices", lambda: simple_prices(ids)) or {}).items() if v})
    try_get("market backdrop", load_macro)
    BACKDROP.clear()
    if ind("betting_markets") and (CFG.get("prediction_markets") or {}).get("enabled", True):
        pm = try_get("Bitcoin betting markets", lambda: polymarket("BTC", "Bitcoin"))
        btc_px = LIVE.get("bitcoin")
        if pm and btc_px:
            bv = market_view(pm, btc_px)
            if bv: BACKDROP["btc"] = bv; print(f"Bitcoin betting backdrop {bv['score']:.0f}/100 from {bv['n']} markets")
    handle_commands(state)   # messages sent since the last run (applies /star, /check, etc.)
    wl = [t for t in wl if t["symbol"] not in PREFS["removed"]] + [norm(x) for x in PREFS["added"] if x not in {t["symbol"] for t in wl}]
    results = []
    for tok in wl:
        try:
            r = check_token(tok, cache, state, calls)
            if r: results.append(r)
        except Exception as ex: print(f"  [error] {tok['symbol']}: {ex}")
    try: results += discover(cache, state, calls, {t["symbol"] for t in wl})
    except Exception as ex: print(f"  [discovery error] {ex}")
    prices = dict(LIVE); prices.update({r["res"]["symbol"]: r["res"]["price"] for r in results})
    update_calls(calls, prices)
    wk = datetime.now(timezone.utc).strftime("%G-W%V")
    if calls and state.get("_report_week") != wk:
        state["_report_week"] = wk; send(report_text(calls), "report")
    save_prefs(state, None)
    save("cache.json", cache); save("state.json", state); save("calls.json", calls); save_hist()
    # export.json / dashboard are big; rewrite them every few hours, not every 15 minutes
    full = DEMO or now() - state.get("_export_t", 0) >= CFG.get("export_every_minutes", 240) * 60
    export(results, calls, full)
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
    a = ap.parse_args()
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path): path = os.path.join(HERE, "config.example.json")
    CFG = json.load(open(path))
    for _k in ("coingecko_api_key", "cryptopanic_api_key", "whale_alert_api_key"):  # keys from GitHub Secrets override config.json
        if os.environ.get(_k.upper()): CFG[_k] = os.environ[_k.upper()]
    if a.demo:
        DEMO = True; DATA = os.path.join(HERE, "data-demo"); CFG["coingecko_min_seconds"] = 0; install_demo()
        CFG["watchlist"] = [{"symbol": "INJ", "team_doxxed": True, "audited": True, "fundamental_grade": "B", "alerts": {"below": 6.5}}]
    if a.report: print(report_text(load("calls.json", []))); return
    if a.demo:
        CFG["watchlist"][0]["starred"] = True
        CFG["alerts"] = {"watchlist": True, "discovered": False, "weekly_report": True}
    if a.test_telegram:
        tg = CFG.get("telegram") or {}
        if not ((os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")) and (os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id"))):
            sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets (or fill them in config.json).")
        load_prefs({}); send(f"Token Watch is connected. You'll get alerts here. ({iso()})\n\n" + HELP); return
    while True:
        run_once()
        if a.once and not a.demo: listen(CFG.get("listen_minutes", 0) if os.environ.get("GITHUB_ACTIONS") else 0)
        if a.once or a.demo: break
        mins = CFG.get("check_every_minutes", 15); print(f"Next check in {mins} min. Ctrl+C to stop."); time.sleep(mins * 60)

CFG = {}
if __name__ == "__main__": main()
