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
  1) 幂等守卫：用「数据日(最后交易日的K线日期)」标记已处理，双 cron/周末/重试看到同一根K线只推一次
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
STALE_TD = 5     # 最新行情距今日超过 5 个交易日即视为数据滞后，发告警
# ATR 侧数据滞后判断也用统一真实交易日历（与 IM/IC 推送一致）
from trade_calendar import trading_days_between, calendar_ok


class StateCorrupt(Exception):
    """状态文件损坏/校验失败：宁可暂停推送，也不在持仓状态未知时误推买卖。"""


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        st = json.loads(raw)
        h = st.get("holding", 0)
        if h not in (0, 1):
            raise ValueError(f"holding 非法: {h}")
        pk = st.get("processed_key", "")
        if not isinstance(pk, str):
            raise ValueError("processed_key 类型错误")
        return st
    except Exception as e:
        print(f"ATR 状态文件读取/校验失败: {e}")
        _alert_once(STATE_FILE + ".corrupt", "⚠️ ATR 状态文件异常",
                    f"atr_state.json 读取/校验失败（{e}），今日暂停推送，请检查文件",
                    dt.date.today().isoformat())
        raise StateCorrupt()


def save_state(st):
    # H4：原子写（先写临时文件再 os.replace），避免半截文件导致下次加载损坏
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _alert_once(marker, title, desp, today):
    """每日最多推送一次某类告警：用 marker 标记文件记录上次推送日期。"""
    try:
        if os.path.exists(marker) and open(marker, "r", encoding="utf-8").read().strip() == today:
            return
        if push_wechat(title, desp):
            with open(marker, "w", encoding="utf-8") as f:
                f.write(today)
    except Exception:
        pass


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
    try:
        st = load_state()
    except StateCorrupt:
        return   # 状态损坏已在 load_state 内告警，今日放弃推送

    # 1) 取信号（失败=数据缺失）
    try:
        d, c, o, up, m, b, buy_trig, sell_trig = compute_signal()
    except Exception as e:
        err = f"ATR 数据源缺失/异常：{e}"
        print(err)
        today = dt.date.today().isoformat()
        if st.get("data_alerted_date") != today:
            if push_wechat("⚠️ ATR 数据缺失告警", err):
                st["data_alerted_date"] = today
                save_state(st)
        return

    d_str = f"{d:%Y-%m-%d}"

    # M2：真实日历降级告警（日历拉取失败 → 滞后计数回退 weekday 近似）
    if not calendar_ok():
        today = dt.date.today().isoformat()
        if st.get("cal_alerted_date") != today:
            if push_wechat("⚠️ 交易日历降级告警",
                           "真实交易日历拉取失败，数据滞后计数已回退为周一到周五近似，请检查 akshare 行情源"):
                st["cal_alerted_date"] = today
                save_state(st)

    # 2) H3 数据滞后告警（必须在幂等守卫之前！即便今天已处理过该数据日，
    #    只要行情冻结(d 不前进)也应每日告警，否则数据源一挂就静默死掉、
    #    且因下面守卫早 return 永远走不到这里）
    tdiff = trading_days_between(d_str, dt.date.today().isoformat())
    if tdiff > STALE_TD:
        today = dt.date.today().isoformat()
        if st.get("stale_alerted_date") != today:
            if push_wechat("⚠️ ATR 数据滞后告警",
                           f"ATR 最新行情为 {d_str}，距今日已 {tdiff} 个交易日（阈值 {STALE_TD}），"
                           f"信号可能基于旧数据，请检查数据源"):
                st["stale_alerted_date"] = today
                save_state(st)

    # 3) 防漏推：该数据日(最后交易日的K线)已处理过（重复跑/双 cron/重试/周末）则跳过。
    #    用数据日而非运行日：双 cron 19:00/20:00 与周末看到的是同一根K线，第二次应跳过。
    if st.get("processed_key") == d_str:
        print(f"{d_str} 已处理过（防重复推送），跳过")
        return

    # 4) 信号推送（仅在状态切换时推一次）
    holding = int(st.get("holding", 0))
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
            # 只有推送成功才写新状态 + 标记已处理（数据日）
            st["holding"] = new_state
            st["processed_key"] = d_str
            save_state(st)
        else:
            # 推送失败：保留旧状态（holding 不变），不标记已处理，下次重试
            print(f"{d_str} 推送失败，保留旧状态，不标记已处理，下次继续尝试")
    else:
        # 无信号日：不翻转状态，仅标记该日已处理（避免重复跑空/周末噪音）
        st["processed_key"] = d_str
        save_state(st)
        print(f"{d_str} 无信号（holding={holding}）")


def main():
    try:
        run_core()
    except StateCorrupt:
        return   # 状态损坏已告警，不再重复推异常
    except Exception as e:
        err = f"ATR 信号系统异常：{e}"
        print(err)
        try:
            st = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception:
            st = {"holding": 0, "processed_key": ""}
        today = dt.date.today().isoformat()
        if st.get("alerted_date") != today:
            try:
                push_wechat("⚠️ ATR 信号系统异常", err)
            except Exception:
                pass
            st["alerted_date"] = today
            json.dump(st, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        # 不标记 processed_key → 下次运行继续重试


if __name__ == "__main__":
    main()
