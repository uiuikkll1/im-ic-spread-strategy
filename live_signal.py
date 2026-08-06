# -*- coding: utf-8 -*-
"""联网获取 IM/IC 当前近月-隔季(c1-c4)合约实时价，计算分位与换月倒计时，输出操作提示。"""
import pandas as pd, numpy as np, requests, re, datetime as dt
import backtest_im_spread as v1
from spread_hold_lib import contract_last_trade_day

# 当前组合：近月-隔季 = 当月(c1) + 隔季(c4)
# 动态推导当月/隔季合约，避免硬编码到期后失效
def current_c1_c4(prod, today=None):
    today = today or dt.date.today()
    y, m = today.year, today.month
    c1 = (y % 100) * 100 + m
    # 隔季 = 下月(M+1)之后的第2个季月(3/6/9/12)；从 M+2 起扫描，避免把下月自身当季月
    qs = []
    mm = m + 2; yy = y
    if mm > 12:
        mm -= 12; yy += 1
    for _ in range(36):
        if mm in (3, 6, 9, 12):
            qs.append((yy, mm))
        mm += 1
        if mm > 12:
            mm = 1; yy += 1
        if len(qs) >= 2:
            break
    q4 = qs[1]
    c4 = (q4[0] % 100) * 100 + q4[1]
    return f'{prod}{c1:04d}', f'{prod}{c4:04d}'

# 各品种：窗口、入场阈值、指数代码（与实操页回测一致）
PARAMS = {
    'IM': (252, 0.20, 'sh000852'),
    'IC': (500, 0.40, 'sh000905'),
}

def fetch_prices():
    """新浪接口获取期货+指数最新价（期货 index 3=最新价；指数 index 3=当前价）。"""
    fut_codes = set(); idx_codes = set()
    today = dt.date.today()
    for prod, (W, eh, idx) in PARAMS.items():
        near, far = current_c1_c4(prod, today)
        fut_codes.add(near); fut_codes.add(far); idx_codes.add(idx)
    # 期货
    url = 'https://hq.sinajs.cn/list=' + ','.join('CFF_' + c for c in fut_codes)
    r = requests.get(url, headers={'Referer': 'https://finance.sina.com.cn'}, timeout=10)
    prices = {}
    for line in r.text.strip().split('\n'):
        m = re.match(r'var hq_str_CFF_(\w+)="([^"]*)";', line)
        if not m or not m.group(2):
            continue
        code = m.group(1)
        f = m.group(2).split(',')
        prices[code] = {
            'last': float(f[3]), 'open': float(f[0]), 'high': float(f[1]),
            'low': float(f[2]), 'vol': int(float(f[4])),
            'date': f[-3], 'time': f[-2],
        }
    # 指数
    url2 = 'https://hq.sinajs.cn/list=' + ','.join(idx_codes)
    r2 = requests.get(url2, headers={'Referer': 'https://finance.sina.com.cn'}, timeout=10)
    for line in r2.text.strip().split('\n'):
        m = re.match(r'var hq_str_(sh\d+)="([^"]*)";', line)
        if not m or not m.group(2):
            continue
        code = m.group(1)
        f = m.group(2).split(',')
        prices[code] = {'last': float(f[3]), 'open': float(f[1]), 'date': f[-3], 'time': f[-2]}
    return prices

def compute_signal():
    prices = fetch_prices()
    today = dt.date.today()
    out = {}
    for prod, (W, eh, idx) in PARAMS.items():
        near, far = current_c1_c4(prod, today)
        pf = pd.read_parquet(f'{prod.lower()}_spread_panel_all_i0_i3.parquet')
        pf['date'] = pd.to_datetime(pf['date'])
        sr = (pf['near_close'] - pf['far_close']) / pf['near_close']
        sr_last = (prices[near]['last'] - prices[far]['last']) / prices[near]['last']
        series = np.append(sr.values, sr_last)
        pct = v1.rolling_pct(series, W)
        cur_pct = float(pct[-1])

        # basis：当月合约相对指数的贴水（负=贴水，正=升水），仅作信息展示，不与入场门槛挂钩
        idx_last = prices[idx]['last']
        near_basis = prices[near]['last'] - idx_last
        far_basis = prices[far]['last'] - idx_last
        basis_ok = near_basis < 0

        # 换月倒计时（用合约代码推导真实最后交易日）
        ltd = contract_last_trade_day(near).date()
        d = today; cnt = 0
        while d <= ltd:
            if d.weekday() < 5:
                cnt += 1
            d += dt.timedelta(days=1)
        in_roll_window = cnt <= 10

        # 操作判定（分位过滤，与实操页回测一致；基差仅作提示）
        if in_roll_window:
            action = f'换月平仓（当月{near}距到期≤10交易日）'
            actionable = True
        elif cur_pct >= eh:
            action = f'满足开仓条件（分位{cur_pct:.2f}≥{eh}，当月{near}相对指数贴水{near_basis:.1f}点），次日开盘开多{near}+开空{far}'
            actionable = True
        else:
            action = f'等待：分位{cur_pct:.2f}<{eh}，需贴水更深（价差率更大）才开仓'
            actionable = False

        out[prod] = {
            'near': near, 'far': far, 'near_px': prices[near]['last'],
            'far_px': prices[far]['last'], 'spread_rel': round(sr_last, 5),
            'spread_pts': round(prices[near]['last'] - prices[far]['last'], 1),
            'idx_px': idx_last, 'near_basis': round(near_basis, 1),
            'far_basis': round(far_basis, 1), 'basis_ok': basis_ok,
            'pct': round(cur_pct, 3), 'W': W, 'enter_high': eh,
            'ltd': ltd.strftime('%Y-%m-%d'), 'roll_days': cnt,
            'in_roll_window': in_roll_window, 'action': action,
            'actionable': actionable,
            'quote_date': prices[near]['date'], 'quote_time': prices[near]['time'],
        }
    return out

if __name__ == '__main__':
    import json
    sig = compute_signal()
    print(json.dumps(sig, ensure_ascii=False, indent=2))
