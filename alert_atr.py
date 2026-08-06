# -*- coding: utf-8 -*-
"""
ATR 突破策略 —— 收盘后信号推送（Server酱 → 微信）
参数（用户报告 top1）：MA2 / ATR20 / 倍数0.6 / 实体0，无止盈止损
ATR 用 Wilder 平滑（与回测 top1 口径一致）。

逻辑：T 日收盘判断 → T+1 日开盘执行。
【推送时机：只在状态切换时推一次】
  - 空仓 & 突破上轨   → 推「买入」（次日开盘）
  - 持仓 & 跌破 MA    → 推「卖出」（次日开盘离场）
  - 持仓中未触发卖出  → 不推（不打扰）
  - 空仓未突破        → 不推

【防漏推措施】
  1) 幂等守卫：用「北京时间运行日期」标记已处理，当天双 cron 不重复推
  2) 数据拉取失败自动重试 3 次，并回退备用数据源
  3) 推送失败 → 保留旧状态、不标记已处理 → 下次重试
  4) 顶层异常兜底 → 推告警、不标记已处理（下次重试）
  5) workflow 双 cron（19:00 + 20:00 北京时间）+ concurrency 防并发双推
"""
import os
import json
import time
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
        s = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        return int(s.get("holding", 0)), str(s.get("processed_date", ""))
    except Exception:
        return 0, ""   # 0=空仓, 1=持仓；processed_date=已处理的运行日期


def save_state(holding, processed_date):
    json.dump({"holding": holding, "processed_date": processed_date,
               "updated": str(dt.date.today())},
              open(STATE_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def fetch_index():
    """主源带重试；全部失败回退备用源。返回标准化列 date/Open/High/Low/Close。"""
    last_err = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_index_daily(symbol=SYMBOL)
            if df is not None and len(df) > ATR_N + 5:
                df = df.rename(columns={"date": "date", "open": "Open", "high": "High",
                                        "low": "Low", "close": "Close", "volume": "Volume"})
                print(f"[fetch] 主源成功，{len(df)} 行")
                return df
            last_err = "主源返回空或行数不足"
        except Exception as e:
            last_err = e
            print(f"[fetch] 主源 attempt {attempt+1}/3 失败: {e}")
            time.sleep(5)
    # 备用源：东财历史日线
    try:
        df = ak.index_zh_a_hist(symbol="000852", period="daily",
                                start_date="20000101", end_date="20500101",
                                adjust="")
        if df is not None and len(df) > ATR_N + 5:
            df = df.rename(columns={"日期": "date", "开盘": "Open", "最高": "High",
                                    "最低": "Low", "收盘": "Close", "成交量": "Volume"})
            print("[fetch] 使用备用源 index_zh_a_hist")
            return df
    except Exception as e:
        print(f"[fetch] 备用源也失败: {e}")
    raise RuntimeError(f"指数数据拉取失败: {last_err}")


def wilder_atr(high, low, close, n):
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr = tr.to_numpy(dtype=float)
    atr = np.full(len(tr), np.nan)
    if len(tr) > n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return pd.Series(atr, index=close.index)


def compute_signal():
    df = fetch_index()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    close = df["Close"]
    ma = close.rolling(MA_N).mean()
    atr = wilder_atr(df["High"], df["Low"], close, ATR_N)
    upper = ma + MULT * atr
    body = (close - df["Open"]).abs() / df["Open"]

    i = -1
    d = df["date"].iloc[i]
    c = float(close.iloc[i])
    o = float(df["Open"].iloc[i])
    up = float(upper.iloc[i])
    m = float(ma.iloc[i])
    b = float(body.iloc[i])

    # M5：数据新鲜度与有效性校验，避免 NaN/半截 bar 导致误判
    if not (c > 0 and not pd.isna(up) and not pd.isna(m) and not pd.isna(b)):
        raise RuntimeError(f"最新行情无效：date={d}, close={c}, up={up}, ma={m}, body={b}")

    buy_trig = (c > up) and (b >= BODY_THR)
    sell_trig = (c < m)
    return d, c, o, up, m, b, buy_trig, sell_trig


def push_wechat(title, desp):
    """调用 Server酱，并校验返回 JSON 中的 code；code=0 才算真正递交到微信。"""
    if not SERVERCHAN_KEY:
        print("未配置 SERVERCHAN_KEY，跳过推送")
        return False
    try:
        r = requests.post(
            f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
            data={"title": title, "desp": desp},
            timeout=10,
        )
        print("Server酱返回:", r.status_code, r.text[:300])
        if r.status_code != 200:
            print(f"推送失败：HTTP {r.status_code}")
            return False
        try:
            data = r.json()
        except Exception as e:
            print(f"推送失败：返回不是合法 JSON，{e}")
            return False
        code = data.get("code")
        if code == 0:
            print("推送成功：平台已递交微信")
            return True
        print(f"推送失败：Server酱 code={code}，message={data.get('message', '')}")
        return False
    except Exception as e:
        print("推送异常:", e)
        return False


def run_core():
    run_day = dt.date.today()              # TZ 已在 workflow 设为 Asia/Shanghai
    run_str = run_day.isoformat()

    holding, processed = load_state()
    # 防漏推 1：该运行日期已处理过（重复跑/双 cron/重试）则跳过
    if processed == run_str:
        print(f"{run_str} 已处理过（防重复推送），跳过")
        return

    d, c, o, up, m, b, buy_trig, sell_trig = compute_signal()
    d_str = f"{d:%Y-%m-%d}"

    msg = None
    new_state = holding

    if holding == 0:                       # 当前空仓
        if buy_trig:
            msg = (f"【ATR 买入信号】{d_str}\n\n"
                   f"中证1000 收盘 {c:.2f} > 上轨 {up:.2f}（MA{MA_N}+{MULT}×ATR{ATR_N}），实体比 {b:.3f}≥{BODY_THR}\n\n"
                   f"→ 次日开盘买入")
            new_state = 1
        # 否则：空仓未突破，不推
    else:                                  # 当前持仓
        if sell_trig:
            msg = (f"【ATR 卖出信号】{d_str}\n\n"
                   f"中证1000 收盘 {c:.2f} < MA{MA_N} {m:.2f}\n\n"
                   f"→ 次日开盘离场")
            new_state = 0
        # 否则：持仓中未触发卖出，不推（不打扰）

    if msg:
        ok = push_wechat("ATR突破策略信号", msg)
        if ok:
            # C2：只有推送成功才写新状态 + 标记已处理
            save_state(new_state, run_str)
        else:
            # 推送失败：保留旧状态（holding 不变），不标记已处理，下次重试
            save_state(holding, processed)
            print(f"{run_str} 推送失败，保留旧状态，不标记已处理，下次继续尝试")
    else:
        # 无信号日：不翻转状态，仅标记该日已处理（避免重复跑空）
        save_state(holding, run_str)
        print(f"{run_str} 无信号（holding={holding}）")


def main():
    try:
        run_core()
    except Exception as e:
        err = f"ATR 信号系统异常：{e}"
        print(err)
        st = load_state.__wrapped__ if False else None
        try:
            st = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception:
            st = {}
        today = dt.date.today().isoformat()
        if st.get("alerted_date") != today:
            try:
                push_wechat("⚠️ ATR 信号系统异常", err)
            except Exception:
                pass
            st["alerted_date"] = today
            json.dump(st, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        # 不标记 processed_date → 下次运行继续重试


if __name__ == "__main__":
    main()
