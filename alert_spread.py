# -*- coding: utf-8 -*-
"""
IM/IC 跨期展期策略 —— 每日收盘后推送「明日操作」到微信（Server酱）
逻辑（近月-隔季 i0_i3，与实操页一致）：
  - 持仓中 & 当月距到期≤ROLL_BUF(10)交易日 → 明日平仓（换月）
  - 持仓中 & 未到换月            → 不推（持仓中不用推）
  - 空仓 & 分位达标 & 未到换月窗口 → 明日开仓：多近月+空隔季
  - 空仓 & 分位不达标            → 明日不用开仓（明确告知）
  - 空仓 & 处于换月窗口         → 不推（等换月后开新合约，避免开快到期/已摘牌合约）
有消息才推。

【防漏推措施】
  1) 幂等守卫：用「北京时间运行日期」标记已处理，当天双 cron 不重复推
  2) 行情实时拉取（akshare 日线），分位基于最新数据；失败回退新浪实时
  3) 推送失败 → 保留旧状态、不标记已处理 → 下次重试
  4) 顶层异常兜底 → 推告警、不标记已处理（下次重试）
  5) workflow 双 cron + concurrency 防并发双推
"""
import os
import json
import datetime as dt
import numpy as np
import pandas as pd
import requests

import akshare as ak
from live_signal import current_c1_c4, PARAMS
import backtest_im_spread as v1
from spread_hold_lib import contract_last_trade_day

SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = "spread_state.json"
ROLL_BUF = 10   # 距到期≤10交易日进入换月窗口（与用户需求/实操页一致）


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


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


def compute_pct(prod, near, far, W):
    """实时拉 near/far 日线，算相对价差滚动分位。返回 (cur_pct, quote_date, nlast, flast)。"""
    try:
        dn = ak.futures_zh_daily_sina(symbol=near)
        df = ak.futures_zh_daily_sina(symbol=far)
        if dn is None or df is None or len(dn) < 5 or len(df) < 5:
            raise RuntimeError("akshare 返回空")
    except Exception as e:
        print(f"[{prod}] akshare 失败，回退新浪: {e}")
        prices = __import__("live_signal").fetch_prices()
        nlast = prices[near]["last"]
        flast = prices[far]["last"]
        # 回退源无法算历史分位，直接用实时价差率近似（不推荐，但至少能推）
        cur_pct = (nlast - flast) / nlast
        return float(cur_pct), "新浪实时", float(nlast), float(flast)

    dn = dn[["date", "close"]].rename(columns={"close": "nclose"})
    df = df[["date", "close"]].rename(columns={"close": "fclose"})
    m = pd.merge(dn, df, on="date").dropna()
    m["date"] = pd.to_datetime(m["date"])
    m = m.sort_values("date").reset_index(drop=True)
    if len(m) < W + 5:
        raise RuntimeError(f"{prod} 历史数据不足 {W} 天，仅 {len(m)} 行（near={near}, far={far}）")
    sr = ((m["nclose"] - m["fclose"]) / m["nclose"]).to_numpy(dtype=float)
    pct_all = v1.rolling_pct(sr, W)
    return float(pct_all[-1]), str(m["date"].iloc[-1].date()), float(m["nclose"].iloc[-1]), float(m["fclose"].iloc[-1])


def analyze(prod, state, run_day):
    W, enter_high, _idx = PARAMS[prod]
    today = run_day                         # 用运行日期判断换月窗口
    near, far = current_c1_c4(prod, today)  # 已过第三个周五会自动滚到下月近月

    cur_pct, qdate, nlast, flast = compute_pct(prod, near, far, W)

    ltd = contract_last_trade_day(near).date()
    d = today
    cnt = 0
    while d <= ltd:
        if d.weekday() < 5:
            cnt += 1
        d += dt.timedelta(days=1)
    in_roll = cnt <= ROLL_BUF

    holding = int(state.get(prod, 0))
    new_h = holding
    msg = None

    if holding:
        if in_roll:
            msg = (f"【{prod} 明日】平仓（换月）：平多 {near} + 平空 {far}\n\n"
                   f"当月距到期 {cnt} 交易日（最后交易日 {ltd}）")
            new_h = 0
        # else 持仓中且未到换月：不推
    else:
        if not in_roll and cur_pct >= enter_high:
            msg = (f"【{prod} 明日】开仓：多 {near} + 空 {far}\n\n"
                   f"分位 {cur_pct:.2f} ≥ {enter_high}，当日价差率 {(nlast - flast) / nlast:.3f}（报价日 {qdate}），次日开盘执行")
            new_h = 1
        elif not in_roll:
            msg = (f"【{prod} 明日】不用开仓\n\n"
                   f"分位 {cur_pct:.2f} < {enter_high}（报价日 {qdate}），等待更深贴水")
            new_h = 0
        # else 空仓且处于换月窗口：不推（避免开快到期/已摘牌合约）

    return msg, new_h, cur_pct, qdate, near, far, cnt


def run_core():
    run_day = dt.date.today()               # TZ 已在 workflow 设为 Asia/Shanghai
    run_str = run_day.isoformat()

    state = load_state()
    if state.get("processed_date") == run_str:
        print(f"{run_str} 已处理（防重复推送），跳过")
        return

    parts = []
    results = {}
    for prod in ["IM", "IC"]:
        msg, new_h, pct, qdate, near, far, cnt = analyze(prod, state, run_day)
        results[prod] = new_h
        if msg:
            parts.append(msg)
            print(f"{prod}: {msg}")
        else:
            print(f"{prod}: 持仓中/换月窗口，不推送")

    if parts:
        desp = f"生成日期：{run_str}\n\n" + "\n\n".join(parts)
        ok = push_wechat("IM/IC 跨期展期 明日操作", desp)
        if ok:
            # C2：只有推送成功才写新状态 + 标记已处理
            state.update(results)
            state["processed_date"] = run_str
        else:
            # 推送失败：保留旧状态，不标记已处理，下次重试
            print(f"{run_str} 推送失败，保留旧状态，不标记已处理，下次继续尝试")
    else:
        # 无操作日：不翻转状态，仅标记已处理（避免重复跑空）
        print("今日 IM/IC 均无操作，标记已处理避免重复跑空")
        state["processed_date"] = run_str

    save_state(state)


def main():
    try:
        run_core()
    except Exception as e:
        err = f"IM/IC 信号系统异常：{e}"
        print(err)
        try:
            st = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception:
            st = {}
        today = dt.date.today().isoformat()
        if st.get("alerted_date") != today:
            try:
                push_wechat("⚠️ IM/IC 信号系统异常", err)
            except Exception:
                pass
            st["alerted_date"] = today
            json.dump(st, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        # 不标记 processed_date → 下次运行继续重试


if __name__ == "__main__":
    main()
