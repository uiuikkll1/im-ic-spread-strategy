# -*- coding: utf-8 -*-
"""生成 IM/IC 各自最推荐跨期展期策略的实操页：
规则 + 实时操作提示卡片 + 走势图(净值/主连/价差率/分位/回撤) + 成交流水明细。"""
import pandas as pd, numpy as np, json, datetime as dt, os
import backtest_im_spread as v1
from spread_hold_lib import build_px, run_hold, MULT, contract_last_trade_day, _trading_days_between
import live_signal as ls
# 统一真实交易日历 + 换月缓冲（与微信推送一致，根除「网页3/微信10」的口径分裂）
from trade_calendar import ROLL_BUF, is_trade_day, cal_json

INIT = 500000.0

def extend_data_to_today(raw, pf, mc, prod):
    """联网用 akshare 把期货日线补到最近交易日，避免图表停留在 parquet 最后日期。"""
    try:
        import akshare as ak
    except Exception as e:
        print(f'[{prod}] akshare 不可用，跳过联网补数据:', e)
        return raw, pf, mc
    # 以 raw/pf/mc 三者最大日期为基准，避免任一面板比 raw 更新时重复追加导致日期不唯一
    last_raw = max(raw['date'].max(), pf['date'].max(), mc['date'].max())
    today = pd.Timestamp(dt.date.today())
    if last_raw >= today:
        return raw, pf, mc
    near_c = pf['near_code'].iloc[-1]
    far_c = pf['far_code'].iloc[-1]
    extra_raw = []
    for code in [near_c, far_c]:
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception as e:
            print(f'[{prod}] 获取 {code} 日线失败:', e)
            continue
        df['date'] = pd.to_datetime(df['date'])
        df = df[(df['date'] > last_raw) & (df['date'] <= today)].copy()
        if len(df) == 0:
            continue
        # settle 为 0 时用 close 填充（akshare 最新一天常给 0）
        df['settle'] = np.where(df['settle'].replace(0, np.nan).isna(), df['close'], df['settle'])
        for _, r in df.iterrows():
            extra_raw.append({
                'date': r['date'], 'code': code,
                'open': float(r['open']), 'high': float(r['high']), 'low': float(r['low']),
                'close': float(r['close']), 'settle': float(r['settle']), 'volume': float(r['volume'])
            })
    if not extra_raw:
        return raw, pf, mc
    extra_raw_df = pd.DataFrame(extra_raw)
    raw = pd.concat([raw, extra_raw_df], ignore_index=True)
    # 构造 panel / main_cont 补充行
    dates = sorted(extra_raw_df['date'].unique())
    extra_panel = []; extra_mc = []
    for d in dates:
        n = extra_raw_df[(extra_raw_df['date']==d) & (extra_raw_df['code']==near_c)]
        f = extra_raw_df[(extra_raw_df['date']==d) & (extra_raw_df['code']==far_c)]
        if len(n)==0 or len(f)==0:
            continue
        nr, fr = n.iloc[0], f.iloc[0]
        spread_abs = float(nr['close'] - fr['close'])
        spread_rel = spread_abs / nr['close'] if nr['close'] != 0 else np.nan
        near_ltd = contract_last_trade_day(near_c)
        far_ltd = contract_last_trade_day(far_c)
        extra_panel.append({
            'date': d, 'near_code': near_c, 'far_code': far_c,
            'near_open': float(nr['open']), 'near_close': float(nr['close']), 'near_settle': float(nr['settle']),
            'far_open': float(fr['open']), 'far_close': float(fr['close']), 'far_settle': float(fr['settle']),
            'spread_abs': spread_abs, 'spread_rel': spread_rel,
            'near_exp': max(0, _trading_days_between(d, near_ltd)),
            'far_exp': max(0, _trading_days_between(d, far_ltd)),
        })
        # 主连用近月合约代理（最后日期主连已是近月合约）
        extra_mc.append({
            'date': d, 'code': near_c, 'close': float(nr['close']), 'vol': float(nr['volume'])
        })
    if extra_panel:
        pf = pd.concat([pf, pd.DataFrame(extra_panel)], ignore_index=True)
        pf = pf.sort_values('date').reset_index(drop=True)
    if extra_mc:
        mc = pd.concat([mc, pd.DataFrame(extra_mc)], ignore_index=True)
        mc = mc.sort_values('date').reset_index(drop=True)
    print(f'[{prod}] 已联网补充 {len(extra_panel)} 个交易日数据至 {extra_panel[-1]["date"].strftime("%Y-%m-%d") if extra_panel else last_raw.strftime("%Y-%m-%d")}')
    return raw, pf, mc

# ---------------- 跑 IM / IC 推荐组合 ----------------
def run_pair(prod, key, W, enter_high, require_basis=False, rb=ROLL_BUF):
    raw = pd.read_parquet(f'{prod.lower()}_future_data.parquet'); raw['date']=pd.to_datetime(raw['date'])
    pf = pd.read_parquet(f'{prod.lower()}_spread_panel_all_{key}.parquet'); pf['date']=pd.to_datetime(pf['date'])
    mc = pd.read_parquet(f'{prod.lower()}_main_cont.parquet'); mc['date']=pd.to_datetime(mc['date'])
    # 联网补数据：把期货日线拉到最近交易日，避免图表滞后
    raw, pf, mc = extend_data_to_today(raw, pf, mc, prod)
    px = build_px(raw)
    # 用合约代码推导真实到期日，而不是 raw 数据的最后日期（raw 可能提前截断）
    all_codes = set(raw['code'].unique()) | set(pf['near_code']) | set(pf['far_code'])
    last_day = {c: contract_last_trade_day(c) for c in all_codes if len(c) == 6 and c[:2].isalpha() and c[2:].isdigit()}
    pf['spread_rel2'] = (pf['near_close']-pf['far_close'])/pf['near_close']
    pct = v1.rolling_pct(pf['spread_rel2'].values, W) if enter_high is not None else None
    basis_ok = None
    if require_basis:
        idx_name = '中证1000' if prod=='IM' else '中证500'
        idx = pd.read_parquet(f'{idx_name}_idx.parquet'); idx['date']=pd.to_datetime(idx['date'])
        pf2 = pd.merge_asof(pf.sort_values('date'), idx[['date','close']].rename(columns={'close':'idx_close'}).sort_values('date'),
                            on='date', direction='backward').sort_values('date')
        basis_ok = (pf2['near_close'].values - pf2['idx_close'].values) < 0
    st, td, eq, pos = run_hold(pf, px, last_day, side='S_far', roll_buf=rb, pct=pct,
                               enter_high=enter_high, exit_pct=-1.0, init_cap=INIT,
                               exec_price='open', entry_price='open', exit_price='close',
                               basis_ok=basis_ok, return_pos=True)
    # 主连对齐
    mc2 = mc[['date','close']].rename(columns={'close':'mc'})
    e2 = pd.merge_asof(eq.sort_values('date'), mc2.sort_values('date'), on='date', direction='backward').sort_values('date')
    e2['dd'] = e2['nav']/e2['nav'].cummax() - 1
    r1 = e2['nav'].pct_change().fillna(0); r2 = e2['mc'].pct_change().fillna(0)
    corr = float(np.corrcoef(r1, r2)[0,1])
    # 与 e2['date'] 对齐的价差率 / 分位序列
    pf_idx = pd.Index(pf['date'])
    idx_pos = pf_idx.get_indexer(e2['date'])
    sr_align = pf['spread_rel2'].values[idx_pos]
    pct_align = pct[idx_pos] if pct is not None else np.full(len(e2), np.nan)
    return st, td, eq, e2, corr, raw, pf, sr_align, pct_align, pos

def clean(v):
    if v is None: return None
    if isinstance(v, (np.floating,)): v=float(v)
    if isinstance(v, (np.integer,)): v=int(v)
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)): return None
    return v

def trades_to_list(td, pos=None, floating_pnl=None):
    rows=[]
    reason_cn={'roll':'换月','timing':'分位','eod':'期末'}
    for i,row in enumerate(td.itertuples(index=False),1):
        rows.append({
            'idx':i,'dir':row.dir_cn,
            'near_c':row.near_code,'far_c':row.far_code,
            'entry_d':pd.Timestamp(row.entry_date).strftime('%Y-%m-%d'),
            'exit_d':pd.Timestamp(row.exit_date).strftime('%Y-%m-%d'),
            'n_ep':round(float(row.near_entry_px),1),'n_xp':round(float(row.near_exit_px),1),
            'f_ep':round(float(row.far_entry_px),1),'f_xp':round(float(row.far_exit_px),1),
            'hold':int(row.hold_days),
            'n_pts':round(float(row.near_pts),1),'f_pts':round(float(row.far_pts),1),
            'tot_pts':round(float(row.total_pts),1),
            'gross':round(float(row.gross),0),'fee':round(float(row.fee),0),
            'pnl':round(float(row.pnl),0),
            'reason':reason_cn.get(row.exit_reason,row.exit_reason),
        })
    if pos is not None:
        rows.append({
            'idx':len(rows)+1,'dir':pos['dir_cn'] if 'dir_cn' in pos else ('多下月+空隔季' if pos.get('side')=='S_far' else '空下月+多隔季'),
            'near_c':pos['nc'],'far_c':pos['fc'],
            'entry_d':pd.Timestamp(pos['d']).strftime('%Y-%m-%d'),
            'exit_d':'持仓中',
            'n_ep':round(float(pos['npx']),1),'n_xp':None,
            'f_ep':round(float(pos['fpx']),1),'f_xp':None,
            'hold':(pd.Timestamp.now().normalize()-pd.Timestamp(pos['d'])).days,
            'n_pts':None,'f_pts':None,'tot_pts':None,
            'gross':None,'fee':None,
            'pnl':round(float(floating_pnl),0) if floating_pnl is not None else None,
            'reason':'持仓中',
        })
    return rows

def build_block(prod, st, td, e2, corr, label, rule_text, W, eh, sr_align, pct_align, pos=None):
    dates=[d.strftime('%Y-%m-%d') for d in e2['date']]
    nav=[round(float(x)/10000,2) for x in e2['nav']]
    mc=[round(float(x),1) for x in e2['mc']]
    dd=[round(float(x)*100,2) for x in e2['dd']]
    spread=[round(float(x)*100,3) if not np.isnan(x) else None for x in sr_align]
    pctv=[round(float(x),3) if not np.isnan(x) else None for x in pct_align]
    # 当前浮动盈亏（策略期末持仓）
    floating_pnl = None
    if pos is not None:
        closed_pnl = float(td['pnl'].sum()) if len(td) else 0.0
        floating_pnl = float(e2['nav'].iloc[-1]) - (INIT + closed_pnl)
    # 持仓区间：用于图表背景阴影
    hold_periods = []
    for _, r in td.iterrows():
        hold_periods.append([pd.Timestamp(r.entry_date).strftime('%Y-%m-%d'),
                             pd.Timestamp(r.exit_date).strftime('%Y-%m-%d')])
    if pos is not None:
        hold_periods.append([pd.Timestamp(pos['d']).strftime('%Y-%m-%d'), dates[-1]])
    # 信号触发日(T日收盘) = 每笔开仓成交日(T+1开盘)的前一个交易日；
    # 成交日 = 开仓日 / 平仓日。三者用于在走势图打标记。
    date_index = {d: k for k, d in enumerate(e2['date'])}
    signal_days = set()
    for _, r in td.iterrows():
        ed = pd.Timestamp(r.entry_date)
        idx = date_index.get(ed)
        if idx is not None and idx > 0:
            signal_days.add(e2['date'][idx-1])
    if pos is not None:
        ed = pd.Timestamp(pos['d'])
        idx = date_index.get(ed)
        if idx is not None and idx > 0:
            signal_days.add(e2['date'][idx-1])
    entry_days = set(pd.Timestamp(r.entry_date) for r in td.itertuples(index=False))
    exit_days = set(pd.Timestamp(r.exit_date) for r in td.itertuples(index=False))
    if pos is not None:
        entry_days.add(pd.Timestamp(pos['d']))
    signal_pct, entry_nav, exit_nav = [], [], []
    for k, d in enumerate(e2['date']):
        signal_pct.append(round(float(pct_align[k]),3) if (d in signal_days and not np.isnan(pct_align[k])) else None)
        entry_nav.append(round(float(e2['nav'].iloc[k])/10000,2) if d in entry_days else None)
        exit_nav.append(round(float(e2['nav'].iloc[k])/10000,2) if d in exit_days else None)
    return {
        'label':label,'prod':prod,
        'rule':rule_text,
        'metrics':{
            'trades':int(st['trades']) + (1 if pos is not None else 0),
            'win':round(float(st['win_rate'])*100,1),
            'ann':round(float(st['ann_ret'])*100,2),
            'mdd':round(float(st['max_dd'])*100,2),
            'calmar':round(float(st['ann_ret']/abs(st['max_dd'])),2) if st['max_dd']<0 else 0,
            'tot_pnl':round(float(st['final_nav']) - INIT,0),
            'avg_hold':round(float(st['avg_hold']),1),
            'fee':round(float(st['fee_sum']),0),
            'corr':round(corr,3),
            'final':round(float(st['final_nav'])/10000,1),
        },
        'dates':dates,'nav':nav,'mc':mc,'dd':dd,'spread':spread,'pct':pctv,
        'hold_periods': hold_periods,
        'signal_pct':signal_pct,'entry_nav':entry_nav,'exit_nav':exit_nav,
        'trades':trades_to_list(td, pos, floating_pnl),
        'pos': {
            'is_holding': pos is not None,
            'near_c': pos['nc'] if pos else None,
            'far_c': pos['fc'] if pos else None,
            'entry_d': pd.Timestamp(pos['d']).strftime('%Y-%m-%d') if pos else None,
            'n_ep': round(float(pos['npx']),1) if pos else None,
            'f_ep': round(float(pos['fpx']),1) if pos else None,
            'floating_pnl': round(float(floating_pnl),0) if floating_pnl is not None else None,
        },
    }

# IM: 近月-隔季 i0_i3 = 多当月(c1)+空隔季(c4), W252, 深贴水分位>=0.20
im_st,im_td,im_eq,im_e2,im_corr,_,_,im_sr,im_pct,im_pos = run_pair('IM','i0_i3',252,0.20,require_basis=False,rb=ROLL_BUF)
im_rule = """<b>品种</b>：中证1000股指期货（IM）｜<b>组合</b>：<b>近月-隔季 = 多当月(c1) + 空隔季(c4)</b>，1:1 锁合约，各1手<br>
（说明：c1=当月合约，c4=隔季合约——即再下一个季月；本组合是网格全参数中最优的，年化/卡玛显著优于旧的远月-隔季 i1_i3。）<br>
<b>入场</b>：spread_rel=(当月收盘−隔季收盘)/当月收盘，取滚动252日分位；当<b>分位≥0.20（深贴水）</b>时，于<b>次日开盘</b>开多当月(c1)+开空隔季(c4)。<br>
<b>出场/换月</b>：仅当<b>当月腿距到期≤10交易日换月</b>；<b>持仓期间不因贴水变浅主动平仓</b>（吃满展期收益）。<br>
<b>本金50万</b>，每对保证金约24万，盯市用收盘价。结构上当月合约常年贴水指数，无需额外基差过滤。"""
im = build_block('IM',im_st,im_td,im_e2,im_corr,'IM 近月-隔季（深贴水分位≥0.20）',im_rule,252,0.20,im_sr,im_pct,im_pos)

# IC: 近月-隔季 i0_i3 = 多当月(c1)+空隔季(c4), W500, 深贴水分位>=0.40
ic_st,ic_td,ic_eq,ic_e2,ic_corr,_,_,ic_sr,ic_pct,ic_pos = run_pair('IC','i0_i3',500,0.40,require_basis=False,rb=ROLL_BUF)
ic_rule = """<b>品种</b>：中证500股指期货（IC）｜<b>组合</b>：<b>近月-隔季 = 多当月(c1) + 空隔季(c4)</b>，1:1 锁合约，各1手<br>
（说明：c1=当月合约，c4=隔季合约；IC 整体弱于 IM，本组合为 IC 中风险收益最优区。）<br>
<b>入场</b>：spread_rel=(当月收盘−隔季收盘)/当月收盘，取滚动500日分位；当<b>分位≥0.40（深贴水）</b>时，于<b>次日开盘</b>开多当月(c1)+开空隔季(c4)。<br>
<b>出场/换月</b>：仅当<b>当月腿距到期≤10交易日换月</b>；<b>持仓期间不主动平仓</b>。<br>
<b>本金50万</b>，每对保证金约24万，盯市用收盘价。"""
ic = build_block('IC',ic_st,ic_td,ic_e2,ic_corr,'IC 近月-隔季（深贴水分位≥0.40）',ic_rule,500,0.40,ic_sr,ic_pct,ic_pos)

# ---------------- 实时操作提示（联网） ----------------
try:
    sig = ls.compute_signal()
    sig_ok = True
except Exception as e:
    sig = {}
    sig_ok = False
    sig_err = repr(e)[:200]

# 用回测期末真实持仓状态覆盖 action：持仓中就显示持仓中，空仓中才显示等待/开仓
if sig_ok:
    for prod, block in [('IM', im), ('IC', ic)]:
        x = sig.get(prod)
        if x is None:
            continue
        pos = block['pos']
        x['is_holding'] = pos['is_holding']
        x['holding_status'] = '持仓中' if pos['is_holding'] else '空仓中'
        x['pos'] = pos
        if pos['is_holding']:
            # 持仓中时，换月倒计时按当前持仓合约计算，而不是行情目标合约
            pos_ltd = contract_last_trade_day(pos['near_c']).date()
            d = dt.date.today(); pos_roll_days = 0
            while d <= pos_ltd:
                if is_trade_day(d): pos_roll_days += 1
                d += dt.timedelta(days=1)
            pos_in_roll = pos_roll_days <= ROLL_BUF
            x['roll_days'] = pos_roll_days
            x['in_roll_window'] = pos_in_roll
            x['ltd'] = pos_ltd.strftime('%Y-%m-%d')
            if pos_in_roll:
                x['action'] = f'【持仓中 · 进入换月窗口】当前持有 <b>{pos["near_c"]}/{pos["far_c"]}</b>（开仓日 {pos["entry_d"]}）；持仓合约{pos["near_c"]}距到期≤10个交易日，次日开盘平掉原两合约，换入 <b>{x["near"]}/{x["far"]}</b>。'
                x['actionable'] = True
            else:
                x['action'] = f'【持仓中】当前持有 <b>{pos["near_c"]}/{pos["far_c"]}</b>，开仓日 {pos["entry_d"]}，浮动盈亏 {pos["floating_pnl"]:,.0f} 元；持仓合约{pos["near_c"]}距到期还余 <b>{pos_roll_days}</b> 个交易日（>10日），继续持有。行情参考合约 {x["near"]}/{x["far"]} 是下次换月目标。'
                x['actionable'] = False
        else:
            if x['in_roll_window']:
                x['action'] = '【空仓中 · 换月窗口】当前空仓；换月窗口内暂不追开，等待窗口过后再按信号开仓。'
                x['actionable'] = False
            elif x['enter_high'] is None:
                x['action'] = f'【空仓中 · 应立即开仓】策略当前空仓，但 IC 为纯持有策略；次日开盘开多 <b>{x["near"]}</b> + 开空 <b>{x["far"]}</b> 并始终持有。'
                x['actionable'] = True
            elif x['pct'] >= x['enter_high'] and x.get('basis_ok'):
                x['action'] = f'【空仓中 · 满足开仓】次日开盘开多 <b>{x["near"]}</b> + 开空 <b>{x["far"]}</b>。'
                x['actionable'] = True
            elif x['pct'] >= x['enter_high'] and not x.get('basis_ok'):
                x['action'] = f'【空仓中 · 结构危险】分位{x["pct"]:.2f}已达标，但下月{x["near"]}相对指数升水{x["near_basis"]:.1f}点，属于危险结构，不开仓。'
                x['actionable'] = False
            else:
                x['action'] = f'【空仓中 · 等待】当前分位 {x["pct"]:.3f} < 阈值 {x["enter_high"]}，继续空仓等待深贴水结构。'
                x['actionable'] = False

DATA = {'im':im,'ic':ic,'signal':sig,'signal_ok':sig_ok,
         'trade_cal': cal_json(), 'roll_buf': ROLL_BUF}
if not sig_ok:
    DATA['signal_err'] = sig_err
DATA_JSON = json.dumps(DATA, ensure_ascii=False)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>股指跨期展期策略 · IM/IC 推荐实操</title>
<script>@@ECHARTS@@</script>
<style>
* { box-sizing: border-box; }
body { margin:0; background:#0f1117; color:#e6e8ee; font-family:-apple-system,"Microsoft YaHei",sans-serif; }
.wrap { max-width:1180px; margin:0 auto; padding:24px 18px 60px; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:#9aa3b2; font-size:13px; margin-bottom:18px; }
.card { background:#181b26; border:1px solid #2a2f3d; border-radius:12px; padding:18px 20px; margin-bottom:18px; }
.card h2 { font-size:17px; margin:0 0 10px; color:#ffd479; }
.metrics { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }
.metric { background:#222633; border-radius:8px; padding:8px 12px; min-width:96px; }
.metric .k { font-size:11px; color:#9aa3b2; }
.metric .v { font-size:17px; font-weight:700; margin-top:2px; }
.v.pos { color:#ff6b6b; } .v.neg { color:#41d18b; }
.rule { background:#20242f; border-radius:8px; padding:12px 14px; font-size:13px; line-height:1.7; color:#cfd5e0; }
.note { background:#20242f; border-left:3px solid #ffd479; border-radius:6px; padding:10px 14px; font-size:12.5px; color:#b9c0cc; margin-bottom:16px; line-height:1.7; }
.chart { width:100%; height:560px; margin-top:6px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
.th, td { padding:6px 7px; border-bottom:1px solid #262b38; white-space:nowrap; text-align:right; }
.th { background:#222633; color:#9aa3b2; position:sticky; top:0; }
td.l, .th.l { text-align:left; }
.tbl-wrap { max-height:430px; overflow:auto; border:1px solid #262b38; border-radius:8px; }
.pnl-pos { color:#ff6b6b; } .pnl-neg { color:#41d18b; }
.tag { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; }
.tag.roll { background:#2c3a52; color:#8fb4ff; } .tag.eod { background:#3a2c52; color:#c79bff; } .tag.timing { background:#52422c; color:#ffcf8f; } .tag.hold { background:#1d4e6f; color:#5aa9ff; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
@media(max-width:820px){ .grid2{grid-template-columns:1fr;} }
.signal { background:#151a24; border:1px solid #2a2f3d; border-radius:12px; padding:16px 18px; margin-bottom:18px; }
.signal h2 { color:#ffd479; font-size:17px; margin:0 0 12px; }
.sig-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.sig-box { background:#20242f; border-radius:10px; padding:14px 16px; border-left:4px solid #444; }
.sig-box.go { border-left-color:#41d18b; }
.sig-box.wait { border-left-color:#ffb347; }
.sig-box.hold { border-left-color:#5aa9ff; }
.sig-box.roll { border-left-color:#ff6b6b; }
.sig-prod { font-size:15px; font-weight:700; margin-bottom:6px; }
.sig-act { font-size:13.5px; line-height:1.6; color:#e6e8ee; margin-bottom:8px; }
.sig-detail { font-size:12px; color:#9aa3b2; line-height:1.7; }
.sig-detail b { color:#cfd5e0; }
.qtime { font-size:11px; color:#6b7280; margin-top:4px; }
</style></head>
<body><div class="wrap">
<h1>股指期货跨期展期策略 · IM / IC 最推荐实操页</h1>
<div class="sub">本质：1:1 多当月(c1)+空隔季(c4)吃贴水收敛（展期收益），指数 delta 中性 ｜ 本金50万、1手、open进close出、仅换月平仓 ｜ 回测区间 IM 2022-07 起 / IC 2015-04 起</div>

<div class="note">两个品种均选 <b>近月-隔季（i0_i3 = c1-c4）</b> 组合：多当月(c1) + 空隔季(c4)。这是无未来函数全参数网格扫描下的最优组合（旧版锁的远月-隔季 i1_i3 年化仅约一半）。IM 在深贴水时分位过滤入场，IC 同样分位过滤。<br>
<b>分位是什么</b>：图中"价差率"=(当月收盘−隔季收盘)/当月收盘，反映贴水深度；"分位"是该价差率的滚动百分位（越高=贴水越深）。IM 分位≥0.20、IC 分位≥0.40 时入场开仓。<br>
<b>标记说明</b>：<span style="color:#ff8c42;">◆橙色菱形</span>=<b>信号触发日</b>（T日收盘达到阈值）；<span style="color:#41d18b;">▲绿三角</span>=<b>开仓成交日</b>（T+1开盘）；<span style="color:#ff5050;">▼红三角</span>=<b>平仓成交日</b>（换月日收盘）。信号日与成交日相差一个交易日，正是"无未来函数"的保证。<br>
<b>操作提示</b>：下方卡片基于生成时的联网行情，给出明天/下次交易日的明确操作（开仓合约、价差阈值、换月倒计时）。<br>
<b>公网页面数据更新</b>：本页为静态快照，历史走势/回测结果需每日重新生成后部署；实时信号卡片在公网可能受行情源跨域限制无法自动刷新。需要 7×24 小时自动更新请配置服务端定时任务（如 GitHub Actions）。</div>

<div class="signal" id="signal_card"></div>

<div class="grid2">
  <div class="card"><h2 id="im_h"></h2><div id="im_m"></div><div class="rule" id="im_r"></div></div>
  <div class="card"><h2 id="ic_h"></h2><div id="ic_m"></div><div class="rule" id="ic_r"></div></div>
</div>

<div class="card"><h2>IM 策略走势（净值 / 现货标的 / 价差率 / 分位 / 回撤）</h2><div id="im_chart" class="chart"></div></div>
<div class="card"><h2>IC 策略走势（净值 / 现货标的 / 价差率 / 分位 / 回撤）</h2><div id="ic_chart" class="chart"></div></div>

<div class="card"><h2>IM 成交流水明细</h2><div class="tbl-wrap"><table id="im_tbl"></table></div></div>
<div class="card"><h2>IC 成交流水明细</h2><div class="tbl-wrap"><table id="ic_tbl"></table></div></div>
</div>

<script>
var DATA = @@DATA@@;
function renderCard(p, pre){
  var m=p.metrics;
  document.getElementById(pre+'_h').textContent = p.label;
  document.getElementById(pre+'_r').innerHTML = p.rule;
  var html='';
  function cell(k,v,cls){return '<div class="metric"><div class="k">'+k+'</div><div class="v '+(cls||'')+'">'+v+'</div></div>';}
  html += cell('笔数', m.trades);
  html += cell('胜率', m.win+'%');
  html += cell('年化', (m.ann>=0?'+':'')+m.ann+'%', m.ann>=0?'pos':'neg');
  html += cell('最大回撤', m.mdd+'%', 'neg');
  html += cell('卡玛', m.calmar);
  html += cell('净盈亏', (m.tot_pnl>=0?'+':'')+m.tot_pnl, m.tot_pnl>=0?'pos':'neg');
  html += cell('期末净值', m.final+'万');
  html += cell('平均持仓', m.avg_hold+'天');
  html += cell('累计手续费', m.fee);
  html += cell('与标的相关', m.corr);
  document.getElementById(pre+'_m').innerHTML = '<div class="metrics">'+html+'</div>';
}
function renderChart(domId, d, name, enterHigh, holdPeriods){
  var el=document.getElementById(domId); if(!el) return;
  var chart=echarts.init(el, null, {renderer:'canvas'});
  var thr = (enterHigh!=null) ? enterHigh : null;
  var holdData = (holdPeriods||[]).map(function(p){return [{name:'持仓区间',xAxis:p[0],label:{show:false,position:'top'}},{xAxis:p[1]}];});
    var series=[
    {name:name+'账户净值(万)',type:'line',xAxisIndex:0,yAxisIndex:0,data:d.nav,showSymbol:false,lineStyle:{width:1.6,color:'#ffd479'},
      markArea:{silent:true,itemStyle:{color:'rgba(65,209,139,0.12)'},data:holdData}},
    {name:name+'主连',type:'line',xAxisIndex:0,yAxisIndex:1,data:d.mc,showSymbol:false,lineStyle:{width:1,color:'#5aa9ff',type:'dashed'}},
    {name:'价差率%',type:'line',xAxisIndex:1,yAxisIndex:2,data:d.spread,showSymbol:false,lineStyle:{width:1.2,color:'#f0a030'}},
    {name:'分位',type:'line',xAxisIndex:2,yAxisIndex:3,data:d.pct,showSymbol:false,lineStyle:{width:1.2,color:'#c79bff'},
      markLine: thr!=null ? {silent:true,symbol:'none',data:[{yAxis:thr,lineStyle:{color:'#41d18b',type:'dashed'},label:{formatter:'入场阈值'+thr,color:'#41d18b',fontSize:10}}]} : undefined},
    {name:'回撤',type:'line',xAxisIndex:3,yAxisIndex:4,data:d.dd,showSymbol:false,lineStyle:{color:'#ff5050',width:1},areaStyle:{color:'rgba(255,80,80,0.22)'}},
    {name:'触发信号日',type:'scatter',xAxisIndex:2,yAxisIndex:3,data:d.signal_pct,symbol:'diamond',symbolSize:10,itemStyle:{color:'#ff8c42'},z:6,
      tooltip:{trigger:'item',formatter:function(p){return '触发信号日<br>分位 '+p.value;}}},
    {name:'成交·开仓',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:d.entry_nav,symbol:'triangle',symbolSize:13,itemStyle:{color:'#41d18b'},z:6,
      tooltip:{trigger:'item',formatter:function(p){return '开仓成交日(T+1开盘)';}}},
    {name:'成交·平仓',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:d.exit_nav,symbol:'triangle',symbolSize:13,symbolRotate:180,itemStyle:{color:'#ff5050'},z:6,
      tooltip:{trigger:'item',formatter:function(p){return '平仓成交日(换月收盘)';}}}
  ];
  var opt={
    backgroundColor:'transparent',
    tooltip:{trigger:'axis'},
    legend:{data:[name+'账户净值(万)', name+'主连','价差率%','分位','触发信号日','成交·开仓','成交·平仓'], textStyle:{color:'#cfd5e0'}, top:0, type:'scroll', width:'92%'},
    axisPointer:{link:[{xAxisIndex:'all'}]},
    grid:[
      {left:58,right:64,top:42,height:'38%'},
      {left:58,right:64,top:'48%',height:'14%'},
      {left:58,right:64,top:'66%',height:'14%'},
      {left:58,right:64,top:'84%',height:'10%'}
    ],
    dataZoom:[
      {type:'slider',xAxisIndex:[0,1,2,3],start:0,end:100,bottom:8,height:22,handleSize:'80%',showDetail:true,
       borderColor:'#333',fillerColor:'rgba(65,209,139,0.15)',handleStyle:{color:'#41d18b'},
       textStyle:{color:'#8a93a3'},dataBackground:{lineStyle:{color:'#555'},areaStyle:{color:'#555'}}},
      {type:'inside',xAxisIndex:[0,1,2,3]}
    ],
    xAxis:[
      {type:'category',data:d.dates,gridIndex:0,axisLabel:{show:false},axisLine:{lineStyle:{color:'#333'}}},
      {type:'category',data:d.dates,gridIndex:1,axisLabel:{show:false},axisLine:{lineStyle:{color:'#333'}}},
      {type:'category',data:d.dates,gridIndex:2,axisLabel:{show:false},axisLine:{lineStyle:{color:'#333'}}},
      {type:'category',data:d.dates,gridIndex:3,axisLabel:{color:'#8a93a3',fontSize:10},axisLine:{lineStyle:{color:'#333'}}}
    ],
    yAxis:[
      {type:'value',name:'净值(万)',gridIndex:0,scale:true,axisLabel:{color:'#8a93a3'},nameTextStyle:{color:'#8a93a3'},splitLine:{lineStyle:{color:'#222633'}}},
      {type:'value',name:'主连',gridIndex:0,scale:true,position:'right',axisLabel:{color:'#8a93a3'},nameTextStyle:{color:'#8a93a3'},splitLine:{show:false}},
      {type:'value',name:'价差率%',gridIndex:1,scale:true,axisLabel:{color:'#8a93a3',fontSize:10},nameTextStyle:{color:'#8a93a3'},splitLine:{lineStyle:{color:'#222633'}}},
      {type:'value',name:'分位',gridIndex:2,max:1,min:0,axisLabel:{color:'#8a93a3',fontSize:10},nameTextStyle:{color:'#8a93a3'},splitLine:{show:false}},
      {type:'value',name:'回撤%',gridIndex:3,axisLabel:{color:'#8a93a3',formatter:'{value}%'},nameTextStyle:{color:'#8a93a3'},splitLine:{lineStyle:{color:'#222633'}}}
    ],
    series:series
  };
  chart.setOption(opt);
  window.addEventListener('resize', function(){chart.resize();});
}
function renderTable(domId, rows){
  var el=document.getElementById(domId); if(!el) return;
  function fmt(v){ return (v==null || v==='') ? '-' : v; }
  var h='<thead><tr class="th">'+
    '<th class="l">#</th><th class="l">方向</th><th class="l">近月合约</th><th class="l">隔季合约</th>'+
    '<th class="l">开仓日</th><th class="l">平仓日</th>'+
    '<th>近月开</th><th>近月平</th><th>远月开</th><th>远月平</th>'+
    '<th>持有(天)</th><th>近月盈亏点</th><th>远月盈亏点</th><th>总盈亏点</th>'+
    '<th>毛盈亏</th><th>手续费</th><th>净盈亏</th><th class="l">状态</th></tr></thead><tbody>';
  rows.forEach(function(r){
    var pc = r.pnl>=0?'pnl-pos':'pnl-neg';
    var tagCls = r.reason=='持仓中'?'roll':r.reason;
    h+='<tr><td class="l">'+r.idx+'</td><td class="l">'+r.dir+'</td><td class="l">'+r.near_c+'</td><td class="l">'+r.far_c+'</td>'+
       '<td class="l">'+r.entry_d+'</td><td class="l">'+r.exit_d+'</td>'+
       '<td>'+fmt(r.n_ep)+'</td><td>'+fmt(r.n_xp)+'</td><td>'+fmt(r.f_ep)+'</td><td>'+fmt(r.f_xp)+'</td>'+
       '<td>'+fmt(r.hold)+'</td><td>'+fmt(r.n_pts)+'</td><td>'+fmt(r.f_pts)+'</td><td>'+fmt(r.tot_pts)+'</td>'+
       '<td>'+fmt(r.gross)+'</td><td>'+fmt(r.fee)+'</td><td class="'+pc+'">'+fmt(r.pnl)+'</td>'+
       '<td class="l"><span class="tag '+tagCls+'">'+r.reason+'</span></td></tr>';
  });
  h+='</tbody>';
  el.innerHTML=h;
}
function rollingPct(series, window){
  var w = Math.min(window, series.length);
  var i = series.length - 1;
  var win = series.slice(Math.max(0, i - w + 1), i + 1);
  var v = series[i];
  var le = 0;
  for(var k=0;k<win.length;k++) if(win[k] <= v) le++;
  return le / win.length;
}
var TRADE_CAL = new Set(DATA.trade_cal);   // 真实交易日历（与 Python 推送同源，build 时注入）
var ROLL_BUF = DATA.roll_buf;               // 换月缓冲（与微信推送一致）
function ymd(d){ var y=d.getFullYear(); var m=('0'+(d.getMonth()+1)).slice(-2); var dd=('0'+d.getDate()).slice(-2); return ''+y+m+dd; }
function isTradeDay(dStr){
  if(TRADE_CAL.has(dStr)) return true;       // 日历覆盖范围内：真实交易日
  var dt0=new Date(dStr); var wd=dt0.getDay();
  return wd!==0 && wd!==6;                    // 越界（日历未覆盖年份）：回退周一到周五
}
function tradingDaysUntil(ltdStr){
  var today = new Date(); today.setHours(0,0,0,0);
  var ltd = new Date(ltdStr); ltd.setHours(0,0,0,0);
  var cnt = 0;
  var d = new Date(today);
  while(d <= ltd){
    if(isTradeDay(ymd(d))) cnt++;
    d.setDate(d.getDate()+1);
  }
  return cnt;
}
var refreshStatus = '初始化';
function updateRefreshStatus(msg){
  refreshStatus = msg;
  var el = document.getElementById('refresh_status');
  if(el) el.textContent = msg;
}
function fetchSinaJsonp(codes, cb){
  var script = document.createElement('script');
  script.src = 'https://hq.sinajs.cn/list=' + codes.join(',') + '&_=' + Date.now();
  script.async = true;
  var done = false;
  function finish(err){
    if(done) return; done = true;
    if(script.parentNode) script.parentNode.removeChild(script);
    cb(err);
  }
  script.onload = function(){ finish(null); };
  script.onerror = function(){ finish('网络错误'); };
  script.onreadystatechange = function(){ if(this.readyState === 'complete' || this.readyState === 'loaded'){ finish(null); } };
  document.head.appendChild(script);
  setTimeout(function(){ finish('超时'); }, 10000);
}
function refreshSignal(){
  var pairs = [
    {prod:'IM', refNear:DATA.signal.IM.near, refFar:DATA.signal.IM.far, holdNear:DATA.signal.IM.pos.near_c, holdFar:DATA.signal.IM.pos.far_c, idx:'sh000852', W:DATA.signal.IM.W, eh:DATA.signal.IM.enter_high, spread:DATA.im.spread},
    {prod:'IC', refNear:DATA.signal.IC.near, refFar:DATA.signal.IC.far, holdNear:DATA.signal.IC.pos.near_c, holdFar:DATA.signal.IC.pos.far_c, idx:'sh000905', W:DATA.signal.IC.W, eh:DATA.signal.IC.enter_high, spread:DATA.ic.spread}
  ];
  var codes = [];
  pairs.forEach(function(p){
    codes.push('CFF_'+p.refNear, 'CFF_'+p.refFar, 'CFF_'+p.holdNear, 'CFF_'+p.holdFar, p.idx);
  });
  updateRefreshStatus('刷新行情中…');
  fetchSinaJsonp(codes, function(err){
    if(err){ updateRefreshStatus('实时刷新受限（行情源跨域限制），显示 '+DATA.signal.IM.quote_date+' 数据'); return; }
    var now = new Date();
    pairs.forEach(function(p){
      var s = DATA.signal[p.prod];
      var pos = s.pos || {};
      var refNearStr = window['hq_str_CFF_'+p.refNear];
      var refFarStr = window['hq_str_CFF_'+p.refFar];
      var holdNearStr = window['hq_str_CFF_'+p.holdNear];
      var holdFarStr = window['hq_str_CFF_'+p.holdFar];
      var idxStr = window['hq_str_'+p.idx];
      if(!refNearStr || !refFarStr) return;
      var refNearPx = parseFloat(refNearStr.split(',')[3]);
      var refFarPx = parseFloat(refFarStr.split(',')[3]);
      var idxPx = idxStr ? parseFloat(idxStr.split(',')[3]) : s.idx_px;
      s.near_px = refNearPx; s.far_px = refFarPx; s.idx_px = idxPx;
      s.spread_rel = (refNearPx - refFarPx) / refNearPx;
      s.spread_pts = +(refNearPx - refFarPx).toFixed(1);
      var series = p.spread.slice(-p.W).concat([s.spread_rel]);
      s.pct = +rollingPct(series, p.W).toFixed(3);
      s.near_basis = s.near_px - s.idx_px;
      s.far_basis = s.far_px - s.idx_px;
      s.basis_ok = s.near_basis < 0;
      s.roll_days = tradingDaysUntil(s.ltd);
      s.in_roll_window = s.roll_days <= ROLL_BUF;
      s.quote_date = now.toISOString().slice(0,10);
      s.quote_time = now.toTimeString().slice(0,8);
      var hNearPx = holdNearStr ? parseFloat(holdNearStr.split(',')[3]) : refNearPx;
      var hFarPx = holdFarStr ? parseFloat(holdFarStr.split(',')[3]) : refFarPx;
      var entrySpread = (pos.n_ep && pos.f_ep) ? (pos.n_ep - pos.f_ep) : 0;
      var curSpread = hNearPx - hFarPx;
      var floatPnl = (curSpread - entrySpread) * 200;
      if(s.in_roll_window){
        s.action = '【换月】下月'+s.near+'距到期≤10交易日，平掉当前持仓 '+pos.near_c+'/'+pos.far_c+'，次日开盘换月至 '+s.near+'/'+s.far;
        s.actionable = true;
      } else if(s.enter_high == null){
        s.action = '【持仓中】当前持有 <b>'+pos.near_c+'/'+pos.far_c+'</b>，开仓日 '+pos.entry_d+'，浮动盈亏约 '+Math.round(floatPnl).toLocaleString()+' 元；纯持有策略始终在场，继续持有。';
        s.actionable = false;
      } else if(pos.is_holding){
        s.action = '【持仓中】当前持有 <b>'+pos.near_c+'/'+pos.far_c+'</b>，开仓日 '+pos.entry_d+'，浮动盈亏约 '+Math.round(floatPnl).toLocaleString()+' 元；继续持有。';
        s.actionable = false;
      } else if(s.pct >= s.enter_high && s.basis_ok){
        s.action = '【开仓】分位 '+s.pct+' ≥ '+s.enter_high+'，且下月'+s.near+'贴水 '+s.near_basis.toFixed(1)+' 点，次日开盘开多'+s.near+'+开空'+s.far;
        s.actionable = true;
      } else if(s.pct >= s.enter_high && !s.basis_ok){
        s.action = '【等待】分位 '+s.pct+' ≥ '+s.enter_high+'，但下月'+s.near+'升水 '+s.near_basis.toFixed(1)+' 点，属于危险结构，不开仓';
        s.actionable = false;
      } else {
        s.action = '【等待】分位 '+s.pct+' < '+s.enter_high+'，需贴水更深（价差率更大）才开仓';
        s.actionable = false;
      }
      s.is_holding = pos.is_holding;
      s.holding_status = pos.is_holding ? '持仓中' : '空仓中';
    });
    renderSignal();
    updateRefreshStatus('行情已更新 '+now.toLocaleTimeString());
  });
}
function renderSignal(){
  var box=document.getElementById('signal_card');
  if(!DATA.signal_ok){
    box.innerHTML='<h2>实时操作提示</h2><div class="sig-detail">联网获取行情失败：'+(DATA.signal_err||'未知错误')+'。请检查网络后重新生成本页。</div>';
    return;
  }
  var s=DATA.signal;
  var html='<h2>实时操作提示（联网最新行情）<span id="refresh_status" style="font-size:11px;font-weight:400;color:#6b7280;margin-left:10px;">'+refreshStatus+'</span></h2><div class="sig-row">';
  for(var prod of ['IM','IC']){
    var x=s[prod];
    var cls = x.actionable ? (x.in_roll_window?'roll':'go') : (x.in_roll_window?'roll':(x.is_holding?'hold':'wait'));
    var thrtxt = x.enter_high!=null ? ('入场分位阈值 <b>'+x.enter_high+'</b>') : '纯持有（无分位过滤）';
    var statusBadge = '<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:'+(x.is_holding?'#1d4e6f;color:#5aa9ff':'#3d2f1d;color:#ffb347')+'">'+x.holding_status+'</span>';
    html+='<div class="sig-box '+cls+'">'+
      '<div class="sig-prod">'+prod+' · 近月-隔季（'+x.near+' / '+x.far+'） '+statusBadge+'</div>'+
      '<div class="sig-act">'+x.action+'</div>'+
      '<div class="sig-detail">'+
        '最新价：'+x.near+'=<b>'+x.near_px+'</b> ， '+x.far+'=<b>'+x.far_px+'</b><br>'+
        '价差率(贴水深度)=<b>'+(x.spread_rel*100).toFixed(2)+'%</b>（'+(x.spread_pts>0?'+':'')+x.spread_pts+'点）；当前分位=<b>'+x.pct+'</b> ｜ '+thrtxt+'<br>'+
        '下月'+x.near+' 最后交易日=<b>'+x.ltd+'</b>，距到期 <b>'+x.roll_days+'</b> 个交易日'+(x.in_roll_window?'（已进入换月窗口≤10日）':'（未到换月窗口）')+
        '<div class="qtime">行情时间：'+x.quote_date+' '+x.quote_time+'</div>'+
      '</div></div>';
  }
  html+='</div>';
  box.innerHTML=html;
}
renderSignal();
refreshSignal();
setInterval(refreshSignal, 30000);
renderCard(DATA.im,'im'); renderCard(DATA.ic,'ic');
renderChart('im_chart', DATA.im, 'IM', 0.20, DATA.im.hold_periods);
renderChart('ic_chart', DATA.ic, 'IC', 0.40, DATA.ic.hold_periods);
renderTable('im_tbl', DATA.im.trades);
renderTable('ic_tbl', DATA.ic.trades);
</script></body></html>"""

echarts_js = open('echarts.min.js', encoding='utf-8').read()
html = TEMPLATE.replace('@@ECHARTS@@', echarts_js).replace('@@DATA@@', DATA_JSON)
out = 'public/index.html'
_d = os.path.dirname(out)
if _d:
    os.makedirs(_d, exist_ok=True)
with open(out,'w',encoding='utf-8') as f:
    f.write(html)
print('已生成:', out)
print('IM:', im['metrics'])
print('IC:', ic['metrics'])
print('信号OK:', DATA['signal_ok'])
if DATA['signal_ok']:
    for p in ['IM','IC']:
        print(' ', p, DATA['signal'][p]['action'])
