# -*- coding: utf-8 -*-
"""统一的真实交易日历与换月计算（IM/IC 跨期策略共用模块）。

所有推送 / 回测 / 实操页都从这里取，根除「周一到周五」(weekday<5) 口径分裂：
- alert_spread.py  （微信推送：换月窗口 + 数据滞后判断）
- alert_atr.py      （微信推送：数据滞后判断）
- live_signal.py    （实操页实时信号：换月倒计时）
- build_reco_strategy_page.py（实操页：回测换月 + 持仓倒计时 + JS 刷新）
- spread_hold_lib._trading_days_between（回测换月计时）

换月缓冲 ROLL_BUF 也在此统一定义，确保网页、推送、回测三处完全一致。
"""
import datetime as dt
import akshare as ak

# 距到期≤ROLL_BUF 交易日进入换月窗口。
# 与微信推送 / 实操页实时信号 / 回测共用同一常量，杜绝「网页说3、微信用10」的分裂。
ROLL_BUF = 10

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
    """两个 ISO 日期之间的交易日数量（含 end，不含 start）。
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


def trading_days_to_last(contract_code, as_of_iso):
    """合约距到期还有几个交易日（含到期日，从 as_of 次日数起）。contract_code 如 'IM2608'。"""
    from spread_hold_lib import contract_last_trade_day
    ltd = contract_last_trade_day(contract_code).date()
    return trading_days_between(as_of_iso, ltd.isoformat())


def cal_json():
    """导出日历集合（字符串列表）供实操页 JS 使用，保证前后端口径一致。"""
    cal = load_trade_calendar()
    return sorted(cal) if cal else []


if __name__ == "__main__":
    print("ROLL_BUF =", ROLL_BUF)
    print("样例 2026-10-01 是否交易日:", is_trade_day(dt.date(2026, 10, 1)))
    print("国庆区间交易日数(9/30~10/9):", trading_days_between("2026-09-30", "2026-10-09"))
