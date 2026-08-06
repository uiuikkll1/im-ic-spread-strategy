"""
ATR 突破策略 —— 收盘后信号推送（Server酱 → 微信）
参数（来自用户报告 top1）：MA2 / ATR20 / 倍数0.6 / 实体0，无止盈止损
逻辑：T 日收盘判断 → T+1 日开盘执行；只在「空仓→开仓」「持仓→平仓」时推送一次
"""
import os
import json
import datetime as dt
import requests
import akshare as ak
import pandas as pd
import numpy as np

# ---------- 策略参数 ----------
SYMBOL = "sh000852"          # 中证1000 指数
MA_N = 2
ATR_N = 20
MULT = 0.6
BODY_THR = 0.0               # 实体阈值（占开盘价比例），0 = 不限制
STATE_FILE = "atr_state.json"

SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("holding", 0)
    except Exception:
        return 0   # 0=空仓, 1=持仓


def save_state(holding):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"holding": holding, "updated": str(dt.date.today())}, f, ensure_ascii=False, indent=2)


def compute_signal():
    df = ak.stock_zh_index_daily(symbol=SYMBOL)
    df = df.rename(columns={"date": "date", "open": "Open", "high": "High",
                            "low": "Low", "close": "Close", "volume": "Volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    close = df["Close"]
    ma = close.rolling(MA_N).mean()
    hl = df["High"] - df["Low"]
    hc = (df["High"] - close.shift()).abs()
    lc = (df["Low"] - close.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(ATR_N).mean()
    upper = ma + MULT * atr
    body = (df["Close"] - df["Open"]).abs() / df["Open"]

    i = -1
    d = df["date"].iloc[i]
    c = float(close.iloc[i])
    o = float(df["Open"].iloc[i])
    up = float(upper.iloc[i])
    m = float(ma.iloc[i])
    b = float(body.iloc[i])

    buy_trig = (c > up) and (b >= BODY_THR)
    sell_trig = (c < m)
    return d, c, o, up, m, b, buy_trig, sell_trig


def push_wechat(title, desp):
    if not SERVERCHAN_KEY:
        print("未配置 SERVERCHAN_KEY，跳过推送")
        return False
    r = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": desp},
        timeout=10,
    )
    print("Server酱返回:", r.status_code, r.text[:200])
    return r.status_code == 200


def main():
    holding = load_state()
    d, c, o, up, m, b, buy_trig, sell_trig = compute_signal()
    d_str = f"{d:%Y-%m-%d}"

    msg = None
    new_state = holding

    if holding == 0:                       # 当前空仓
        if buy_trig:
            msg = (f"【ATR 买入信号】{d_str}\n"
                   f"中证1000 收盘 {c:.2f} > 上轨 {up:.2f}（MA{MA_N}+{MULT}×ATR{ATR_N}），实体比 {b:.3f}≥{BODY_THR}\n"
                   f"→ 次日开盘买入")
            new_state = 1
    else:                                  # 当前持仓
        if sell_trig:
            msg = (f"【ATR 卖出信号】{d_str}\n"
                   f"中证1000 收盘 {c:.2f} < MA{MA_N} {m:.2f}\n"
                   f"→ 次日开盘离场")
            new_state = 0
        # 否则继续持仓，不推送（避免每天刷屏）

    save_state(new_state)

    if msg:
        push_wechat("ATR突破策略信号", msg)
    else:
        print(f"{d_str} 无信号（holding={holding}）")


if __name__ == "__main__":
    main()
