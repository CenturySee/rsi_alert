"""FastAPI 入口：K 线 + RSI + 预警价格。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import calculator as calc
import fetcher

app = FastAPI(title="RSI 预警")

FRONTEND = Path(__file__).resolve().parent / "frontend" / "index.html"

DEFAULT_DOWN = [30, 25, 20, 15, 10]
DEFAULT_UP = [75, 80]


def _parse_thresholds(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default
    try:
        return [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail=f"阈值格式错误：{raw}")


def _build_payload(code: str, market: str | None, period: int, days: int,
                   down: list[float], up: list[float], *, force: bool = False) -> dict:
    market = market or fetcher.infer_market(code)
    try:
        bars = fetcher.load_klines(code, market, days, force=force)
    except Exception as exc:  # opentdx / 网络问题
        raise HTTPException(status_code=502, detail=f"行情获取失败：{exc}")

    closes = [b["close"] for b in bars]
    if len(closes) < period + 1:
        raise HTTPException(status_code=422, detail=f"{market}.{code} 数据不足，无法计算 RSI({period})")

    limit = calc.limit_pct(code)
    tick = calc.tick_size(code)
    alerts = calc.build_alerts(closes, down, up, period, limit=limit, tick=tick)
    try:
        next_day = fetcher.next_trading_day()
    except Exception:
        next_day = None

    return {
        "code": code,
        "market": market,
        "period": period,
        "limit_pct": limit,
        "last_close": closes[-1],
        "current_rsi": round(calc.current_rsi(closes, period), 2),
        "next_trading_day": next_day,
        "kline": bars,
        "rsi": calc.rsi_series(closes, period),
        # 副图：标准 6/12/24 + 当前预警周期 period（去重）
        "rsi_period": period,
        "rsi_multi": {
            str(p): calc.rsi_series(closes, p) for p in sorted({6, 12, 24, period})
        },
        "alerts": {
            "down": [asdict(a) for a in alerts["down"]],
            "up": [asdict(a) for a in alerts["up"]],
        },
    }


@app.get("/api/kline")
def api_kline(
    code: str,
    market: str | None = None,
    period: int = 6,
    days: int = 300,
    down_thresholds: str | None = Query(default=None),
    up_thresholds: str | None = Query(default=None),
):
    down = _parse_thresholds(down_thresholds, DEFAULT_DOWN)
    up = _parse_thresholds(up_thresholds, DEFAULT_UP)
    return _build_payload(code, market, period, days, down, up)


@app.post("/api/refresh")
def api_refresh(
    code: str,
    market: str | None = None,
    period: int = 6,
    days: int = 300,
    down_thresholds: str | None = Query(default=None),
    up_thresholds: str | None = Query(default=None),
):
    down = _parse_thresholds(down_thresholds, DEFAULT_DOWN)
    up = _parse_thresholds(up_thresholds, DEFAULT_UP)
    return _build_payload(code, market, period, days, down, up, force=True)


@app.get("/")
def index():
    if not FRONTEND.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html 不存在")
    return FileResponse(FRONTEND)
