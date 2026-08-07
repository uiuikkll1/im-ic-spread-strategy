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
  1) 幂等守卫：按品种各存「数据日」(IM_key / IC_key)，同一根K线只推一次；双 cron/周末/重试不重复推
  2) 分位信号基于连续价差面板序列（先刷新原始数据+重建面板，失败回退旧面板），
     绝不从单个月份合约历史价算长窗口分位（月份合约寿命仅 1~2 月，历史不足）
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
from live_signal import PARAMS
import backtest_im_spread as v1
from spread_hold_lib import contract_last_trade_day

SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = "spread_state.json"
ROLL_BUF = 10   # 距到期≤10交易日进入换月窗口（与用户需求/实操页一致）
STALE_TD = 5     # 面板末行距运行日超过 5 个交易日即视为数据滞后，发告警


# ---------- 真实交易日历（H2：换月计数/滞后判断需剔除法定假日）----------
_TRADE_CAL = None   # 中国股市真实交易日集合，格式 'YYYYMMDD'
_CAL_MIN = None     # 日历最小日期（字符串），用于越界判断
_CAL_MAX = None     # 日历最大日期（字符串）

def load_trade_calendar(force=False):
    """拉取中国股市真实交易日历（剔除周末+法定假日）。
    失败则留空集合，is_trade_day 自动回退『周一到周五』简单判断，保证不死机。"""
    global _TRADE_CAL, _CAL_MIN, _CAL_MAX
    if _TRADE_CAL is not None and not force:
        return _TRADE_CAL
    try:
        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        vals = df[col].astype(str).str.replace("-", "").str.strip()
        _TRADE_CAL = set(vals)
        _CAL_MIN = min(_TRADE_CAL)
        _CAL_MAX = max(_TRADE_CAL)
        print(f"真实交易日历加载完成，共 {len(_TRADE_CAL)} 天（{_CAL_MIN}~{_CAL_MAX}）")
    except Exception as e:
        print(f"真实交易日历拉取失败（回退 weekday 简单判断）: {e}")
        _TRADE_CAL = set()   # 空集合 → is_trade_day 回退 weekday
        _CAL_MIN = None
        _CAL_MAX = None
    return _TRADE_CAL


def is_trade_day(d):
    """判断某日期是否为股市交易日。
    - 日历已加载且在覆盖范围内：用真实交易日历（剔除法定假日）。
    - 日历未加载 / 日期超出日历覆盖范围（如 akshare 只到当年12-31，而合约到期在次年）：
      回退为『周一到周五』简单判断，避免越界日期被静默当成非交易日导致换月计数严重低估。"""
    cal = load_trade_calendar()
    if cal:
        key = d.strftime("%Y%m%d")
        if _CAL_MIN is not None and (key < _CAL_MIN or key > _CAL_MAX):
            return d.weekday() < 5   # 越界回退（跨年近月合约到期场景）
        return key in cal
    return d.weekday() < 5


def trading_days_between(start_iso, end_iso):
    """两个 ISO 日期之间的交易日数量（含 end，不含 start），用于数据新鲜度判断。
    使用真实交易日历（剔除法定假日），避免长假误判。"""
    s = dt.date.fromisoformat(start_iso)
    e = dt.date.fromisoformat(end_iso)
    if e <= s:
        return 0
    cnt = 0
    d = s + dt.timedelta(days=1)
    while d <= e:
        if is_trade_day(d):
            cnt += 1
        d += dt.timedelta(days=1)
    return cnt


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


def refresh_panels():
    """刷新全合约原始数据并重建 i0_i3 面板（与实操页同一套管线）。
    任一环节失败都回退到仓库里已有的面板文件（至少能用陈旧数据，不死机）。"""
    try:
        import fetch_full_data as ffd
        for prod in ["IM", "IC"]:
            parquet = f"{prod.lower()}_future_data.parquet"
            try:
                ffd.update_future_data(prod, parquet)
                print(f"[{prod}] 原始数据刷新完成")
            except Exception as e:
                print(f"[{prod}] 原始数据刷新失败（用旧数据）: {e}")
        try:
            import build_all_spread_panels as bap
            bap.build_panels("im_future_data.parquet", "im")
            bap.build_panels("ic_future_data.parquet", "ic")
            print("面板重建完成")
        except Exception as e:
            print(f"面板重建失败（用旧面板）: {e}")
    except Exception as e:
        print(f"refresh_panels 异常（用旧面板）: {e}")


def compute_pct(prod, W):
    """从 i0_i3 面板的连续 spread_rel2 序列算滚动分位。
    返回 (cur_pct, quote_date, near_code, far_code, nlast, flast)。
    必须重算 spread_rel2 = (near_close - far_close) / near_close（深贴水为正，与实操页一致），
    绝不能复用面板里反号的 spread_rel=(far-near)/near。
    月份合约历史太短（仅 1~2 月），无法单独算长窗口分位，必须从连续面板序列取。"""
    panel = pd.read_parquet(f"{prod.lower()}_spread_panel_all_i0_i3.parquet")
    panel = panel.dropna(subset=["near_close", "far_close"]).sort_values("date").reset_index(drop=True)
    if len(panel) < W + 5:
        raise RuntimeError(f"{prod} 面板数据不足 {W} 天，仅 {len(panel)} 行")
    sr = ((panel["near_close"] - panel["far_close"]) / panel["near_close"]).to_numpy(dtype=float)
    pct_all = v1.rolling_pct(sr, W)
    last = panel.iloc[-1]
    return (float(pct_all[-1]), str(last["date"])[:10],
            str(last["near_code"]), str(last["far_code"]),
            float(last["near_close"]), float(last["far_close"]))


def run_core():
    state = load_state()

    # 先刷新面板（失败回退旧面板），保证分位基于最新连续序列
    refresh_panels()

    run_day = dt.date.today()           # TZ 已在 workflow 设为 Asia/Shanghai
    gen_date = run_day.isoformat()

    # 逐品种算分位；失败的记下来单独告警，不拖累另一个品种
    pct_map = {}
    failed = []
    for prod in ["IM", "IC"]:
        W, _enter_high, _idx = PARAMS[prod]
        try:
            cur_pct, qdate, pnear, pfar, nlast, flast = compute_pct(prod, W)
            pct_map[prod] = (cur_pct, qdate, pnear, pfar, nlast, flast)
        except Exception as e:
            failed.append(prod)
            print(f"[{prod}] 分位计算失败: {e}")

    # 数据缺失告警（每日最多一次）：某个品种数据坏了，用户至少知道，不会静默丢信号
    # 注意：推送失败则不写去重键 → 下次重试（与主信号同样严格，避免告警静默丢失）
    if failed and state.get("broken_alerted_date") != gen_date:
        ok = push_wechat("⚠️ IM/IC 数据缺失告警",
                         f"{'、'.join(failed)} 分位计算失败，今日不推送该品种（其余正常），请检查数据源")
        if ok:
            state["broken_alerted_date"] = gen_date

    if not pct_map:
        print("两品种分位均计算失败，本次不推送（保留旧状态，下次重试）")
        save_state(state)
        return

    parts = []
    results = {}
    evaluated = {}          # 本批实际评估过的品种 -> 数据日（推送成功才标记键）
    stale_msgs = []
    for prod in ["IM", "IC"]:
        if prod not in pct_map:
            continue
        cur_pct, qdate, pnear, pfar, nlast, flast = pct_map[prod]
        # 直接用面板末行合约作为交易合约，保证「推送合约」与「算分位合约」同源，
        # 杜绝 current_c1_c4 推导与面板不一致导致的错单（如把隔季算成下季）。
        near, far = pnear, pfar
        W, enter_high, _idx = PARAMS[prod]

        # 换月窗口用「数据日(面板末行)」而非「运行日」，消除双 cron 跨午夜的口径漂移
        # H2：用真实交易日历数距到期的交易日，剔除国庆/春节等法定假日，避免误报换月
        qday = dt.date.fromisoformat(qdate)
        ltd = contract_last_trade_day(near).date()
        d = qday
        cnt = 0
        while d <= ltd:
            if is_trade_day(d):
                cnt += 1
            d += dt.timedelta(days=1)
        in_roll = cnt <= ROLL_BUF

        # 数据新鲜度：面板末行距运行日超过阈值交易日 → 告警（否则数据源一挂就无声死掉）
        tdiff = trading_days_between(qdate, gen_date)
        if tdiff > STALE_TD:
            stale_msgs.append(f"{prod} 面板数据滞后 {tdiff} 个交易日（末行 {qdate}），信号可能基于旧数据")

        # 按品种独立幂等：该品种此数据日已处理过 → 跳过（不重推、不重评）
        if state.get(f"{prod}_key") == qdate:
            print(f"{prod}: 数据日 {qdate} 已处理，跳过")
            continue

        evaluated[prod] = qdate
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

        results[prod] = new_h
        if msg:
            parts.append(msg)
            print(f"{prod}: {msg}")
        else:
            print(f"{prod}: 持仓中/换月窗口，不推送")

    # 数据滞后告警（每日最多一次）：推送失败则不写去重键 → 下次重试
    if stale_msgs and state.get("stale_alerted_date") != gen_date:
        ok = push_wechat("⚠️ IM/IC 数据滞后告警", "\n".join(stale_msgs))
        if ok:
            state["stale_alerted_date"] = gen_date

    if parts:
        desp = f"生成日期：{gen_date}\n\n" + "\n\n".join(parts)
        ok = push_wechat("IM/IC 跨期展期 明日操作", desp)
        if ok:
            # 只有推送成功才写新状态 + 标记各品种已处理数据日
            state.update(results)
            for p, q in evaluated.items():
                state[f"{p}_key"] = q
        else:
            # 推送失败：保留旧状态，不标记已处理，下次重试
            print(f"数据日 {gen_date} 推送失败，保留旧状态，不标记已处理，下次继续尝试")
    else:
        # 无操作日：不翻转持仓，但评估过的品种标记数据日（避免重复跑空/周末噪音）
        for p, q in evaluated.items():
            state[f"{p}_key"] = q

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
