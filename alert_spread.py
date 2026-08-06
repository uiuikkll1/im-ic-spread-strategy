"""
IM/IC 跨期展期策略 —— 每日收盘后推送「明日操作」到微信（Server酱）
逻辑（与实操页一致，近月-隔季 i0_i3）：
  - 持仓中 & 当月距到期≤10交易日 → 明日平仓（换月）
  - 持仓中 & 未到换月            → 不推送（持仓中不用推）
  - 空仓 & 分位达标              → 明日开仓：多近月+空隔季
  - 空仓 & 分位不达标            → 明日不用开仓
有消息才推，没消息（两边都持仓中）就不打扰。
【防漏推措施】
  1) 幂等守卫：用「最新数据日期」标记已处理，重复跑/双 cron 不重复推
  2) 价格拉取失败自动回退备用数据源（已含）
  3) workflow 双 cron（23:00 + 00:00 北京时间）+ 幂等，单日被跳过也能补跑
"""
import os
import json
import datetime as dt
import numpy as np
import pandas as pd
import requests

import live_signal as ls
from live_signal import current_c1_c4, PARAMS
import backtest_im_spread as v1
from spread_hold_lib import contract_last_trade_day

SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = "spread_state.json"


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def get_latest_spread(prod, near, far):
    """优先 akshare 日线收盘，失败回退新浪实时。"""
    try:
        import akshare as ak
        dn = ak.futures_zh_daily_sina(symbol=near)
        df = ak.futures_zh_daily_sina(symbol=far)
        nlast = float(pd.to_numeric(dn["close"]).dropna().iloc[-1])
        flast = float(pd.to_numeric(df["close"]).dropna().iloc[-1])
        return nlast, flast
    except Exception as e:
        print(f"[{prod}] akshare 失败，回退新浪: {e}")
        prices = ls.fetch_prices()
        return prices[near]["last"], prices[far]["last"]


def analyze(prod, state):
    W, enter_high, _idx = PARAMS[prod]
    today = dt.date.today()
    near, far = current_c1_c4(prod, today)

    pf = pd.read_parquet(f"{prod.lower()}_spread_panel_all_i0_i3.parquet")
    pf["date"] = pd.to_datetime(pf["date"])
    sr = (pf["near_close"] - pf["far_close"]) / pf["near_close"]

    nlast, flast = get_latest_spread(prod, near, far)
    sr_last = (nlast - flast) / nlast
    series = np.append(sr.values, sr_last)
    pct = v1.rolling_pct(series, W)
    cur_pct = float(pct[-1])

    ltd = contract_last_trade_day(near).date()
    d = today
    cnt = 0
    while d <= ltd:
        if d.weekday() < 5:
            cnt += 1
        d += dt.timedelta(days=1)
    in_roll = cnt <= 10

    holding = int(state.get(prod, 0))
    if holding:
        if in_roll:
            msg = f"【{prod} 明日】平仓（换月）：平多 {near} + 平空 {far}（当月距到期 {cnt} 交易日）"
            new_h = 0
        else:
            # 持仓中且未到换月：不推送，保持持仓状态
            return None, 1, cur_pct, near, far, cnt
    else:
        if cur_pct >= enter_high:
            msg = f"【{prod} 明日】开仓：多 {near} + 空 {far}（分位 {cur_pct:.2f} ≥ {enter_high}，次日开盘执行）"
            new_h = 1
        else:
            msg = f"【{prod} 明日】不用开仓（分位 {cur_pct:.2f} < {enter_high}，等待更深贴水）"
            new_h = 0

    return msg, new_h, cur_pct, near, far, cnt


def push_wechat(title, desp):
    if not SERVERCHAN_KEY:
        print("未配置 SERVERCHAN_KEY，跳过推送")
        return
    r = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": desp},
        timeout=10,
    )
    print("Server酱返回:", r.status_code, r.text[:120])


def main():
    state = load_state()

    # 防漏推 1：该数据日期已处理过（重复跑/双 cron/重试）则跳过，避免重复推送
    as_of = str(pd.read_parquet("im_spread_panel_all_i0_i3.parquet")["date"].max().date())
    if state.get("processed_date") == as_of:
        print(f"{as_of} 数据已处理过（防重复推送），跳过")
        return

    parts = []
    for prod in ["IM", "IC"]:
        msg, new_h, pct, near, far, cnt = analyze(prod, state)
        state[prod] = new_h
        if msg:
            parts.append(msg)
            print(f"{prod}: {msg}")
        else:
            print(f"{prod}: 持仓中，不推送")

    state["processed_date"] = as_of
    save_state(state)

    if not parts:
        print("今日 IM/IC 均无操作，跳过推送")
        return

    desp = f"数据日期：{as_of}\n\n" + "\n\n".join(parts)
    push_wechat("IM/IC 跨期展期 明日操作", desp)


if __name__ == "__main__":
    main()
