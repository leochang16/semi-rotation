#!/usr/bin/env python3
"""US Semiconductor Sector Rotation Dashboard — data engine.

Fetches ~1y daily OHLCV from Yahoo Finance, builds equal-weight and
cap-weight sector indices, and computes rotation metrics.
Everything is recomputed from price history, so no state carries between runs.
"""
import urllib.request, urllib.parse, json, http.cookiejar, time, math, sys
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

# ---------------------------------------------------------------- config
SECTORS = {
    "算力晶片":      ["NVDA", "AVGO", "AMD", "MRVL", "ARM", "CBRS"],
    "晶圓代工/封測": ["TSM", "INTC", "ASX", "UMC", "GFS", "AMKR"],
    "半導體設備":    ["ASML", "AMAT", "LRCX", "KLAC", "TER", "ONTO"],
    "記憶體/儲存":   ["MU", "SKHY", "SNDK", "STX", "WDC", "SIMO"],
    "光通訊/CPO":    ["COHR", "LITE", "NOK", "TSEM", "FN", "AAOI", "AXTI"],
    "高速互連/網通": ["CSCO", "ANET", "ALAB", "CRDO", "CIEN", "APH"],
    "類比/功率/被動":["TXN", "ADI", "MPWR", "NXPI", "MCHP", "ON"],
}
OUTER = {
    "AI 伺服器/ODM":    ["SMCI", "DELL", "HPE", "CLS", "FLEX", "JBL"],
    "Neocloud/AI 租賃": ["CRWV", "NBIS", "IREN", "APLD", "WULF", "CIFR"],
    "AI 軟體/應用":     ["PLTR", "ORCL", "CRM", "NOW", "SNOW", "DDOG", "TEAM"],
    "資安":             ["PANW", "CRWD", "FTNT", "NET", "ZS", "OKTA"],
    "電力/散熱基建":    ["GEV", "ETN", "VRT", "PWR", "CEG", "VST"],
    "機器人/實體 AI":   ["ROK", "ZBRA", "SYM", "CGNX", "OUST", "CCXI"],
    "七巨頭":           ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
}
SECTOR_EN = {
    "算力晶片": "AI Compute", "晶圓代工/封測": "Foundry / OSAT",
    "半導體設備": "Semicap Equipment", "材料/零組件": "Materials / Subsystems",
    "EDA/IP": "EDA / IP", "記憶體/儲存": "Memory / Storage",
    "光通訊/CPO": "Optical / CPO", "高速互連/網通": "Interconnect / Networking",
    "類比/功率/被動": "Analog / Power / Passive",
    "AI 伺服器/ODM": "AI Servers / ODM",
    "Neocloud/AI 租賃": "Neocloud / AI Rental", "AI 軟體/應用": "AI Software",
    "資安": "Cybersecurity", "電力/散熱基建": "Power & Thermal Infra",
    "機器人/實體 AI": "Robotics / Physical AI",
    "七巨頭": "Magnificent 7", "半導體整體": "Semis Composite",
}
# supply-chain position: downstream (end demand) -> upstream (materials).
# The framework question is "which layer is getting paid now, and which later".
STACK = {
    "高速互連/網通": (1, "系統 / 網路"),
    "光通訊/CPO":    (2, "模組 / 引擎"),
    "算力晶片":      (3, "晶片 / 元件"),
    "記憶體/儲存":   (3, "晶片 / 元件"),
    "類比/功率/被動":(3, "晶片 / 元件"),
    "晶圓代工/封測": (5, "代工 / 封測"),
    "半導體設備":    (6, "設備"),
}
CATEGORY = {**{k: "半導體" for k in SECTORS}, **{k: "其他 AI" for k in OUTER}}
ALL_GROUPS = {**SECTORS, **OUTER}
BENCH = ["^SOX", "SPY"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

ALL_TICKERS = sorted({t for v in SECTORS.values() for t in v}
                     | {t for v in OUTER.values() for t in v} | set(BENCH))

# ---------------------------------------------------------------- fetch
def make_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(urllib.request.Request("https://fc.yahoo.com", headers=UA), timeout=15)
    except Exception:
        pass
    crumb = None
    try:
        crumb = op.open(urllib.request.Request(
            "https://query2.finance.yahoo.com/v1/test/getcrumb", headers=UA), timeout=15).read().decode()
    except Exception as e:
        print("crumb failed:", e, file=sys.stderr)
    return op, crumb

def get_json(op, url, tries=4):
    last = None
    for i in range(tries):
        try:
            return json.load(op.open(urllib.request.Request(url, headers=UA), timeout=30))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")

def fetch_chart(op, tk):
    u = (f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(tk)}"
         f"?range=2y&interval=1d&events=div%2Csplit")
    d = get_json(op, u)["chart"]["result"][0]
    ts = d["timestamp"]
    q = d["indicators"]["quote"][0]
    adj = d["indicators"].get("adjclose", [{}])[0].get("adjclose", q["close"])
    idx = pd.to_datetime([datetime.fromtimestamp(t, tz=timezone.utc).date() for t in ts])
    df = pd.DataFrame({"close": q["close"], "adj": adj, "volume": q["volume"]}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["adj"])
    df["name"] = d["meta"].get("shortName", tk)
    return df

def fetch_shares(op, crumb, tickers):
    out = {}
    for i in range(0, len(tickers), 25):
        chunk = tickers[i:i + 25]
        u = ("https://query2.finance.yahoo.com/v7/finance/quote?symbols="
             + ",".join(chunk) + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""))
        try:
            for r in get_json(op, u)["quoteResponse"]["result"]:
                out[r["symbol"]] = {"mcap": r.get("marketCap"),
                                    "shares": r.get("sharesOutstanding"),
                                    "name": r.get("shortName") or r.get("longName")}
        except Exception as e:
            print("quote chunk failed:", e, file=sys.stderr)
        time.sleep(0.4)
    # fallback: quoteSummary carries nonDilutedMarketCap when quote omits marketCap
    for tk in tickers:
        if out.get(tk, {}).get("mcap"):
            continue
        try:
            u = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{tk}"
                 f"?modules=summaryDetail,price"
                 + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""))
            r = get_json(op, u, 2)["quoteSummary"]["result"][0]
            mc = (r.get("summaryDetail", {}).get("nonDilutedMarketCap", {}) or {}).get("raw")
            px = (r.get("price", {}).get("regularMarketPrice", {}) or {}).get("raw")
            d = out.setdefault(tk, {"mcap": None, "shares": None, "name": tk})
            if mc:
                d["mcap"] = mc
                if px:
                    d["shares"] = mc / px
            if not d.get("name") or d["name"] == tk:
                d["name"] = (r.get("price", {}).get("shortName")
                             or r.get("price", {}).get("longName") or tk)
        except Exception as e:
            print("quoteSummary fallback failed", tk, e, file=sys.stderr)
        time.sleep(0.3)
    return out

# ---------------------------------------------------------------- metrics
def rsi(series, n=14):
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs))

def pct(series, n):
    """% return over n trading bars ending at each point."""
    return series.pct_change(n) * 100

def build():
    op, crumb = make_opener()
    charts, failed = {}, []
    for tk in ALL_TICKERS:
        try:
            charts[tk] = fetch_chart(op, tk)
        except Exception as e:
            failed.append(tk); print("FAIL", tk, e, file=sys.stderr)
        time.sleep(0.25)
    info = fetch_shares(op, crumb, [t for t in ALL_TICKERS if t in charts])

    adj = pd.DataFrame({t: c["adj"] for t, c in charts.items()})
    raw = pd.DataFrame({t: c["close"] for t, c in charts.items()})
    vol = pd.DataFrame({t: c["volume"] for t, c in charts.items()})
    adj = adj.sort_index().ffill(limit=3)
    dv = (raw * vol).sort_index()                     # dollar volume
    ret = adj.pct_change()

    dates = adj.index
    asof = dates[-1]

    def compute_tier(groups, bench_tk, extra_index=None):
        """Build sector indices + rotation metrics for one tier, vs one benchmark."""
        sec_ew, sec_cw, sec_dv = {}, {}, {}
        for s, tks in groups.items():
            tks = [t for t in tks if t in adj.columns]
            r = ret[tks]
            sec_ew[s] = (1 + r.mean(axis=1).fillna(0)).cumprod()
            sh = pd.Series({t: (info.get(t, {}).get("shares") or np.nan) for t in tks})
            cap = adj[tks].mul(sh, axis=1)
            w = cap.div(cap.sum(axis=1), axis=0).shift(1)
            sec_cw[s] = (1 + (r * w).sum(axis=1).fillna(0)).cumprod()
            sec_dv[s] = dv[tks].sum(axis=1)
        sec_ew = pd.DataFrame(sec_ew); sec_cw = pd.DataFrame(sec_cw); sec_dv = pd.DataFrame(sec_dv)
        dv_share = sec_dv.div(sec_dv.sum(axis=1), axis=0) * 100
        n_last = len(dates) - 1

        def rs_at(off):
            i = n_last - off
            return pd.DataFrame({
                s: {lbl: (sec_ew[s].iloc[i] / sec_ew[s].iloc[i - n] - 1) * 100
                         - (adj[bench_tk].iloc[i] / adj[bench_tk].iloc[i - n] - 1) * 100
                    for lbl, n in WINDOWS.items()} for s in groups}).T

        rs_now, rs_3ago = rs_at(0), rs_at(3)
        RANK_WIN = "1M"   # 20 交易日。5 日窗實測不穩且無預測力，20 日才站得住。
        rank_now = rs_now[RANK_WIN].rank(ascending=False, method="min").astype(int)
        rank_3ago = rs_3ago[RANK_WIN].rank(ascending=False, method="min").astype(int)

        i = n_last
        out = []
        for s, tks in groups.items():
            tks = [t for t in tks if t in adj.columns]
            idx_ew, idx_cw = sec_ew[s], sec_cw[s]
            w52 = idx_ew.iloc[-252:] if len(idx_ew) >= 252 else idx_ew
            r1 = ret[tks].iloc[-1] * 100
            r5 = (adj[tks].iloc[-1] / adj[tks].iloc[-6] - 1) * 100
            dvs = sec_dv[s]
            rec = {
                "sector": s, "sector_en": SECTOR_EN.get(s, s), "n": len(tks),
                "layer": STACK.get(s, (99, ""))[0], "layer_name": STACK.get(s, (99, ""))[1],
                "cat": CATEGORY.get(s, ""),
                "bench": bench_tk,
                "ret": {k: float((idx_ew.iloc[i] / idx_ew.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
                "ret_cw": {k: float((idx_cw.iloc[i] / idx_cw.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
                "rs_sox": {k: float((idx_ew.iloc[i] / idx_ew.iloc[i - n] - 1) * 100
                                    - (adj["^SOX"].iloc[i] / adj["^SOX"].iloc[i - n] - 1) * 100)
                           for k, n in WINDOWS.items()},
                "rs_spy": {k: float((idx_ew.iloc[i] / idx_ew.iloc[i - n] - 1) * 100
                                    - (adj["SPY"].iloc[i] / adj["SPY"].iloc[i - n] - 1) * 100)
                           for k, n in WINDOWS.items()},
                "rank": int(rank_now[s]), "rank_prev": int(rank_3ago[s]),
                "d_rank": int(rank_3ago[s] - rank_now[s]),
                "dv_share": float(dv_share[s].iloc[-1]),
                "dv_share_avg20": float(dv_share[s].iloc[-21:-1].mean()),
                "vol_ratio": float(dvs.iloc[-1] / dvs.iloc[-21:-1].mean()),
                "breadth": float((adj[tks].iloc[-1] > adj[tks].rolling(20).mean().iloc[-1]).mean() * 100),
                "disp_1d": float(r1.std()), "disp_1w": float(r5.std()),
                "vs_ma20": float((idx_ew.iloc[-1] / idx_ew.rolling(20).mean().iloc[-1] - 1) * 100),
                "vs_ma50": float((idx_ew.iloc[-1] / idx_ew.rolling(50).mean().iloc[-1] - 1) * 100),
                "from_52wh": float((idx_ew.iloc[-1] / w52.max() - 1) * 100),
                "rsi": float(rsi(idx_ew).iloc[-1]),
                "vol20": float(idx_ew.pct_change().iloc[-20:].std() * (252 ** 0.5) * 100),
                "vol60": float(idx_ew.pct_change().iloc[-60:].std() * (252 ** 0.5) * 100),
                "vol_trend": float(idx_ew.pct_change().iloc[-20:].std()
                                   / idx_ew.pct_change().iloc[-60:].std())
                              if idx_ew.pct_change().iloc[-60:].std() > 0 else 1.0,
                "corr_nvda": float(idx_ew.pct_change().iloc[-20:].corr(ret["NVDA"].iloc[-20:])),
                "spark": [float(x) for x in (idx_ew.iloc[-30:] / idx_ew.iloc[-30] * 100)],
            }
            rec["rs"] = rec["rs_sox"] if bench_tk == "^SOX" else rec["rs_spy"]
            rec["dv_share_chg"] = rec["dv_share"] - rec["dv_share_avg20"]
            rec["cap_minus_ew"] = rec["ret_cw"]["1W"] - rec["ret"]["1W"]
            bench_1w = (adj["^SOX"].iloc[-1] / adj["^SOX"].iloc[-6] - 1) * 100

            stocks = []
            for t in tks:
                p = adj[t].dropna()
                w52t = p.iloc[-252:] if len(p) >= 252 else p
                stocks.append({
                    "ticker": t,
                    "name": (info.get(t, {}).get("name") or charts[t]["name"].iloc[0])[:26],
                    "mcap": info.get(t, {}).get("mcap"),
                    "price": float(raw[t].dropna().iloc[-1]),
                    "r1": float((p.iloc[-1] / p.iloc[-2] - 1) * 100),
                    "r3": float((p.iloc[-1] / p.iloc[-4] - 1) * 100) if len(p) > 4 else None,
                    "r5": float((p.iloc[-1] / p.iloc[-6] - 1) * 100) if len(p) > 6 else None,
                    "r20": float((p.iloc[-1] / p.iloc[-21] - 1) * 100) if len(p) > 21 else None,
                    "rs5": float((p.iloc[-1] / p.iloc[-6] - 1) * 100 - bench_1w) if len(p) > 6 else None,
                    "volr": float(dv[t].iloc[-1] / dv[t].iloc[-21:-1].mean()),
                    "adv20": float(dv[t].iloc[-21:-1].mean()),
                    "f52": float((p.iloc[-1] / w52t.max() - 1) * 100),
                    "rsi": float(rsi(p).iloc[-1]),
                    "a20": bool(p.iloc[-1] > p.rolling(20).mean().iloc[-1]),
                })
            rec["stocks"] = sorted(stocks, key=lambda x: -(x["r5"] if x["r5"] is not None else -99))
            out.append(rec)
        return out, sec_ew

    WINDOWS = {"1D": 1, "3D": 3, "1W": 5, "1M": 20}

    allrows, _ = compute_tier(ALL_GROUPS, "^SOX")

    # ---- 半導體整體 composite: all tier-1 names, equal weight — the cross-tier yardstick
    semi_tks = [t for v in SECTORS.values() for t in v if t in adj.columns]
    comp = (1 + ret[semi_tks].mean(axis=1).fillna(0)).cumprod()
    i = len(dates) - 1
    composite = {
        "sector": "半導體整體", "sector_en": SECTOR_EN["半導體整體"], "n": len(semi_tks),
        "ret": {k: float((comp.iloc[i] / comp.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
        "rs_spy": {k: float((comp.iloc[i] / comp.iloc[i - n] - 1) * 100
                            - (adj["SPY"].iloc[i] / adj["SPY"].iloc[i - n] - 1) * 100)
                   for k, n in WINDOWS.items()},
        "breadth": float((adj[semi_tks].iloc[-1] > adj[semi_tks].rolling(20).mean().iloc[-1]).mean() * 100),
        "vs_ma20": float((comp.iloc[-1] / comp.rolling(20).mean().iloc[-1] - 1) * 100),
        "rsi": float(rsi(comp).iloc[-1]),
        "vol20": float(comp.pct_change().iloc[-20:].std() * (252 ** 0.5) * 100),
        "from_52wh": float((comp.iloc[-1] / (comp.iloc[-252:] if len(comp) >= 252 else comp).max() - 1) * 100),
        "dv_share": None, "dv_share_chg": None, "breadth_": None,
        "spark": [float(x) for x in (comp.iloc[-30:] / comp.iloc[-30] * 100)],
    }
    composite["rs"] = composite["rs_spy"]

    bench = {}
    for b in BENCH:
        bp = adj[b]
        w52b = bp.iloc[-252:] if len(bp) >= 252 else bp
        bench[b] = {
            "ret": {k: float((bp.iloc[i] / bp.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
            "vol20": float(bp.pct_change().iloc[-20:].std() * (252 ** 0.5) * 100),
            "from_52wh": float((bp.iloc[-1] / w52b.max() - 1) * 100),
            "rsi": float(rsi(bp).iloc[-1]),
            "spark": [float(x) for x in (bp.iloc[-30:] / bp.iloc[-30] * 100)],
        }

    return {
        "asof": str(asof.date()),
        "sectors": allrows, "composite": composite,
        "bench": bench, "failed": failed, "windows": list(WINDOWS.keys()),
    }


import html, os
OUT_HTML = os.environ.get("OUT_HTML", "index.html")
OUT_JSON = os.environ.get("OUT_JSON", "data.json")
D = build()
S, C, B = D["sectors"], D["composite"], D["bench"]
OTHERS = [s for s in S if s.get("cat") == "其他 AI"]

W4 = ["1D", "3D", "1W", "1M"]

def add_rel_vol(rows):
    """相對量能 = (板塊成交額 ÷ 自身 20 日均量) ÷ 全表平均。

    比「成交額佔比」乾淨：佔比是零和的（9 塊加總必為 100%），兩個權值最大的板塊
    會主導分母，別的板塊即使量沒變也會被動位移。相對量能是絕對量的比值，又用全表
    平均校正掉「大盤整體放量」的日子。
    """
    m = sum(s["vol_ratio"] for s in rows) / max(len(rows), 1)
    for s in rows:
        s["rel_vol"] = s["vol_ratio"] / m if m else 1.0
    return rows

add_rel_vol(S)

# ---------------------------------------------------------------- color
def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _hex(c):
    return "#%02x%02x%02x" % c

# diverging: red = up (台股慣例), blue = down; neutral gray midpoint
POLES = {
    "light": {"neutral": (240, 239, 236), "up": (164, 40, 34), "down": (24, 79, 149)},
    "dark":  {"neutral": (56, 56, 53),    "up": (230, 103, 103), "down": (57, 135, 229)},
}

def heat(v, scale, mode="light"):
    p = POLES[mode]
    if v is None or scale <= 0:
        return "transparent", "var(--ink-2)"
    t = max(-1.0, min(1.0, v / scale))
    a = abs(t) ** 0.75
    return _hex(_mix(p["neutral"], p["up"] if t >= 0 else p["down"], a)), \
           ("#ffffff" if a > 0.55 else "var(--ink-1)")

def cell(v, scale, fmt="{:+.2f}", suffix="", cls=""):
    if v is None:
        return f'<td class="num {cls}">—</td>'
    bg, fg = heat(v, scale, "dark")
    return (f'<td class="num heat {cls}" style="--bg:{bg};--fg:{fg}">'
            f'{fmt.format(v)}{suffix}</td>')

def maxabs(vals, floor=0.5):
    vs = [abs(v) for v in vals if v is not None]
    return max(max(vs) if vs else floor, floor)

def money(v):
    if not v:
        return "—"
    return f"{v/1e9:.1f}B" if v >= 1e9 else f"{v/1e6:.0f}M"

# ---------------------------------------------------------------- signals
def signals(s):
    """該板塊當天的重點，做成短標籤。最多 3 個，最重要的在前。"""
    out = []
    if s["ret"]["1W"] > 2 and s["rel_vol"] <= 0.85:
        out.append(("warn", "縮量",
                    f'漲 {s["ret"]["1W"]:+.1f}% 但相對量能只有 {s["rel_vol"]:.2f}×，沒有增量資金'))
    if s["rel_vol"] >= 1.3:
        out.append(("up", "爆量",
                    f'相對量能 {s["rel_vol"]:.2f}×（自身量能 {s["vol_ratio"]:.2f}× 對比全表平均）'))
    if s["ret"]["1W"] > 2 and s["breadth"] <= 40:
        out.append(("warn", "個股行情",
                    f'只有 {s["breadth"]:.0f}% 成分股站上 20MA，不是板塊性行情'))
    if s["breadth"] >= 90:
        out.append(("up", "全面性", f'{s["breadth"]:.0f}% 成分股站上 20MA'))
    if s["breadth"] <= 15:
        out.append(("down", "全面走弱", f'僅 {s["breadth"]:.0f}% 成分股站上 20MA'))
    spread = s.get("cap_minus_ew", 0.0)
    if spread <= -3:
        out.append(("up", "小型領漲",
                    f'等權比市值加權高 {abs(spread):.1f} pp——板塊內中小型股在領，龍頭沒跟上'))
    if spread >= 3:
        out.append(("warn", "大型獨撐",
                    f'市值加權比等權高 {spread:.1f} pp——只有權值股在撐，其餘成分股弱'))
    if s["d_rank"] >= 3:
        out.append(("up", f'躍升 {s["d_rank"]}', f'3 日內 {s["rank_prev"]} → {s["rank"]} 名'))
    if s["d_rank"] <= -3:
        out.append(("down", f'下滑 {abs(s["d_rank"])}', f'3 日內 {s["rank_prev"]} → {s["rank"]} 名'))
    return out[:3]

def chips(s):
    return "".join(f'<span class="sg {t}" title="{html.escape(tip)}">{html.escape(lbl)}</span>'
                   for t, lbl, tip in signals(s))

# ---------------------------------------------------------------- pieces
def sparkline(vals, w=98, h=22):
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i/(len(vals)-1)*w:.1f},{h-2-(v-lo)/rng*(h-4):.1f}" for i, v in enumerate(vals))
    col = "var(--up)" if vals[-1] >= vals[0] else "var(--down)"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')

def slope_chart(rows, lw=200):
    n = len(rows)
    W, H = 1180, 46 + (n - 1) * 30
    pad_t, x0, x1 = 34, lw, W - lw
    rowh = (H - pad_t - 16) / max(n - 1, 1)
    def y(r): return pad_t + (r - 1) * rowh
    p = [f'<svg viewBox="0 0 {W} {H}" class="slope" role="img" aria-label="板塊排名遷移">',
         f'<text x="{x0}" y="15" class="axl" text-anchor="middle">3 個交易日前</text>',
         f'<text x="{x1}" y="15" class="axl" text-anchor="middle">最新</text>']
    for r in range(1, n + 1):
        p.append(f'<line x1="{x0}" y1="{y(r):.1f}" x2="{x1}" y2="{y(r):.1f}" class="glin"/>')
    for s in sorted(rows, key=lambda z: z["rank"]):
        a, b, d = s["rank_prev"], s["rank"], s["d_rank"]
        col = "var(--up)" if d > 0 else ("var(--down)" if d < 0 else "var(--ink-3)")
        p.append(f'<path d="M{x0},{y(a):.1f} C{x0+80},{y(a):.1f} {x1-80},{y(b):.1f} {x1},{y(b):.1f}" '
                 f'fill="none" stroke="{col}" stroke-width="{2.6 if d else 1.4}" '
                 f'opacity="{0.95 if d else 0.45}"/>')
        p.append(f'<circle cx="{x0}" cy="{y(a):.1f}" r="3.4" fill="{col}"/>')
        p.append(f'<circle cx="{x1}" cy="{y(b):.1f}" r="3.4" fill="{col}"/>')
        p.append(f'<text x="{x0-9}" y="{y(a)+4:.1f}" class="slab" text-anchor="end">'
                 f'{a}. {html.escape(s["sector"])}</text>')
        p.append(f'<text x="{x1+9}" y="{y(b)+4:.1f}" class="slab">'
                 f'{b}. {html.escape(s["sector"])}{f" ({d:+d})" if d else ""}</text>')
    return "".join(p) + "</svg>"

# ---------------------------------------------------------------- table
HEAD = """<tr class="h">
<th class="nosort">名次<br><span class="th2">Δ3日</span></th>
<th class="nosort">板塊</th><th class="nosort">30日走勢</th>
<th class="num">1D</th><th class="num">3D</th><th class="num">1W</th><th class="num">1M</th>
<th class="num sep" title="（板塊今日成交額 ÷ 自身 20 日均量）÷ 全表平均。&gt;1 代表這板塊放量放得比大盤兇。">相對量能</th>
<th class="num sep" title="板塊內站上 20 日均線的成分股比例">廣度<br><span class="th2">&gt;20MA</span></th>
<th class="num" title="近 20 日報酬的標準差 σ（下方為 σ×√5 的一週推估）。約 8 成的交易日會落在 ±σ 內，不是平均變動幅度——平常大約只動一半。抓停損寬度用：停損設得比它窄，會被日常波動掃掉。標「偏高」＝這個 σ 被近期一段劇烈行情灌大了，波動率會均值回歸，接下來多半回落，照它設停損會太寬。標「偏低」＝目前異常平靜，之後多半回升，照它設停損會被掃掉——這個比較危險。">預期波動<br><span class="th2">單日 σ / 一週</span></th></tr>"""

def ref_row(name, note, d, breadth=None):
    tds = ['<td class="rk">—</td>',
           f'<td class="sec"><div class="sname">{html.escape(name)}</div>'
           f'<div class="sen">{html.escape(note)}</div></td>',
           f'<td class="spk">{sparkline(d["spark"])}</td>']
    tds += [f'<td class="num b">{d["ret"][k]:+.2f}%</td>' for k in W4]
    tds.append('<td class="num sep">—</td>')
    tds.append(f'<td class="num sep">{breadth:.0f}%</td>' if breadth is not None
               else '<td class="num sep">—</td>')
    tds.append(f'<td class="num">±{d["vol20"]/15.875:.1f}%<br><span class="th2">±{d["vol20"]/7.211:.1f}%</span></td>')
    return f'<tr class="comp">{"".join(tds)}</tr>'

def sector_rows(rows):
    sc = {k: maxabs([s["ret"][k] for s in rows], 1.0) for k in W4}
    out = []
    for s in sorted(rows, key=lambda z: z["rank"]):
        d = s["d_rank"]
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        dcls = "up" if d > 0 else ("down" if d < 0 else "flat")
        lyr = s.get("layer", 99)
        r = [f'<tr data-anom="{1 if signals(s) else 0}" data-layer="{lyr}">',
             f'<td class="rk"><span class="rknum">{s["rank"]}</span>'
             f'<span class="drk {dcls}">{arrow}{abs(d) if d else ""}</span></td>',
             f'<td class="sec"><div class="sname">{html.escape(s["sector"])}'
             f'<span class="cnt">{s["n"]}</span>'
             + (f'<span class="cat{"2" if s.get("cat")=="其他 AI" else ""}">{html.escape(s.get("cat",""))}</span>')
             + (f'<span class="lyrn">{html.escape(s.get("layer_name",""))}</span>' if lyr != 99 else '')
             + f'</div><div class="sgs">{chips(s)}</div></td>',
             f'<td class="spk">{sparkline(s["spark"])}</td>']
        for k in W4:
            r.append(cell(s["ret"][k], sc[k], "{:+.2f}", "%"))
        rv = s["rel_vol"]
        r.append(f'<td class="num sep"><span class="chip {"hot" if rv>=1.3 else ("cold" if rv<=0.7 else "")}">'
                 f'{rv:.2f}×</span></td>')
        bd = s["breadth"]
        r.append(f'<td class="num sep"><div class="bar"><i style="width:{bd:.0f}%"></i></div>'
                 f'<span class="bn">{bd:.0f}%</span></td>')
        vt = s.get("vol_trend", 1.0)
        v6 = s.get("vol60", 0) / 15.875
        v2 = s["vol20"] / 15.875
        tip = f"20日 σ {v2:.2f}% ÷ 60日 σ {v6:.2f}% = {vt:.2f}"
        mk = (f' <span class="vt hi" title="{tip}｜≥1.25 判定偏高">偏高</span>' if vt >= 1.25
              else f' <span class="vt lo" title="{tip}｜≤0.80 判定偏低">偏低</span>' if vt <= 0.80 else "")
        r.append(f'<td class="num">±{s["vol20"]/15.875:.1f}%{mk}<br><span class="th2 dim">±{s["vol20"]/7.211:.1f}%</span></td>')
        out.append("".join(r) + "</tr>")
    return "".join(out)

def stock_blocks(rows, tier):
    out = []
    for s in sorted(rows, key=lambda z: z["rank"]):
        st = s["stocks"]
        sc = {k: maxabs([x[k] for x in st], 1.0) for k in ("r1", "r3", "r5", "r20")}
        trs = []
        for x in st:
            trs.append(
                f'<tr><td class="tk">{x["ticker"]}</td><td class="nm">{html.escape(x["name"])}</td>'
                f'<td class="num dim">{money(x.get("adv20"))}</td>'
                + cell(x["r1"], sc["r1"], "{:+.2f}", "%") + cell(x["r3"], sc["r3"], "{:+.2f}", "%")
                + cell(x["r5"], sc["r5"], "{:+.2f}", "%") + cell(x["r20"], sc["r20"], "{:+.2f}", "%")
                + f'<td class="num"><span class="chip {"hot" if x["volr"]>=1.5 else ""}">'
                  f'{x["volr"]:.2f}×</span></td>'
                  f'<td class="num">{x["f52"]:+.1f}%</td>'
                  f'<td class="num"><span class="chip {"hot" if x["rsi"]>80 else ""}">'
                  f'{x["rsi"]:.0f}</span></td>'
                  f'<td class="num">{"✓" if x["a20"] else "·"}</td></tr>')
        out.append(
            f'<details class="sblock"><summary><span class="srk">{tier}#{s["rank"]}</span>'
            f'{html.escape(s["sector"])}<span class="ssum">1W {s["ret"]["1W"]:+.2f}% · '
            f'廣度 {s["breadth"]:.0f}% · 相對量能 {s["rel_vol"]:.2f}×</span></summary>'
            f'<div class="tw"><table class="stk"><thead><tr><th>代號</th><th>名稱</th>'
            f'<th class="num" title="20 日平均成交金額——這檔能吃多少量不滑價">20日均額</th>'
            f'<th class="num">1D</th><th class="num">3D</th><th class="num">1W</th>'
            f'<th class="num">1M</th>'
            f'<th class="num" title="今日成交額 ÷ 自身 20 日均量">量能</th>'
            f'<th class="num">距52週高</th>'
            f'<th class="num" title="RSI(14)。只標 &gt;80——回測中唯一報酬翻負的區間。70-80 反而是表現最好的一段，不是警訊；&lt;30 絕對報酬也不差。">RSI</th>'
            f'<th class="num" title="收盤價是否站上 20 日均線">&gt;20MA</th>'
            f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div></details>')
    return "".join(out)

# ---------------------------------------------------------------- 方向判讀
def read_out():
    """三行規則判讀。純粹把成立的條件攤開，不是進出場指令。"""
    out = []

    # 偏多：名次最前、且有量能與廣度確認
    longs = [s for s in S if s["rel_vol"] >= 1.0 and s["breadth"] >= 60]
    lg = min(longs, key=lambda z: z["rank"]) if longs else min(S, key=lambda z: z["rank"])
    ok = lg["rel_vol"] >= 1.0 and lg["breadth"] >= 60
    tp = lg["stocks"][0]
    if ok:
        txt = (f'<b>{html.escape(lg["sector"])}</b> 三個條件都成立：1W {lg["ret"]["1W"]:+.1f}%、'
               f'相對量能 {lg["rel_vol"]:.2f}×、廣度 {lg["breadth"]:.0f}%。'
               f'領頭 {tp["ticker"]} {tp["r5"]:+.1f}%。')
    else:
        txt = (f'<b>{html.escape(lg["sector"])}</b> 名次第一但確認不足：'
               f'相對量能 {lg["rel_vol"]:.2f}×、廣度 {lg["breadth"]:.0f}%——追價要小心。')
    out.append(("up", "偏多" if ok else "偏多（弱）", txt))

    # 偏空：名次最後，量能決定賣壓真不真
    wk = max(S, key=lambda z: z["rank"])
    bot_stock = wk["stocks"][-1]
    heavy = wk["rel_vol"] >= 1.0
    txt = (f'<b>{html.escape(wk["sector"])}</b> 1W {wk["ret"]["1W"]:+.1f}%、廣度 {wk["breadth"]:.0f}%、'
           f'相對量能 {wk["rel_vol"]:.2f}×'
           + ("，<b>跌得有量</b>，賣壓是真的。" if heavy else "，量縮陰跌，反彈也沒力。"))
    if wk["d_rank"] <= -2:
        txt += f' 3 日內 {wk["rank_prev"]} → {wk["rank"]} 名。'
    txt += f' 最弱 {bot_stock["ticker"]} {bot_stock["r5"]:+.1f}%。'
    out.append(("down", "偏空", txt))

    # 留意：當日最值得警戒的一件事
    fake = [s for s in S if s["ret"]["1W"] > 2 and s["rel_vol"] <= 0.85]
    narrow = [s for s in S if s["ret"]["1W"] > 2 and s["breadth"] <= 40]
    jump = [s for s in S if abs(s["d_rank"]) >= 3]
    best_o = max(OTHERS, key=lambda z: z["ret"]["1W"]) if OTHERS else None
    if fake:
        f = min(fake, key=lambda z: z["rel_vol"])
        note = (f'<b>{html.escape(f["sector"])}</b> 漲 {f["ret"]["1W"]:+.1f}% 但相對量能只有 '
                f'{f["rel_vol"]:.2f}×——縮量反彈，別當突破追。')
    elif narrow:
        n = min(narrow, key=lambda z: z["breadth"])
        note = (f'<b>{html.escape(n["sector"])}</b> 漲 {n["ret"]["1W"]:+.1f}% 但廣度只有 '
                f'{n["breadth"]:.0f}%——是個股在拉，不是板塊行情。')
    elif jump:
        j = max(jump, key=lambda z: abs(z["d_rank"]))
        note = (f'<b>{html.escape(j["sector"])}</b> 3 日內 {j["rank_prev"]} → {j["rank"]} 名，'
                f'相對量能 {j["rel_vol"]:.2f}×——輪動剛換手。')
    elif best_o and best_o["ret"]["1W"] > C["ret"]["1W"]:
        note = (f'<b>{html.escape(best_o["sector"])}</b>（其他 AI）1W {best_o["ret"]["1W"]:+.1f}%，'
                f'比半導體整體 {C["ret"]["1W"]:+.1f}% 強——題材重心不在半導體這邊。')
    else:
        hv = max(S, key=lambda z: z["rel_vol"])
        note = (f'<b>{html.escape(hv["sector"])}</b> 相對量能 {hv["rel_vol"]:.2f}× 全表最高，'
                f'1W {hv["ret"]["1W"]:+.1f}%——量先到，價還沒走完。')
    out.append(("warn", "留意", note))
    return out

DIGEST = "".join(f'<li class="dg {t}"><span class="dtag">{tag}</span><span>{txt}</span></li>'
                 for t, tag, txt in read_out())

# ---------------------------------------------------------------- KPIs
top, bot = min(S, key=lambda z: z["rank"]), max(S, key=lambda z: z["rank"])
hivol = max(S, key=lambda z: z["rel_vol"])

def kpi(lbl, val, sub, tone=""):
    return (f'<div class="kpi"><div class="klbl">{lbl}</div><div class="kval {tone}">{val}</div>'
            f'<div class="ksub">{sub}</div></div>')

KPIS = "".join([
    kpi("半導體整體 1W", f'{C["ret"]["1W"]:+.2f}%',
        f'費半 {B["^SOX"]["ret"]["1W"]:+.2f}% · SPY {B["SPY"]["ret"]["1W"]:+.2f}%',
        "up" if C["ret"]["1W"] >= 0 else "down"),
    kpi("最強板塊", html.escape(top["sector"]),
        f'1W {top["ret"]["1W"]:+.2f}% · 廣度 {top["breadth"]:.0f}%', "up"),
    kpi("最弱板塊", html.escape(bot["sector"]),
        f'1W {bot["ret"]["1W"]:+.2f}% · 廣度 {bot["breadth"]:.0f}%', "down"),
    kpi("量能最集中", html.escape(hivol["sector"]),
        f'相對量能 {hivol["rel_vol"]:.2f}× · 1W {hivol["ret"]["1W"]:+.2f}%', "up"),
])

ramp = ("".join(f'<span style="background:{_hex(_mix(POLES["dark"]["down"], POLES["dark"]["neutral"], i/5))}"></span>' for i in range(6))
        + "".join(f'<span style="background:{_hex(_mix(POLES["dark"]["neutral"], POLES["dark"]["up"], (i+1)/5))}"></span>' for i in range(5)))

CSS = """
:root{color-scheme:dark;--page:#0d0d0d;--surf:#1a1a19;--ink-1:#fff;--ink-2:#c3c2b7;
 --ink-3:#898781;--grid:#2c2c2a;--rule:#383835;--ring:rgba(255,255,255,.10);
 --up:#e66767;--down:#3987e5;--warn:#fab219;--chipbg:rgba(255,255,255,.07)}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink-1);font-size:13px;line-height:1.45;
 font-family:system-ui,-apple-system,"Segoe UI","PingFang TC","Noto Sans TC",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:20px 18px 44px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px 15px}
h1{font-size:18px;margin:0;letter-spacing:-.01em}
.meta{color:var(--ink-3);font-size:12px}
.hero{background:var(--surf);border:1px solid var(--ring);border-radius:13px;padding:14px 17px 13px;
 margin:15px 0 12px}
.hero h2{font-size:13.5px;margin:0 0 9px}
.hnote{font-weight:400;font-size:11px;color:var(--ink-3);margin-left:8px}
ul.dgl{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
li.dg{display:flex;gap:10px;align-items:flex-start;font-size:13px;line-height:1.5}
.dtag{flex:0 0 auto;font-size:11px;padding:2px 8px;border-radius:5px;background:var(--chipbg);
 color:var(--ink-2);margin-top:1px;min-width:64px;text-align:center;font-weight:600}
li.dg.up .dtag{background:rgba(230,103,103,.16);color:var(--up)}
li.dg.down .dtag{background:rgba(57,135,229,.16);color:var(--down)}
li.dg.warn .dtag{background:rgba(250,178,25,.18);color:var(--warn)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:var(--surf);border:1px solid var(--ring);border-radius:11px;padding:11px 14px}
.klbl{font-size:11px;color:var(--ink-3)}
.kval{font-size:17px;font-weight:640;margin:3px 0 2px;letter-spacing:-.015em}
.kval.up{color:var(--up)}.kval.down{color:var(--down)}
.ksub{font-size:11.5px;color:var(--ink-2)}
.card{background:var(--surf);border:1px solid var(--ring);border-radius:12px;padding:13px 15px 11px;
 margin-bottom:16px}
.card h2{font-size:13px;margin:0 0 2px}
.card p.hint{font-size:11.5px;color:var(--ink-3);margin:0 0 9px}
svg.slope{width:100%;height:auto;display:block;overflow:visible}
.glin{stroke:var(--grid);stroke-width:1}
.axl{font-size:10.5px;fill:var(--ink-3)}
.slab{font-size:11px;fill:var(--ink-2)}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:7px 8px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--grid)}
thead th{position:sticky;top:0;background:var(--surf);z-index:2;font-size:11px;color:var(--ink-3);
 font-weight:600;border-bottom:1px solid var(--rule);cursor:pointer;user-select:none}
.th2{font-weight:400}
.vt{font-size:10px;cursor:help;font-weight:600;padding:0 4px;border-radius:3px;vertical-align:1px}
.vt.hi{background:rgba(255,255,255,.08);color:var(--ink-3)}
.vt.lo{background:rgba(250,178,25,.18);color:var(--warn)}
td.num,th.num{text-align:right}
td.heat{background:var(--bg);color:var(--fg);font-weight:520}
.sep{border-left:1px solid var(--grid)}
td.dim{color:var(--ink-3)}
td.b{font-weight:560}
td.rk{width:50px}
.rknum{font-size:15px;font-weight:660}
.drk{font-size:10.5px;margin-left:4px;color:var(--ink-3)}
.drk.up{color:var(--up)}.drk.down{color:var(--down)}
.sname{font-weight:590}
.cnt{font-size:10.5px;color:var(--ink-3);font-weight:400;margin-left:6px}
.cat{font-size:10px;color:var(--ink-3);font-weight:400;margin-left:7px;padding:1px 6px;
 border-radius:4px;border:1px solid var(--ring)}
.cat2{font-size:10px;font-weight:400;margin-left:7px;padding:1px 6px;border-radius:4px;
 background:rgba(57,135,229,.14);color:#7fb2f0}
.lyrn{font-size:10.5px;color:var(--ink-3);font-weight:400;margin-left:8px;padding:1px 6px;
 border-radius:4px;background:var(--chipbg)}
td.lyrc{color:var(--ink-2);font-weight:600;cursor:help}
th.lyr{text-align:right}
.sen{font-size:10.5px;color:var(--ink-3)}
.sgs{display:flex;gap:4px;margin-top:2px;flex-wrap:wrap}
.sg{font-size:10.5px;padding:1px 6px;border-radius:4px;background:var(--chipbg);color:var(--ink-2);
 cursor:help}
.sg.up{background:rgba(164,40,34,.12);color:var(--up)}
.sg.down{background:rgba(24,79,149,.12);color:var(--down)}
.sg.warn{background:rgba(250,178,25,.20);color:var(--warn)}
tr.comp{background:var(--chipbg)}
tr.comp td{border-bottom:1px solid var(--rule)}
.spark{display:block}
.bar{display:inline-block;width:38px;height:5px;background:var(--grid);border-radius:3px;
 overflow:hidden;vertical-align:middle;margin-right:5px}
.bar i{display:block;height:100%;background:var(--ink-2);border-radius:3px}
.bn{font-size:11.5px;color:var(--ink-2)}
.chip{display:inline-block;padding:1px 6px;border-radius:5px;background:var(--chipbg);font-size:11.5px}
.chip.hot{background:rgba(250,178,25,.22);color:var(--ink-1)}
.chip.cold{background:rgba(57,135,229,.18);color:var(--ink-1)}
.sblock{background:var(--surf);border:1px solid var(--ring);border-radius:11px;margin-bottom:7px;
 overflow:hidden}
.sblock summary{padding:9px 14px;cursor:pointer;font-weight:590;display:flex;align-items:baseline;
 gap:9px;font-size:12.5px}
.sblock summary::-webkit-details-marker{display:none}
.srk{color:var(--ink-3);font-size:11.5px;font-weight:600}
.ssum{margin-left:auto;font-weight:400;color:var(--ink-2);font-size:11.5px}
table.stk td,table.stk th{padding:5px 8px;font-size:12px}
td.tk{font-weight:620}
td.nm{color:var(--ink-2);max-width:180px;overflow:hidden;text-overflow:ellipsis}
.legend{display:flex;align-items:center;gap:9px;font-size:11.5px;color:var(--ink-3);
 padding:9px 12px 10px;flex-wrap:wrap}
.lramp{display:flex;height:9px;border-radius:5px;overflow:hidden;width:150px}
.lramp span{flex:1}
.sechd{display:flex;align-items:baseline;gap:11px;margin:22px 0 8px;flex-wrap:wrap}
.sechd h2{font-size:13.5px;margin:0}
.sechd p{margin:0;font-size:11.5px;color:var(--ink-3);max-width:640px}
.foot{margin-top:20px;color:var(--ink-3);font-size:11px}
@media(max-width:640px){.wrap{padding:14px 10px 34px}h1{font-size:16px}
 .kpis{grid-template-columns:repeat(2,1fr)}}
"""

JS = """
document.querySelectorAll('table.main thead tr.h th').forEach((th,i)=>{
  if(th.classList.contains('nosort'))return;
  th.onclick=()=>{const tb=th.closest('table').tBodies[1];if(!tb)return;
    const rows=[...tb.rows];const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
    const num=t=>{const v=parseFloat(t.replace(/[^0-9.+-]/g,''));return isNaN(v)?-1e9:v;};
    rows.sort((a,b)=>{const x=num(a.cells[i].innerText),y=num(b.cells[i].innerText);
      return asc?x-y:y-x;});rows.forEach(r=>tb.appendChild(r));};});
"""

HTML = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股半導體板塊資金輪動 · {D["asof"]}</title>
<style>{CSS}</style></head><body><div class="wrap">
<header><h1>美股半導體板塊資金輪動</h1>
  <div class="meta">資料日 {D["asof"]}（美股收盤）</div>
</header>

<div class="hero"><h2>方向判讀 <span class="hnote">規則判讀，非投資建議</span></h2>
  <ul class="dgl">{DIGEST}</ul></div>

<div class="kpis">{KPIS}</div>

<div class="card"><h2>板塊排名遷移</h2>
  <p class="hint">依 1 個月（20 交易日）報酬排名。左為 3 個交易日前，右為最新。紅線往上＝名次爬升，藍線往下＝退位。</p>
  {slope_chart(S)}</div>

<div class="sechd"><h2>板塊總表</h2><p>等權每日再平衡 · {sum(1 for s in S if s["cat"]=="半導體")} 個半導體板塊 + {sum(1 for s in S if s["cat"]=="其他 AI")} 個其他 AI 板塊 · 共 {sum(s["n"] for s in S)} 檔</p></div>
<div class="card" style="padding:0 4px 0">
<div class="tw"><table class="main"><thead>{HEAD}</thead>
<tbody>{ref_row("半導體整體", f"{sum(1 for s in S if s['cat']=='半導體')} 個半導體板塊 · {C['n']} 檔等權", C, C["breadth"])}
{ref_row("費半 SOX", "費城半導體指數 · 市值加權", B["^SOX"])}</tbody>
<tbody>{sector_rows(S)}</tbody></table></div>
<div class="legend"><span>弱</span><div class="lramp">{ramp}</div><span>強</span>
  <span style="margin-left:8px">點欄位標題可排序 · 板塊名稱下的標籤可滑鼠停留看說明</span></div></div>

<div class="sechd"><h2>個股明細</h2><p>點開展開</p></div>
{stock_blocks(S, "")}

<div class="foot">Yahoo Finance 日線 · 還原權值價{
  "　|　抓取失敗：" + ", ".join(D["failed"]) if D["failed"] else ""}</div>
</div><script>{JS}</script></body></html>"""

# --- guard: header and body column counts must match in every table, or fail loudly
import re as _re
def _check(page):
    bad = []
    for tbl in _re.findall(r"<table[^>]*>.*?</table>", page, _re.S):
        name = "main" if 'class="main"' in tbl else "stk"
        heads = _re.findall(r"<tr class=\"h\">(.*?)</tr>", tbl, _re.S) or \
                _re.findall(r"<thead>.*?<tr>(.*?)</tr>", tbl, _re.S)
        if not heads:
            continue
        ncol = len(_re.findall(r"<th", heads[0]))
        body = tbl[tbl.find("<tbody"):]
        for row in _re.findall(r"<tr[^>]*>(.*?)</tr>", body, _re.S):
            n = len(_re.findall(r"<td", row))
            if n and n != ncol:
                bad.append((name, ncol, n, _re.sub(r"<[^>]+>", " ", row)[:60].strip()))
    return bad

_bad = _check(HTML)
if _bad:
    for x in _bad[:8]:
        print("COLUMN MISMATCH", x)
    raise SystemExit(f"aborting: {len(_bad)} misaligned rows")
print("column check OK")

open(OUT_HTML, "w").write(HTML)
json.dump(D, open(OUT_JSON, "w"), ensure_ascii=False)
print("ASOF", D["asof"], "| failed:", D["failed"], "| bytes", len(HTML))
