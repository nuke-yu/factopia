import { useEffect, useMemo, useState } from "react";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import EChart from "../components/EChart";
import VersionPicker, { useVersionPick } from "../components/VersionPicker";
import { api, visibleMarkets, type FeedbackDecision, type Market, type PerfStats, type PortfolioSeries, type PortfolioSummary, type ReportRef } from "../api";
import { cssVar, fix, MARKET_LABEL, marketColor, pct, signed, useAsync, useThemeVersion } from "../lib";
import { t, tf, joinList } from "../i18n";

// 组合台（Spec 第 14.13 节；BR-03、14、32、40）：授权组合、多币种回测、汇率拆分、Brinson 归因、反馈闭环、研究报告

const ROWS: [keyof PerfStats, string, boolean][] = [
  ["return_ann", t("年化收益"), false], ["vol", t("年化波动率"), false], ["sharpe", t("夏普比率"), true], ["max_drawdown", t("最大回撤"), false],
  ["bench_return_ann", t("基准年化收益"), false], ["excess_ann", t("年化超额"), false], ["tracking_error", t("跟踪误差"), false], ["info_ratio", t("信息比率"), true],
];
const ACTIONS: [string, string][] = [["keep", t("保留")], ["research", t("进一步研究")], ["restrict", t("限定地区（暂停该市场）")], ["recalibrate", t("重新校准")], ["retire", t("停用")]];
const ACTION_TONE: Record<string, "good" | "warn" | "bad" | "accent" | "muted"> = { keep: "good", research: "accent", restrict: "warn", recalibrate: "warn", retire: "bad" };
const v = (x: number | null | undefined, ratio: boolean) => (ratio ? fix(x, 2) : pct(x, 2));

function NavChart({ s, base }: { s: PortfolioSeries; base: string }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line"), accent = cssVar("--accent");
    return {
      animationDuration: 700,
      legend: { top: 0, right: 8, textStyle: { color: muted }, data: [t("授权组合"), t("复合基准")] },
      grid: { left: 48, right: 16, top: 30, bottom: 28 },
      tooltip: { trigger: "axis", valueFormatter: (x: number) => x.toFixed(3) },
      xAxis: { type: "category", data: s.dates, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: line, opacity: 0.35 } } },
      series: [
        { name: t("授权组合"), type: "line", data: s.port, showSymbol: false, lineStyle: { width: 2, color: accent }, itemStyle: { color: accent } },
        { name: t("复合基准"), type: "line", data: s.bench, showSymbol: false, lineStyle: { width: 1.5, type: "dashed", color: muted }, itemStyle: { color: muted } },
      ],
      title: { text: tf("净值（{b}）", { b: base }), left: 0, top: 0, textStyle: { color: muted, fontSize: 12, fontWeight: "normal" } },
    };
  }, [s, base, theme]);
  return <EChart option={option} notMerge className="h-[300px]" />;
}

function FeedbackBlock({ pf, decisions, onSave }: { pf: PortfolioSummary; decisions: FeedbackDecision[];
  onSave: (m: Market, action: string, note: string) => Promise<void> }) {
  const [draft, setDraft] = useState<Record<string, { action: string; note: string }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <div className="flex flex-col divide-y divide-line">
      {pf.feedback.map((s) => {
        const last = [...decisions].reverse().find((d) => d.market === s.market);
        const d = draft[s.market] ?? { action: s.action, note: "" };
        return (
          <div key={s.market} className="py-3 grid gap-3 lg:grid-cols-[9rem_minmax(0,1fr)_minmax(0,22rem)]">
            <div className="flex flex-col gap-1">
              <span className="font-medium" style={{ color: marketColor(s.market) }}>{MARKET_LABEL[s.market]}</span>
              <span className="num text-xs text-faint">{s.signal.run_id ?? "—"}{s.signal.loop != null ? tf(" · 第 {n} 轮", { n: s.signal.loop }) : ""}</span>
            </div>
            <div className="text-sm">
              <div className="flex items-center gap-2"><span className="label">{t("平台建议")}</span><Badge tone={ACTION_TONE[s.action]}>{t(s.action_zh)}</Badge></div>
              <ul className="text-xs text-muted mt-1 flex flex-col gap-0.5">{s.reasons.map((r) => <li key={r}>· {r}</li>)}</ul>
              {last && <div className="text-xs mt-1.5">{t("研究员决定：")}<Badge tone={ACTION_TONE[last.action]}>{t(last.action_zh)}</Badge>
                <span className="text-faint ml-1">{last.by} · {last.at.slice(0, 16).replace("T", " ")}{last.note ? ` · ${last.note}` : ""}</span></div>}
            </div>
            <div className="flex flex-col gap-2">
              <select id={`fb-action-${s.market}`} className="field" value={d.action} aria-label={tf("{m} 的决定", { m: MARKET_LABEL[s.market] })}
                onChange={(e) => setDraft((x) => ({ ...x, [s.market]: { ...d, action: e.target.value } }))}>
                {ACTIONS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
              <input id={`fb-note-${s.market}`} className="field" placeholder={t("说明（可选）")} value={d.note}
                onChange={(e) => setDraft((x) => ({ ...x, [s.market]: { ...d, note: e.target.value } }))} />
              <button className="btn self-start" disabled={busy === s.market} onClick={async () => {
                setBusy(s.market); try { await onSave(s.market, d.action, d.note); } finally { setBusy(null); }
              }}>{busy === s.market ? t("记录中…") : t("记录决定")}</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// 调仓明细（PORT-04 运营视角）：相邻调仓的逐股持仓变化（买 / 卖，市场内权重）+ 执行参数
function RebalancePanel({ id, version, builtAt, markets }: { id: string; version: number; builtAt: string; markets: Market[] }) {
  const [sel, setSel] = useState<string>("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const { data, error, loading } = useAsync(() => api.portfolioRebalances(id, version), [id, version, builtAt]);
  if (loading) return <Panel eyebrow={t("PORT-04：相邻调仓的逐股持仓变化")} title={t("调仓明细")}><Loading what={t("调仓明细")} /></Panel>;
  if (error || !data || data.rebalances.length === 0)
    return <Panel eyebrow={t("PORT-04：相邻调仓的逐股持仓变化")} title={t("调仓明细")}>{error ? <ErrorBox error={error} /> : <p className="text-sm text-muted">{t("无调仓记录")}</p>}</Panel>;
  const list = data.rebalances;
  const cur = list.find((r) => r.date === sel) ?? list[list.length - 1];
  const N = 12;
  return (
    <Panel eyebrow={t("PORT-04：相邻调仓的逐股持仓变化（市场内权重）")} title={t("调仓明细")}>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <label htmlFor="rebal-date" className="label">{t("调仓日")}</label>
        <select id="rebal-date" className="field w-auto" value={cur.date} aria-label={t("选择调仓日")}
          onChange={(e) => { setSel(e.target.value); setOpen({}); }}>
          {[...list].reverse().map((r) => <option key={r.date} value={r.date}>{r.date}{r.prev_date ? "" : t("（建仓）")}</option>)}
        </select>
        <span className="text-xs text-faint">{cur.prev_date ? tf("对比上一调仓日 {d}", { d: cur.prev_date }) : t("首次建仓，全部为买入")}</span>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        {markets.filter((m) => cur.markets[m]).map((m) => {
          const mk = cur.markets[m];
          const e = mk.exec;
          const shown = open[m] ? mk.changes : mk.changes.slice(0, N);
          return (
            <div key={m} className="rounded border border-line p-3">
              <div className="flex items-baseline justify-between">
                <span className="font-medium" style={{ color: marketColor(m) }}>{MARKET_LABEL[m]}</span>
                <span className="text-xs text-faint">{tf("买 {b} · 卖 {s} · 持有 {h}", { b: mk.n_buy, s: mk.n_sell, h: mk.n_hold })}</span>
              </div>
              <div className="text-xs text-muted mt-1">
                {e ? tf("换手 {t}{sc} · 成本 {c} · α {a} · 事前TE {te}{ok} · 持仓 {h} 只（市场权重 {w}）", {
                  t: pct(e.turnover_target, 1),
                  sc: e.turnover_scale != null && e.turnover_scale < 1 ? tf("（按 {s} 缩减）", { s: fix(e.turnover_scale, 2) }) : "",
                  c: pct(e.cost, 3), a: fix(e.alpha, 2),
                  te: pct(e.te_ex_ante, 2), ok: e.te_ok ? "" : t("（超限）"),
                  h: e.holdings ?? 0, w: pct(e.market_weight, 0),
                }) : t("执行参数仅保留最近数次")}
              </div>
              <table className="w-full text-xs num mt-2">
                <thead className="label text-left"><tr><th className="py-1">{t("股票")}</th><th>{t("动作")}</th><th className="text-right">{t("旧权重")}</th><th className="text-right">{t("新权重")}</th><th className="text-right">{t("变化")}</th></tr></thead>
                <tbody>
                  {shown.map((c) => (
                    <tr key={c.symbol} className="border-t border-line">
                      <td className="py-1 font-sans">{c.symbol}</td>
                      <td><Badge tone={c.action === "buy" ? "good" : "bad"}>{c.action === "buy" ? t("买入") : t("卖出")}</Badge></td>
                      <td className="text-right">{c.old_w == null ? "—" : pct(c.old_w, 2)}</td>
                      <td className="text-right">{c.new_w == null ? "—" : pct(c.new_w, 2)}</td>
                      <td className="text-right">{signed(c.delta, 2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {mk.changes.length > N && (
                <button className="text-xs text-accent mt-2" onClick={() => setOpen((o) => ({ ...o, [m]: !o[m] }))}>
                  {open[m] ? t("收起") : tf("显示全部 {n} 笔", { n: mk.changes.length })}
                </button>
              )}
            </div>
          );
        })}
      </div>
      <p className="text-xs text-faint mt-2">{t("权重为市场内目标权重；「买入 / 卖出」含新开仓、加仓 / 减仓与清仓，按权重变化从大到小排序。")}</p>
    </Panel>
  );
}

function Detail({ id, version }: { id: string; version: number }) {
  const [state, setState] = useState<{ summary?: PortfolioSummary | null; feedback: FeedbackDecision[]; error?: string; loading: boolean }>({ feedback: [], loading: true });
  const [series, setSeries] = useState<PortfolioSeries | null>(null);
  const [building, setBuilding] = useState(false);
  const [buildErr, setBuildErr] = useState<string | null>(null);
  const [report, setReport] = useState<{ busy: boolean; error?: string }>({ busy: false });
  const reports = useAsync(() => api.reports(id, version), [id, version]);

  useEffect(() => {
    let alive = true;
    setState({ feedback: [], loading: true }); setSeries(null);
    api.portfolio(id, version).then((r) => {
      if (!alive) return;
      setState({ summary: r.summary, feedback: r.feedback, loading: false });
      if (r.summary) api.portfolioSeries(id, version).then((s) => alive && setSeries(s), () => undefined);
    }, (e) => alive && setState({ feedback: [], error: (e as Error).message, loading: false }));
    return () => { alive = false; };
  }, [id, version]);

  async function build() {
    setBuilding(true);
    setBuildErr(null);
    try {
      const r = await api.buildPortfolio(id, version);
      setState({ summary: r.summary, feedback: r.feedback, loading: false });
      setSeries(await api.portfolioSeries(id, version));
    } catch (e) { setBuildErr((e as Error).message); } finally { setBuilding(false); }
  }
  async function makeReport() {
    setReport({ busy: true });
    try { const r: ReportRef = await api.makeReport(id, version); reports.reload(); window.open(r.url, "_blank", "noopener"); setReport({ busy: false }); }
    catch (e) { setReport({ busy: false, error: (e as Error).message }); }
  }

  if (state.loading) return <Loading what={t("组合")} />;
  const pf = state.summary;
  const actions = (
    <div className="flex flex-wrap items-center gap-2">
      <button className="btn btn-primary" disabled={building} onClick={build}>{building ? t("构建中…") : pf ? t("重新构建组合") : t("构建组合")}</button>
      <button className="btn" disabled={report.busy} onClick={makeReport}>{report.busy ? t("生成中…") : t("生成研究报告")}</button>
      {reports.data && reports.data.length > 0 && (
        <a className="text-xs text-accent" href={reports.data[0].url} target="_blank" rel="noopener">{tf("最近一份：{d} · {k} KB", { d: reports.data[0].name.slice(-19, -5), k: Math.round(reports.data[0].bytes / 1024) })}</a>
      )}
    </div>
  );
  return (
    <div className="flex flex-col gap-5">
      {actions}
      {state.error && <ErrorBox error={state.error} />}
      {buildErr && (
        <Notice tone="accent" title={t("还不能构建组合")}>
          {buildErr}{t("。可切换上方「研究项目 / 版本」到有采纳轮次的版本，或先在 04 运行中完成挖掘。")}
        </Notice>
      )}
      {report.error && <ErrorBox error={report.error} />}
      {!pf ? <Notice tone="accent" title={t("尚未构建授权组合")}>{t("按授权条款与规则包，用各市场采纳轮次的打分构建月度只做多组合并回测（约数秒）。")}</Notice> : (
        <>
          <div className="panel p-4 grid gap-4 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label={tf("年化收益（{c}）", { c: pf.base_currency })} value={pct(pf.metrics.total.return_ann, 2)} sub={tf("基准 {x}", { x: pct(pf.metrics.total.bench_return_ann, 2) })} />
            <Stat label={t("年化超额")} value={signed(pf.metrics.total.excess_ann, 2)} sub={tf("区间 {x}", { x: signed(pf.attribution.total_excess, 2) })} />
            <Stat label={t("跟踪误差")} value={pct(pf.metrics.total.tracking_error, 2)} sub={tf("上限 {x}", { x: pct(pf.terms.max_tracking_error, 0) })} />
            <Stat label={t("信息比率")} value={fix(pf.metrics.total.info_ratio, 2)} sub={tf("夏普 {x}", { x: fix(pf.metrics.total.sharpe, 2) })} />
            <Stat label={t("最大回撤")} value={pct(pf.metrics.total.max_drawdown, 1)} sub={tf("平均单次换手 {x}", { x: pct(pf.metrics.avg_turnover, 1) })} />
            <Stat label={t("区间")} value={tf("{n} 天", { n: pf.period.days })} sub={tf("{a} 至 {b} · {r} 次调仓", { a: pf.period.start, b: pf.period.end, r: pf.period.rebalances })} />
          </div>
          <div className="text-xs text-muted">
            {tf("授权：{m} · 基础币种 {b} · 报告币种 {r} · 基准 {k} · 构建于 {d} UTC", { m: pf.mandate_name, b: pf.base_currency, r: pf.report_currency, k: pf.benchmark.label, d: pf.built_at.slice(0, 16).replace("T", " ") })}
          </div>
          <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
            <Panel eyebrow={t("基础币种净值，月度调仓")} title={t("组合与复合基准")}>
              {series ? <NavChart s={series} base={pf.base_currency} /> : <Loading what={t("净值")} />}
              <p className="text-xs text-faint mt-1">{pf.backtest_note}</p>
            </Panel>
            <Panel eyebrow={t("PORT-07：基础币种收益 =（1 + 本地收益）×（1 + 汇率收益）− 1")} title={t("汇率拆分（区间累计）")}>
              <table className="w-full text-sm num">
                <thead className="label text-left"><tr><th className="py-1">{t("市场")}</th><th>{t("本地收益")}</th><th>{t("汇率收益")}</th><th>{t("交叉项")}</th><th>{tf("{c} 收益", { c: pf.base_currency })}</th></tr></thead>
                <tbody>
                  {pf.markets.map((m) => { const f = pf.fx_split[m]; return (
                    <tr key={m} className="border-t border-line"><td className="py-1.5" style={{ color: marketColor(m) }}>{MARKET_LABEL[m]}</td>
                      <td>{signed(f.local, 2)}</td><td>{signed(f.fx, 2)}</td><td>{signed(f.cross, 2)}</td><td>{signed(f.base, 2)}</td></tr>); })}
                </tbody>
              </table>
              <p className="text-xs text-faint mt-2">{tf("逐日恒等误差最大 {x}。", { x: Math.max(...pf.markets.map((m) => pf.fx_split[m].identity_max_error)).toExponential(1) })}
                {tf("组合的汇率贡献（绝对）：{l}", { l: joinList(pf.markets.map((m) => `${MARKET_LABEL[m]} ${signed(pf.attribution.fx_contribution_abs[m], 2)}`)) })}</p>
            </Panel>
          </div>
          <Panel eyebrow={t("PORT-09：全组合（基础币种，相对复合基准）与每个市场（本地币种 / 基础币种，相对本地指数）")} title={t("业绩指标")}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm num">
                <thead className="label text-left">
                  <tr><th className="py-1 pr-3">{t("指标")}</th><th className="pr-3">{t("全组合")}</th>
                    {pf.markets.map((m) => <th key={m} className="pr-3">{tf("{m}（本地）", { m: MARKET_LABEL[m] })}</th>)}
                    {pf.markets.map((m) => <th key={`b${m}`} className="pr-3">{tf("{m}（{c}）", { m: MARKET_LABEL[m], c: pf.base_currency })}</th>)}</tr>
                </thead>
                <tbody>
                  {ROWS.map(([k, label, ratio]) => (
                    <tr key={k} className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{label}</td><td className="pr-3">{v(pf.metrics.total[k] as number, ratio)}</td>
                      {pf.markets.map((m) => <td key={m} className="pr-3">{v(pf.metrics.markets[m].local[k] as number, ratio)}</td>)}
                      {pf.markets.map((m) => <td key={`b${m}`} className="pr-3">{v(pf.metrics.markets[m].base[k] as number, ratio)}</td>)}</tr>
                  ))}
                  <tr className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{t("平均权重 / 年化成本")}</td><td className="pr-3">—</td>
                    {pf.markets.map((m) => <td key={m} className="pr-3">{pct(pf.metrics.markets[m].avg_weight, 0)} / {pct(pf.metrics.markets[m].cost_ann, 2)}</td>)}
                    {pf.markets.map((m) => <td key={`b${m}`} />)}</tr>
                </tbody>
              </table>
            </div>
          </Panel>
          <RebalancePanel id={id} version={version} builtAt={pf.built_at} markets={pf.markets} />
          <div className="grid gap-5 xl:grid-cols-2">
            <Panel eyebrow={pf.attribution.method} title={t("Brinson 归因")}>
              <table className="w-full text-sm num">
                <thead className="label text-left"><tr><th className="py-1">{t("市场")}</th><th>{t("配置效应")}</th><th>{t("选股效应（本地）")}</th><th>{t("汇率与交互")}</th><th>{t("合计")}</th></tr></thead>
                <tbody>
                  {pf.markets.map((m) => { const a = pf.attribution.markets[m]; return (
                    <tr key={m} className="border-t border-line"><td className="py-1.5" style={{ color: marketColor(m) }}>{MARKET_LABEL[m]}</td>
                      <td>{signed(a.allocation, 2)}</td><td>{signed(a.selection, 2)}</td><td>{signed(a.fx_interaction, 2)}</td>
                      <td>{signed(a.allocation + a.selection + a.fx_interaction, 2)}</td></tr>); })}
                  <tr className="border-t border-line font-semibold"><td className="py-1.5">{t("合计")}</td><td>{signed(pf.attribution.by_effect.allocation, 2)}</td>
                    <td>{signed(pf.attribution.by_effect.selection, 2)}</td><td>{signed(pf.attribution.by_effect.fx_interaction, 2)}</td><td>{signed(pf.attribution.sum_of_effects, 2)}</td></tr>
                </tbody>
              </table>
              <p className="text-xs text-faint mt-2">{tf("组合相对基准的区间超额 {e}，各项之和与之相差 {d}（PORT-08 要求 < 1bp）。", { e: signed(pf.attribution.total_excess, 2), d: pf.attribution.linking_error.toExponential(1) })}</p>
            </Panel>
            <Panel eyebrow={t("PORT-02：每个约束注明来源条款或规则编号")} title={t("约束与执行情况")}>
              <ul className="flex flex-col gap-2 text-sm">
                {pf.constraints.map((c) => (
                  <li key={c.name} className="grid grid-cols-[9rem_minmax(0,1fr)] gap-2">
                    <span className="font-medium">{c.name}</span>
                    <span><span>{c.value}</span><span className="block text-xs text-faint">{c.source} · {c.status}</span></span>
                  </li>
                ))}
              </ul>
            </Panel>
          </div>
          <Panel eyebrow={t("BR-32：组合结果与归因反馈到各市场的信号；平台建议，研究员决定")} title={t("反馈闭环")}>
            <FeedbackBlock pf={pf} decisions={state.feedback} onSave={async (m, action, note) => {
              const fb = await api.portfolioFeedback(id, version, { market: m, action, note });
              setState((s) => ({ ...s, feedback: fb }));
            }} />
          </Panel>
        </>
      )}
    </div>
  );
}

// 互补组合（第 14.19 节；BR-28）：同一市场多个采纳信号的冗余与合成，前半段定权重、后半段（样本外）评估
function ComplementPanel() {
  const [m, setM] = useState<Market>(visibleMarkets()[0] ?? "hk");
  const [keys, setKeys] = useState<string[] | null>(null);
  const { data, error, loading } = useAsync(() => api.complement(m, keys ?? undefined), [m, (keys ?? []).join(",")]);
  const st = (x?: { rank_ic: number | null; ir: number | null; t: number | null }) =>
    x ? <span className="num">{fix(x.rank_ic, 4)} · IR {fix(x.ir, 2)} · t {fix(x.t, 2)}</span> : "—";
  const toggle = (k: string, all: string[]) => {
    const cur = keys ?? all;
    const next = cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k];
    setKeys(next.length >= 2 ? next : cur);
  };
  const span = (xs: string[]) => (xs.length === 2 ? tf("{a} 至 {b}", { a: xs[0], b: xs[1] }) : xs.join(" – "));
  return (
    <Panel eyebrow={t("同一市场多个采纳信号 · 前半段定权重、后半段评估")} title={t("互补组合")}>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <Seg label={t("市场")} value={m} options={visibleMarkets().map((x) => ({ value: x, label: MARKET_LABEL[x] }))} onChange={(x) => { setM(x); setKeys(null); }} />
        {data?.price_source?.simulated && <Badge tone="warn">{t("模拟数据，仅用于流程演示")}</Badge>}
      </div>
      {loading ? <Loading what={t("互补组合")} /> : error || !data ? <ErrorBox error={error ?? t("无数据")} /> : (
        <div className="flex flex-col gap-3 text-sm">
          <div className="flex flex-wrap gap-2 text-xs">
            {data.candidates.map((c) => {
              const all = data.candidates.map((x) => x.key);
              const on = (keys ?? all).includes(c.key);
              return <label key={c.key} className="flex items-center gap-1"><input id={`cmp-${c.key}`} type="checkbox" checked={on} onChange={() => toggle(c.key, all)} />
                <span className={on && !c.excluded ? "" : "text-faint"}>{c.title ?? c.key}{c.kind === "text" ? tf("（{k}，文本）", { k: c.key }) : tf("（{k}）", { k: c.key })}{c.excluded && tf(" · 未参与：{e}", { e: c.excluded })}</span></label>;
            })}
          </div>
          {!data.ok ? <p className="text-muted">{data.reason}</p> : (
            <>
              <p><Badge tone={data.verdict?.startsWith("互补") ? "good" : "muted"}>{t(data.verdict ?? "")}</Badge></p>
              <div className="text-xs text-faint">{tf("定权重 {a}；评估（样本外）{b}；预测周期 {h} 日", { a: span(data.period!.fit), b: span(data.period!.eval), h: data.horizon ?? 0 })}</div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-muted"><tr>
                    <th className="text-left font-normal py-1 pr-2">{t("信号")}</th><th className="text-right font-normal pr-2">{t("权重")}</th>
                    <th className="text-left font-normal pr-2">{t("前半段 rank IC")}</th><th className="text-left font-normal pr-2">{t("后半段 rank IC（样本外）")}</th>
                  </tr></thead>
                  <tbody>
                    {data.signals!.map((x) => (
                      <tr key={x.key} className="border-t border-line align-top">
                        <td className="py-1 pr-2">{x.title}<div className="text-faint">{x.key} · {joinList(x.factors.slice(0, 2))}{x.factors.length > 2 ? t(" 等") : ""}{x.key === data.best_single ? t(" · 最好的单个信号") : ""}</div></td>
                        <td className="pr-2 text-right num">{pct(x.weight, 0)}</td>
                        <td className="pr-2">{st(x.first)}</td><td className="pr-2">{st(x.second)}</td>
                      </tr>
                    ))}
                    <tr className="border-t border-line-2 font-medium"><td className="py-1 pr-2">{t("合成（按权重）")}</td><td /><td /><td className="pr-2">{st(data.combined!.optimal.second)}</td></tr>
                    <tr className="border-t border-line"><td className="py-1 pr-2">{t("合成（等权对照）")}</td><td /><td /><td className="pr-2">{st(data.combined!.equal.second)}</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="text-xs">
                <div className="label mb-1">{t("冗余：打分的平均截面相关")}</div>
                {Object.entries(data.score_corr ?? {}).map(([k, c]) => <div key={k} className="num">{k.replace("|", " × ")}{t("：")}{fix(c, 2)}</div>)}
              </div>
              <p className="text-xs text-faint">{data.note}{t("；IR 为逐日 rank IC 的年化信息比率（相邻日期的未来收益有重叠，只作相对比较）")}</p>
            </>
          )}
        </div>
      )}
    </Panel>
  );
}

export default function PortfolioPage() {
  const pick = useVersionPick();
  if (pick.loading) return <Loading what={t("研究项目")} />;
  if (pick.error) return <ErrorBox error={pick.error} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("组合台")}</div>
          <h1 className="font-display text-xl font-semibold">{t("授权组合、汇率拆分与归因")}</h1>
        </div>
        <span className="text-sm text-muted">{t("多币种回测 · Brinson 归因 · 反馈闭环 · 中文主报告与英 / 法文证据附件")}</span>
      </div>
      {pick.list.length === 0 ? <Notice tone="accent" title={t("还没有锁定的研究版本")}>{t("在“研究立项”起草并锁定、完成运行后，这里构建授权组合。")}</Notice> : (
        <>
          <VersionPicker pick={pick} />
          {pick.id && pick.version > 0 && <Detail key={`${pick.id}-${pick.version}`} id={pick.id} version={pick.version} />}
          <ComplementPanel />
        </>
      )}
    </div>
  );
}
