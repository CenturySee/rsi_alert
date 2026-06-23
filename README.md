# RSI 预警终端

本地 Web 页面：展示指定 A 股品种的日线 K 线与 RSI 指标，并**反推下一交易日 RSI 触及各阈值时对应的收盘价和涨跌幅**。

适合盯 RSI 做网格 / 抄底逃顶的个人交易者：一眼看清「明天跌到多少 RSI 会破 30」「涨到多少会上 80」。

![终端式深色界面：左侧 K 线 + 成交量 + RSI 图，右侧价格阶梯预警表]

## 功能

- **日线 K 线**：最近 300 个交易日 OHLCV（前复权），默认显示最近约 80 根，可缩放
- **RSI 指标**：周期可调（默认 6），口径与通达信一致（`SMA(N,1)` 递推平滑）
- **预警价格阶梯**：反推下一交易日收盘价需达到多少，才能使 RSI 恰好触及阈值
  - 下穿：30 / 25 / 20 / 15 / 10（可增删）
  - 上穿：75 / 80（可增删）
  - 同时给出相对今收的涨跌幅，并标注「已触及」「需超涨跌停」
- **状态条**：当前 RSI + 距最近阈值还需涨跌幅 + 下一交易日日期
- 市场按代码前缀自动推断（SH / SZ / BJ），参数（周期、阈值）本地持久化

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)（已锁定 Python ≥ 3.12）。

```bash
cd rsi-alert
uv sync                                          # 创建 venv、安装依赖
uv run uvicorn main:app --reload --port 8000     # 启动
```

浏览器打开 <http://localhost:8000>，输入品种代码（如 `000001`）回车查询。

测试：

```bash
uv run pytest -q
```

## 部署到公网

把本应用部署到「有公网 IP 但无固定 IP、运营商封了 80/443」的小主机（Cloudflare Tunnel 方案，
免 DDNS、免端口转发、自动 HTTPS）：见 [`deploy/部署指南.md`](deploy/部署指南.md)，
服务器端可直接跑 [`deploy/deploy.sh`](deploy/deploy.sh) 一键完成。

## 工作原理

**预警价格用解析解直接求出，不做暴力扫描。** 下一根 K 线的 RSI 只取决于上一根的递推状态 `(avg_gain, avg_abs)`，对次日变动 `delta` 是线性的：

```
down: delta = (N-1)·(T·a − 100·g) / T          # 反推下穿阈值 T
up:   delta = (N-1)·(T·a − 100·g) / (100 − T)  # 反推上穿阈值 T
```

其中 `N` = RSI 周期，`g/a` = 最后一根的 `avg_gain / avg_abs`。求出 `delta` 即得触发价 `今收 + delta`，O(1) 且精确（详见 `calculator.py` 与 `需求与设计.md` 第五节）。

- `delta` 符号与方向矛盾 → **已触及**（次日收盘不变即越过阈值）
- 触发价越过当日涨跌停（按板块 ±10/20/30/5%）→ **需超涨跌停**

## 项目结构

```
rsi-alert/
├── pyproject.toml      uv 项目定义（依赖 / Python 版本 / opentdx 本地源）
├── main.py             FastAPI：/api/kline、/api/refresh、/
├── fetcher.py          opentdx 行情 + JSON 缓存 + akshare 交易日历
├── calculator.py       RSI 计算 + 预警价格反推（纯函数，无第三方依赖）
├── frontend/index.html 单页应用（ECharts，深色终端风格）
├── tests/              calculator 单测
├── data/               缓存目录（自动创建）
└── 需求与设计.md        完整需求 / 设计 / 算法 / 验收文档
```

## API

| Method | Path | 说明 |
|--------|------|------|
| GET  | `/api/kline`   | 参数 `code` `market` `period` `days` `down_thresholds` `up_thresholds`，返回 K 线 + RSI + 预警 |
| POST | `/api/refresh` | 同上，忽略缓存强制重拉 |
| GET  | `/`            | 返回前端页面 |

## 说明与注意

- **行情数据**来自公开 PyPI 包 `opentdx`（**不是** `pytdx`）。本仓库为方便本地开发，在 `pyproject.toml` 的 `[tool.uv.sources]` 里用 editable 本地源覆盖了它；部署时去掉该覆盖即从 PyPI 安装（见 `deploy/`）。
- **交易日历**用 `akshare.tool_trade_date_hist_sina`，本地缓存到 `data/trade_calendar.json`，超出覆盖区间自动更新；akshare 不可用时回退到「工作日」启发式（跳周末、不含法定节假日）。
- **缓存**每品种一个 JSON 文件，原子写（tmp + rename），区分盘中 / 盘后；每日整体覆盖，不做增量追加（前复权价会随除权平移）。
- **验收**：与通达信对比最后 5 根 K 线收盘价、最后 3 个 RSI(6) 值；预警价代回 RSI 应等于阈值（详见 `需求与设计.md` 第十节）。
- v1 仅支持沪深 A 股（含北交所），**不支持期货**。
