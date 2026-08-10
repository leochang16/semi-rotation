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
    "算力晶片":      ["NVDA", "AMD", "AVGO", "MRVL"],
    "晶圓代工/封測": ["TSM", "INTC", "GFS", "UMC", "AMKR", "ASX"],
    "半導體設備":    ["AMAT", "LRCX", "KLAC", "ASML", "TER", "ONTO", "ACMR", "CAMT"],
    "材料/零組件":   ["ENTG", "MKSI", "UCTT", "ICHR"],
    "EDA/IP":        ["SNPS", "CDNS", "ARM"],
    "記憶體/儲存":   ["MU", "SNDK", "WDC", "STX", "SIMO"],
    "光通訊/CPO":    ["COHR", "LITE", "AAOI", "FN", "POET", "AXTI"],
    "高速互連/網通": ["ANET", "CRDO", "ALAB", "CIEN", "CSCO", "NOK"],
    "類比/功率/被動":["TXN", "ADI", "ON", "MPWR", "NXPI", "STM", "MCHP", "WOLF", "VSH"],
}
OUTER = {
    "Neocloud/AI 租賃": ["CRWV", "NBIS", "IREN", "CIFR", "APLD", "WULF", "CORZ", "GLXY"],
    "AI 軟體/應用":     ["PLTR", "NOW", "SNOW", "MDB", "DDOG", "CRM", "ORCL", "TEAM"],
    "資安":             ["CRWD", "PANW", "ZS", "FTNT", "S", "RBRK", "OKTA", "NET"],
    "電力/散熱基建":    ["VRT", "ETN", "GEV", "PWR", "MOD", "NVT", "CEG", "VST", "OKLO"],
    "機器人/實體 AI":   ["ROK", "ZBRA", "OUST", "CGNX", "AMBA", "SYM", "SERV", "MBLY"],
    "七巨頭":           ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
}
SECTOR_EN = {
    "算力晶片": "AI Compute", "晶圓代工/封測": "Foundry / OSAT",
    "半導體設備": "Semicap Equipment", "材料/零組件": "Materials / Subsystems",
    "EDA/IP": "EDA / IP", "記憶體/儲存": "Memory / Storage",
    "光通訊/CPO": "Optical / CPO", "高速互連/網通": "Interconnect / Networking",
    "類比/功率/被動": "Analog / Power / Passive",
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
    "EDA/IP":        (4, "設計工具 / IP"),
    "晶圓代工/封測": (5, "代工 / 封測"),
    "半導體設備":    (6, "設備"),
    "材料/零組件":   (7, "材料 / 基板"),
}
BENCH = ["SMH", "SPY"]
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
        rank_now = rs_now["1W"].rank(ascending=False, method="min").astype(int)
        rank_3ago = rs_3ago["1W"].rank(ascending=False, method="min").astype(int)

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
                "bench": bench_tk,
                "ret": {k: float((idx_ew.iloc[i] / idx_ew.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
                "ret_cw": {k: float((idx_cw.iloc[i] / idx_cw.iloc[i - n] - 1) * 100) for k, n in WINDOWS.items()},
                "rs_smh": {k: float((idx_ew.iloc[i] / idx_ew.iloc[i - n] - 1) * 100
                                    - (adj["SMH"].iloc[i] / adj["SMH"].iloc[i - n] - 1) * 100)
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
                "corr_nvda": float(idx_ew.pct_change().iloc[-20:].corr(ret["NVDA"].iloc[-20:])),
                "spark": [float(x) for x in (idx_ew.iloc[-60:] / idx_ew.iloc[-60] * 100)],
            }
            rec["rs"] = rec["rs_smh"] if bench_tk == "SMH" else rec["rs_spy"]
            rec["dv_share_chg"] = rec["dv_share"] - rec["dv_share_avg20"]
            rec["cap_minus_ew"] = rec["ret_cw"]["1W"] - rec["ret"]["1W"]
            bench_1w = (adj["SMH"].iloc[-1] / adj["SMH"].iloc[-6] - 1) * 100

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
                    "r63": float((p.iloc[-1] / p.iloc[-64] - 1) * 100) if len(p) > 64 else None,
                    "rs5": float((p.iloc[-1] / p.iloc[-6] - 1) * 100 - bench_1w) if len(p) > 6 else None,
                    "volr": float(dv[t].iloc[-1] / dv[t].iloc[-21:-1].mean()),
                    "f52": float((p.iloc[-1] / w52t.max() - 1) * 100),
                    "rsi": float(rsi(p).iloc[-1]),
                    "a20": bool(p.iloc[-1] > p.rolling(20).mean().iloc[-1]),
                })
            rec["stocks"] = sorted(stocks, key=lambda x: -(x["r5"] if x["r5"] is not None else -99))
            out.append(rec)
        return out, sec_ew

    WINDOWS = {"1D": 1, "3D": 3, "1W": 5, "1M": 20, "3M": 63}

    tier1, ew1 = compute_tier(SECTORS, "SMH")
    tier2, _   = compute_tier(OUTER, "SPY")

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
        "spark": [float(x) for x in (comp.iloc[-60:] / comp.iloc[-60] * 100)],
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
            "spark": [float(x) for x in (bp.iloc[-60:] / bp.iloc[-60] * 100)],
        }

    return {
        "asof": str(asof.date()),
        "generated": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M TPE"),
        "sectors": tier1, "outer": tier2, "composite": composite,
        "bench": bench, "failed": failed, "windows": list(WINDOWS.keys()),
    }


import html, os
OUT_HTML = os.environ.get("OUT_HTML", "index.html")
OUT_JSON = os.environ.get("OUT_JSON", "data.json")
D = build()
S, O, C, B = D["sectors"], D["outer"], D["composite"], D["bench"]

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
    bl, fl = heat(v, scale, "light")
    bd, fd = heat(v, scale, "dark")
    return (f'<td class="num heat {cls}" style="--bl:{bl};--fl:{fl};--bd:{bd};--fd:{fd}">'
            f'{fmt.format(v)}{suffix}</td>')

def maxabs(vals, floor=0.5):
    vs = [abs(v) for v in vals if v is not None]
    return max(max(vs) if vs else floor, floor)

W4 = ["1D", "3D", "1W", "1M"]

# ---------------------------------------------------------------- signals
def signals(s):
    """The per-sector story, as short chips. Max 3, most important first."""
    out = []
    if s["ret"]["1W"] > 2 and s["dv_share_chg"] <= -1.0:
        out.append(("warn", "縮量", f'漲 {s["ret"]["1W"]:+.1f}% 但成交額佔比掉 {abs(s["dv_share_chg"]):.2f} pp，沒有增量資金'))
    if s["vol_ratio"] >= 1.3:
        out.append(("up", "爆量", f'成交額為 20 日均的 {s["vol_ratio"]:.2f} 倍'))
    if s["ret"]["1W"] > 2 and s["breadth"] <= 40:
        out.append(("warn", "個股行情", f'只有 {s["breadth"]:.0f}% 成分股站上 20MA，不是板塊性資金流入'))
    if s["breadth"] >= 90:
        out.append(("up", "全面性", f'{s["breadth"]:.0f}% 成分股站上 20MA'))
    if s["breadth"] <= 15:
        out.append(("down", "全面走弱", f'僅 {s["breadth"]:.0f}% 成分股站上 20MA'))
    if s["rsi"] >= 70:
        out.append(("warn", "過熱", f'RSI {s["rsi"]:.0f}'))
    if s["rsi"] <= 30:
        out.append(("down", "超賣", f'RSI {s["rsi"]:.0f}'))
    if s["d_rank"] >= 3:
        out.append(("up", f'躍升 {s["d_rank"]}', f'3 日內 {s["rank_prev"]} → {s["rank"]} 名'))
    if s["d_rank"] <= -3:
        out.append(("down", f'下滑 {abs(s["d_rank"])}', f'3 日內 {s["rank_prev"]} → {s["rank"]} 名'))
    return out[:3]

def chips(s):
    return "".join(f'<span class="sg {t}" title="{html.escape(tip)}">{html.escape(lbl)}</span>'
                   for t, lbl, tip in signals(s))

# ---------------------------------------------------------------- digest (3 lines)
def digest():
    out = []
    cr = C["ret"]["1W"] - B["SPY"]["ret"]["1W"]
    best_o = max(O, key=lambda z: z["ret"]["1W"])
    if cr >= 0:
        t = (f'半導體整體本週 <b>{C["ret"]["1W"]:+.2f}%</b>，領先 SPY {cr:+.2f} 個百分點，錢還在半導體。')
        if best_o["ret"]["1W"] - B["SPY"]["ret"]["1W"] > cr:
            t += f' 但外圍的<b>{html.escape(best_o["sector"])}</b>更強（{best_o["ret"]["1W"]:+.2f}%），有分流。'
    else:
        t = (f'半導體整體本週 <b>{C["ret"]["1W"]:+.2f}%</b>，落後 SPY {abs(cr):.2f} 個百分點，'
             f'資金正在離開；外圍最強的是<b>{html.escape(best_o["sector"])}</b>（{best_o["ret"]["1W"]:+.2f}%）。')
    out.append(("up" if cr >= 0 else "down", "資金位置", t))

    ldr = min(S, key=lambda z: z["rank"])
    top = ldr["stocks"][0]
    conf = (f'成交額佔比同步 {ldr["dv_share_chg"]:+.2f} pp，<b>有量有價</b>'
            if ldr["dv_share_chg"] > 0.3 else
            f'但成交額佔比 {ldr["dv_share_chg"]:+.2f} pp，<b>價漲量沒跟上</b>')
    out.append(("up", "領漲",
                f'<b>{html.escape(ldr["sector"])}</b> 本週 {ldr["ret"]["1W"]:+.2f}% 居首，{conf}。'
                f'帶頭的是 {top["ticker"]} {top["r5"]:+.1f}%。'))

    # short side gets equal billing — he trades both directions
    wk = max(S, key=lambda z: z["rank"])
    weak_bits = [f'本週 {wk["ret"]["1W"]:+.2f}%、廣度 {wk["breadth"]:.0f}%']
    if wk["dv_share_chg"] < 0:
        weak_bits.append(f'成交額佔比 {wk["dv_share_chg"]:+.2f} pp')
    if wk["d_rank"] <= -2:
        weak_bits.append(f'3 日內 {wk["rank_prev"]} → {wk["rank"]} 名')
    if wk["vol_ratio"] >= 1.2:
        weak_bits.append(f'但量能 {wk["vol_ratio"]:.2f}×，跌得有量')
    out.append(("down", "空方",
                f'最弱是<b>{html.escape(wk["sector"])}</b>，' + "、".join(weak_bits) + "。"))
    return out[:3]

DIGEST = "".join(f'<li class="dg {t}"><span class="dtag">{tag}</span><span>{txt}</span></li>'
                 for t, tag, txt in digest())

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

def slope_chart(rows, lw=118):
    n = len(rows)
    W, H = 560, 46 + (n - 1) * 30
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
        p.append(f'<path d="M{x0},{y(a):.1f} C{x0+70},{y(a):.1f} {x1-70},{y(b):.1f} {x1},{y(b):.1f}" '
                 f'fill="none" stroke="{col}" stroke-width="{2.6 if d else 1.4}" '
                 f'opacity="{0.95 if d else 0.45}"/>')
        p.append(f'<circle cx="{x0}" cy="{y(a):.1f}" r="3.4" fill="{col}"/>')
        p.append(f'<circle cx="{x1}" cy="{y(b):.1f}" r="3.4" fill="{col}"/>')
        p.append(f'<text x="{x0-9}" y="{y(a)+4:.1f}" class="slab" text-anchor="end">'
                 f'{a}. {html.escape(s["sector"])}</text>')
        p.append(f'<text x="{x1+9}" y="{y(b)+4:.1f}" class="slab">'
                 f'{b}. {html.escape(s["sector"])}{f" ({d:+d})" if d else ""}</text>')
    return "".join(p) + "</svg>"

def flow_chart(rows, lw=112):
    rows = sorted(rows, key=lambda z: -z["dv_share_chg"])
    m = maxabs([z["dv_share_chg"] for z in rows], 0.5)
    W, rowh = 560, 26
    H = len(rows) * rowh + 26
    cx = (lw + 8 + (W - 62)) / 2
    maxbw = cx - lw - 80
    p = [f'<svg viewBox="0 0 {W} {H}" class="flow" role="img" aria-label="成交額佔比變化">',
         f'<line x1="{cx}" y1="18" x2="{cx}" y2="{H-6}" class="glin"/>',
         f'<text x="{cx}" y="12" class="axl" text-anchor="middle">0</text>']
    for i, s in enumerate(rows):
        yy = 24 + i * rowh
        v = s["dv_share_chg"]
        bw = abs(v) / m * maxbw
        col = "var(--up)" if v >= 0 else "var(--down)"
        p.append(f'<rect x="{(cx if v>=0 else cx-bw):.1f}" y="{yy:.1f}" width="{bw:.1f}" '
                 f'height="13" rx="3" fill="{col}"/>')
        p.append(f'<text x="{lw}" y="{yy+11:.1f}" class="slab" text-anchor="end">'
                 f'{html.escape(s["sector"])}</text>')
        lx, anc = (cx + bw + 7, "start") if v >= 0 else (cx - bw - 7, "end")
        p.append(f'<text x="{lx:.1f}" y="{yy+11:.1f}" class="slab" text-anchor="{anc}">{v:+.2f} pp</text>')
    return "".join(p) + "</svg>"

# ---------------------------------------------------------------- table
def scales(rows):
    return dict(ret={k: maxabs([s["ret"][k] for s in rows], 1.0) for k in W4},
                share=maxabs([s["dv_share_chg"] for s in rows], 0.5),
                f52=maxabs([s["from_52wh"] for s in rows], 1.0))

def ref_row(name, note, d, breadth=None):
    """Reference row: 半導體整體 / SMH / SPY."""
    tds = ['<td class="rk">—</td>', '<td class="num dim">—</td>',
           f'<td class="sec"><div class="sname">{html.escape(name)}</div>'
           f'<div class="sen">{html.escape(note)}</div></td>',
           f'<td class="spk">{sparkline(d["spark"])}</td>']
    tds += [f'<td class="num b">{d["ret"][k]:+.2f}%</td>' for k in W4]
    tds.append('<td class="num sep">—</td>')          # Δ成交額佔比
    tds.append('<td class="num">—</td>')              # 量能倍數
    tds.append(f'<td class="num sep">{breadth:.0f}%</td>' if breadth is not None
               else '<td class="num sep">—</td>')
    tds.append(f'<td class="num">{d["vol20"]:.0f}%</td>')
    tds.append(f'<td class="num">{d["rsi"]:.0f}</td>')
    return f'<tr class="comp">{"".join(tds)}</tr>'

def sector_rows(rows):
    sc = scales(rows)
    out = []
    for s in sorted(rows, key=lambda z: z["rank"]):
        d = s["d_rank"]
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        dcls = "up" if d > 0 else ("down" if d < 0 else "flat")
        lyr = s.get("layer", 99)
        r = [f'<tr data-anom="{1 if signals(s) else 0}" data-layer="{lyr}">',
             f'<td class="rk"><span class="rknum">{s["rank"]}</span>'
             f'<span class="drk {dcls}">{arrow}{abs(d) if d else ""}</span></td>',
             (f'<td class="num lyrc" title="{html.escape(s.get("layer_name",""))}">{lyr}</td>'
              if lyr != 99 else '<td class="num dim">—</td>'),
             f'<td class="sec"><div class="sname">{html.escape(s["sector"])}'
             f'<span class="cnt">{s["n"]}</span>'
             + (f'<span class="lyrn">{html.escape(s.get("layer_name",""))}</span>' if lyr != 99 else '')
             + f'</div><div class="sgs">{chips(s)}</div></td>',
             f'<td class="spk">{sparkline(s["spark"])}</td>']
        for k in W4:
            r.append(cell(s["ret"][k], sc["ret"][k], "{:+.2f}", "%"))
        r.append(cell(s["dv_share_chg"], sc["share"], "{:+.2f}", " pp", "sep"))
        vr = s["vol_ratio"]
        r.append(f'<td class="num"><span class="chip {"hot" if vr>=1.3 else ("cold" if vr<=0.7 else "")}">'
                 f'{vr:.2f}×</span></td>')
        bd = s["breadth"]
        r.append(f'<td class="num sep"><div class="bar"><i style="width:{bd:.0f}%"></i></div>'
                 f'<span class="bn">{bd:.0f}%</span></td>')
        r.append(f'<td class="num dim">{s["vol20"]:.0f}%</td>')
        rs_ = s["rsi"]
        r.append(f'<td class="num"><span class="chip {"hot" if rs_>=70 else ("cold" if rs_<=30 else "")}">'
                 f'{rs_:.0f}</span></td>')
        out.append("".join(r) + "</tr>")
    return "".join(out)

HEAD = """<tr class="h">
<th class="nosort">名次<br><span class="th2">Δ3日</span></th>
<th class="lyr" title="供應鏈位置：1 最下游（系統），7 最上游（材料）。點此依層級排序。">層</th>
<th class="nosort">板塊</th><th class="nosort">60日走勢</th>
<th class="num">1D</th><th class="num">3D</th><th class="num">1W</th><th class="num">1M</th>
<th class="num sep" title="該板塊成交金額佔全部板塊總成交額的比重，減去其 20 日平均。錢的分配比例變了多少。">Δ成交額佔比<br><span class="th2">vs 20日均</span></th>
<th class="num" title="板塊今日成交金額 ÷ 其 20 日均量。事件行情最重要的確認：有沒有真的爆量。">量能<br><span class="th2">vs 20日均</span></th>
<th class="num sep" title="板塊內站上 20 日均線的成分股比例">廣度<br><span class="th2">&gt;20MA</span></th>
<th class="num" title="板塊指數近 20 日報酬標準差，年化。抓停損寬度用。">波動率<br><span class="th2">20日年化</span></th>
<th class="num" title="板塊指數 RSI(14)。≥70 超買、≤30 超賣。">RSI</th></tr>"""

def stock_blocks(rows, tier):
    out = []
    for s in sorted(rows, key=lambda z: z["rank"]):
        st = s["stocks"]
        sc = {k: maxabs([x[k] for x in st], 1.0) for k in ("r1", "r5", "r20", "r63")}
        trs = []
        for x in st:
            trs.append(
                f'<tr><td class="tk">{x["ticker"]}</td><td class="nm">{html.escape(x["name"])}</td>'
                f'<td class="num">{x["price"]:,.2f}</td>'
                + cell(x["r1"], sc["r1"], "{:+.2f}", "%") + cell(x["r5"], sc["r5"], "{:+.2f}", "%")
                + cell(x["r20"], sc["r20"], "{:+.2f}", "%") + cell(x["r63"], sc["r63"], "{:+.2f}", "%")
                + f'<td class="num dim">{x["volr"]:.2f}×</td><td class="num">{x["f52"]:+.1f}%</td>'
                  f'<td class="num">{"✓" if x["a20"] else "·"}</td></tr>')
        out.append(
            f'<details class="sblock"><summary><span class="srk">{tier}#{s["rank"]}</span>'
            f'{html.escape(s["sector"])}<span class="ssum">1W {s["ret"]["1W"]:+.2f}% · '
            f'廣度 {s["breadth"]:.0f}%</span></summary>'
            f'<div class="tw"><table class="stk"><thead><tr><th>代號</th><th>名稱</th>'
            f'<th class="num">收盤</th><th class="num">市值</th><th class="num">1D</th>'
            f'<th class="num">1W</th><th class="num">1M</th><th class="num">3M</th>'
            f'<th class="num">量能</th><th class="num">距52週高</th><th class="num">&gt;20MA</th>'
            f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div></details>')
    return "".join(out)

# ---------------------------------------------------------------- KPIs
top, bot = min(S, key=lambda z: z["rank"]), max(S, key=lambda z: z["rank"])
inflow = max(S, key=lambda z: z["dv_share_chg"])
faller = min(S, key=lambda z: z["d_rank"])

def kpi(lbl, val, sub, tone="", tip=""):
    return (f'<div class="kpi"{f" title={chr(34)}{html.escape(tip)}{chr(34)}" if tip else ""}>'
            f'<div class="klbl">{lbl}</div><div class="kval {tone}">{val}</div>'
            f'<div class="ksub">{sub}</div></div>')

KPIS = "".join([
    kpi("半導體整體 1W", f'{C["ret"]["1W"]:+.2f}%',
        f'SMH {B["SMH"]["ret"]["1W"]:+.2f}% · SPY {B["SPY"]["ret"]["1W"]:+.2f}%',
        "up" if C["ret"]["1W"] >= 0 else "down"),
    kpi("最強板塊", html.escape(top["sector"]), f'1W {top["ret"]["1W"]:+.2f}%', "up"),
    kpi("最弱板塊", html.escape(bot["sector"]), f'1W {bot["ret"]["1W"]:+.2f}%', "down"),
    kpi("資金流入最多", html.escape(inflow["sector"]),
        f'成交額佔比 {inflow["dv_share_chg"]:+.2f} pp vs 20 日均', "up",
        "成交額佔比 = 該板塊今日成交金額 ÷ 全部板塊總成交金額。這裡看的是它比自己過去 20 日平均高出多少個百分點。"),
])

ramp = ("".join(f'<span style="background:{_hex(_mix(POLES["light"]["down"], POLES["light"]["neutral"], i/5))}"></span>' for i in range(6))
        + "".join(f'<span style="background:{_hex(_mix(POLES["light"]["neutral"], POLES["light"]["up"], (i+1)/5))}"></span>' for i in range(5)))

CSS = """
:root{color-scheme:light dark;--page:#f9f9f7;--surf:#fcfcfb;--ink-1:#0b0b0b;--ink-2:#52514e;
 --ink-3:#898781;--grid:#e1e0d9;--rule:#c3c2b7;--ring:rgba(11,11,11,.10);
 --up:#a42822;--down:#184f95;--warn:#8a6100;--chipbg:rgba(11,11,11,.05)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
 --page:#0d0d0d;--surf:#1a1a19;--ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#898781;--grid:#2c2c2a;
 --rule:#383835;--ring:rgba(255,255,255,.10);--up:#e66767;--down:#3987e5;--warn:#fab219;
 --chipbg:rgba(255,255,255,.07)}}
:root[data-theme=dark]{--page:#0d0d0d;--surf:#1a1a19;--ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#898781;
 --grid:#2c2c2a;--rule:#383835;--ring:rgba(255,255,255,.10);--up:#e66767;--down:#3987e5;
 --warn:#fab219;--chipbg:rgba(255,255,255,.07)}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink-1);font-size:13px;line-height:1.45;
 font-family:system-ui,-apple-system,"Segoe UI","PingFang TC","Noto Sans TC",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1360px;margin:0 auto;padding:20px 18px 56px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px 15px}
h1{font-size:18px;margin:0;letter-spacing:-.01em}
.meta{color:var(--ink-3);font-size:12px}
.tgl{margin-left:auto;display:flex;gap:6px}
button.t{background:var(--surf);border:1px solid var(--ring);color:var(--ink-2);border-radius:7px;
 padding:5px 11px;font:inherit;font-size:12px;cursor:pointer}
button.t[aria-pressed=true]{background:var(--ink-1);color:var(--surf);border-color:var(--ink-1)}
.hero{background:var(--surf);border:1px solid var(--ring);border-radius:13px;padding:14px 17px 13px;
 margin:15px 0 16px}
.hero h2{font-size:13.5px;margin:0 0 9px}
ul.dgl{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:7px}
li.dg{display:flex;gap:10px;align-items:flex-start;font-size:13px;line-height:1.5}
.dtag{flex:0 0 auto;font-size:11px;padding:2px 8px;border-radius:5px;background:var(--chipbg);
 color:var(--ink-2);margin-top:1px;min-width:56px;text-align:center}
li.dg.up .dtag{background:rgba(164,40,34,.12);color:var(--up)}
li.dg.down .dtag{background:rgba(24,79,149,.12);color:var(--down)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:var(--surf);border:1px solid var(--ring);border-radius:11px;padding:11px 14px}
.klbl{font-size:11px;color:var(--ink-3)}
.kval{font-size:18px;font-weight:640;margin:3px 0 2px;letter-spacing:-.015em}
.kval.up{color:var(--up)}.kval.down{color:var(--down)}
.ksub{font-size:11.5px;color:var(--ink-2)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:13px;margin-bottom:16px}
.card{background:var(--surf);border:1px solid var(--ring);border-radius:12px;padding:13px 15px 11px}
.card h2{font-size:13px;margin:0 0 2px}
.card p.hint{font-size:11.5px;color:var(--ink-3);margin:0 0 9px}
svg.slope,svg.flow{width:100%;height:auto;display:block;overflow:visible}
.glin{stroke:var(--grid);stroke-width:1}
.axl{font-size:10.5px;fill:var(--ink-3)}
.slab{font-size:11px;fill:var(--ink-2)}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:7px 8px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--grid)}
thead th{position:sticky;top:0;background:var(--surf);z-index:2;font-size:11px;color:var(--ink-3);
 font-weight:600;border-bottom:1px solid var(--rule);cursor:pointer;user-select:none}
.th2{font-weight:400}
td.num,th.num{text-align:right}
td.heat{background:var(--bl);color:var(--fl);font-weight:520}
@media(prefers-color-scheme:dark){:root:where(:not([data-theme=light])) td.heat{
 background:var(--bd);color:var(--fd)}}
:root[data-theme=dark] td.heat{background:var(--bd);color:var(--fd)}
.sep{border-left:1px solid var(--grid)}
td.dim{color:var(--ink-3)}
td.b{font-weight:560}
td.rk{width:50px}
.rknum{font-size:15px;font-weight:660}
.drk{font-size:10.5px;margin-left:4px;color:var(--ink-3)}
.drk.up{color:var(--up)}.drk.down{color:var(--down)}
.sname{font-weight:590}
.cnt{font-size:10.5px;color:var(--ink-3);font-weight:400;margin-left:6px}
.lyrn{font-size:10.5px;color:var(--ink-3);font-weight:400;margin-left:8px;
 padding:1px 6px;border-radius:4px;background:var(--chipbg)}
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
td.nm{color:var(--ink-2);max-width:190px;overflow:hidden;text-overflow:ellipsis}
.legend{display:flex;align-items:center;gap:9px;font-size:11.5px;color:var(--ink-3);
 padding:9px 12px 10px;flex-wrap:wrap}
.lramp{display:flex;height:9px;border-radius:5px;overflow:hidden;width:150px}
.lramp span{flex:1}
.sechd{display:flex;align-items:baseline;gap:11px;margin:22px 0 8px;flex-wrap:wrap}
.sechd h2{font-size:13.5px;margin:0}
.sechd p{margin:0;font-size:11.5px;color:var(--ink-3)}
.sechd .tgl{margin-left:auto}
body.anom tbody tr[data-anom="0"]{display:none}
footer{margin-top:24px;color:var(--ink-3);font-size:11.5px;line-height:1.75}
@media(max-width:640px){.wrap{padding:14px 10px 40px}h1{font-size:16px}
 .kpis{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
"""

JS = """
document.querySelectorAll('button.t[data-theme]').forEach(b=>b.onclick=()=>{
  document.documentElement.dataset.theme=b.dataset.theme;
  document.querySelectorAll('button.t[data-theme]').forEach(x=>x.setAttribute('aria-pressed',x===b));});
const sb=document.getElementById('stackord');
if(sb)sb.onclick=()=>{const on=sb.getAttribute('aria-pressed')!=='true';
  sb.setAttribute('aria-pressed',on);
  sb.textContent=on?'依名次排序':'依供應鏈層級排序';
  document.querySelectorAll('table.main').forEach(tb=>{
    const body=tb.tBodies[1];if(!body)return;
    const rows=[...body.rows];
    rows.sort((a,b)=>on
      ?(+a.dataset.layer-+b.dataset.layer)||(+a.cells[0].innerText.trim().split(/\s/)[0]-+b.cells[0].innerText.trim().split(/\s/)[0])
      :(+a.cells[0].innerText.trim().split(/\s/)[0]-+b.cells[0].innerText.trim().split(/\s/)[0]));
    rows.forEach(r=>body.appendChild(r));});};
const ab=document.getElementById('anom');
ab.onclick=()=>{const on=!document.body.classList.contains('anom');
  document.body.classList.toggle('anom',on);ab.setAttribute('aria-pressed',on);
  ab.textContent=on?'顯示全部':'只顯示有訊號的';};
document.querySelectorAll('table.main thead tr.h th').forEach((th,i)=>{
  if(th.classList.contains('nosort'))return;
  th.onclick=()=>{const tb=th.closest('table').tBodies[1]||th.closest('table').tBodies[0];
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
  <div class="meta">資料日 {D["asof"]}（美股收盤）· 產生於 {D["generated"]}</div>
  <div class="tgl"><button class="t" data-theme="light" aria-pressed="false">淺色</button>
  <button class="t" data-theme="dark" aria-pressed="false">深色</button></div></header>

<div class="hero"><h2>今日重點</h2><ul class="dgl">{DIGEST}</ul></div>

<div class="kpis">{KPIS}</div>

<div class="grid2">
  <div class="card"><h2>板塊排名遷移</h2>
    <p class="hint">依 1 週報酬排名。左為 3 個交易日前，右為最新。線往上＝資金流進，往下＝撤出。</p>
    {slope_chart(S)}</div>
  <div class="card"><h2>成交額佔比變化</h2>
    <p class="hint">各板塊今日成交金額佔 {len(S)} 大半導體板塊總額的比重，減去其 20 日平均（百分點）。
    這欄比報酬率誠實——有時候板塊在漲，錢卻在退。</p>
    {flow_chart(S)}</div>
</div>

<div class="sechd"><h2>半導體板塊</h2><p>等權每日再平衡 · 共 {sum(s["n"] for s in S)} 檔</p>
  <div class="tgl"><button class="t" id="stackord" aria-pressed="false">依供應鏈層級排序</button>
  <button class="t" id="anom" aria-pressed="false">只顯示有訊號的</button></div></div>
<div class="card" style="padding:0 4px 0">
<div class="tw"><table class="main"><thead>{HEAD}</thead>
<tbody>{ref_row("半導體整體", f"本表 {sum(s['n'] for s in S)} 檔等權", C, C["breadth"])}
{ref_row("SMH", "半導體 ETF · 基準", B["SMH"])}
{ref_row("SPY", "S&P 500 · 大盤", B["SPY"])}</tbody>
<tbody>{sector_rows(S)}</tbody></table></div>
<div class="legend"><span>弱</span><div class="lramp">{ramp}</div><span>強</span>
  <span style="margin-left:8px">點欄位標題可排序 · 板塊名稱下的標籤可滑鼠停留看說明</span></div></div>

<div class="sechd"><h2>外圍資金池</h2>
  <p>判斷錢是否整片離開半導體 · 共 {sum(s["n"] for s in O)} 檔</p></div>
<div class="card" style="padding:0 4px 0">
<div class="tw"><table class="main"><thead>{HEAD}</thead>
<tbody>{ref_row("SPY", "S&P 500 · 大盤", B["SPY"])}</tbody>
<tbody>{sector_rows(O)}</tbody></table></div></div>

<div class="sechd"><h2>個股明細</h2><p>點開展開</p></div>
{stock_blocks(S, "")}{stock_blocks(O, "外圍 ")}

<footer>
<b>怎麼用</b>：每天看最上面「今日重點」三行就夠；有句話讓你在意，再看下面的圖表。
板塊名稱下方的標籤（縮量、爆量、個股行情、過熱…）就是該板塊當天的重點，滑鼠停留看細節。<br><br>
· <b>名次 / Δ3日</b>：依 1 週報酬排序。▲＝3 個交易日內名次上升，是輪動最直接的訊號。<br>
· <b>成交額佔比 / Δ佔比</b>：該板塊成交金額佔全部板塊總成交額的比重，以及它比自己過去 20 日平均高出或低於多少個百分點。<b>報酬為正但 Δ佔比為負 ＝ 縮量反彈。</b><br>
· <b>廣度</b>：板塊內站上 20 日均線的成分股比例。80%↑ 是全面性行情，30%↓ 通常只有一兩檔在撐。<br>
· <b>波動率</b>：板塊指數近 20 日報酬標準差年化。數字高代表這個板塊現在很躁動，進出要抓更寬的停損。<br>
· <b>半導體整體 / SMH / SPY</b>：表格最上方三列參照。板塊漲 8%、SMH 漲 7.8%，代表它只是跟著大盤走。<br><br>
分類原則採<b>股價驅動因素</b>而非產業鏈位置：AVGO 歸算力晶片、AXTI 歸光通訊、NOK 歸網通、ARM 歸 EDA/IP。
NVDA 同時在算力晶片與七巨頭，分屬不同表，不影響排名。<br>
資料來源：Yahoo Finance 日線（報酬採還原權值價）。本表為量化統計，非投資建議。
{"<br>抓取失敗：" + ", ".join(D["failed"]) if D["failed"] else ""}
</footer>
</div><script>{JS}</script></body></html>"""

open(OUT_HTML, "w").write(HTML)
json.dump(D, open(OUT_JSON, "w"), ensure_ascii=False)
print("ASOF", D["asof"], "| failed:", D["failed"], "| bytes", len(HTML))
