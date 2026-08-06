# -*- coding: utf-8 -*-
"""跨期价差【持有型】回测公共库（锁定真实合约，无换约偷价）。

与 backtest_spread_v2 的区别：不做分位均值回归择时，而是持续持有某方向的
价差组合，近月腿临近最后交易日时换月。用于捕捉展期收益(贴水收敛)。
"""
import pandas as pd, numpy as np, datetime as dt

MULT = 200.0
FEE_RATE = 0.00005
PAIRS = {"nym_nexq": "远月-下季", "nym_nxfq": "远月-隔季", "nexq_nxfq": "下季-隔季"}
PRODS = {"ic": "IC", "im": "IM"}


def build_px(raw):
    px = {}
    for row in raw.itertuples(index=False):
        px[(row.date, row.code)] = (row.open, row.close, row.settle)
    return px


def _trading_days_between(start, end):
    """计算 start 到 end 之间（含两端）的交易日数量。"""
    if start is None or end is None or start > end:
        return 0
    s = start.date() if hasattr(start, 'date') else start
    e = end.date() if hasattr(end, 'date') else end
    days = 0
    d = s
    while d <= e:
        if d.weekday() < 5:
            days += 1
        d += dt.timedelta(days=1)
    return days


def contract_last_trade_day(code):
    """由股指期货合约代码（如 IM2609）推导最后交易日：到期月第三个周五。"""
    ym = int(code[2:6]); y = 2000 + ym // 100; m = ym % 100
    d = dt.date(y, m, 1)
    offset = (4 - d.weekday()) % 7
    return pd.Timestamp(d + dt.timedelta(days=offset + 14))


def run_hold(panel, px, last_day, side="S_far", roll_buf=5, init_cap=250000.0,
             lots=1, exec_price="settle", entry_price=None, exit_price=None,
             pct=None, enter_high=None, exit_pct=0.5, slip_pts=0.0,
             basis_ok=None, return_pos=False):
    """持续持有 side 方向价差组合，近月腿距最后交易日<=roll_buf 换月。

    side: S_far=多近月+空远月 ; L_far=空近月+多远月
    enter_high=None -> 纯持有(始终在场)；否则仅当分位>=enter_high 开仓。
    exit_pct<=0 -> 持仓期间不因分位变化平仓，仅换月/到期/EOD平仓。
    entry_price/exit_price 可分别指定，默认沿用 exec_price；盯市用 close。
    return_pos=True 同时返回循环结束时仍在持仓的 pos 字典（None=空仓）。
    全程锁定具体合约，不存在换约偷价。
    """
    dates = panel["date"].tolist()
    dpos = {d: i for i, d in enumerate(dates)}
    nc = panel["near_code"].values
    fc = panel["far_code"].values
    entry_price = entry_price or exec_price
    exit_price = exit_price or exec_price
    fi_entry = {"open": 0, "close": 1, "settle": 2}[entry_price]
    fi_exit = {"open": 0, "close": 1, "settle": 2}[exit_price]

    trades, equity = [], []
    nav = init_cap
    pos = None

    for i, d in enumerate(dates):
        if pos is not None:
            ld = last_day.get(pos["nc"])
            dte = _trading_days_between(d, ld)
            need_roll = dte <= roll_buf
            timing_exit = False
            if enter_high is not None and pct is not None and not np.isnan(pct[i]) and exit_pct > 0:
                timing_exit = pct[i] < exit_pct
            if need_roll or timing_exit:
                a = px.get((d, pos["nc"])); b = px.get((d, pos["fc"]))
                if a is not None and b is not None and not pd.isna(a[fi_exit]) and not pd.isna(b[fi_exit]):
                    npx, fpx = a[fi_exit], b[fi_exit]
                    sgn_n = 1 if pos["side"] == "S_far" else -1
                    npts = (npx - pos["npx"]) * sgn_n
                    fpts = (fpx - pos["fpx"]) * (-sgn_n)
                    gross = (npts + fpts) * MULT * pos["lots"]
                    fee = FEE_RATE * MULT * (pos["npx"] + pos["fpx"] + npx + fpx) * pos["lots"]
                    slip = slip_pts * MULT * 4 * pos["lots"]
                    pnl = gross - fee - slip
                    nav += pnl
                    trades.append({
                        "entry_date": pos["d"], "exit_date": d, "side": pos["side"],
                        "dir_cn": "多近月+空远月" if pos["side"] == "S_far" else "空近月+多远月",
                        "near_code": pos["nc"], "far_code": pos["fc"],
                        "near_entry_px": pos["npx"], "near_exit_px": npx,
                        "far_entry_px": pos["fpx"], "far_exit_px": fpx,
                        "near_pts": npts, "far_pts": fpts, "total_pts": npts + fpts,
                        "entry_spread": pos["fpx"] - pos["npx"], "exit_spread": fpx - npx,
                        "hold_days": i - dpos[pos["d"]], "lots": pos["lots"],
                        "gross": gross, "fee": fee, "slip": slip, "pnl": pnl,
                        "exit_reason": "roll" if need_roll else ("timing" if timing_exit else "eod"),
                    })
                    pos = None

        if pos is None and i < len(dates) - 1:
            ok_timing = True
            if enter_high is not None:
                ok_timing = (pct is not None and not np.isnan(pct[i]) and pct[i] >= enter_high)
            if basis_ok is not None and not basis_ok[i]:
                ok_timing = False
            ld = last_day.get(nc[i])
            dte = _trading_days_between(d, ld)
            if ok_timing and dte > roll_buf:
                a = px.get((d, nc[i])); b = px.get((d, fc[i]))
                if a is not None and b is not None and not pd.isna(a[fi_entry]) and not pd.isna(b[fi_entry]):
                    pos = {"side": side, "nc": nc[i], "fc": fc[i],
                           "npx": a[fi_entry], "fpx": b[fi_entry], "d": d, "lots": lots}

        floating = 0.0
        if pos is not None:
            a = px.get((d, pos["nc"])); b = px.get((d, pos["fc"]))
            if a is not None and b is not None and not pd.isna(a[1]) and not pd.isna(b[1]):
                sgn_n = 1 if pos["side"] == "S_far" else -1
                floating = ((a[1] - pos["npx"]) * sgn_n
                            + (b[1] - pos["fpx"]) * (-sgn_n)) * MULT * pos["lots"]
        equity.append((d, nav + floating))

    eq = pd.DataFrame(equity, columns=["date", "nav"])
    td = pd.DataFrame(trades)
    if len(td) == 0:
        if return_pos:
            return {"trades": 0}, td, eq, pos
        return {"trades": 0}, td, eq
    years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.0
    fin = eq["nav"].iloc[-1]
    peak = np.maximum.accumulate(eq["nav"].values)
    dd = (eq["nav"].values / peak - 1).min()
    ann = (fin / init_cap) ** (1 / years) - 1 if fin > 0 else np.nan
    st = {"trades": len(td), "win_rate": (td["pnl"] > 0).mean(),
          "total_pnl": td["pnl"].sum(), "final_nav": fin, "years": years,
          "ann_ret": ann, "max_dd": dd, "avg_pts": td["total_pts"].mean(),
          "avg_hold": td["hold_days"].mean(), "fee_sum": td["fee"].sum(),
          "resid": abs(fin - (init_cap + td["pnl"].sum()))}
    if return_pos:
        return st, td, eq, pos
    return st, td, eq
