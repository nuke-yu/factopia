"""研究报告（业务需求 BR-03、40；Spec RPT-01 至 03、第 14.13 节）：单文件 HTML，可离线打开。

- 中文主报告，七章（RPT-02）：① 运行信息 ② 数据质量 ③ 三市场因子评估与迁移性分类 ④ 自动挖掘结果与验证记录
  ⑤ 组合表现（含汇率拆分和归因）⑥ 规则命中 ⑦ 已知局限（RPT-03 固定三项 + 本次运行的说明）。
- 附录 A（English）与附录 B（Français）：证据附件。数字与规则编号逐项列出；研究假设、可证伪测试、AI 生成的轮次假设与因子名
  保留原文（不翻译），标签与固定说明用对应语言。附件不含新的结论，只是主报告证据的另一语言版本。
- 币种：组合与报告按授权的基础币种 / 报告币种（BR-03）。
报告存档在 runtime/reports/，文件名含研究编号、版本与生成时间。
"""
import html
import json
from datetime import datetime, timezone

import pandas as pd

MARKET = {"zh": {"hk": "香港", "uk": "英国", "fr": "法国", "all": "通用"},
          "en": {"hk": "Hong Kong", "uk": "United Kingdom", "fr": "France", "all": "All markets"},
          "fr": {"hk": "Hong Kong", "uk": "Royaume-Uni", "fr": "France", "all": "Tous marchés"}}
SEV = {"en": {"block": "Block", "warn": "Warning", "info": "Info"}, "fr": {"block": "Blocage", "warn": "Alerte", "info": "Information"}}
L = {
    "en": {"title": "Appendix A — Evidence (English)", "spec": "Research specification (original text)", "hyp_orig": "Hypothesis as entered",
           "hyp": "Normalised hypothesis", "fals": "Falsification test", "signals": "Signal sources by market",
           "run": "Run", "round": "Adopted round", "factors": "Factors (code names)", "round_hyp": "Round hypothesis (AI, original)",
           "eval": "Factor evaluation", "ric": "Rank IC", "t": "t-stat", "ret": "Excess return after costs (ann.)", "verdict": "Verdict",
           "pf": "Portfolio performance", "total": "Total portfolio", "metric": "Metric", "value": "Value",
           "fxsplit": "FX split (cumulative)", "local": "Local-currency return", "fx": "FX return", "base": "Base-currency return",
           "attr": "Brinson attribution (base currency, Carino-linked)", "alloc": "Allocation", "sel": "Selection (local)",
           "fxi": "FX & interaction", "rules": "Rule hits", "rule": "Rule", "sev": "Severity", "market": "Market", "evidence": "Evidence (raw)",
           "limits": "Known limitations", "period": "Period", "bench": "Benchmark", "currency": "Base currency",
           "packs": "Rule packs", "none": "none", "note": "Figures are identical to the Chinese main report; original-language text is kept verbatim.",
           "limits_text": ["Survivorship bias: the universe uses current index constituents, not historical membership.",
                           "Yahoo Finance data are not point-in-time: adjusted prices are restated after later dividends and splits.",
                           "Taxes and costs come from rule packs that are pending confirmation by local compliance.",
                           "This is a proof of concept with simulated mandates; nothing here is investment advice or a legal conclusion."]},
    "fr": {"title": "Annexe B — Éléments de preuve (Français)", "spec": "Spécification de recherche (texte original)",
           "hyp_orig": "Hypothèse saisie", "hyp": "Hypothèse normalisée", "fals": "Test de réfutation", "signals": "Sources des signaux par marché",
           "run": "Exécution", "round": "Itération retenue", "factors": "Facteurs (noms dans le code)", "round_hyp": "Hypothèse de l'itération (IA, original)",
           "eval": "Évaluation des facteurs", "ric": "Rank IC", "t": "Statistique t", "ret": "Rendement excédentaire net de coûts (annualisé)",
           "verdict": "Verdict", "pf": "Performance du portefeuille", "total": "Portefeuille total", "metric": "Indicateur", "value": "Valeur",
           "fxsplit": "Décomposition de change (cumulée)", "local": "Rendement en devise locale", "fx": "Effet de change",
           "base": "Rendement en devise de référence", "attr": "Attribution de Brinson (devise de référence, liaison de Carino)",
           "alloc": "Allocation", "sel": "Sélection (locale)", "fxi": "Change et interaction", "rules": "Règles déclenchées", "rule": "Règle",
           "sev": "Gravité", "market": "Marché", "evidence": "Éléments (bruts)", "limits": "Limites connues", "period": "Période",
           "bench": "Indice de référence", "currency": "Devise de référence", "packs": "Paquets de règles", "none": "aucun",
           "note": "Les chiffres sont identiques au rapport principal en chinois ; les textes en langue originale sont reproduits tels quels.",
           "limits_text": ["Biais de survivant : l'univers utilise les composants actuels des indices, pas l'historique.",
                           "Les données Yahoo Finance ne sont pas « point-in-time » : les cours ajustés sont retraités après dividendes et divisions.",
                           "Les taxes et coûts proviennent de paquets de règles en attente de validation par la conformité locale.",
                           "Preuve de concept avec des mandats simulés ; aucun contenu ne constitue un conseil en investissement ni une conclusion juridique."]},
}
METRICS = [("return_ann", {"zh": "年化收益", "en": "Annualised return", "fr": "Rendement annualisé"}),
           ("vol", {"zh": "年化波动率", "en": "Volatility", "fr": "Volatilité"}),
           ("sharpe", {"zh": "夏普比率", "en": "Sharpe ratio", "fr": "Ratio de Sharpe"}),
           ("max_drawdown", {"zh": "最大回撤", "en": "Max drawdown", "fr": "Perte maximale"}),
           ("bench_return_ann", {"zh": "基准年化收益", "en": "Benchmark return (ann.)", "fr": "Rendement de l'indice (annualisé)"}),
           ("excess_ann", {"zh": "年化超额", "en": "Excess return (ann.)", "fr": "Rendement excédentaire (annualisé)"}),
           ("tracking_error", {"zh": "跟踪误差", "en": "Tracking error", "fr": "Erreur de suivi"}),
           ("info_ratio", {"zh": "信息比率", "en": "Information ratio", "fr": "Ratio d'information"})]
RATIO = {"sharpe", "info_ratio"}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def pct(x, nd=2) -> str:
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def _cut(x, n: int) -> str:
    x = str(x or "")
    return x if len(x) <= n else x[:n].rstrip() + "…"


def fmt(k: str, v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}" if k in RATIO else pct(v)


def _nav_svg(daily: pd.DataFrame, base: str) -> str:
    """组合与复合基准的净值（基础币种）。"""
    nav = (1 + daily["port"]).cumprod()
    bn = (1 + daily["bench"]).cumprod()
    W, H, pl, pr, pt, pb = 760, 240, 48, 12, 12, 28
    lo, hi = float(min(nav.min(), bn.min())), float(max(nav.max(), bn.max()))
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    n = len(nav)
    X = lambda i: pl + (W - pl - pr) * i / max(n - 1, 1)  # noqa: E731
    Y = lambda v: pt + (H - pt - pb) * (hi - v) / (hi - lo)  # noqa: E731
    path = lambda s: " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(s.to_numpy()))  # noqa: E731
    ticks = [lo + (hi - lo) * k / 4 for k in range(5)]
    grid = "".join(f'<line x1="{pl}" x2="{W - pr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#d9dde3" stroke-width="1"/>'
                   f'<text x="{pl - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="#5b6470">{v:.2f}</text>' for v in ticks)
    dates = daily.index
    lab = "".join(f'<text x="{X(i):.1f}" y="{H - 8}" text-anchor="{a}" font-size="11" fill="#5b6470">{dates[i].strftime("%Y-%m")}</text>'
                  for i, a in ((0, "start"), (n // 2, "middle"), (n - 1, "end")))
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="组合与基准净值（{e(base)}）" style="width:100%;height:auto">{grid}{lab}'
            f'<path d="{path(bn)}" fill="none" stroke="#8a93a0" stroke-width="1.5" stroke-dasharray="4 3"/>'
            f'<path d="{path(nav)}" fill="none" stroke="#1f5fa8" stroke-width="2"/></svg>')


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def _hits_zh(pre: dict | None) -> str:
    if not pre:
        return "<p>尚未生成。</p>"
    rows = [[e(h["severity_zh"]), f'<span class="mono">{e(h["rule_id"])}</span>', e(h["market_zh"]), e(h["title"]), e(h["reason"]),
             e("、".join(h["approvers"]) or "—")] for h in pre["hits"]]
    return (f'<p>阻断 {pre["counts"]["block"]} · 警告 {pre["counts"]["warn"]} · 提示 {pre["counts"]["info"]}；规则包 '
            + e(" · ".join(f'{p["pack"]} {p["version"]}' for p in pre["packs"])) + "</p>"
            + _table(["程度", "编号", "市场", "规则", "命中原因", "所需审批"], rows))


def render(d: dict) -> str:
    """d：spec、study、rules（两次预检）、portfolio（summary）、daily、pit、quality、explanation、feedback。"""
    spec, st, pf, rl = d["spec"], d["study"], d.get("portfolio"), d["rules"]
    ms = spec["markets"]
    Z = MARKET["zh"]
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = (pf or {}).get("base_currency") or "—"
    out = [f"""<title>{e(spec.get('title_zh') or spec['id'])} · 研究报告 v{spec['version']}</title>
<style>
:root{{--ink:#1b2430;--mute:#5b6470;--line:#d9dde3;--bg:#fbfbfc;--acc:#1f5fa8;--bad:#b3261e;--warn:#9a6700}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 "PingFang SC","Noto Sans SC","Microsoft YaHei",system-ui,sans-serif}}
main{{max-width:980px;margin:0 auto;padding-block:32px 64px;padding-inline:20px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:36px 0 10px;padding-top:10px;border-top:2px solid var(--ink)}}
h3{{font-size:15px;margin:20px 0 6px}} .meta{{color:var(--mute);font-size:12px}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px;font-variant-numeric:tabular-nums}}
th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}} th{{color:var(--mute);font-weight:600;font-size:12px}}
.note{{background:#eef3fa;border-left:3px solid var(--acc);padding:8px 12px;font-size:13px}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
blockquote{{margin:6px 0;padding:6px 12px;border-left:3px solid var(--line);color:var(--ink);white-space:pre-wrap}}
.appendix{{margin-top:56px;border-top:4px double var(--ink)}} @media print{{h2{{break-after:avoid}} .appendix{{break-before:page}}}}
</style>
<main>
<div class="meta">Factopia 跨境量化研究平台 · 最小 POC · 生成于 {gen}</div>
<h1>{e(spec.get('title_zh') or spec['id'])}</h1>
<div class="meta">研究编号 <span class="mono">{e(spec['id'])}</span> · 版本 v{spec['version']} · 授权 {e((pf or {}).get('mandate_name') or spec.get('mandate'))} · 基础币种 {e(base)} · 报告币种 {e((pf or {}).get('report_currency') or base)}</div>
<p class="note">本报告为中文主报告；附录 A（English）与附录 B（Français）为证据附件，数字与主报告一致，原文保留。平台只发现、解释和路由，不作投资建议或法律结论；规则内容待各地区合规确认。</p>
"""]
    # ① 运行信息
    runs = [[e(Z[m]), f'<span class="mono">{e((st["markets"].get(m) or {}).get("run_id") or "—")}</span>',
             e(((st["markets"].get(m) or {}).get("focus") or {}).get("loop", "—")), e((st["markets"].get(m) or {}).get("state"))] for m in ms]
    out.append(f"""<h2>① 运行信息</h2>
<p>假设（规范化，英文）：</p><blockquote>{e(spec.get('hypothesis'))}</blockquote>
<p>可证伪测试：</p><blockquote>{e(spec.get('falsification'))}</blockquote>
<p>预测周期 {e(spec.get('horizon'))} 个交易日 · 锁定于 {e(spec.get('locked_at'))}（{e(spec.get('locked_by'))}）</p>
{_table(["市场", "运行", "采纳轮次", "状态"], runs)}
<p>规则包：{e(" · ".join(f'{p["pack"]} {p["version"]}' for p in rl["setup"]["packs"]))} · 数据来源登记表 {e(rl["setup"]["sources_version"])}</p>""")
    # ② 数据质量
    q = d.get("quality") or {}
    qrows = []
    for m in ms:
        qm = (q.get("markets") or {}).get(m) or {}
        ps = qm.get("per_symbol") or {}
        qrows.append([e(Z[m]), e(qm.get("symbols")), e("、".join(qm.get("excluded") or []) or "无"),
                      e(f'{qm.get("sessions", ["", "", ""])[0]} 至 {qm.get("sessions", ["", "", ""])[1]}'),
                      e(sum(v.get("missing_sessions", 0) for v in ps.values())), e(sum(v.get("jumps_over_50pct", 0) for v in ps.values()))])
    pit = d.get("pit") or {}
    out.append(f"""<h2>② 数据质量</h2>
{_table(["市场", "股票数", "剔除", "交易日范围", "缺失交易日（合计）", "单日涨跌超 50%"], qrows)}
<p>时间点一致性：<b>{e(pit.get('label', '未生成'))}</b>。{e('；'.join(pit.get('reasons') or []))}</p>""")
    # ③ 因子评估与迁移性
    erows = []
    for m in ms:
        f = (st["markets"].get(m) or {}).get("focus") or {}
        mt, sig = f.get("metrics") or {}, f.get("significance") or {}
        erows.append([e(Z[m]), e(f.get("loop", "—")), e(None if mt.get("rank_ic") is None else f'{mt["rank_ic"]:.4f}'),
                      e(None if sig.get("t") is None else f'{sig["t"]:.2f}'), pct(mt.get("annual_return_with_cost")),
                      "通过" if f.get("passed") else ("未通过" if f else "—"), e(((f.get("diagnostics") or {}).get("economic") or {}).get("label"))])
    tr = st.get("transfer") or {}
    cm = st.get("cross_market") or {}
    out.append(f"""<h2>③ 三市场因子评估与迁移性分类</h2>
<p>整体判定：<b>{e((st.get('overall') or {}).get('label'))}</b>；迁移性：<b>{e(tr.get('label'))}</b>（{e(tr.get('reason') or '')}）</p>
{_table(["市场", "采纳轮次", "Rank IC", "t 值", "扣成本年化超额", "可证伪测试", "经济意义"], erows)}
<p>三市场相关结构（逐月 Rank IC，{e(cm.get('months'))} 个月）：{e('；'.join(f"{Z[p['a']]}—{Z[p['b']]} {p['corr']}" for p in cm.get('pairs') or []))}</p>""")
    ex = d.get("explanation")
    if ex:
        sents = "".join(f"<li>{e(s.get('text'))} <span class='meta mono'>{e(' '.join(s.get('cites') or []))}</span></li>" for s in ex.get("sentences") or [])
        out.append(f"<h3>AI 解释市场差异（每句引用平台事实编号；需研究员确认）</h3><ul>{sents}</ul>")
    # ④ 自动挖掘与验证
    out.append("<h2>④ 自动挖掘结果与验证记录</h2>")
    for m in ms:
        e_m = st["markets"].get(m) or {}
        rows = [[e(r["loop"]), "采纳" if r.get("decision") else "未采纳", e(None if r.get("rank_ic") is None else f'{r["rank_ic"]:.4f}'),
                 e(None if r.get("rank_ic_t") is None else f'{r["rank_ic_t"]:.2f}'), pct(r.get("return")), "是" if r.get("passed") else "否",
                 f'<span class="mono">{e(_cut(r.get("hypothesis"), 200))}</span>'] for r in e_m.get("rounds") or []]
        cred = ((e_m.get("focus") or {}).get("credibility") or {})
        out.append(f"<h3>{e(Z[m])}（{e(e_m.get('run_id') or '未运行')}）</h3>"
                   + (_table(["轮次", "RD-Agent 决定", "Rank IC", "t", "扣成本年化", "过可证伪", "本轮假设（AI 原文）"], rows) if rows else "<p>没有轮次。</p>")
                   + (f"<p>可信度：<b>{e(cred.get('label'))}</b> {e('；'.join('未通过：' + c.get('label', '') for c in cred.get('checks') or [] if c.get('ok') is False))}</p>" if cred else ""))
    # ⑤ 组合表现
    out.append("<h2>⑤ 组合表现（含汇率拆分和归因）</h2>")
    if pf:
        t = pf["terms"]
        out.append(f"""<p>区间 {e(pf['period']['start'])} 至 {e(pf['period']['end'])}（{pf['period']['days']} 个交易日，{pf['period']['rebalances']} 次月度调仓）；
基准 {e(pf['benchmark']['label'])}；基础币种 {e(base)}。只做多，单只上限 {pct(t['max_stock_weight'], 0)}，跟踪误差上限 {pct(t['max_tracking_error'], 0)}，单次换手上限 {pct(t['max_turnover'], 0)}。</p>""")
        if d.get("daily") is not None:
            out.append(_nav_svg(d["daily"], base) + '<p class="meta">实线：组合；虚线：复合基准（均为基础币种净值）。</p>')
        mt = pf["metrics"]
        head = ["指标", "全组合"] + [f"{Z[m]}（本地）" for m in ms] + [f"{Z[m]}（{e(base)}）" for m in ms]
        rows = [[lab["zh"], fmt(k, mt["total"].get(k))] + [fmt(k, mt["markets"][m]["local"].get(k)) for m in ms]
                + [fmt(k, mt["markets"][m]["base"].get(k)) for m in ms] for k, lab in METRICS]
        rows.append(["平均单次换手", pct(mt.get("avg_turnover"))] + ["" for _ in ms * 2])
        out.append("<h3>业绩指标</h3>" + _table(head, rows))
        fx = pf["fx_split"]
        out.append("<h3>汇率拆分（区间累计；基础币种收益 =（1 + 本地收益）×（1 + 汇率收益）− 1）</h3>" + _table(
            ["市场", "本地收益", "汇率收益", "交叉项", "基础币种收益", "逐日恒等误差（最大）"],
            [[e(Z[m]), pct(fx[m]["local"]), pct(fx[m]["fx"]), pct(fx[m]["cross"]), pct(fx[m]["base"]), f'{fx[m]["identity_max_error"]:.1e}'] for m in ms]))
        a = pf["attribution"]
        out.append(f"<h3>Brinson 归因（基础币种，复合基准，Carino 连接）</h3>" + _table(
            ["市场", "配置效应", "选股效应（本地）", "汇率与交互", "合计"],
            [[e(Z[m]), pct(a["markets"][m]["allocation"]), pct(a["markets"][m]["selection"]), pct(a["markets"][m]["fx_interaction"]),
              pct(sum(a["markets"][m].values()))] for m in ms]
            + [["<b>合计</b>", pct(a["by_effect"]["allocation"]), pct(a["by_effect"]["selection"]), pct(a["by_effect"]["fx_interaction"]),
                f'<b>{pct(a["sum_of_effects"])}</b>']])
            + f'<p>组合相对基准的区间超额 {pct(a["total_excess"])}；各项之和与超额之差 {a["linking_error"]:.1e}（PORT-08 要求 &lt; 1bp）。'
              f'组合的汇率贡献（绝对）：{e("、".join(f"{Z[m]} {pct(v)}" for m, v in a["fx_contribution_abs"].items()))}。</p>')
        out.append("<h3>约束与来源（PORT-02）</h3>" + _table(["约束", "取值", "来源", "执行情况"],
                                                          [[e(c["name"]), e(c["value"]), e(c["source"]), e(c["status"])] for c in pf["constraints"]]))
        fb = d.get("feedback") or []
        out.append("<h3>反馈闭环（BR-32；平台建议，研究员决定）</h3>" + _table(
            ["市场", "平台建议", "依据", "研究员决定"],
            [[e(Z[s["market"]]), e(s["action_zh"]), e("；".join(s["reasons"])),
              e(next((f"{x['action_zh']}（{x.get('by', '')}，{x['at'][:10]}）" for x in reversed(fb) if x["market"] == s["market"]), "未记录"))]
             for s in pf["feedback"]]))
    else:
        out.append("<p>尚未构建授权组合（组合台 → 构建组合）。</p>")
    # ⑥ 规则命中
    ap = (rl.get("portfolio") or rl["setup"])["approval_path"]
    out.append("<h2>⑥ 规则命中</h2><h3>立项锁定前</h3>" + _hits_zh(rl["setup"]) + "<h3>组合生成后</h3>" + _hits_zh(rl.get("portfolio"))
               + "<h3>建议审批路径</h3><ol>" + "".join(f"<li><b>{e(s['role'])}</b> <span class='meta'>{e('、'.join(s['reasons']))}</span></li>" for s in ap["steps"])
               + f"</ol><p class='{'bad' if ap['blocked'] else 'meta'}'>{e(ap['note'])}</p>")
    # ⑦ 已知局限
    lim = ["幸存者偏差：股票池使用当前成分股，历史上被剔除、退市的股票不在样本中（取得数据商历史成分股后替换）。",
           "Yahoo Finance 数据非时间点一致：复权价按之后的分红、拆股回溯调整；只适合研究，生产须改用授权数据商。",
           "税费与成本参数来自规则包，状态为\"待本地合规确认\"。",
           "本 POC 使用模拟授权；不构成投资建议或法律结论。"]
    notes = sorted({(f.get("diagnostics") or {}).get("backtest_note") for f in ((st["markets"].get(m) or {}).get("focus") or {} for m in ms)} - {None, ""})
    lim += notes + ([pf["backtest_note"]] if pf else [])
    out.append("<h2>⑦ 已知局限</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in lim) + "</ul>")
    # 附录
    for lang in ("en", "fr"):
        out.append(_appendix(lang, d))
    out.append("</main>")
    return "\n".join(out)


def _appendix(lang: str, d: dict) -> str:
    T, M = L[lang], MARKET[lang]
    spec, st, pf, rl = d["spec"], d["study"], d.get("portfolio"), d["rules"]
    ms = spec["markets"]
    s = [f'<section class="appendix" lang="{lang}"><h2>{T["title"]}</h2><p class="note">{T["note"]}</p>',
         f'<h3>{T["spec"]}</h3><p><b>{T["hyp_orig"]}</b></p><blockquote>{e(spec.get("hypothesis_original"))}</blockquote>'
         f'<p><b>{T["hyp"]}</b></p><blockquote>{e(spec.get("hypothesis"))}</blockquote><p><b>{T["fals"]}</b></p><blockquote>{e(spec.get("falsification"))}</blockquote>']
    rows = []
    for m in ms:
        f = (st["markets"].get(m) or {}).get("focus") or {}
        rows.append([e(M[m]), f'<span class="mono">{e((st["markets"].get(m) or {}).get("run_id") or "—")}</span>', e(f.get("loop", "—")),
                     f'<span class="mono">{e(", ".join(f.get("factors") or []) or T["none"])}</span>', f'<span class="mono">{e(f.get("hypothesis"))}</span>'])
    s.append(f'<h3>{T["signals"]}</h3>' + _table([T["market"], T["run"], T["round"], T["factors"], T["round_hyp"]], rows))
    rows = []
    for m in ms:
        f = (st["markets"].get(m) or {}).get("focus") or {}
        mt, sig = f.get("metrics") or {}, f.get("significance") or {}
        rows.append([e(M[m]), e(None if mt.get("rank_ic") is None else f'{mt["rank_ic"]:.4f}'), e(None if sig.get("t") is None else f'{sig["t"]:.2f}'),
                     pct(mt.get("annual_return_with_cost")), "✓" if f.get("passed") else ("✗" if f else "—")])
    s.append(f'<h3>{T["eval"]}</h3>' + _table([T["market"], T["ric"], T["t"], T["ret"], T["verdict"]], rows))
    if pf:
        base = pf["base_currency"]
        s.append(f'<h3>{T["pf"]}</h3><p>{T["period"]}: {e(pf["period"]["start"])} – {e(pf["period"]["end"])} · {T["currency"]}: {e(base)} · '
                 f'{T["bench"]}: {e(" + ".join(f"{pf['benchmark']['indices'][m]} {pf['benchmark']['weights'][m]:.0%}" for m in ms))}</p>')
        mt = pf["metrics"]
        s.append(_table([T["metric"], T["total"]] + [f"{M[m]} ({base})" for m in ms],
                        [[lab[lang], fmt(k, mt["total"].get(k))] + [fmt(k, mt["markets"][m]["base"].get(k)) for m in ms] for k, lab in METRICS]))
        fx = pf["fx_split"]
        s.append(f'<h3>{T["fxsplit"]}</h3>' + _table([T["market"], T["local"], T["fx"], T["base"]],
                                                     [[e(M[m]), pct(fx[m]["local"]), pct(fx[m]["fx"]), pct(fx[m]["base"])] for m in ms]))
        a = pf["attribution"]
        s.append(f'<h3>{T["attr"]}</h3>' + _table([T["market"], T["alloc"], T["sel"], T["fxi"]],
                                                  [[e(M[m]), pct(a["markets"][m]["allocation"]), pct(a["markets"][m]["selection"]),
                                                    pct(a["markets"][m]["fx_interaction"])] for m in ms])
                 + f'<p>Σ = {pct(a["sum_of_effects"])} · excess = {pct(a["total_excess"])}</p>')
    hits = [h for pre in (rl["setup"], rl.get("portfolio")) if pre for h in pre["hits"]]
    s.append(f'<h3>{T["rules"]}</h3><p>{T["packs"]}: {e(" · ".join(f"{p['pack']} {p['version']}" for p in rl["setup"]["packs"]))}</p>'
             + _table([T["rule"], T["sev"], T["market"], T["evidence"]],
                      [[f'<span class="mono">{e(h["rule_id"])}</span>', e(SEV[lang][h["severity"]]),
                        e(", ".join(M.get(x, x) for x in str(h["market"]).split(","))),
                        f'<span class="mono">{e(json.dumps(h["evidence"], ensure_ascii=False))}</span>'] for h in hits]))
    s.append(f'<h3>{T["limits"]}</h3><ul>' + "".join(f"<li>{e(x)}</li>" for x in T["limits_text"]) + "</ul></section>")
    return "\n".join(s)
