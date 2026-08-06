"""IM 跨期套利回测引擎（真实合约数据，无未来函数）。

策略：基于跨期价差 spread_rel=(far.close-near.close)/near.close 的滚动历史分位择时。
- 高分位(远月相对贵) -> 空远月 + 多近月 (赌价差收窄, spread 回落更负)
- 低分位(远月相对便宜) -> 多远月 + 空近月 (赌价差走扩, spread 回升)
T日收盘信号 -> T+1开盘成交；平仓信号T日 -> T+1收盘平仓；合约临近到期(<5天)强制移仓。

用法：python backtest_im_spread.py
"""
import pandas as pd
import numpy as np
import datetime as dt

PANEL = r"D:\WorkBuddy-workspaces\2026-07-29-16-29-57\bt_opt\im_spread_panel.parquet"
MULT = 200.0
MARGIN_RATE = 0.12
FEE_RATE = 0.00005  # 单边万分之0.5(含期货公司的保守估算)
INIT_CAP = 500000.0

def third_friday(yyyymm):
    y, m = yyyymm // 100, yyyymm % 100
    d = dt.date(y, m, 1)
    offset = (4 - d.weekday()) % 7  # Monday=0..Friday=4
    first_fri = d + dt.timedelta(days=offset)
    return first_fri + dt.timedelta(days=14)

def rolling_pct(s, window):
    """当前值在滚动窗口内的分位(0~1)。窗口超出数据长度则用全部已有哪些。"""
    w = min(window, len(s))
    out = np.full(len(s), np.nan)
    for i in range(w - 1, len(s)):
        win = s[max(0, i - w + 1): i + 1]
        out[i] = (win < s[i]).mean() + (win == s[i]).mean() * 0.0  # 严格小于占比
        # 用 <= 的占比作为分位更平滑
        out[i] = (win <= s[i]).mean()
    return out

def run_backtest(gap=2, window=504, enter_high=0.90, enter_low=0.10,
                 exit_low=0.40, exit_high=0.60, max_hold=120,
                 fee_rate=FEE_RATE, margin_rate=MARGIN_RATE, init_cap=INIT_CAP,
                 dir_mode="both", panel=None, pair_name=None, pct=None,
                 exec_price="open", lot_capital=None, max_lots=None, slip_pts=0.0):
    """lot_capital: 每多少元权益开一对(1手近+1手远)。None=全程固定1对(不复利)。
    开仓时按当时账户权益(无持仓, 故=已实现nav)计算 lots=max(1, floor(nav/lot_capital))。"""
    if panel is None:
        panel = pd.read_parquet(PANEL)
    s = panel["spread_rel"].values
    if pct is None:
        pct = rolling_pct(s, window)

    trades = []
    equity = []  # (date, nav)
    nav = init_cap
    floating = 0.0
    pos = None  # dict
    pending_open = None  # (side, row_idx)
    pending_close = False

    n = len(panel)
    for i in range(n):
        row = panel.iloc[i]
        p = pct[i]
        # 1) 先执行昨日pending平仓 (T+1 按exec_price成交)，释放仓位
        if pending_close and pos is not None and i + 1 < n:
            nxt = panel.iloc[i]
            near_px = nxt[f"near_{exec_price}"]; far_px = nxt[f"far_{exec_price}"]
            realized = calc_pnl(pos, near_px, far_px, fee_rate, slip_pts) * pos["lots"]
            nav += realized
            near_sign = 1 if pos["side"] == "S_far" else -1
            far_sign = -1 if pos["side"] == "S_far" else 1
            trades.append({
                "entry_date": pos["entry_date"], "exit_date": nxt["date"],
                "side": pos["side"], "near_code": pos["near_code"], "far_code": pos["far_code"],
                "near_open": pos["near_exec"], "far_open": pos["far_exec"],
                "near_close": near_px, "far_close": far_px,
                "entry_pct": pos["entry_pct"], "exit_pct": p,
                "hold_days": pos["hold_days"] + 1,
                "lots": pos["lots"], "cap_at_entry": pos["cap_at_entry"],
                "pnl": realized, "pnl_per_lot": realized / pos["lots"],
                "near_pts": (near_px - pos["near_exec"]) * near_sign,
                "far_pts": (far_px - pos["far_exec"]) * far_sign,
            })
            pos = None
            pending_close = False
        # 2) 再执行昨日pending开仓 (T+1 按exec_price成交)
        if pending_open is not None and i + 1 < n:
            side, sig_idx = pending_open
            nxt = panel.iloc[i]  # 信号日后第一天
            near_px = nxt[f"near_{exec_price}"]; far_px = nxt[f"far_{exec_price}"]
            near_code = nxt["near_code"]; far_code = nxt["far_code"]
            # 按当时权益决定开几对(此刻无持仓, nav即全部权益)
            if lot_capital is None:
                lots = 1
            else:
                lots = max(1, int(nav // lot_capital))
                if max_lots is not None:
                    lots = min(lots, max_lots)
            pos = {
                "side": side, "near_code": near_code, "far_code": far_code,
                "near_exec": near_px, "far_exec": far_px,
                "entry_date": nxt["date"], "entry_pct": pct[sig_idx],
                "hold_days": 0, "lots": lots, "cap_at_entry": nav,
            }
            pending_open = None

        # 3) 计算当前浮动盈亏
        if pos is not None:
            pos["hold_days"] += 1
            near_cl = row["near_close"]; far_cl = row["far_close"]
            floating = calc_pnl(pos, near_cl, far_cl, 0.0) * pos["lots"]  # 浮动不计费
        else:
            floating = 0.0

        # 4) 生成信号
        if not np.isnan(p):
            # 平仓判定
            if pos is not None and not pending_close:
                near_exp = int(row["near_exp"])
                dte = (third_friday(near_exp) - row["date"].date()).days
                if pos["side"] == "S_far":  # 空远多近: 价差回落(分位下降)平仓
                    if p <= exit_low or dte <= 5 or pos["hold_days"] >= max_hold:
                        pending_close = True
                else:  # L_far: 价差回升(分位上升)平仓
                    if p >= exit_high or dte <= 5 or pos["hold_days"] >= max_hold:
                        pending_close = True
            # 开仓判定 (空仓且无pending)
            if pos is None and pending_open is None and not pending_close:
                if dir_mode in ("both", "S_far") and p >= enter_high:
                    pending_open = ("S_far", i)  # 空远月多近月
                elif dir_mode in ("both", "L_far") and p <= enter_low:
                    pending_open = ("L_far", i)  # 多远月空近月

        equity.append((row["date"], nav + floating))

    # 末日残留持仓强制平仓(计入nav与trades, 保证对账为0)
    if pos is not None:
        last = panel.iloc[-1]
        near_px = last[f"near_{exec_price}"]; far_px = last[f"far_{exec_price}"]
        realized = calc_pnl(pos, near_px, far_px, fee_rate, slip_pts) * pos["lots"]
        nav += realized
        near_sign = 1 if pos["side"] == "S_far" else -1
        far_sign = -1 if pos["side"] == "S_far" else 1
        trades.append({
            "entry_date": pos["entry_date"], "exit_date": last["date"],
            "side": pos["side"], "near_code": pos["near_code"], "far_code": pos["far_code"],
            "near_open": pos["near_exec"], "far_open": pos["far_exec"],
            "near_close": near_px, "far_close": far_px,
            "entry_pct": pos["entry_pct"], "exit_pct": pct[-1] if not np.isnan(pct[-1]) else np.nan,
            "hold_days": pos["hold_days"],
            "lots": pos["lots"], "cap_at_entry": pos["cap_at_entry"],
            "pnl": realized, "pnl_per_lot": realized / pos["lots"],
            "near_pts": (near_px - pos["near_exec"]) * near_sign,
            "far_pts": (far_px - pos["far_exec"]) * far_sign,
        })
        pos = None
        equity[-1] = (equity[-1][0], nav)  # 最后一点净值不含浮动

    eq = pd.DataFrame(equity, columns=["date", "nav"])
    # 统计
    if trades:
        tdf = pd.DataFrame(trades)
        wins = (tdf["pnl"] > 0).sum()
        total = len(tdf)
        win_rate = wins / total
        total_pnl = tdf["pnl"].sum()
        # 年化(按交易日)
        days = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days
        years = days / 365.0
        ann_ret = (eq["nav"].iloc[-1] / init_cap) ** (1 / years) - 1 if years > 0 else 0
        # 回撤
        nav_arr = eq["nav"].values
        peak = np.maximum.accumulate(nav_arr)
        dd = nav_arr / peak - 1
        max_dd = dd.min()
        avg_hold = tdf["hold_days"].mean()
        avg_pts = (tdf["near_pts"] + tdf["far_pts"]).mean()
    else:
        tdf = pd.DataFrame()
        win_rate = total_pnl = ann_ret = max_dd = avg_hold = avg_pts = 0
        total = 0

    stats = {
        "gap": gap, "pair_name": pair_name, "window": window, "enter_high": enter_high, "enter_low": enter_low,
        "exit_low": exit_low, "exit_high": exit_high, "max_hold": max_hold,
        "trades": total, "win_rate": win_rate, "total_pnl": total_pnl,
        "ann_ret": ann_ret, "max_dd": max_dd, "avg_hold": avg_hold,
        "avg_pts": avg_pts, "final_nav": eq["nav"].iloc[-1],
        "init_cap": init_cap, "lot_capital": lot_capital, "exec_price": exec_price,
    }
    if total > 0 and "lots" in tdf.columns:
        stats["max_lots"] = int(tdf["lots"].max())
        stats["avg_lots"] = float(tdf["lots"].mean())
        nav_arr = eq["nav"].values
        stats["max_dd_amt"] = float((nav_arr - np.maximum.accumulate(nav_arr)).min())
    # 会计对平校验: 末值 = 初始 + 累计已实现 (末日应无持仓)
    if total > 0:
        chk = abs(eq["nav"].iloc[-1] - (init_cap + total_pnl))
        assert chk < 1.0, f"会计对平失败 残差={chk}"
        stats["audit_resid"] = chk
    else:
        stats["audit_resid"] = 0.0
    # side 分布
    if total > 0:
        side_cnt = tdf["side"].value_counts().to_dict()
        side_win = tdf.groupby("side").apply(lambda x: (x["pnl"] > 0).mean()).to_dict()
        stats["side_cnt"] = side_cnt
        stats["side_win"] = side_win
    return stats, tdf, eq

def calc_pnl(pos, near_close, far_close, fee_rate, slip_pts=0.0):
    """slip_pts: 每腿每次成交的不利滑点(点)。一轮完整交易共4次成交(开2腿+平2腿)。"""
    mult = MULT
    if pos["side"] == "S_far":  # 多近 + 空远
        near_pl = (near_close - pos["near_exec"]) * +1 * mult
        far_pl = (far_close - pos["far_exec"]) * -1 * mult
    else:  # L_far: 空近 + 多远
        near_pl = (near_close - pos["near_exec"]) * -1 * mult
        far_pl = (far_close - pos["far_exec"]) * +1 * mult
    fee = fee_rate * mult * (pos["near_exec"] + pos["far_exec"] + near_close + far_close)
    slip = slip_pts * mult * 4.0
    return near_pl + far_pl - fee - slip

if __name__ == "__main__":
    # 默认参数测试
    for eh, el in [(0.90, 0.10), (0.85, 0.15), (0.95, 0.05)]:
        stats, tdf, eq = run_backtest(enter_high=eh, enter_low=el, window=504)
        print(f"enter_high={eh} enter_low={el}: 笔数={stats['trades']} 胜率={stats['win_rate']:.2%} "
              f"总盈亏={stats['total_pnl']:.0f} 年化={stats['ann_ret']:.2%} 回撤={stats['max_dd']:.2%} "
              f"均持={stats['avg_hold']:.0f}天 均赚点={stats['avg_pts']:.1f} 对账残差={stats['audit_resid']:.4f}")
        print(f"   side分布={stats.get('side_cnt')} side胜率={stats.get('side_win')}")
        if len(tdf):
            print(tdf[["entry_date","exit_date","side","near_code","far_code","entry_pct","exit_pct","hold_days","pnl"]].to_string(index=False))
