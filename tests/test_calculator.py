"""calculator 单测：重点验证解析解的自洽性与边界状态。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calculator as calc  # noqa: E402
from calculator import AlertStatus  # noqa: E402


# --- 一段普通的历史收盘价（无极端单边） ---
CLOSES = [
    10.0, 10.2, 10.1, 9.8, 9.5, 9.2, 9.4, 9.6, 9.9, 10.3,
    10.5, 10.2, 10.0, 9.7, 9.9, 10.1, 10.4, 10.6, 10.3, 10.5,
]


def _brute_force_trigger(closes, threshold, direction, period, limit=0.10, step=0.001):
    """暴力扫描参照实现：与解析解对拍。"""
    close_today = closes[-1]
    lo = round(close_today * (1 - limit), 3)
    hi = round(close_today * (1 + limit), 3)
    if direction == "down":
        price = hi
        while price >= lo:
            r = calc.rsi_series(list(closes) + [round(price, 3)], period)[-1]
            if r is not None and r <= threshold:
                return round(price, 3)
            price -= step
    else:
        price = lo
        while price <= hi:
            r = calc.rsi_series(list(closes) + [round(price, 3)], period)[-1]
            if r is not None and r >= threshold:
                return round(price, 3)
            price += step
    return None


# ---------------------------------------------------------------------------
# 解析解自洽：用极细 tick + 放开涨跌停（纯数学），触发价代回 RSI 应几乎等于阈值
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("threshold", [30, 25, 20])
def test_down_trigger_roundtrip(threshold):
    alert = calc.trigger_alert(CLOSES, threshold, "down", period=6, limit=1.0, tick=1e-9)
    assert alert.status is AlertStatus.NORMAL
    assert alert.price < CLOSES[-1]
    assert abs(alert.est_rsi - threshold) < 0.01


@pytest.mark.parametrize("threshold", [75, 80])
def test_up_trigger_roundtrip(threshold):
    alert = calc.trigger_alert(CLOSES, threshold, "up", period=6, limit=1.0, tick=1e-9)
    assert alert.status is AlertStatus.NORMAL
    assert alert.price > CLOSES[-1]
    assert abs(alert.est_rsi - threshold) < 0.01


def test_real_tick_rounding_small_error():
    # 真实 0.01 元 tick 下，代回 RSI 与阈值的偏差应在合理范围（< 0.5）
    alert = calc.trigger_alert(CLOSES, 75, "up", period=6, limit=1.0)
    assert abs(alert.est_rsi - 75) < 0.5


# ---------------------------------------------------------------------------
# 解析解 vs 暴力扫描：触发价应接近（差异在扫描步长量级内）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("threshold,direction", [(30, "down"), (25, "down"), (75, "up"), (80, "up")])
def test_analytic_matches_bruteforce(threshold, direction):
    alert = calc.trigger_alert(CLOSES, threshold, direction, period=6, limit=1.0, tick=1e-9)
    brute = _brute_force_trigger(CLOSES, threshold, direction, period=6, limit=1.0)
    assert brute is not None
    assert abs(alert.price - brute) < 0.02


# ---------------------------------------------------------------------------
# 边界状态
# ---------------------------------------------------------------------------
def test_already_reached_down():
    # 一路下跌 -> 当前 RSI 已很低，阈值 30 应判为已触及
    falling = [10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0]
    alert = calc.trigger_alert(falling, 30, "down", period=6)
    assert alert.status is AlertStatus.REACHED
    assert alert.price is None


def test_already_reached_up():
    rising = [7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0]
    alert = calc.trigger_alert(rising, 75, "up", period=6)
    assert alert.status is AlertStatus.REACHED


def test_unreachable_when_outside_limit():
    # 极低阈值在 ±10% 内不可达 -> 需超涨跌停
    alert = calc.trigger_alert(CLOSES, 1, "down", period=6, limit=0.10)
    assert alert.status is AlertStatus.UNREACHABLE
    assert alert.price is None


# ---------------------------------------------------------------------------
# RSI 序列 & 口径
# ---------------------------------------------------------------------------
def test_rsi_series_warmup_and_range():
    series = calc.rsi_series(CLOSES, period=6)
    assert series[:6] == [None] * 6
    tail = [v for v in series[6:] if v is not None]
    assert all(0 <= v <= 100 for v in tail)


def test_rsi_matches_reference():
    """回归基准：与参考脚本 calc_rsi（pandas tdx_sma，通达信口径）逐位对齐。

    期望值由 hongli/scripts/calc_rsi_trigger_price.py 的 calc_rsi 在同一序列上算出并钉死，
    避免改动 RSI 实现时口径悄悄偏离。
    """
    closes = [10.0, 10.2, 10.1, 9.8, 9.5, 9.2, 9.4, 9.6, 9.9, 10.3, 10.5, 10.2]
    assert calc.current_rsi(closes, period=6) == pytest.approx(63.294960, abs=1e-5)
    assert calc.current_rsi(closes, period=12) == pytest.approx(70.785352, abs=1e-5)


# ---------------------------------------------------------------------------
# 各板块涨跌停
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,expected",
    [("600000", 0.10), ("000001", 0.10), ("688981", 0.20), ("300750", 0.20), ("830799", 0.30)],
)
def test_limit_pct(code, expected):
    assert calc.limit_pct(code) == expected


def test_limit_pct_st():
    assert calc.limit_pct("600000", is_st=True) == 0.05
