# -*- coding: utf-8 -*-
"""修复验证（v2）：ak_compat 三路径 + trade_calendar 三级兜底 + 覆盖守卫。

v1 的问题（独立审查指出）：第 5 步把「内置表兜底时 calendar_ok()==True」当成期望，
但那恰恰是 bug —— 内置表只覆盖 2026/2027，拿它跑十年回测会静默污染 pkl。
v2 改为断言 calendar_covers() 必须拦住。

本脚本用 stub 模块模拟不同 akshare 版本，**不依赖本机装的是哪一版**。
"""
import datetime as dt
import sys
import types

import pandas as pd

import ak_compat
import trade_calendar as tc

FAIL = []


def check(name, cond, extra=''):
    print('  [%s] %s %s' % ('OK ' if cond else 'FAIL', name, extra))
    if not cond:
        FAIL.append(name)


# ================================================================ 造数据 / stub
_real = ak_compat._http_sina('sh000852', datalen=60)      # 真实但很短的一份，仅用于桩
_DF = pd.DataFrame({
    'date': pd.date_range('2026-01-01', periods=40, freq='D').strftime('%Y-%m-%d'),
    'open': range(100, 140), 'high': range(101, 141),
    'low': range(99, 139), 'close': range(100, 140),
})


def _stub(names, fail=False):
    """构造一个只含指定函数名的假 akshare 模块。"""
    m = types.ModuleType('akshare')
    m.__version__ = 'stub'
    for n in names:
        if fail:
            def boom(*a, **k):
                raise ConnectionError('模拟网络故障')
            setattr(m, n, boom)
        else:
            setattr(m, n, lambda *a, **k: _DF.copy())
    return m


def _with_stub(mod, fn):
    """临时把 sys.modules['akshare'] 换成 stub，跑完恢复。

    注意：因 trade_calendar / ak_compat 都改成懒加载，首次调用时 sys.modules 里
    可能**还没有** akshare，此时 saved is None，必须 pop 而不是留着 stub ——
    否则 stub 会永久驻留，污染后续用例（v2 初版就在这里踩过，导致真实 akshare 用不到）。
    """
    saved = sys.modules.get('akshare')
    sys.modules['akshare'] = mod
    try:
        return fn()
    finally:
        if saved is not None:
            sys.modules['akshare'] = saved
        else:
            sys.modules.pop('akshare', None)


print('=' * 74)
print('1) 旧版 akshare（只有旧名）')
print('=' * 74)
df = _with_stub(_stub(['stock_zh_index_daily']),
                lambda: ak_compat.index_daily_sina('sh000852')[0])
src = _with_stub(_stub(['stock_zh_index_daily']),
                 lambda: ak_compat.index_daily_sina('sh000852')[1])
check('旧名可取数', len(df) == 40, '来源=%s 行数=%d' % (src, len(df)))
check('列名归一', list(df.columns) == ['date', 'Open', 'High', 'Low', 'Close'], str(list(df.columns)))

print()
print('=' * 74)
print('2) 新版 akshare（只有新名，旧名不存在）')
print('=' * 74)
src2 = _with_stub(_stub(['index_zh_daily_sina']),
                  lambda: ak_compat.index_daily_sina('sh000852')[1])
check('新名可取数', src2 == 'akshare:index_zh_daily_sina', '来源=%s' % src2)

src2b = _with_stub(_stub(['index_zh_hist_em']),
                   lambda: ak_compat.index_daily_em('000852')[1])
check('东财新名可取数', src2b == 'akshare:index_zh_hist_em', '来源=%s' % src2b)

print()
print('=' * 74)
print('3) akshare 全废 -> HTTP 直连')
print('=' * 74)
src3 = _with_stub(_stub([], fail=True),
                  lambda: ak_compat.index_daily_sina('sh000852')[1])
check('HTTP 兜底生效', str(src3).startswith('http:'), '来源=%s' % src3)
if str(src3).startswith('http:'):
    d3 = _with_stub(_stub([], fail=True), lambda: ak_compat.index_daily_sina('sh000852')[0])
    check('HTTP 兜底行数够用', len(d3) > 2900, '行数=%d 首行=%s' % (len(d3), d3['date'].iloc[0].date()))
    check('HTTP 兜底列序自洽',
          bool((d3['High'] >= d3[['Open', 'Close']].max(axis=1)).all()
               and (d3['Low'] <= d3[['Open', 'Close']].min(axis=1)).all()),
          'High>=max(O,C) 且 Low<=min(O,C)')

print()
print('=' * 74)
print('4) 交易日历：新版接口名 calendar_trade_date_hist_sina')
print('=' * 74)
_cal_df = pd.DataFrame({'trade_date': pd.date_range('2026-01-01', '2026-12-31', freq='D')})
new_cal_stub = types.ModuleType('akshare')
new_cal_stub.__version__ = 'stub'
new_cal_stub.calendar_trade_date_hist_sina = lambda: _cal_df.copy()

tc.load_trade_calendar(force=True)                       # 先清成正常来源
saved_cal = tc._TRADE_CAL
tc._TRADE_CAL = None
_with_stub(new_cal_stub, lambda: tc.load_trade_calendar(force=True))
check('新版日历接口名可用', 'akshare:calendar_trade_date_hist_sina' in (tc.calendar_source() or ''),
      '来源=%s' % tc.calendar_source())
tc._TRADE_CAL = None
tc.load_trade_calendar(force=True)

print()
print('=' * 74)
print('5) 真实环境日历 + 2027 覆盖')
print('=' * 74)
cal = tc.load_trade_calendar(force=True)
print('  来源: %s | 天数 %d | %s ~ %s' % (tc.calendar_source(), len(cal), tc._CAL_MIN, tc._CAL_MAX))
check('覆盖到 2027 年底', tc._CAL_MAX >= '20271231', tc._CAL_MAX)
check('2026 天数正常', sum(1 for x in cal if x.startswith('2026')) >= 240)
check('2027 天数正常', sum(1 for x in cal if x.startswith('2027')) >= 240)
check('calendar_ok', tc.calendar_ok() is True)
check('覆盖 2015-2026 长区间', tc.calendar_covers('2015-04-01', '2026-11-13') is True)
check('覆盖跨年区间', tc.calendar_covers('2026-12-01', '2027-03-19') is True)

cases = [
    ('2026-10-01', False, '国庆'), ('2026-11-16', True, '普通周一'),
    ('2027-01-01', False, '元旦'), ('2027-02-05', False, '春节首日'),
    ('2027-02-08', False, '春节期间'), ('2027-02-15', True, '春节后首日'),
    ('2027-04-05', False, '清明'), ('2027-05-04', False, '劳动节'),
    ('2027-06-09', False, '端午'), ('2027-09-15', False, '中秋'),
    ('2027-10-05', False, '国庆'), ('2027-10-08', True, '国庆后首日'),
    ('2027-01-31', False, '调休上班周日(股市仍休市)'),
]
bad = [s for s, w, _ in cases if tc.is_trade_day(dt.date.fromisoformat(s)) != w]
check('2026/2027 节假日判定', not bad, '不符: %s' % bad if bad else '14 项全对')

print()
print('=' * 74)
print('6) 关键回归：akshare+XSHG 全废，只剩内置表')
print('=' * 74)


def _fail():
    raise RuntimeError('模拟接口改名故障')


_oa, _ox = tc._from_akshare, tc._from_xshg
tc._from_akshare = _fail
tc._from_xshg = _fail
tc.load_trade_calendar(force=True)
print('  来源: %s | 天数 %d | %s ~ %s'
      % (tc.calendar_source(), len(tc._TRADE_CAL), tc._CAL_MIN, tc._CAL_MAX))
check('内置表独立兜底成功', 'builtin' in (tc.calendar_source() or ''))
check('calendar_ok 为 True（不发无谓告警）', tc.calendar_ok() is True)
# ★ 这是 v1 漏掉的关键断言：内置表只有近两年，不能用于长区间回测
check('长区间未被覆盖 -> 守卫必须拦住', tc.calendar_covers('2015-04-01', '2026-11-13') is False)
check('近期区间被覆盖 -> 允许运行', tc.calendar_covers('2026-01-05', '2027-03-19') is True)
tc._from_akshare, tc._from_xshg = _oa, _ox
tc.load_trade_calendar(force=True)
print('  已恢复，来源:', tc.calendar_source())

print()
print('=' * 74)
print('结果: %s' % ('全部通过' if not FAIL else '失败 %d 项: %s' % (len(FAIL), FAIL)))
print('=' * 74)
sys.exit(1 if FAIL else 0)
