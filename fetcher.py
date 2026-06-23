"""行情获取、本地缓存与交易日历。

- 日线行情来自本地 opentdx 包（C:/xzq/projects/github/opentdx），前复权（QFQ，以最新为基准）。
- 缓存：一品种一 JSON 文件，原子写；区分盘中 / 盘后，盘后不读盘中残缺缓存。
- 交易日历：akshare.tool_trade_date_hist_sina，本地缓存，超出覆盖区间则更新。
- 每日整体覆盖，不做增量追加（前复权价会随除权平移）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CALENDAR_FILE = DATA_DIR / "trade_calendar.json"

Market = str  # "SH" | "SZ" | "BJ"


# ---------------------------------------------------------------------------
# 市场推断
# ---------------------------------------------------------------------------
def infer_market(code: str) -> Market:
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("4", "8")) or code.startswith("920"):
        return "BJ"
    return "SZ"


def _market_enum(market: Market):
    from opentdx.const import MARKET

    return {"SH": MARKET.SH, "SZ": MARKET.SZ, "BJ": MARKET.BJ}[market]


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# 交易日历（akshare）
# ---------------------------------------------------------------------------
def _load_calendar_cache() -> list[str] | None:
    if not CALENDAR_FILE.exists():
        return None
    data = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
    return data.get("dates")


def _refresh_calendar() -> list[str]:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    dates = sorted(str(d) for d in df["trade_date"].tolist())
    _atomic_write_json(CALENDAR_FILE, {"updated_at": datetime.now().isoformat(timespec="seconds"), "dates": dates})
    return dates


def get_trade_calendar(today: date | None = None) -> list[str] | None:
    """返回交易日（YYYY-MM-DD 字符串）。超出缓存覆盖区间则用 akshare 刷新。

    akshare 不可用 / 取数失败时返回 None，由调用方回退到工作日启发式。
    """
    today = today or date.today()
    dates = _load_calendar_cache()
    if not dates or today.isoformat() > dates[-1]:
        try:
            dates = _refresh_calendar()
        except Exception:
            return dates  # 可能为 None；旧缓存仍可用则返回旧缓存
    return dates


def next_trading_day(after: date | None = None) -> str:
    """返回 `after`（默认今天）之后的第一个交易日；无日历时回退到下一个工作日。"""
    after = after or date.today()
    cal = get_trade_calendar(after)
    if cal:
        for d in cal:
            if d > after.isoformat():
                return d
    nxt = after + timedelta(days=1)
    while nxt.weekday() >= 5:  # 跳过周末（不含法定节假日）
        nxt += timedelta(days=1)
    return nxt.isoformat()


def is_trading_session(now: datetime | None = None) -> bool:
    """当前是否处于交易日盘中（09:30–15:00）；无日历时按工作日判断。"""
    now = now or datetime.now()
    if not (time(9, 30) <= now.time() <= time(15, 0)):
        return False
    cal = get_trade_calendar(now.date())
    if cal is not None:
        return now.strftime("%Y-%m-%d") in set(cal)
    return now.weekday() < 5


# ---------------------------------------------------------------------------
# 行情获取
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def _fetch_from_opentdx(code: str, market: Market, count: int) -> list[Bar]:
    """盘中会多取一根并去掉实时未收盘的最新一根。"""
    from opentdx.const import ADJUST, PERIOD
    from opentdx.tdxClient import TdxClient

    intraday = is_trading_session()
    fetch_count = count + 1 if intraday else count

    with TdxClient() as client:
        rows = client.stock_kline(_market_enum(market), code, PERIOD.DAILY, count=fetch_count, adjust=ADJUST.QFQ)
    if not rows:
        raise RuntimeError(f"opentdx 未返回 {market}.{code} 的日 K 线")

    bars: list[Bar] = []
    seen: set[str] = set()
    for r in rows:
        d = str(r["datetime"])[:10]
        if d in seen:
            continue
        seen.add(d)
        vol = r.get("volume", r.get("vol", 0))
        bars.append(Bar(
            d,
            round(float(r["open"]), 3),
            round(float(r["high"]), 3),
            round(float(r["low"]), 3),
            round(float(r["close"]), 3),
            float(vol),
        ))
    bars.sort(key=lambda b: b.date)

    if intraday and len(bars) > count:
        bars = bars[:-1]
    return bars


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------
def _cache_path(market: Market, code: str) -> Path:
    return DATA_DIR / f"{market}_{code}.json"


def _cache_valid(payload: dict, now: datetime) -> bool:
    """命中条件：今日抓取，且盘后不复用盘中残缺缓存。"""
    fetched = payload.get("fetched_at", "")
    if fetched[:10] != now.strftime("%Y-%m-%d"):
        return False
    if payload.get("session") == "intraday" and not is_trading_session(now):
        return False
    return True


def load_klines(code: str, market: Market | None = None, days: int = 300, *, force: bool = False) -> list[dict]:
    """读取日线（前复权）。命中有效缓存则用缓存，否则重拉并覆盖。"""
    market = market or infer_market(code)
    path = _cache_path(market, code)
    now = datetime.now()

    if not force and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if _cache_valid(payload, now) and len(payload.get("bars", [])) >= days:
            return payload["bars"][-days:]

    bars = _fetch_from_opentdx(code, market, days)
    payload = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "session": "intraday" if is_trading_session(now) else "closed",
        "bars": [b.as_dict() for b in bars],
    }
    _atomic_write_json(path, payload)
    return payload["bars"][-days:]
