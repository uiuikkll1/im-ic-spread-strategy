# -*- coding: utf-8 -*-
"""统一的真实交易日历与换月计算（IM/IC 跨期策略共用模块）。

所有推送 / 回测 / 实操页都从这里取，根除「周一到周五」(weekday<5) 口径分裂：
- alert_spread.py  （微信推送：换月窗口 + 数据滞后判断）
- alert_atr.py      （微信推送：数据滞后判断）
- live_signal.py    （实操页实时信号：换月倒计时）
- build_reco_strategy_page.py（实操页：回测换月 + 持仓倒计时 + JS 刷新）
- spread_hold_lib._trading_days_between（回测换月计时）

换月缓冲 ROLL_BUF 也在此统一定义，确保网页、推送、回测三处完全一致。

数据来源（三级兜底，2026-11 加固）
--------------------------------
1. akshare `tool_trade_date_hist_sina`（覆盖 1990 ~ 当年 12-31）
2. `exchange_calendars` 的 XSHG 日历（离线，无需联网；已验证与新浪在 2025/2026
   全年逐日**完全一致**，0 差异）
3. 内置官方节假日表（补新数据源覆盖不到的年份，当前 = 2027）

只有三级全部失效、回退到「周一到周五」近似时，`calendar_ok()` 才返回 False 并告警。
这样即使新浪接口再改名/被封，也不会天天刷「降级告警」，更不会让换月倒计时失真。
"""
import datetime as dt

# 注意：这里**不在模块顶层 import akshare**。
# 否则 akshare 装坏/装不上时，整个模块 import 就崩，alert_atr 一行代码都跑不到，
# 连「系统异常」告警都发不出去，下面精心设计的 XSHG / 内置表兜底全成死代码。
# 改为在 _from_akshare() 内部懒加载。

# 距到期≤ROLL_BUF 交易日进入换月窗口。
# 与微信推送 / 实操页实时信号 / 回测共用同一常量，杜绝「网页说3、微信用10」的分裂。
ROLL_BUF = 10

_TRADE_CAL = None   # 交易日集合，格式 'YYYYMMDD'
_CAL_MIN = None     # 日历最小日期（字符串），用于越界判断
_CAL_MAX = None     # 日历最大日期（字符串）
_SRC = None         # 实际生效来源，供日志与告警使用

# ---------------------------------------------------------------- 内置节假日表
# 只列「放假」日期；周末本身另有判断 —— A 股周末一律休市，调休补班日也不开市，
# 已用 exchange_calendars XSHG 对 2025-2026 全量验证：落在周末的交易日 0 天。
_BUILTIN_HOLIDAYS = {
    # 2026 年：由 exchange_calendars XSHG 反推并固化为常量
    # （与 akshare 新浪日历 2026 全年 242 个交易日逐日一致，0 差异）。
    # 固化的意义：让内置表不依赖运行时库，可在 CI 接口全废/断网时独立工作。
    2026: {
        '20260101', '20260102',                                      # 元旦
        '20260216', '20260217', '20260218', '20260219', '20260220',  # 春节
        '20260223',
        '20260406',                                                  # 清明
        '20260501', '20260504', '20260505',                          # 劳动节
        '20260619',                                                  # 端午
        '20260925',                                                  # 中秋
        '20261001', '20261002', '20261005', '20261006', '20261007',  # 国庆
    },
    # 2027 年：国务院办公厅 2026-11-05 发布（新华社），多源核对一致。
    2027: {
        # 元旦 1/1(五)~1/3(日)
        '20270101', '20270102', '20270103',
        # 春节 2/5(除夕,五)~2/13(初八,六)，共 9 天
        '20270205', '20270206', '20270207', '20270208', '20270209',
        '20270210', '20270211', '20270212', '20270213',
        # 清明 4/3(六)~4/5(一)
        '20270403', '20270404', '20270405',
        # 劳动节 5/1(六)~5/5(三)
        '20270501', '20270502', '20270503', '20270504', '20270505',
        # 端午 6/9(三)
        '20270609',
        # 中秋 9/15(三)
        '20270915',
        # 国庆 10/1(五)~10/7(四)
        '20271001', '20271002', '20271003', '20271004',
        '20271005', '20271006', '20271007',
    },
}


def _builtin_year(year):
    """按内置表生成某年交易日集合（工作日 − 法定节假日）。无该年数据返回空集。"""
    hol = _BUILTIN_HOLIDAYS.get(year)
    if hol is None:
        return set()
    out = set()
    d = dt.date(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5 and d.strftime('%Y%m%d') not in hol:
            out.add(d.strftime('%Y%m%d'))
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 来源加载
def _from_akshare():
    """akshare 交易日历，**候选名探测**。

    ⚠️ 这个接口也被改名过，而且是同一批：1.20.44 还叫 tool_trade_date_hist_sina，
    1.20.50 起改名为 calendar_trade_date_hist_sina（旧名不保留）。
    只写新名会在 1.20.44 挂，只写旧名会在现行版挂 —— 必须两个都试。
    """
    import akshare as ak

    errs = []
    for n in ('calendar_trade_date_hist_sina', 'tool_trade_date_hist_sina'):
        f = getattr(ak, n, None)
        if not callable(f):
            errs.append('%s 不存在' % n)
            continue
        try:
            df = f()
        except Exception as e:
            errs.append('%s: %s: %s' % (n, type(e).__name__, str(e)[:100]))
            continue
        col = 'trade_date' if 'trade_date' in df.columns else df.columns[0]
        vals = df[col].astype(str).str.replace('-', '').str.strip()
        vals = set(v for v in vals if len(v) == 8 and v.isdigit())
        if vals:
            return vals, 'akshare:%s' % n
        errs.append('%s 返回空日历' % n)
    raise RuntimeError('; '.join(errs))


def _from_xshg():
    import exchange_calendars as xcals
    cal = xcals.get_calendar('XSHG')
    vals = set(cal.sessions.strftime('%Y%m%d'))
    if not vals:
        raise RuntimeError('XSHG 返回空日历')
    return vals, 'xshg'


def load_trade_calendar(force=False):
    """加载交易日历（三级兜底）。全部失败才留空集合，is_trade_day 回退 weekday。"""
    global _TRADE_CAL, _CAL_MIN, _CAL_MAX, _SRC
    if _TRADE_CAL is not None and not force:
        return _TRADE_CAL

    cal = set()
    srcs = []
    errs = []

    # 1) akshare（主源）。只有网络/抖动类异常才重试；
    #    接口改名(AttributeError)、依赖缺失(ImportError)、签名变更(TypeError) 重试无意义，
    #    否则旧代码会在 CI 上白等 3 次 + 6 秒 sleep，最后还是失败。
    for attempt in range(3):
        try:
            vals, tag = _from_akshare()
            cal |= vals
            srcs.append(tag)
            break
        except (AttributeError, ImportError, TypeError) as e:
            errs.append('akshare: %s' % e)
            break
        except Exception as e:
            errs.append('akshare: %s' % e)
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))

    # 2) XSHG（离线兜底；与新浪在 2025/2026 全年逐日一致）
    if not cal:
        try:
            vals, tag = _from_xshg()
            cal |= vals
            srcs.append(tag)
        except Exception as e:
            errs.append('xshg: %s' % e)

    # 3) 内置表：
    #    - 有来源时，补「该来源覆盖不到的年份」；
    #    - 1/2 全废时，内置表独立兜底，避免直接掉到 weekday。
    #
    #    年份取并集而不是只取 last_year+1：若数据源停在更早年份（如 2025-12-31），
    #    只推到 2026 会让 2027 无声缺失；而「今天所在年 + 次年」才是实盘真正要用的区间。
    ty = dt.date.today().year
    if cal:
        last_year = int(max(cal)[:4])
        years = sorted({last_year, last_year + 1, ty, ty + 1})
    else:
        years = sorted({ty, ty + 1})

    for y in years:
        built = _builtin_year(y)
        if not built:
            continue
        existing = sum(1 for x in cal if x[:4] == str(y))
        if existing < 200:      # 该年覆盖不全（正常年份约 240 天）
            # 用「替换」而不是「并集」：若该年只有部分数据，并集会把数据源判定为
            # 休市的日子（如临时休市）重新算成交易日。内置表是整年完整快照，直接覆盖更干净。
            cal = set(x for x in cal if x[:4] != str(y)) | built
            srcs.append('builtin%d' % y)

    if cal:
        _TRADE_CAL = cal
        _CAL_MIN = min(cal)
        _CAL_MAX = max(cal)
        _SRC = '+'.join(srcs)
        print('交易日历加载完成，共 %d 天（%s ~ %s），来源 %s'
              % (len(cal), _CAL_MIN, _CAL_MAX, _SRC))
        return _TRADE_CAL

    # 4) 全部失败 -> weekday 近似
    print('交易日历全部来源失败（回退 weekday 近似）: %s' % ' | '.join(errs))
    _TRADE_CAL = set()
    _CAL_MIN = None
    _CAL_MAX = None
    _SRC = 'weekday'
    return _TRADE_CAL


def calendar_ok():
    """日历是否**有可信来源**（用于「降级告警」）。

    只要 akshare / XSHG / 内置表任一成功就不算降级 —— 注意它**不代表覆盖范围足够**：
    内置表只覆盖最近两年，拿它跑十年回测仍会让越界日期静默回退 weekday。
    凡是「落盘/回测」类场景，必须另用 calendar_covers() 校验区间。
    """
    load_trade_calendar()
    return _SRC not in (None, '', 'weekday')


def calendar_covers(start_iso, end_iso):
    """给定日期区间是否被真实日历**完整覆盖**（不依赖越界 weekday 回退）。

    回测/落盘前的正确守卫。calendar_ok() 只回答「有没有来源」，
    而这里回答「这段区间是否真的有真实日历」，两者不可混用：
    内置表兜底时 calendar_ok() 为 True，但 2015-2025 全在覆盖范围外。
    """
    load_trade_calendar()
    if not _TRADE_CAL or _CAL_MIN is None or _CAL_MAX is None:
        return False
    s = str(start_iso).replace('-', '')[:8]
    e = str(end_iso).replace('-', '')[:8]
    return _CAL_MIN <= s and e <= _CAL_MAX


def calendar_source():
    """返回当前日历来源标签（如 'akshare+builtin2027' / 'xshg' / 'weekday'）。"""
    load_trade_calendar()
    return _SRC or '未加载'


def cal_meta():
    """返回日历覆盖范围（字符串 'YYYYMMDD'），供实操页 JS 判断越界回退。
    未加载时返回 (None, None)。"""
    load_trade_calendar()
    return _CAL_MIN, _CAL_MAX


def is_trade_day(d):
    """判断某日期是否为股市交易日。
    - 日历已加载且在覆盖范围内：用真实交易日历（剔除法定假日）。
    - 日历未加载 / 日期超出覆盖范围（如合约到期在更远的未来）：
      回退为『周一到周五』简单判断，避免越界日期被静默当成非交易日导致换月计数严重低估。"""
    cal = load_trade_calendar()
    if cal:
        key = d.strftime('%Y%m%d')
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


if __name__ == '__main__':
    print('ROLL_BUF =', ROLL_BUF)
    print('来源 =', calendar_source())
    print('2026-10-01 是否交易日:', is_trade_day(dt.date(2026, 10, 1)), '(国庆应为 False)')
    print('2027-02-08 是否交易日:', is_trade_day(dt.date(2027, 2, 8)), '(春节应为 False)')
    print('2027-02-22 是否交易日:', is_trade_day(dt.date(2027, 2, 22)))
    print('2027-04-05 是否交易日:', is_trade_day(dt.date(2027, 4, 5)), '(清明应为 False)')
    print('国庆区间交易日数(2026-09-30~10-09):', trading_days_between('2026-09-30', '2026-10-09'))
