# -*- coding: utf-8 -*-
"""从 akshare 全量拉取 IM/IC 期货日线、主连、指数，并构造跨期价差面板。

用于 GitHub Actions / 本地无 parquet 环境下重建所有数据，不依赖任何本地 zip。
输出目录：本脚本所在目录（与 build_reco_strategy_page.py 同目录）。
"""
import os
import datetime as dt
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MULT = {'IM': 200.0, 'IC': 300.0}
START_YM = {'IM': (2022, 7), 'IC': (2015, 4)}   # 上市月
END_YM = (2027, 12)                              # 多拉到未来，覆盖当前挂牌合约


def _code(prefix, y, m):
    return f"{prefix}{y % 100:02d}{m:02d}"


def _enum_contracts(prefix):
    sy, sm = START_YM[prefix]
    ey, em = END_YM
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(_code(prefix, y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _active_contracts(prefix, today):
    """按中金所规则返回当月、次月、随后两个季月合约。"""
    y, m = today.year, today.month
    codes = [_code(prefix, y, m)]
    nm, ny = m + 1, y
    if nm > 12:
        nm, ny = 1, y + 1
    codes.append(_code(prefix, ny, nm))
    q = (m - 1) // 3
    for k in range(1, 3):
        q2 = (q + k) % 4
        yy = y + (q + k) // 4
        codes.append(_code(prefix, yy, (q2 + 1) * 3))
    return sorted(set(codes))


def _clean_future_df(df, code, prefix):
    """统一清洗 akshare 返回的单个合约日线。"""
    if df is None or len(df) == 0 or 'close' not in df.columns:
        return None
    if df['close'].replace(0, np.nan).dropna().empty:
        return None
    df = df.copy()
    df['code'] = code
    df = df.rename(columns={'volume': 'vol', 'hold': 'oi'})
    df = df[['date', 'code', 'open', 'high', 'low', 'close', 'settle', 'vol', 'oi']]
    df['date'] = pd.to_datetime(df['date'])
    df['multiplier'] = MULT[prefix]
    df['settle'] = np.where(df['settle'].replace(0, np.nan).isna(), df['close'], df['settle'])
    return df


def fetch_future_data(prefix):
    import akshare as ak
    codes = _enum_contracts(prefix)
    rows = []
    for code in codes:
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception as e:
            continue
        df = _clean_future_df(df, code, prefix)
        if df is not None:
            rows.append(df)
    if not rows:
        raise RuntimeError(f'{prefix} 未拉到任何合约数据')
    out = pd.concat(rows, ignore_index=True)
    out = out.sort_values(['code', 'date']).reset_index(drop=True)
    return out


def update_future_data(prefix, parquet_path, lookback_days=90):
    """已有 parquet 时增量更新：补最近 lookback_days 的数据，不破坏历史。"""
    import akshare as ak
    old = pd.read_parquet(parquet_path)
    old['date'] = pd.to_datetime(old['date'])
    last_date = old['date'].max()
    start = last_date - pd.Timedelta(days=lookback_days)
    today = dt.date.today()

    # 需要更新的合约：最近有交易的 + 当前挂牌的
    recent_codes = set(old[old['date'] >= start]['code'].unique())
    codes = sorted(recent_codes | set(_active_contracts(prefix, today)))

    rows = []
    for code in codes:
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception as e:
            continue
        df = _clean_future_df(df, code, prefix)
        if df is None:
            continue
        df = df[df['date'] >= start]
        if len(df):
            rows.append(df)

    if rows:
        new = pd.concat(rows, ignore_index=True)
        out = pd.concat([old, new], ignore_index=True)
        out = out.drop_duplicates(subset=['date', 'code'], keep='last')
    else:
        out = old
    out = out.sort_values(['code', 'date']).reset_index(drop=True)
    out.to_parquet(parquet_path, index=False)
    return out


def fetch_main_cont(prefix):
    import akshare as ak
    df = ak.futures_main_sina(symbol=prefix + '0')
    df = df.rename(columns={'日期': 'date', '收盘价': 'close', '成交量': 'vol'})
    df['date'] = pd.to_datetime(df['date'])
    df['code'] = prefix + '0'
    df = df[['date', 'code', 'close', 'vol']].sort_values('date').reset_index(drop=True)
    return df


def fetch_index(sym, name):
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=sym)
    df = df[['date', 'close']].copy()
    df['date'] = pd.to_datetime(df['date']).astype('datetime64[ns]')
    df = df.sort_values('date').reset_index(drop=True)
    df.to_parquet(os.path.join(HERE, f'{name}_idx.parquet'), index=False)
    return df


def main():
    import build_all_spread_panels as bp
    for prod in ['IM', 'IC']:
        parquet = os.path.join(HERE, f'{prod.lower()}_future_data.parquet')
        if os.path.exists(parquet):
            fd = update_future_data(prod, parquet)
            mode = '增量更新'
        else:
            fd = fetch_future_data(prod)
            fd.to_parquet(parquet, index=False)
            mode = '全量重建'
        print(f'[{prod}] {mode} 行数={len(fd)} 合约数={fd["code"].nunique()}')
        mc = fetch_main_cont(prod)
        mc.to_parquet(os.path.join(HERE, f'{prod.lower()}_main_cont.parquet'), index=False)
        print(f'[{prod}] main_cont 行数={len(mc)}')
        bp.build_panels(parquet, prod.lower())
    fetch_index('sh000852', '中证1000')
    fetch_index('sh000905', '中证500')
    print('\n所有数据已重建到', HERE)


if __name__ == '__main__':
    main()
