# -*- coding: utf-8 -*-
"""生成 data/event_avoid.json: 自动事件扫描器筛出+精测robust的一批事件避雷(合并, 避免碎文件)。
验证(gate_event_scanner2+_sv2 分年): 都是中位负+四年全负的robust避雷:
 - 高管辞职(董事长/总经理): 20日中位-6.41%/四年全负(高管跑路)
 - 会计师事务所变更: 60日中位-7.80%/四年全负(财务存疑)
 - 减持计划: 20日中位-4.44%/四年全负(供给冲击)
 - 员工持股计划草案: 20日中位-2.65%/三年负(弱, ESOP托底=缺信心)
源 巨潮cninfo公告标题关键词。各自避雷窗(20或60交易日)。
跑: D:/anaconda3/python.exe scripts/export_event_avoid.py
"""
import os, json, datetime
try:
    from scripts.cninfo_query import query_announcements
except ImportError:  # direct ``python scripts/export_event_avoid.py`` execution
    from cninfo_query import query_announcements
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "event_avoid.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")

# (类别, 关键词, 标题过滤, 避雷窗自然日, 验证文案)
CATS = [
    ("高管辞职", "辞职", lambda t: ('董事长' in t or '总经理' in t) and ('辞职' in t or '辞任' in t), 30,
     "董事长/总经理辞职=高管跑路, 20日中位-6.41%四年全负"),
    ("换会计师事务所", "变更会计师事务所", lambda t: '会计师事务所' in t and '变更' in t, 90,
     "更换会计师事务所=财务存疑, 60日中位-7.80%四年全负"),
    ("减持计划", "减持计划", lambda t: '减持' in t and '计划' in t and '增持' not in t, 30,
     "大股东减持计划=供给冲击, 20日中位-4.44%四年全负"),
    ("员工持股草案", "员工持股计划", lambda t: '员工持股' in t and ('草案' in t or '计划' in t), 30,
     "员工持股草案(弱)=ESOP托底缺信心, 20日中位-2.65%三年负"),
    ("重组复牌补跌", "重大资产重组", lambda t: '重大资产重组' in t and ('复牌' in t or '继续停牌' in t or '进展' in t), 90,
     "重大资产重组复牌/进展=重组预期落空补跌, 60日中位-11.44%三年全负(最狠)"),
    ("加帽ST/退市风险", "退市风险警示", lambda t: ('实施' in t or '被' in t or '披星' in t or '戴帽' in t) and ('退市风险' in t or '其他风险警示' in t) and '撤销' not in t and '关于' not in t[:3], 90,
     "被实施ST/退市风险警示, 60日中位-5.79%(2023/24强负, 2025改革后微正)"),
    ("行政处罚", "行政处罚", lambda t: '行政处罚' in t and ('决定' in t or '收到' in t) and '听证' not in t, 90,
     "收到行政处罚决定书=监管定性违规, 60日中位-3.87%近三年全负且加深"),
    ("账户/资产冻结", "冻结", lambda t: '冻结' in t and ('账户' in t or '股份' in t or '资产' in t) and '解除' not in t and '解封' not in t, 90,
     "账户/股份/资产被冻结=流动性/债务危机, 60日中位-5.50%四年全负"),
    ("限售解禁", "解除限售", lambda t: '解除限售' in t or ('限售股' in t and '上市流通' in t), 30,
     "限售股解禁=供给冲击, 20日中位-2.44%四年全负"),
]


def cninfo(kw, sedate, col):
    out = []
    for a in query_announcements(kw, sedate, col, max_pages=11, pause=0.4):
        code = str(a.get('secCode', ''))[:6]; t = a.get('announcementTime'); ti = a.get('announcementTitle', '')
        try:
            ad = datetime.datetime.utcfromtimestamp(t / 1000).strftime('%Y-%m-%d')
        except Exception:
            ad = None
        out.append((code, ad, a.get('secName', ''), ti))
    return out


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    today = datetime.date.today()
    cats_out = {}
    for cat, kw, tf, win_days, desc in CATS:
        start = (today - datetime.timedelta(days=win_days + 15)).strftime('%Y-%m-%d')
        sedate = f"{start}~{today.strftime('%Y-%m-%d')}"
        seen = {}
        for col in ['szse', 'sse']:
            for code, ad, name, title in cninfo(kw, sedate, col):
                if not (ad and code and code[0] in '036' and tf(title)):
                    continue
                ts_code = code + (".SH" if code[0] == '6' else ".SZ")
                if ts_code not in seen or ad > seen[ts_code]['ann_date']:
                    days = (today - datetime.date.fromisoformat(ad)).days
                    seen[ts_code] = {"code": code, "ts_code": ts_code, "name": name, "ann_date": ad,
                                     "days_since": days, "in_window": days <= win_days, "title": title[:36]}
        items = sorted(seen.values(), key=lambda x: x['ann_date'], reverse=True)
        cats_out[cat] = {"desc": desc, "win_days": win_days, "n": len(items),
                         "n_window": sum(1 for x in items if x['in_window']), "items": items}
        print(f"[event_avoid] {cat}: {len(items)}只(窗口内{cats_out[cat]['n_window']})", flush=True)
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "as_of": today.strftime("%Y-%m-%d"),
           "cats": cats_out, "note": "自动事件扫描器筛出+分年精测robust的事件避雷(高管辞职/换会计师/减持计划/员工持股草案)。标记不剔除人工判断。"}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "event_avoid.json"))
    except Exception as e:
        print(f"[event_avoid] 拷NAS失败: {e}")
    print(f"[event_avoid] 共{sum(c['n'] for c in cats_out.values())}只 -> {OUT}")


if __name__ == "__main__":
    main()
