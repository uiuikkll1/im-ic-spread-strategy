# -*- coding: utf-8 -*-
"""联网获取 IM/IC 当前远月-隔季(c2-c4)合约实时价，计算分位与换月倒计时，输出操作提示。

- 合约按当天日期动态推导（c2=下月, c4=隔季），不在代码里写死，避免合约到期后信号失效。
- 行情优先新浪，失败回退 akshare，保证 CI / 公网环境也能算出信号。
"""
import pandas as pd
import numpy as np
import datetime as dt
import backtest_im_spread as v1
from spread_hold_lib import contract_last_trade_day

# 每个品种：滚动窗口、入场分位阈值（None=纯持有）、指数代码
PAIRS = {
    'IM': (120, 0.40, 'sh000852'),
    'IC': (252, None, 'sh000905'),
}

PREFIX = {'IM': 'IM', 'IC': 'IC'}


def _contract(prefix, y, m):
    return f"{prefix}{y % 100:02d}{m:02d}"


def current_i1_i3(prefix, today=None):
    """返回 (c2=下月合约, c4=隔季合约)。"""
    today = today or dt.date.today()
    y, m = today.year, today.month
    nm, ny = m + 1, y
    if nm > 12:
        nm, ny = 1, y + 1
    c2 = _contract(prefix, ny, nm)
    cur_q = (m - 1) // 3            # 0=Q1,1=Q2,2=Q3,3=Q4
    steps = cur_q + 2               # 隔季 = 当前季 + 2 季
    yy = y + steps // 4
    q = steps % 4
    c4_month = (q + 1) * 3          # 季序号 -> 月份 (3,6,9,12)
    c4 = _contract(prefix, yy, c4_month)
    return c2, c4


def _sina_price(codes):
    """codes: list of futures codes (no prefix). 返回 {code: last_price}。"""
    import requests, re
    url = 'https://hq.sinajs.cn/list=' + ','.join('CFF_' + c for c in codes)
    r = requests.get(url, headers={'Referer': 'https://finance.sina.com.cn'}, timeout=10)
    out = {}
    for line in r.text.strip().split('\n'):
        m = re.match(r'var hq_str_CFF_(\w+)="([^"]*)";', line)
        if not m or not m.group(2):
            continue
        f = m.group(2).split(',')
        out[m.group(1)] = float(f[3])
    return out


def _ak_price(code):
    import akshare as ak
    df = ak.futures_zh_daily_sina(symbol=code)
    return float(df['close'].iloc[-1])


def _get_price(code, is_idx=False, idx_sym=None):
    """优先新浪，失败回退 akshare。"""
    try:
        if is_idx:
            sina = _sina_price([])  # 指数走另一条
        fut = _sina_price([code])
        if code in fut:
            return fut[code]
    except Exception:
        pass
    # 回退 akshare
    if is_idx:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=idx_sym)
        return float(df['close'].iloc[-1])
    return _ak_price(code)


def compute_signal():
    today = dt.date.today()
    out = {}
    for prod, (W, eh, idx) in PAIRS.items():
        prefix = PREFIX[prod]
        near, far = current_i1_i3(prefix, today)
        pf = pd.read_parquet(f'{prod.lower()}_spread_panel_all_i1_i3.parquet')
        pf['date'] = pd.to_datetime(pf['date'])
        sr = (pf['near_close'] - pf['far_close']) / pf['near_close']
        near_px = _get_price(near)
        far_px = _get_price(far)
        idx_last = _get_price(idx, is_idx=True, idx_sym=idx)
        sr_last = (near_px - far_px) / near_px
        series = np.append(sr.values, sr_last)
        pct = v1.rolling_pct(series, W)
        cur_pct = float(pct[-1])
        near_basis = near_px - idx_last
        far_basis = far_px - idx_last
        basis_ok = near_basis < 0
        ltd = contract_last_trade_day(near).date()
        d = today
        cnt = 0
        while d <= ltd:
            if d.weekday() < 5:
                cnt += 1
            d += dt.timedelta(days=1)
        in_roll_window = cnt <= 10
        if in_roll_window:
            action = f'换月平仓（下月{near}距到期≤10交易日）'
            actionable = True
        elif eh is None:
            action = '始终持有，无需操作（纯持有策略）'
            actionable = False
        elif cur_pct >= eh and basis_ok:
            action = f'满足开仓条件（分位{cur_pct:.2f}≥{eh}，且下月{near}相对指数贴水{near_basis:.1f}点），次日开盘开多{near}+开空{far}'
            actionable = True
        elif cur_pct >= eh and not basis_ok:
            action = f'分位{cur_pct:.2f}≥{eh}，但下月{near}相对指数升水{near_basis:.1f}点，属于危险结构，不开仓，等待'
            actionable = False
        else:
            action = f'等待：分位{cur_pct:.2f}<{eh}，需贴水更深（价差率更大）才开仓'
            actionable = False
        out[prod] = {
            'near': near, 'far': far, 'near_px': near_px,
            'far_px': far_px, 'spread_rel': round(sr_last, 5),
            'spread_pts': round(near_px - far_px, 1),
            'idx_px': idx_last, 'near_basis': round(near_basis, 1),
            'far_basis': round(far_basis, 1), 'basis_ok': basis_ok,
            'pct': round(cur_pct, 3), 'W': W, 'enter_high': eh,
            'ltd': ltd.strftime('%Y-%m-%d'), 'roll_days': cnt,
            'in_roll_window': in_roll_window, 'action': action,
            'actionable': actionable,
            'quote_date': today.strftime('%Y-%m-%d'), 'quote_time': '',
        }
    return out


if __name__ == '__main__':
    import json
    sig = compute_signal()
    print(json.dumps(sig, ensure_ascii=False, indent=2))
