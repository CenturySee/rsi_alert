"""RSI 计算与预警价格反推。

RSI 口径与通达信一致（`SMA(N, 1)` 递推平滑，等价 Wilder），
对应参考脚本 `hongli/scripts/calc_rsi_trigger_price.py` 的 `calc_rsi`。

本模块为纯函数、无第三方依赖，便于单测和复用。预警价格用解析解直接求出，
不做暴力扫描：下一根 K 线的 RSI 只取决于上一根的递推状态 (avg_gain, avg_abs)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Sequence

Direction = Literal["up", "down"]


class AlertStatus(str, Enum):
    """单个阈值的预警状态。"""

    NORMAL = "normal"  # 正常：给出触发价
    REACHED = "reached"  # 已触及：次日收盘不变即已越过阈值
    UNREACHABLE = "unreachable"  # 需超涨跌停：触发价越过当日涨跌停板


@dataclass(frozen=True)
class Alert:
    """一个阈值的反推结果。"""

    threshold: float
    direction: Direction
    status: AlertStatus
    price: float | None = None  # 触发价（已按 tick 取整）；非 NORMAL 为 None
    pct_change: float | None = None  # 相对今收涨跌幅 %
    est_rsi: float | None = None  # 触发价代回 RSI，用于自检


# ---------------------------------------------------------------------------
# RSI 计算
# ---------------------------------------------------------------------------
def _rma(values: Sequence[float | None], period: int) -> list[float | None]:
    """通达信 SMA(period, 1) 递推平滑。

    与参考脚本 `tdx_sma(weight=1)` 行为一致：遇到 None（序列首位的 diff）跳过、
    不更新状态；首个有效值作为种子，其后 prev = (v + (period-1)*prev) / period。
    """
    out: list[float | None] = []
    prev: float | None = None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        if prev is None:
            prev = float(v)
        else:
            prev = (float(v) + (period - 1) * prev) / period
        out.append(prev)
    return out


def _gains_and_abs(closes: Sequence[float]) -> tuple[list[float | None], list[float | None]]:
    """返回 (上涨幅度, 变动绝对值) 两个序列，首位为 None（无前收）。"""
    gains: list[float | None] = [None]
    abs_deltas: list[float | None] = [None]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        abs_deltas.append(abs(delta))
    return gains, abs_deltas


def rsi_series(closes: Sequence[float], period: int = 6) -> list[float | None]:
    """计算整条 RSI 序列，用于绘图。前 `period` 个值置 None（预热期）。"""
    if len(closes) < 2:
        return [None] * len(closes)

    gains, abs_deltas = _gains_and_abs(closes)
    avg_gain = _rma(gains, period)
    avg_abs = _rma(abs_deltas, period)

    rsi: list[float | None] = []
    for g, a in zip(avg_gain, avg_abs):
        if g is None or a is None:
            rsi.append(None)
        elif a == 0:
            rsi.append(50.0)  # 无任何波动
        else:
            rsi.append(g / a * 100)

    for i in range(min(period, len(rsi))):
        rsi[i] = None
    return rsi


def _rsi_state(closes: Sequence[float], period: int) -> tuple[float, float]:
    """返回最后一根 K 线的 (avg_gain, avg_abs) 递推状态。"""
    gains, abs_deltas = _gains_and_abs(closes)
    g = _rma(gains, period)[-1]
    a = _rma(abs_deltas, period)[-1]
    if g is None or a is None:
        raise ValueError("收盘价不足以计算 RSI 状态")
    return g, a


def current_rsi(closes: Sequence[float], period: int = 6) -> float:
    """最后一根 K 线的 RSI 值（不预热置空，供状态条展示）。"""
    g, a = _rsi_state(closes, period)
    if a == 0:
        return 50.0
    return g / a * 100


# ---------------------------------------------------------------------------
# 涨跌停区间（按代码前缀，A 股；期货 v1 不支持）
# ---------------------------------------------------------------------------
def limit_pct(code: str, *, is_st: bool = False) -> float:
    """返回当日涨跌幅上限（小数）。ST 需调用方传入 is_st。"""
    if is_st:
        return 0.05
    if code.startswith("688"):  # 科创板
        return 0.20
    if code.startswith(("300", "301")):  # 创业板
        return 0.20
    if code.startswith(("4", "8")) or code.startswith("920"):  # 北交所
        return 0.30
    return 0.10  # 沪深主板


def tick_size(code: str) -> float:
    """最小变动价位（元）。

    A 股不同品种的报价档位不同：股票为 0.01，ETF/LOF 基金、REITs、
    可转债为 0.001。用错档位会反推出市场上不存在的触发价，故按代码前缀判定。
    """
    # 基金：沪 50/51/52/56/58（含科创ETF、REITs 508）、深 15/16/18（含 REITs 180）
    if code.startswith(("50", "51", "52", "56", "58", "15", "16", "18")):
        return 0.001
    # 可转债：沪 11、深 12
    if code.startswith(("11", "12")):
        return 0.001
    return 0.01  # 股票


# ---------------------------------------------------------------------------
# 预警价格反推（解析解）
# ---------------------------------------------------------------------------
def _round_tick(price: float, tick: float = 0.01) -> float:
    # 末位 round 仅清理浮点噪声，精度由 tick 决定（tick=0.01 即两位小数）
    return round(round(price / tick) * tick, 10)


def trigger_alert(
    closes: Sequence[float],
    threshold: float,
    direction: Direction,
    period: int = 6,
    *,
    limit: float = 0.10,
    tick: float = 0.01,
) -> Alert:
    """反推使下一根 RSI 恰好触及 `threshold` 所需的收盘价。

    设今收为 C、次日收盘 P、delta = P - C。次日新增一根：
      上涨 (delta>0): gain=delta, abs=delta
      下跌 (delta<0): gain=0,     abs=-delta
    递推 (N=period, weight=1):
      avg_gain' = (gain + (N-1)*g) / N
      avg_abs'  = (abs  + (N-1)*a) / N
      RSI = avg_gain'/avg_abs' * 100 = threshold

    两个分支对 delta 都是线性的，直接解出：
      down: delta = (N-1)*(T*a - 100*g) / T
      up:   delta = (N-1)*(T*a - 100*g) / (100 - T)
    """
    if len(closes) < period + 1:
        raise ValueError(f"收盘价数量不足，至少需要 {period + 1} 个")

    g, a = _rsi_state(closes, period)
    close_today = closes[-1]
    n1 = period - 1
    t = float(threshold)

    if direction == "down":
        delta = n1 * (t * a - 100 * g) / t
        already = delta >= 0  # 需上涨才能到达 => 当前已在阈值下方
        bound = _round_tick(close_today * (1 - limit), tick)
        outside = lambda p: p < bound  # noqa: E731
    else:
        delta = n1 * (t * a - 100 * g) / (100 - t)
        already = delta <= 0  # 需下跌才能到达 => 当前已在阈值上方
        bound = _round_tick(close_today * (1 + limit), tick)
        outside = lambda p: p > bound  # noqa: E731

    if already:
        return Alert(threshold, direction, AlertStatus.REACHED)

    price = _round_tick(close_today + delta, tick)
    if outside(price):
        return Alert(threshold, direction, AlertStatus.UNREACHABLE)

    pct = (price - close_today) / close_today * 100
    est = rsi_series(list(closes) + [price], period)[-1]
    return Alert(
        threshold=threshold,
        direction=direction,
        status=AlertStatus.NORMAL,
        price=price,
        pct_change=round(pct, 2),
        est_rsi=round(est, 2) if est is not None else None,
    )


def build_alerts(
    closes: Sequence[float],
    down_thresholds: Sequence[float],
    up_thresholds: Sequence[float],
    period: int = 6,
    *,
    limit: float = 0.10,
    tick: float = 0.01,
) -> dict[str, list[Alert]]:
    """批量计算下穿 / 上穿预警。"""
    return {
        "down": [trigger_alert(closes, t, "down", period, limit=limit, tick=tick) for t in down_thresholds],
        "up": [trigger_alert(closes, t, "up", period, limit=limit, tick=tick) for t in up_thresholds],
    }
