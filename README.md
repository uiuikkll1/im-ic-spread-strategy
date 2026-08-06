# 股指期货跨期展期策略 · IM/IC 推荐实操页

多下月(c2)+空隔季(c4) 吃贴水收敛（展期收益）的股指跨期套利策略实操页，
由 GitHub Actions **每日自动重建数据并重新部署**，电脑关机也能更新。

## 策略概要
- **IM（中证1000）**：远月-隔季（i1_i3），滚动120日分位 ≥ 0.40（深贴水）时次日开盘开仓；要求下月合约相对指数贴水。
- **IC（中证500）**：远月-隔季（i1_i3），纯持有，始终在场。
- 本金 50 万、各 1 手、open 进 close 出、仅换月平仓（下月距到期 ≤ 10 交易日）。
- 实时操作提示卡片基于生成时联网行情（公网页面为静态快照，浏览器端实时刷新受行情源跨域限制可能不生效）。

## 自动更新
- 工作流 `.github/workflows/deploy.yml` 每天 **18:00（北京时间）** 自动运行：
  1. 用 akshare 联网拉取 IM/IC 全部合约日线、主连、指数；
  2. 重建跨期价差面板；
  3. 重新生成 `public/index.html` 并部署到 GitHub Pages。
- 也可在仓库 **Actions** 标签页手动 **Run workflow** 立即更新。

## 本地运行
```bash
pip install -r requirements.txt
python build_reco_strategy_page.py          # 复用本地 parquet（若存在）
FORCE_FETCH=1 python build_reco_strategy_page.py   # 强制联网重建全部数据
```
生成 `股指跨期展期策略_IM_IC推荐实操页.html` 与 `public/index.html`。

## 文件说明
- `build_reco_strategy_page.py` — 主脚本：跑回测 + 生成 HTML
- `fetch_full_data.py` — 从 akshare 全量拉取并重建所有 parquet / 面板
- `spread_hold_lib.py` / `backtest_im_spread.py` — 回测引擎
- `live_signal.py` — 实时信号（合约按当日动态推导，行情优先新浪、回退 akshare）
- `build_all_spread_panels.py` — 跨期价差面板构造
- `echarts.min.js` — 离线内联图表库
