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


def fetch_future_data(prefix):
    import akshare as ak
    codes = _enum_contracts(prefix)
    rows = []
    for code in codes:
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception as e:
            continue
        if df is None or len(df) == 0 or 'close' not in df.columns:
            continue
        if df['close'].replace(0, np.nan).dropna().empty:
            continue
        df = df.copy()
        df['code'] = code
        df = df.rename(columns={'volume': 'vol', 'hold': 'oi'})
        df = df[['date', 'code', 'open', 'high', 'low', 'close', 'settle', 'vol', 'oi']]
        df['date'] = pd.to_datetime(df['date'])
        df['multiplier'] = MULT[prefix]
        # settle 为 0 时用 close 填充（akshare 最新一天常给 0）
        df['settle'] = np.where(df['settle'].replace(0, np.nan).isna(), df['close'], df['settle'])
        rows.append(df)
    if not rows:
        raise RuntimeError(f'{prefix} 未拉到任何合约数据')
    out = pd.concat(rows, ignore_index=True)
    out = out.sort_values(['code', 'date']).reset_index(drop=True)
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
        fd = fetch_future_data(prod)
        fd.to_parquet(os.path.join(HERE, f'{prod.lower()}_future_data.parquet'), index=False)
        print(f'[{prod}] future_data 行数={len(fd)} 合约数={fd["code"].nunique()}')
        mc = fetch_main_cont(prod)
        mc.to_parquet(os.path.join(HERE, f'{prod.lower()}_main_cont.parquet'), index=False)
        print(f'[{prod}] main_cont 行数={len(mc)}')
        bp.build_panels(os.path.join(HERE, f'{prod.lower()}_future_data.parquet'), prod.lower())
    fetch_index('sh000852', '中证1000')
    fetch_index('sh000905', '中证500')
    print('\n所有数据已重建到', HERE)


if __name__ == '__main__':
    main()
