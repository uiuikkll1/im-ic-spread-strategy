# -*- coding: utf-8 -*-
"""akshare 接口兼容层 —— 防「改名」型 breaking change。

背景
----
akshare 1.20.44 一次性 rename 68 个接口，**旧名不再保留别名**，调用直接抛
AttributeError（1.20.55 起还带提示文案）：

    stock_zh_index_daily     ->  index_zh_daily_sina
    index_zh_a_hist          ->  index_zh_hist_em
    stock_zh_index_daily_em  ->  index_zh_daily_em
    stock_zh_index_daily_tx  ->  index_zh_daily_tx

而 requirements.txt 原先只写 `akshare`、没锁版本，CI 每次 pip install 都装最新版，
于是 alert_atr.py 的**主源和备用源在同一天一起失效**（两个都是旧名），
表现为「ATR 数据缺失告警」——这正是 2026-11-14 那次线上故障的根因。

策略
----
1. 按候选函数名依次探测：**新名优先、旧名兜底**，哪个存在用哪个；
2. 调用时按函数真实签名过滤参数（如 `adjust` 在新签名里已被移除）；
3. 全部不存在 / 全部失败时，**直接 HTTP 拉新浪、腾讯日线**，彻底不依赖 akshare 版本。

所有对外函数统一返回列：`date / Open / High / Low / Close`（date 为 datetime64）。
"""
import inspect
import json
import urllib.request

import pandas as pd

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
_TMO = 20


# ---------------------------------------------------------------- HTTP helper
def _http_get(url, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=_TMO).read().decode('utf-8', 'ignore')
        except Exception as e:
            last = e
    raise last


# ---------------------------------------------------------------- akshare 探测
def _try_ak(names, **kw):
    """按候选名依次调用 akshare，返回 (df, 实际用到的函数名)。

    自动剔除函数签名不接受的参数，避免「接口还在但参数被删」也被判死。
    全部候选失败时抛 RuntimeError，把每条失败原因串起来，便于日志定位。
    """
    import akshare as ak
    errs = []
    for n in names:
        f = getattr(ak, n, None)
        if not callable(f):
            errs.append('%s 不存在' % n)
            continue
        try:
            params = inspect.signature(f).parameters
            kwargs = {k: v for k, v in kw.items() if k in params}
            df = f(**kwargs)
            if df is not None and len(df):
                return df, n
            errs.append('%s 返回空' % n)
        except Exception as e:
            errs.append('%s: %s: %s' % (n, type(e).__name__, str(e)[:120]))
    raise RuntimeError('；'.join(errs))


# ---------------------------------------------------------------- 列名归一
_MAP = {
    'date': 'date', 'day': 'date', '日期': 'date',
    'open': 'Open', '开盘': 'Open',
    'high': 'High', '最高': 'High',
    'low': 'Low', '最低': 'Low',
    'close': 'Close', '收盘': 'Close',
}


def _norm(df):
    """统一成 date/Open/High/Low/Close，转类型、去重、按日升序。"""
    ren = {}
    for c in df.columns:
        k = str(c).strip()
        t = _MAP.get(k) or _MAP.get(k.lower())
        if t:
            ren[c] = t
    df = df.rename(columns=ren)

    need = ['date', 'Open', 'High', 'Low', 'Close']
    miss = [x for x in need if x not in df.columns]
    if miss:
        raise RuntimeError('返回列缺少 %s（实际列 %s）' % (miss, list(df.columns)))

    df = df[need].copy()
    df['date'] = pd.to_datetime(df['date'])
    for c in need[1:]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    n0 = len(df)
    df = df.dropna(subset=need).drop_duplicates('date').sort_values('date')
    # 显式打印丢弃量：若最新一根 bar 缺值被整行丢掉，上游会静默沿用上一交易日，
    # 直到超过 STALE_TD 才告警。留一行日志，排查时能立刻看到。
    if len(df) < n0:
        print('[ak_compat] 丢弃 %d 行缺值/重复数据（原始 %d 行 → 保留 %d 行）'
              % (n0 - len(df), n0, len(df)))
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- HTTP 直连源
def _http_sina(symbol, datalen=3000):
    """新浪日线。symbol 形如 sh000852。

    datalen 实测上限就是 2936 根（2014-10 起，与 akshare 分支行数完全一致），
    给 3000 即可拿满；若给 1500 会静默截断到 2020-09，导致下游基差/年化计算
    的起始年份整体后移（这个坑很隐蔽，别调小）。
    """
    u = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
         'CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d'
         % (symbol, datalen))
    j = json.loads(_http_get(u))
    if not isinstance(j, list):
        raise RuntimeError('新浪返回非列表: %s' % str(j)[:120])
    rows = [(x['day'], x['open'], x['high'], x['low'], x['close']) for x in j]
    return pd.DataFrame(rows, columns=['date', 'Open', 'High', 'Low', 'Close'])


def _http_tx(symbol, count=800):
    """腾讯日线。返回行格式 [date, open, close, high, low, volume]，注意列序。"""
    u = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'
         'param=%s,day,,,%d,qfq' % (symbol, count))
    j = json.loads(_http_get(u))
    d = (j.get('data') or {}).get(symbol) or {}
    arr = d.get('day') or d.get('qfqday') or []
    if not arr:
        raise RuntimeError('腾讯无 day 数据')
    rows = [(x[0], x[1], x[3], x[4], x[2]) for x in arr]
    return pd.DataFrame(rows, columns=['date', 'Open', 'High', 'Low', 'Close'])


def _http_em(symbol, beg='20000101', end='20500101'):
    """东财日线。symbol 形如 000852（6 位数字）。"""
    secid = ('1.' if symbol.startswith('0') else '0.') + symbol
    u = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s'
         '&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=101&fqt=0'
         '&beg=%s&end=%s' % (secid, beg, end))
    j = json.loads(_http_get(u))
    kl = ((j.get('data') or {}).get('klines')) or []
    if not kl:
        raise RuntimeError('东财无 klines 数据')
    rows = []
    for k in kl:
        p = k.split(',')
        # 东财列序: date, open, close, high, low
        rows.append((p[0], p[1], p[3], p[4], p[2]))
    return pd.DataFrame(rows, columns=['date', 'Open', 'High', 'Low', 'Close'])


# ---------------------------------------------------------------- 对外接口
def index_daily_sina(symbol='sh000852'):
    """新浪系指数日线。返回 (DataFrame, 来源标签)。

    级别：akshare 新名 -> akshare 旧名 -> HTTP 新浪 -> HTTP 腾讯。
    来源标签会打进日志，方便下次再出问题时一眼看出走的是哪条路。
    """
    try:
        df, name = _try_ak(['index_zh_daily_sina', 'stock_zh_index_daily'], symbol=symbol)
        return _norm(df), 'akshare:%s' % name
    except Exception as e_ak:
        err_ak = str(e_ak)

    for tag, fn in (('http:sina', _http_sina), ('http:tx', _http_tx)):
        try:
            df = fn(symbol)
            if len(df) > 20:
                return _norm(df), tag
        except Exception:
            continue
    raise RuntimeError('指数日线全部来源失败 | akshare: %s | HTTP 新浪/腾讯亦失败' % err_ak)


def index_daily_em(symbol='000852'):
    """东财系指数日线（alert_atr 的备用源）。返回 (DataFrame, 来源标签)。

    HTTP 兜底顺序：东财 -> 腾讯。东财接口在部分网络环境（代理/机房 IP）会被断连，
    而腾讯同花顺源对同一指数的日线内容等价，作为最后一道保险。
    """
    try:
        df, name = _try_ak(
            ['index_zh_hist_em', 'index_zh_a_hist'],
            symbol=symbol, period='daily',
            start_date='20000101', end_date='20500101', adjust='')
        return _norm(df), 'akshare:%s' % name
    except Exception as e_ak:
        err_ak = str(e_ak)

    for tag, fn in (('http:em', lambda s: _http_em(s)),
                    ('http:tx', lambda s: _http_tx('sh' + s))):
        try:
            df = fn(symbol)
            if len(df) > 20:
                return _norm(df), tag
        except Exception:
            continue
    raise RuntimeError('东财指数日线全部来源失败 | akshare: %s | HTTP 东财/腾讯亦失败' % err_ak)


def client_version():
    """当前 akshare 版本，供日志/告警打印（装不上则返回 '未安装'）。"""
    try:
        import akshare as ak
        return getattr(ak, '__version__', '未知')
    except Exception:
        return '未安装'
