#!/usr/bin/env python3
"""
JY Fund 시그널 파이프라인 (Phase 1 MVP).

data/universe.json 에 정의된 종목별로 Yahoo Finance에서 200일 OHLCV를 받아
7개 팩터 기반 JY Score / State / Trigger 가격을 계산해 data/data.json 에 저장.

GitHub Actions runner에서 30분마다 실행. 로컬 실행도 가능.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = ROOT / "data" / "universe.json"
OUTPUT_PATH = ROOT / "data" / "data.json"

UA = {"User-Agent": "Mozilla/5.0 (compatible; JYFundBot/1.0)"}
SPY_TICKER = "SPY"
VIX_TICKER = "^VIX"


# ────────────────────────── Yahoo Finance ──────────────────────────
def fetch_ohlcv(ticker: str, range_: str = "1y", interval: str = "1d") -> dict | None:
    """Yahoo chart API → open/high/low/close/volume 리스트."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(ticker)}?range={range_}&interval={interval}"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code == 200:
                data = r.json()
                break
            if r.status_code in (429, 503):
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(1)
    else:
        return None

    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    r0 = result[0]
    ts = r0.get("timestamp") or []
    q = r0["indicators"]["quote"][0]
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []

    out = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for i, tstamp in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        dt = datetime.fromtimestamp(tstamp, tz=timezone.utc)
        out["t"].append(dt.strftime("%Y-%m-%d"))
        out["o"].append(round(opens[i], 4) if i < len(opens) and opens[i] is not None else c)
        out["h"].append(round(highs[i], 4) if i < len(highs) and highs[i] is not None else c)
        out["l"].append(round(lows[i], 4) if i < len(lows) and lows[i] is not None else c)
        out["c"].append(round(c, 4))
        out["v"].append(int(vols[i]) if i < len(vols) and vols[i] is not None else 0)
    return out if len(out["c"]) >= 30 else None


# ────────────────────────── 지표 ──────────────────────────
def sma(series: list[float], period: int) -> float | None:
    if len(series) < period:
        return None
    return sum(series[-period:]) / period


def rolling_sma(series: list[float], period: int) -> list[float | None]:
    """각 인덱스마다 그 시점까지의 period일 SMA. 앞쪽 (period-1)개는 None."""
    out: list[float | None] = [None] * len(series)
    if len(series) < period:
        return out
    window_sum = sum(series[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(series)):
        window_sum += series[i] - series[i - period]
        out[i] = window_sum / period
    return out


def slope_pct(series: list[float], period: int) -> float | None:
    """period 전 SMA 대비 현재 SMA 변화율(%)."""
    if len(series) < period * 2:
        return None
    now = sum(series[-period:]) / period
    prev = sum(series[-(period * 2):-period]) / period
    if prev == 0:
        return None
    return (now - prev) / prev * 100


def pct_return(series: list[float], period: int) -> float | None:
    if len(series) < period + 1:
        return None
    prev = series[-(period + 1)]
    if prev == 0:
        return None
    return (series[-1] - prev) / prev * 100


# ────────────────────────── Factor 계산 ──────────────────────────
def factor_trend(c: list[float]) -> int:
    """MA20 > MA50 > MA200 정렬 + 가격이 MA20 위. 0-20."""
    ma20 = sma(c, 20)
    ma50 = sma(c, 50)
    ma200 = sma(c, 200)
    price = c[-1]
    if ma20 is None or ma50 is None:
        return 0
    score = 0
    # 정렬
    if ma200 is not None and price > ma20 > ma50 > ma200:
        score += 12  # 완벽 정렬
    elif price > ma20 > ma50:
        score += 8
    elif price > ma20:
        score += 4
    elif price > ma50:
        score += 2
    # MA 기울기 (상승 추세)
    sl = slope_pct(c, 20) or 0
    if sl > 3:
        score += 8
    elif sl > 1:
        score += 5
    elif sl > 0:
        score += 3
    elif sl < -3:
        score += 0
    else:
        score += 1
    return min(20, score)


def factor_pullback(c: list[float]) -> int:
    """20일 고점 대비 조정률의 sweet spot. 0-15."""
    if len(c) < 20:
        return 0
    high20 = max(c[-20:])
    price = c[-1]
    if high20 == 0:
        return 0
    pull = (high20 - price) / high20 * 100  # % below 20-day high
    ma20 = sma(c, 20) or 0
    above_ma20 = price >= ma20
    # sweet spot: 2-8% pullback while still above MA20
    if 2 <= pull <= 8 and above_ma20:
        return 15
    if 0 <= pull < 2:  # 신고가 근처
        return 10
    if 8 < pull <= 15 and above_ma20:
        return 8
    if 8 < pull <= 15:
        return 4
    if pull > 15:
        return 2
    return 5


def factor_volume(c: list[float], v: list[int]) -> tuple[int, float, str]:
    """오늘 volume vs 20일 평균. 0-15, RVOL, quality."""
    if len(v) < 21:
        return 0, 0.0, "UNKNOWN"
    avg20 = sum(v[-21:-1]) / 20
    today = v[-1]
    if avg20 == 0:
        return 0, 0.0, "UNKNOWN"
    rvol = today / avg20
    # 오늘 캔들 색
    green = c[-1] > c[-2] if len(c) >= 2 else True

    if rvol >= 1.5:
        score = 15 if green else 10
        quality = "STRONG" if green else "MIXED"
    elif rvol >= 1.2:
        score = 12 if green else 8
        quality = "STRONG" if green else "MIXED"
    elif rvol >= 0.9:
        score = 8
        quality = "NEUTRAL"
    elif rvol >= 0.6:
        score = 4
        quality = "WEAK"
    else:
        score = 1
        quality = "WEAK"
    return score, round(rvol, 2), quality


def factor_reversal(o: list[float], h: list[float], l: list[float], c: list[float]) -> int:
    """Hammer / Bullish Engulfing / Higher Low. 0-15."""
    if len(c) < 5:
        return 0
    score = 0
    # 오늘 캔들
    body = abs(c[-1] - o[-1])
    upper = h[-1] - max(c[-1], o[-1])
    lower = min(c[-1], o[-1]) - l[-1]
    rng = h[-1] - l[-1]
    if rng > 0:
        # Hammer: 아래 꼬리 길고 몸통 작음, 위 꼬리 짧음
        if lower >= 2 * body and upper <= body and c[-1] >= o[-1]:
            score += 8
    # Bullish engulfing: 어제 음봉 + 오늘 양봉이 어제 몸통 감쌈
    if len(c) >= 2 and c[-2] < o[-2] and c[-1] > o[-1] and c[-1] > o[-2] and o[-1] < c[-2]:
        score += 10
    # Higher Low (최근 5일 저가가 그 전 5일 저가보다 높음)
    if len(l) >= 10:
        recent_low = min(l[-5:])
        prev_low = min(l[-10:-5])
        if recent_low > prev_low:
            score += 5
    return min(15, score)


def factor_rs(c: list[float], spy_c: list[float]) -> int:
    """SPY 대비 20일 상대강도. 0-10."""
    my = pct_return(c, 20)
    spy = pct_return(spy_c, 20)
    if my is None or spy is None:
        return 0
    diff = my - spy
    if diff >= 8:
        return 10
    if diff >= 4:
        return 8
    if diff >= 1:
        return 6
    if diff >= -1:
        return 4
    if diff >= -5:
        return 2
    return 0


def factor_market(spy_c: list[float], vix_c: list[float]) -> int:
    """VIX + SPY 추세. 0-10."""
    score = 0
    # VIX 낮을수록 좋음
    vix = vix_c[-1] if vix_c else 20
    if vix < 15:
        score += 4
    elif vix < 20:
        score += 3
    elif vix < 25:
        score += 1
    # SPY vs MA50
    ma50 = sma(spy_c, 50)
    if ma50 and spy_c[-1] > ma50:
        score += 3
    # SPY vs MA200
    ma200 = sma(spy_c, 200)
    if ma200 and spy_c[-1] > ma200:
        score += 3
    return min(10, score)


def factor_rr(c: list[float], trigger_early: float, invalidation: float, target: float) -> int:
    """Trigger 진입 시 R:R 비율. 0-15."""
    if trigger_early <= 0 or invalidation <= 0 or target <= 0:
        return 0
    risk = trigger_early - invalidation
    reward = target - trigger_early
    if risk <= 0 or reward <= 0:
        return 0
    rr = reward / risk
    if rr >= 4:
        return 15
    if rr >= 3:
        return 12
    if rr >= 2:
        return 9
    if rr >= 1.5:
        return 6
    if rr >= 1:
        return 3
    return 0


# ────────────────────────── Trigger / State ──────────────────────────
def compute_triggers(c: list[float]) -> dict:
    """
    EARLY = MA20 (뒤에서 지지받고 반등 진입)
    CONFIRM = 최근 20일 종가 신고점 돌파
    TREND = CONFIRM × 1.10 (추세 확장 타깃)
    Invalidation = MA50 (또는 MA20 - 5% 중 낮은 값)
    """
    ma20 = sma(c, 20) or c[-1]
    ma50 = sma(c, 50) or c[-1] * 0.95
    high20 = max(c[-20:]) if len(c) >= 20 else c[-1]
    early = round(ma20, 2)
    confirm = round(high20, 2)
    trend = round(high20 * 1.10, 2)
    invalidation = round(min(ma50, ma20 * 0.95), 2)
    return {
        "early": early,
        "confirm": confirm,
        "trend": trend,
        "invalidation": invalidation,
    }


def score_to_state(score: int) -> tuple[str, str]:
    """
    STRONG (78+), ACTIONABLE (65-77), READY (52-64), WATCH (38-51), PASS (<38)
    confidence: HIGH (75+), MED (55+), LOW (<55)
    """
    if score >= 78:
        state = "STRONG"
    elif score >= 65:
        state = "ACTIONABLE"
    elif score >= 52:
        state = "READY"
    elif score >= 38:
        state = "WATCH"
    else:
        state = "PASS"
    if score >= 75:
        conf = "HIGH"
    elif score >= 55:
        conf = "MED"
    else:
        conf = "LOW"
    return state, conf


# ────────────────────────── main ──────────────────────────
def analyze_ticker(
    ticker: str,
    ohlcv: dict,
    spy_c: list[float],
    vix_c: list[float],
) -> dict:
    o, h, l, c, v = ohlcv["o"], ohlcv["h"], ohlcv["l"], ohlcv["c"], ohlcv["v"]

    trend_s = factor_trend(c)
    pullback_s = factor_pullback(c)
    vol_s, rvol, vol_quality = factor_volume(c, v)
    reversal_s = factor_reversal(o, h, l, c)
    rs_s = factor_rs(c, spy_c)
    market_s = factor_market(spy_c, vix_c)

    triggers = compute_triggers(c)
    rr_s = factor_rr(c, triggers["early"], triggers["invalidation"], triggers["trend"])

    score = trend_s + pullback_s + vol_s + reversal_s + rs_s + market_s + rr_s
    state, conf = score_to_state(score)

    ma20 = sma(c, 20)
    ma50 = sma(c, 50)
    ma200 = sma(c, 200)
    avg20_v = sum(v[-21:-1]) / 20 if len(v) >= 21 else None

    # 차트용 200일 데이터. MA200이 200일 내내 그려지도록 전체 히스토리로 MA 계산 후 마지막 200일만 슬라이스.
    tail = 200
    ma20_series = rolling_sma(c, 20)
    ma50_series = rolling_sma(c, 50)
    ma200_series = rolling_sma(c, 200)

    def _round(x: float | None) -> float | None:
        return round(x, 2) if x is not None else None

    chart = {
        "labels": ohlcv["t"][-tail:],
        "close": c[-tail:],
        "volume": v[-tail:],
        "ma20": [_round(x) for x in ma20_series[-tail:]],
        "ma50": [_round(x) for x in ma50_series[-tail:]],
        "ma200": [_round(x) for x in ma200_series[-tail:]],
    }

    return {
        "ticker": ticker,
        "score": score,
        "state": state,
        "confidence": conf,
        "price": {
            "current": round(c[-1], 2),
            "ma20": round(ma20, 2) if ma20 else None,
            "ma50": round(ma50, 2) if ma50 else None,
            "ma200": round(ma200, 2) if ma200 else None,
            "change1d": round(pct_return(c, 1) or 0, 2),
        },
        "volume": {
            "today": v[-1],
            "avg20": int(avg20_v) if avg20_v else None,
            "rvol": rvol,
            "quality": vol_quality,
        },
        "factors": {
            "trend": trend_s,
            "pullback": pullback_s,
            "volume": vol_s,
            "reversal": reversal_s,
            "rs": rs_s,
            "market": market_s,
            "rr": rr_s,
        },
        "trigger": triggers,
        "chart": chart,
    }


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"[{started.isoformat()}] refresh start")

    universe = json.loads(UNIVERSE_PATH.read_text())
    tickers: list[str] = universe.get("tickers", [])
    print(f"universe: {len(tickers)} tickers")

    # 시장 지수 먼저
    print("Fetching SPY / VIX...")
    spy = fetch_ohlcv(SPY_TICKER, "1y", "1d")
    vix = fetch_ohlcv(VIX_TICKER, "1y", "1d")
    if not spy or not vix:
        print("  ⚠ SPY/VIX fetch 실패 — 시장 팩터 열화됨")
    spy_c = spy["c"] if spy else []
    vix_c = vix["c"] if vix else []

    signals: dict[str, dict] = {}
    failures: list[str] = []

    for t in tickers:
        d = fetch_ohlcv(t, "2y", "1d")
        if not d:
            print(f"  ⚠ {t} fetch 실패")
            failures.append(t)
            continue
        try:
            sig = analyze_ticker(t, d, spy_c, vix_c)
            signals[t] = sig
            print(f"  {t}: score={sig['score']} state={sig['state']} rvol={sig['volume']['rvol']}")
        except Exception as e:
            print(f"  ⚠ {t} 분석 실패: {e}")
            failures.append(t)

    market_summary = {
        "spy": {
            "price": round(spy_c[-1], 2) if spy_c else None,
            "ma50": round(sma(spy_c, 50) or 0, 2) if spy_c else None,
            "ma200": round(sma(spy_c, 200) or 0, 2) if spy_c else None,
            "change20d": round(pct_return(spy_c, 20) or 0, 2) if spy_c else None,
        },
        "vix": {
            "value": round(vix_c[-1], 2) if vix_c else None,
        },
    }

    output = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "universe": tickers,
        "market": market_summary,
        "signals": signals,
        "failures": failures,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    size = OUTPUT_PATH.stat().st_size
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"→ {OUTPUT_PATH.name} 저장 "
        f"({size:,} bytes, {len(signals)}/{len(tickers)} ok, {elapsed:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
