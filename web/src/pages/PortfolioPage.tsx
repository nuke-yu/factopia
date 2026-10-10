import { useEffect, useMemo, useState } from "react";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import EChart from "../components/EChart";
import VersionPicker, { useVersionPick } from "../components/VersionPicker";
import { api, visibleMarkets, type FeedbackDecision, type Market, type PerfStats, type PortfolioSeries, type PortfolioSummary, type ReportRef } from "../api";
import { cssVar, fix, MARKET_LABEL, marketColor, pct, signed, useAsync, useThemeVersion } from "../lib";

// 组合台（Spec 第 14.13 节；BR-03、14、32、40）：授权组合、多币种回测、汇率拆分、Brinson 归因、反馈闭环、研究报告

const ROWS: [keyof PerfStats, string, boolean][] = [
  ["return_ann", "年化收益", false], ["vol", "年化波动率", false], ["sharpe", "夏普比率", true], ["max_drawdown", "最大回撤", false],
  ["bench_return_ann", "基准年化收益", false], ["excess_ann", "年化超额", false], ["tracking_error", "跟踪误差", false], ["info_ratio", "信息比率", true],
];
const ACTIONS: [string, string][] = [["keep", "保留"], ["research", "进一步研究"], ["restrict", "限定地区（暂停该市场）"], ["recalibrate", "重新校准"], ["retire", "停用"]];
const ACTION_TONE: Record<string, "good" | "warn" | "bad" | "accent" | "muted"> = { keep: "good", research: "accent", restrict: "warn", recalibrate: "warn", retire: "bad" };
const v = (x: number | null | undefined, ratio: boolean) => (ratio ? fix(x, 2) : pct(x, 2));

function NavChart({ s, base }: { s: PortfolioSeries; base: string }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line"), accent = cssVar("--accent");
    return {
      animationDuration: 700,
      legend: { top: 0, right: 8, textStyle: { color: muted }, data: ["授权组合", "复合基准"] },
      grid: { left: 48, right: 16, top: 30, bottom: 28 },
      tooltip: { trigger: "axis", valueFormatter: (x: number) => x.toFixed(3) },
      xAxis: { type: "category", data: s.dates, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: line, opacity: 0.35 } } },
      series: [
        { name: "授权组合", type: "line", data: s.port, showSymbol: false, lineStyle: { width: 2, color: accent }, itemStyle: { color: accent } },
        { name: "复合基准", type: "line", data: s.bench, showSymbol: false, lineStyle: { width: 1.5, type: "dashed", color: muted }, itemStyle: { color: muted } },
      ],
      title: { text: `净值（${base}）`, left: 0, top: 0, textStyle: { color: muted, fontSize: 12, fontWeight: "normal" } },
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
              <span className="num text-xs text-faint">{s.signal.run_id ?? "—"}{s.signal.loop != null ? ` · 第 ${s.signal.loop} 轮` : ""}</span>
            </div>
            <div className="text-sm">
              <div className="flex items-center gap-2"><span className="label">平台建议</span><Badge tone={ACTION_TONE[s.action]}>{s.action_zh}</Badge></div>
              <ul className="text-xs text-muted mt-1 flex flex-col gap-0.5">{s.reasons.map((r) => <li key={r}>· {r}</li>)}</ul>
              {last && <div className="text-xs mt-1.5">研究员决定：<Badge tone={ACTION_TONE[last.action]}>{last.action_zh}</Badge>
                <span className="text-faint ml-1">{last.by} · {last.at.slice(0, 16).replace("T", " ")}{last.note ? ` · ${last.note}` : ""}</span></div>}
            </div>
            <div className="flex flex-col gap-2">
              <select id={`fb-action-${s.market}`} className="field" value={d.action} aria-label={`${MARKET_LABEL[s.market]}的决定`}
                onChange={(e) => setDraft((x) => ({ ...x, [s.market]: { ...d, action: e.target.value } }))}>
                {ACTIONS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
              <input id={`fb-note-${s.market}`} className="field" placeholder="说明（可选）" value={d.note}
                onChange={(e) => setDraft((x) => ({ ...x, [s.market]: { ...d, note: e.target.value } }))} />
              <button className="btn self-start" disabled={busy === s.market} onClick={async () => {
                setBusy(s.market); try { await onSave(s.market, d.action, d.note); } finally { setBusy(null); }
              }}>{busy === s.market ? "记录中…" : "记录决定"}</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Detail({ id, version }: { id: string; version: number }) {
  const [state, setState] = useState<{ summary?: PortfolioSummary | null; feedback: FeedbackDecision[]; error?: string; loading: boolean }>({ feedback: [], loading: true });
  const [series, setSeries] = useState<PortfolioSeries | null>(null);
  const [building, setBuilding] = useState(false);
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
    try {
      const r = await api.buildPortfolio(id, version);
      setState({ summary: r.summary, feedback: r.feedback, loading: false });
      setSeries(await api.portfolioSeries(id, version));
    } catch (e) { setState((s) => ({ ...s, error: (e as Error).message })); } finally { setBuilding(false); }
  }
  async function makeReport() {
    setReport({ busy: true });
    try { const r: ReportRef = await api.makeReport(id, version); reports.reload(); window.open(r.url, "_blank", "noopener"); setReport({ busy: false }); }
    catch (e) { setReport({ busy: false, error: (e as Error).message }); }
  }

  if (state.loading) return <Loading what="组合" />;
  const pf = state.summary;
  const actions = (
    <div className="flex flex-wrap items-center gap-2">
      <button className="btn btn-primary" disabled={building} onClick={build}>{building ? "构建中…" : pf ? "重新构建组合" : "构建组合"}</button>
      <button className="btn" disabled={report.busy} onClick={makeReport}>{report.busy ? "生成中…" : "生成研究报告"}</button>
      {reports.data && reports.data.length > 0 && (
        <a className="text-xs text-accent" href={reports.data[0].url} target="_blank" rel="noopener">最近一份：{reports.data[0].name.slice(-19, -5)} · {Math.round(reports.data[0].bytes / 1024)} KB</a>
      )}
    </div>
  );
  return (
    <div className="flex flex-col gap-5">
      {actions}
      {state.error && <ErrorBox error={state.error} />}
      {report.error && <ErrorBox error={report.error} />}
      {!pf ? <Notice tone="accent" title="尚未构建授权组合">按授权条款与规则包，用各市场采纳轮次的打分构建月度只做多组合并回测（约数秒）。</Notice> : (
        <>
          <div className="panel p-4 grid gap-4 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label={`年化收益（${pf.base_currency}）`} value={pct(pf.metrics.total.return_ann, 2)} sub={`基准 ${pct(pf.metrics.total.bench_return_ann, 2)}`} />
            <Stat label="年化超额" value={signed(pf.metrics.total.excess_ann, 2)} sub={`区间 ${signed(pf.attribution.total_excess, 2)}`} />
            <Stat label="跟踪误差" value={pct(pf.metrics.total.tracking_error, 2)} sub={`上限 ${pct(pf.terms.max_tracking_error, 0)}`} />
            <Stat label="信息比率" value={fix(pf.metrics.total.info_ratio, 2)} sub={`夏普 ${fix(pf.metrics.total.sharpe, 2)}`} />
            <Stat label="最大回撤" value={pct(pf.metrics.total.max_drawdown, 1)} sub={`平均单次换手 ${pct(pf.metrics.avg_turnover, 1)}`} />
            <Stat label="区间" value={`${pf.period.days} 天`} sub={`${pf.period.start} 至 ${pf.period.end} · ${pf.period.rebalances} 次调仓`} />
          </div>
          <div className="text-xs text-muted">
            授权：{pf.mandate_name} · 基础币种 {pf.base_currency} · 报告币种 {pf.report_currency} · 基准 {pf.benchmark.label} · 构建于 {pf.built_at.slice(0, 16).replace("T", " ")} UTC
          </div>
          <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
            <Panel eyebrow="基础币种净值，月度调仓" title="组合与复合基准">
              {series ? <NavChart s={series} base={pf.base_currency} /> : <Loading what="净值" />}
              <p className="text-xs text-faint mt-1">{pf.backtest_note}</p>
            </Panel>
            <Panel eyebrow="PORT-07：基础币种收益 =（1 + 本地收益）×（1 + 汇率收益）− 1" title="汇率拆分（区间累计）">
              <table className="w-full text-sm num">
                <thead className="label text-left"><tr><th className="py-1">市场</th><th>本地收益</th><th>汇率收益</th><th>交叉项</th><th>{pf.base_currency} 收益</th></tr></thead>
                <tbody>
                  {pf.markets.map((m) => { const f = pf.fx_split[m]; return (
                    <tr key={m} className="border-t border-line"><td className="py-1.5" style={{ color: marketColor(m) }}>{MARKET_LABEL[m]}</td>
                      <td>{signed(f.local, 2)}</td><td>{signed(f.fx, 2)}</td><td>{signed(f.cross, 2)}</td><td>{signed(f.base, 2)}</td></tr>); })}
                </tbody>
              </table>
              <p className="text-xs text-faint mt-2">逐日恒等误差最大 {Math.max(...pf.markets.map((m) => pf.fx_split[m].identity_max_error)).toExponential(1)}。
                组合的汇率贡献（绝对）：{pf.markets.map((m) => `${MARKET_LABEL[m]} ${signed(pf.attribution.fx_contribution_abs[m], 2)}`).join("、")}</p>
            </Panel>
          </div>
          <Panel eyebrow="PORT-09：全组合（基础币种，相对复合基准）与每个市场（本地币种 / 基础币种，相对本地指数）" title="业绩指标">
            <div className="overflow-x-auto">
              <table className="w-full text-sm num">
                <thead className="label text-left">
                  <tr><th className="py-1 pr-3">指标</th><th className="pr-3">全组合</th>
                    {pf.markets.map((m) => <th key={m} className="pr-3">{MARKET_LABEL[m]}（本地）</th>)}
                    {pf.markets.map((m) => <th key={`b${m}`} className="pr-3">{MARKET_LABEL[m]}（{pf.base_currency}）</th>)}</tr>
                </thead>
                <tbody>
                  {ROWS.map(([k, label, ratio]) => (
                    <tr key={k} className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{label}</td><td className="pr-3">{v(pf.metrics.total[k] as number, ratio)}</td>
                      {pf.markets.map((m) => <td key={m} className="pr-3">{v(pf.metrics.markets[m].local[k] as number, ratio)}</td>)}
                      {pf.markets.map((m) => <td key={`b${m}`} className="pr-3">{v(pf.metrics.markets[m].base[k] as number, ratio)}</td>)}</tr>
                  ))}
                  <tr className="border-t border-line"><td className="py-1.5 pr-3 font-sans">平均权重 / 年化成本</td><td className="pr-3">—</td>
                    {pf.markets.map((m) => <td key={m} className="pr-3">{pct(pf.metrics.markets[m].avg_weight, 0)} / {pct(pf.metrics.markets[m].cost_ann, 2)}</td>)}
                    {pf.markets.map((m) => <td key={`b${m}`} />)}</tr>
                </tbody>
              </table>
            </div>
          </Panel>
          <div className="grid gap-5 xl:grid-cols-2">
            <Panel eyebrow={pf.attribution.method} title="Brinson 归因">
              <table className="w-full text-sm num">
                <thead className="label text-left"><tr><th className="py-1">市场</th><th>配置效应</th><th>选股效应（本地）</th><th>汇率与交互</th><th>合计</th></tr></thead>
                <tbody>
                  {pf.markets.map((m) => { const a = pf.attribution.markets[m]; return (
                    <tr key={m} className="border-t border-line"><td className="py-1.5" style={{ color: marketColor(m) }}>{MARKET_LABEL[m]}</td>
                      <td>{signed(a.allocation, 2)}</td><td>{signed(a.selection, 2)}</td><td>{signed(a.fx_interaction, 2)}</td>
                      <td>{signed(a.allocation + a.selection + a.fx_interaction, 2)}</td></tr>); })}
                  <tr className="border-t border-line font-semibold"><td className="py-1.5">合计</td><td>{signed(pf.attribution.by_effect.allocation, 2)}</td>
                    <td>{signed(pf.attribution.by_effect.selection, 2)}</td><td>{signed(pf.attribution.by_effect.fx_interaction, 2)}</td><td>{signed(pf.attribution.sum_of_effects, 2)}</td></tr>
                </tbody>
              </table>
              <p className="text-xs text-faint mt-2">组合相对基准的区间超额 {signed(pf.attribution.total_excess, 2)}，各项之和与之相差 {pf.attribution.linking_error.toExponential(1)}（PORT-08 要求 &lt; 1bp）。</p>
            </Panel>
            <Panel eyebrow="PORT-02：每个约束注明来源条款或规则编号" title="约束与执行情况">
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
          <Panel eyebrow="BR-32：组合结果与归因反馈到各市场的信号；平台建议，研究员决定" title="反馈闭环">
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
  return (
    <Panel eyebrow="同一市场多个采纳信号 · 前半段定权重、后半段评估" title="互补组合">
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <Seg label="市场" value={m} options={visibleMarkets().map((x) => ({ value: x, label: MARKET_LABEL[x] }))} onChange={(x) => { setM(x); setKeys(null); }} />
        {data?.price_source?.simulated && <Badge tone="warn">模拟数据，仅用于流程演示</Badge>}
      </div>
      {loading ? <Loading what="互补组合" /> : error || !data ? <ErrorBox error={error ?? "无数据"} /> : (
        <div className="flex flex-col gap-3 text-sm">
          <div className="flex flex-wrap gap-2 text-xs">
            {data.candidates.map((c) => {
              const all = data.candidates.map((x) => x.key);
              const on = (keys ?? all).includes(c.key);
              return <label key={c.key} className="flex items-center gap-1"><input id={`cmp-${c.key}`} type="checkbox" checked={on} onChange={() => toggle(c.key, all)} />
                <span className={on && !c.excluded ? "" : "text-faint"}>{c.title ?? c.key}（{c.key}{c.kind === "text" ? "，文本" : ""}）{c.excluded && ` · 未参与：${c.excluded}`}</span></label>;
            })}
          </div>
          {!data.ok ? <p className="text-muted">{data.reason}</p> : (
            <>
              <p><Badge tone={data.verdict?.startsWith("互补") ? "good" : "muted"}>{data.verdict}</Badge></p>
              <div className="text-xs text-faint">定权重 {data.period!.fit.join(" 至 ")}；评估（样本外）{data.period!.eval.join(" 至 ")}；预测周期 {data.horizon} 日</div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-muted"><tr>
                    <th className="text-left font-normal py-1 pr-2">信号</th><th className="text-right font-normal pr-2">权重</th>
                    <th className="text-left font-normal pr-2">前半段 rank IC</th><th className="text-left font-normal pr-2">后半段 rank IC（样本外）</th>
                  </tr></thead>
                  <tbody>
                    {data.signals!.map((x) => (
                      <tr key={x.key} className="border-t border-line align-top">
                        <td className="py-1 pr-2">{x.title}<div className="text-faint">{x.key} · {x.factors.slice(0, 2).join("、")}{x.factors.length > 2 ? " 等" : ""}{x.key === data.best_single ? " · 最好的单个信号" : ""}</div></td>
                        <td className="pr-2 text-right num">{pct(x.weight, 0)}</td>
                        <td className="pr-2">{st(x.first)}</td><td className="pr-2">{st(x.second)}</td>
                      </tr>
                    ))}
                    <tr className="border-t border-line-2 font-medium"><td className="py-1 pr-2">合成（按权重）</td><td /><td /><td className="pr-2">{st(data.combined!.optimal.second)}</td></tr>
                    <tr className="border-t border-line"><td className="py-1 pr-2">合成（等权对照）</td><td /><td /><td className="pr-2">{st(data.combined!.equal.second)}</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="text-xs">
                <div className="label mb-1">冗余：打分的平均截面相关</div>
                {Object.entries(data.score_corr ?? {}).map(([k, c]) => <div key={k} className="num">{k.replace("|", " × ")}：{fix(c, 2)}</div>)}
              </div>
              <p className="text-xs text-faint">{data.note}；IR 为逐日 rank IC 的年化信息比率（相邻日期的未来收益有重叠，只作相对比较）</p>
            </>
          )}
        </div>
      )}
    </Panel>
  );
}

export default function PortfolioPage() {
  const pick = useVersionPick();
  if (pick.loading) return <Loading what="研究项目" />;
  if (pick.error) return <ErrorBox error={pick.error} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">组合台</div>
          <h1 className="font-display text-xl font-semibold">授权组合、汇率拆分与归因</h1>
        </div>
        <span className="text-sm text-muted">多币种回测 · Brinson 归因 · 反馈闭环 · 中文主报告与英 / 法文证据附件</span>
      </div>
      {pick.list.length === 0 ? <Notice tone="accent" title="还没有锁定的研究版本">在“研究立项”起草并锁定、完成运行后，这里构建授权组合。</Notice> : (
        <>
          <VersionPicker pick={pick} />
          {pick.id && pick.version > 0 && <Detail key={`${pick.id}-${pick.version}`} id={pick.id} version={pick.version} />}
          <ComplementPanel />
        </>
      )}
    </div>
  );
}
