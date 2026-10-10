import { Link } from "react-router-dom";
import { useState } from "react";
import { api, type Check, type CredCheck, type Diagnostics, type Explanation, type Market, type Study, type StudyMarket } from "../api";
import { cssVar, fix, MARKET_LABEL, pct, signed, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Panel } from "./ui";
import StudyScatter from "./charts/StudyScatter";

const OVERALL_TONE = { rejected: "bad", passed: "good", pending: "accent", insufficient: "warn" } as const;
const FINDING_COLOR = { bad: "--bad", warn: "--warn", good: "--good" } as const;
const STATE_ZH: Record<string, string> = {
  queued: "排队中", running: "运行中", done: "已完成", failed: "失败", cancelled: "已停止", budget_stopped: "超出预算停止",
  incomplete: "未完成", none: "尚未运行",
};

/**
 * 研究结论（Spec 第 14.5 节）：同一锁定版本在各市场的结果，按运行前写好的可证伪测试判定。
 * 判定与观察由平台按导出的数据计算（factopia/research/verdict.py），不是 AI 撰写。
 */
export default function StudyPanel({ researchId, version, current }: { researchId: string; version: number; current?: string }) {
  const study = useAsync(() => api.study(researchId, version), [researchId, version]);
  if (study.error) return <ErrorBox error={study.error} />;
  if (!study.data) return <Loading what="研究结论" />;
  const s = study.data;
  const markets = s.rule.markets.map((m) => s.markets[m]).filter(Boolean) as StudyMarket[];
  const anyRounds = markets.some((e) => e.rounds.length > 0);
  const primaryCred = s.markets[s.rule.primary]?.focus?.credibility;
  const textStudy = markets.some((e) => e.kind === "text");

  return (
    <Panel eyebrow={`研究结论 · ${s.research_id} v${s.version} · ${s.rule.markets.map((m) => MARKET_LABEL[m]).join(" / ")}`}
      title="按事先写好的标准判定"
      actions={<>
        {primaryCred && primaryCred.state !== "unchecked" && <Badge tone={CRED_TONE[primaryCred.state]}>可信度{primaryCred.label}</Badge>}
        <Badge tone={OVERALL_TONE[s.overall.state]}>{s.overall.state === "passed" ? s.overall.label : `假设${s.overall.label}`}</Badge></>}>
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline gap-3">
              <span className="font-display text-3xl font-semibold whitespace-nowrap shrink-0" style={{ color: cssVar(`--${OVERALL_TONE[s.overall.state]}`) }}>
                {s.overall.label}
              </span>
              <span className="text-sm text-muted">{s.overall.reason}</span>
            </div>
            {primaryCred && primaryCred.state !== "unchecked" && (
              <p className="text-sm">
                <span className="text-muted">可信度检查（主市场）：</span>
                <span style={{ color: cssVar(`--${({ credible: "good", doubtful: "bad", pending: "accent", unchecked: "muted" } as const)[primaryCred.state]}`) }}>{primaryCred.label}</span>
                <span className="text-muted">
                  {primaryCred.state === "doubtful" && `——未通过：${primaryCred.checks.filter((c) => c.ok === false).map((c) => c.label.replace(/（.*?）/g, "")).join("、")}。即使按标准判定通过，也不能当作研究结论。`}
                  {primaryCred.state === "credible" && "——显著、在 AI 未见过的数据上延续、明显好于随机因子。"}
                </span>
              </p>
            )}
            <p className="text-sm leading-relaxed">{s.title && <span className="font-medium">{s.title}：</span>}{s.hypothesis}</p>
          </div>
          <div className="text-xs text-muted leading-relaxed flex flex-col gap-1.5">
            <div>
              <span className="text-text">判定方法：</span>主市场（{MARKET_LABEL[s.rule.primary]}）看{textStudy ? "按验证期判定采纳的轮次（文本信号研究）" : " RD-Agent 最终采纳的轮次"}，需同时满足
              rank IC {s.rule.ric_sign > 0 ? "为正" : "为负"}、扣成本年化超额高于起点、{textStudy ? "绝对回撤（扣成本）不比等权持有股票池差" : "最大回撤不比起点差"}；其他市场看采纳轮次的 rank IC 方向是否与假设一致。
              不在各轮中挑最好的一轮，避免多次尝试挑结果。原文中的其他条件（如 t 值门槛、回撤容忍幅度）平台尚未检验，需人工确认。
              {!s.rule.sign_from_text && " 原文没有写明 rank IC 方向，按为正判定。"}
            </div>
            <details>
              <summary className="cursor-pointer text-text">可证伪测试原文（运行前锁定，不可修改）</summary>
              <p className="mt-1 num whitespace-pre-wrap">{s.rule.text}</p>
            </details>
          </div>
        </div>

        <div className={`grid gap-3 ${markets.length >= 3 ? "md:grid-cols-3" : markets.length === 2 ? "md:grid-cols-2" : ""}`}>
          {markets.map((e) => <MarketCard key={e.market} e={e} current={current === e.run_id} />)}
        </div>

        <TransferBlock s={s} />
        <ExplainBlock s={s} onDone={study.reload} />
        <RulesLine s={s} />

        {anyRounds && (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
            <div>
              <div className="label mb-1">各轮的 rank IC（横轴）与扣成本年化超额（纵轴）</div>
              <StudyScatter study={s} />
              <p className="text-[11px] text-faint mt-1">
                每个点是一轮，大点为 RD-Agent 采纳的轮次，空心菱形为起点（20 个基础因子）。打分真正有效时，点应落在白色一侧的上方；
                落在红色区域上方的点，是"收益变好、排序能力却与假设相反"。
              </p>
            </div>
            <div>
              <div className="label mb-2">平台观察</div>
              <ul className="flex flex-col gap-2">
                {s.findings.map((f, i) => (
                  <li key={i} className="flex gap-2 text-sm leading-relaxed">
                    <span className="size-1.5 rounded-full mt-2 shrink-0" style={{ background: cssVar(FINDING_COLOR[f.tone]) }} />
                    <span className={f.tone === "bad" ? "text-text" : "text-muted"}>{f.text}</span>
                  </li>
                ))}
                {s.findings.length === 0 && <li className="text-sm text-faint">运行完成后显示。</li>}
              </ul>
              <p className="text-[11px] text-faint mt-3">以上判定与观察由平台按回测数据计算，不是 AI 撰写。测试期只有两年、每个市场只运行一次，结论需在 2024 年以后的数据上复核。</p>
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

function CheckRow({ c }: { c: Check }) {
  const mark = c.ok === true ? "✓" : c.ok === false ? "✗" : "—";
  const color = c.ok === true ? "--good" : c.ok === false ? "--bad" : "--muted";
  const val = c.key === "rank_ic" ? fix(c.value, 4) : `${pct(c.value)}（起点 ${pct(c.ref)}）`;
  return (
    <li className="flex items-baseline gap-2 text-xs">
      <span className="num w-3 text-center" style={{ color: cssVar(color) }}>{mark}</span>
      <span className="text-muted">{c.label}</span>
      <span className="num ml-auto">{val}</span>
    </li>
  );
}

const CRED_TONE = { unchecked: "muted", pending: "accent", credible: "good", doubtful: "bad" } as const;

function credValue(c: CredCheck): string {
  const v = c.value;
  if (v == null) return "—";
  if (c.key === "significance") return `t = ${(v as number).toFixed(1)}`;
  if (c.key === "holdout") return `IC ${fix(v.rank_ic, 3)} · ${pct(v.ai)}（起点 ${pct(v.baseline)}）`;
  if (c.key === "placebo") return `${pct(v.share, 0)}（${v.n} 组）`;
  return pct(v as number, 1);
}

/** 可信度检查：显著性、AI 未见过的数据、随机因子对照、复现（factopia/validate）。 */
function CredibilityBlock({ e }: { e: StudyMarket }) {
  const f = e.focus!, cred = f.credibility, job = f.validation_job;
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState<string | null>(null);
  const running = job && (job.state === "queued" || job.state === "running");
  return (
    <div className="border-t border-line pt-2 flex flex-col gap-1">
      <div className="flex items-center gap-2 text-xs">
        <span className="label">可信度</span>
        <Badge tone={CRED_TONE[cred.state]}>{running && cred.state === "unchecked" ? (job!.state === "queued" ? "排队中" : "检查中") : cred.label}</Badge>
        {cred.noise?.range && <span className="num text-faint ml-auto" title="同一个模型多次回测（打分相同的股票处理顺序不同）的测试期扣成本年化">
          多次回测 {pct(cred.noise.range[0])}~{pct(cred.noise.range[1])}</span>}
      </div>
      <ul className="flex flex-col gap-1">{cred.checks.map((c) => (
        <li key={c.key} className="flex items-baseline gap-2 text-xs">
          <span className="num w-3 text-center" style={{ color: cssVar(c.ok === true ? "--good" : c.ok === false ? "--bad" : "--muted") }}>{c.ok === true ? "✓" : c.ok === false ? "✗" : "—"}</span>
          <span className="text-muted">{c.label}</span>
          <span className="num ml-auto text-right">{credValue(c)}</span>
        </li>
      ))}</ul>
      {cred.state === "unchecked" && !running && e.run_id && (
        started ? <span className="text-[11px] text-faint">已排队，约 10–15 分钟</span> : (
          <button className="btn self-start mt-1" disabled={busy} onClick={async () => {
            setBusy(true);
            try { const j = await api.validate(e.run_id!, f.loop); setStarted(j.job_id); } finally { setBusy(false); }
          }}>运行可信度检查</button>)
      )}
      {job?.state === "failed" && cred.state === "unchecked" && <span className="text-[11px] text-bad truncate" title={job.error ?? ""}>上次检查失败：{job.error}</span>}
    </div>
  );
}

function MarketCard({ e, current }: { e: StudyMarket; current: boolean }) {
  const color = cssVar(`--${e.market}`);
  const f = e.focus, q = f?.quantile, b = e.baseline;
  const done = e.state === "done";
  return (
    <div className="rounded border p-3 flex flex-col gap-3"
      style={{ borderColor: current ? color : `color-mix(in oklab, ${color} 35%, var(--line))`, background: `color-mix(in oklab, ${color} 5%, transparent)` }}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="size-2.5 rounded-full" style={{ background: color }} />
        <span className="font-medium">{MARKET_LABEL[e.market as Market]}</span>
        {e.primary && <Badge tone="accent">主市场</Badge>}
        {current && <Badge>当前运行</Badge>}
        {e.kind === "text" && <Badge tone="accent">文本信号</Badge>}
        {e.price_source?.simulated && <Badge tone="warn">模拟数据，仅用于流程演示</Badge>}
        <span className="ml-auto">
          {done && e.insufficient ? <Badge tone="warn">数据不足</Badge>
            : !done ? <Badge tone={e.state === "running" ? "accent" : "muted"}>{STATE_ZH[e.state] ?? e.state}</Badge>
            : !f ? <Badge tone={e.primary ? "bad" : "muted"}>没有采纳的轮次</Badge>
            : f.passed ? <Badge tone="good">{e.primary ? "通过" : "方向一致"}</Badge>
            : <Badge tone="bad">{e.primary ? "未通过" : "方向相反"}</Badge>}
        </span>
      </div>

      {e.insufficient && <p className="text-xs text-muted">{e.insufficient}</p>}
      {done && f ? (
        <>
          <div className="text-xs text-faint">第 {f.loop} 轮（{e.kind === "text" ? "验证期判定采纳" : "RD-Agent 采纳"}）· {f.factors.slice(0, 3).join("、")}{f.factors.length > 3 ? ` 等 ${f.factors.length} 个` : ""}</div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
            <div><dt className="text-faint">扣成本年化</dt><dd className="num text-base">{pct(b?.annual_return_with_cost)} → <span className="text-text">{pct(f.metrics.annual_return_with_cost)}</span></dd></div>
            {e.kind === "text"
              ? <div><dt className="text-faint">最大回撤（绝对）</dt><dd className="num text-base">{pct(b?.max_drawdown_abs)} → <span className="text-text">{pct(f.metrics.max_drawdown_abs)}</span></dd></div>
              : <div><dt className="text-faint">最大回撤</dt><dd className="num text-base">{pct(b?.max_drawdown_with_cost)} → <span className="text-text">{pct(f.metrics.max_drawdown_with_cost)}</span></dd></div>}
            <div><dt className="text-faint">rank IC</dt><dd className="num text-base">{fix(f.metrics.rank_ic, 4)}</dd></div>
            <div><dt className="text-faint">第 {q?.groups ?? 5} 组 − 第 1 组</dt>
              <dd className="num text-base" style={{ color: q?.spread_mean == null ? undefined : cssVar(q.spread_mean > 0 ? "--good" : "--bad") }}>{signed(q?.spread_mean, 2)}</dd></div>
            <div><dt className="text-faint">同分股票</dt><dd className="num">{pct(q?.tie_share, 0)}</dd></div>
            <div><dt className="text-faint">费用 / 耗时</dt><dd className="num">${(e.cost_usd ?? 0).toFixed(2)} / {Math.round((e.duration_s ?? 0) / 60)} 分钟</dd></div>
          </dl>
          <ul className="flex flex-col gap-1 border-t border-line pt-2">{f.checks.map((c) => <CheckRow key={c.key} c={c} />)}</ul>
          <CredibilityBlock e={e} />
          {f.diagnostics && <DiagnosticsBlock d={f.diagnostics} />}
        </>
      ) : done && e.insufficient ? null : done ? (
        <p className="text-xs text-muted">{e.kind === "text"
          ? `验证期未满足采纳条件（rank IC 方向与假设一致且扣成本超额高于等权持有股票池），不采纳；起点扣成本年化超额 ${pct(b?.annual_return_with_cost)}。`
          : `RD-Agent 没有采纳任何一轮：新因子都没有超过起点（扣成本年化 ${pct(b?.annual_return_with_cost)}）。`}</p>
      ) : (
        <p className="text-xs text-muted">{e.job ? `任务${e.job.state_zh}，完成后自动判定。` : "这个版本还没有在该市场运行。"}</p>
      )}

      <div className="flex flex-wrap gap-2 mt-auto">
        {e.run_id && !current && <Link className="btn" to={`/results?run=${e.run_id}`}>查看结果</Link>}
        {e.run_id && <Link className="btn" to={`/run?run=${e.run_id}`}>{e.state === "running" ? "查看过程" : "回放过程"}</Link>}
        {!e.run_id && e.job && <Link className="btn" to={`/run?job=${e.job.job_id}`}>查看任务</Link>}
        {e.attempts > 1 && <span className="text-[11px] text-faint self-center">共运行 {e.attempts} 次，取最近一次完成的</span>}
      </div>
    </div>
  );
}


const STYLE_ZH: Record<string, string> = { size: "规模", momentum: "动量", volatility: "波动", reversal: "短期反转" };
const money = (x: number | null | undefined, ccy: string) => (x == null ? "—" : `${(x / 1e6).toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 百万 ${ccy}`);

/** 研究结果诊断（第 14.11 节；BR-04、26、27、28）：成本、换手、流动性、容量、汇率、暴露、中性化、稳定性。 */
function DiagnosticsBlock({ d }: { d: Diagnostics }) {
  const c = d.cost, l = d.liquidity, x = d.fx, ex = d.exposure, sg = d.signal;
  const row = (k: string, v: string, tone?: "good" | "bad" | "warn") => (
    <div className="flex justify-between gap-3"><span className="text-faint">{k}</span><span className="num text-right" style={tone ? { color: cssVar(`--${tone}`) } : undefined}>{v}</span></div>
  );
  return (
    <details className="border-t border-line pt-2 text-xs">
      <summary className="cursor-pointer flex flex-wrap items-center gap-2">
        <span className="label">诊断</span>
        <Badge tone={d.economic.meaningful ? "good" : "warn"}>{d.economic.label}</Badge>
      </summary>
      <div className="flex flex-col gap-1 mt-2">
        {d.backtest_note && <p className="text-warn">{d.backtest_note}</p>}
        {row("扣成本前 → 扣成本后年化超额", `${pct(c.gross_excess)} → ${pct(c.net_excess)}`, (c.net_excess ?? 0) > 0 ? "good" : "bad")}
        {row("成本拖累（其中税费）", `${pct(c.cost_drag)}（${pct(c.tax_share, 0)}）`)}
        {row("成本 2 倍 / 3 倍时超额", `${pct(c.excess_by_cost_multiple["2"])} / ${pct(c.excess_by_cost_multiple["3"])}`)}
        {row("盈亏平衡成本倍数", c.breakeven_cost_multiple == null ? "—" : `${c.breakeven_cost_multiple.toFixed(1)} 倍`, (c.breakeven_cost_multiple ?? 0) < 2 ? "warn" : undefined)}
        {row("年化换手 / 平均持有", `${fix(c.turnover_annual, 1)} 倍 / ${fix(c.holding_days, 0)} 天`)}
        {row("容量（调仓 ≤ 日均成交额 10%）", money(l.capacity_base, d.base_currency))}
        {row("持仓在低流动性股票的比例", pct(l.low_liquidity_weight, 0))}
        {!x.same_currency && row(`汇率贡献（${x.local}→${x.base}）`, signed(x.fx_contribution))}
        {row("对基准 beta", fix(ex.beta, 2))}
        {row("风格暴露", Object.entries(ex.style).map(([k, v]) => `${STYLE_ZH[k] ?? k} ${v == null ? "—" : v.toFixed(2)}`).join(" · "))}
        {ex.max_sector_weight != null && row("最大行业权重", pct(ex.max_sector_weight, 0))}
        {sg && <>
          {row("中性化后 Rank IC（原始）", `${fix(sg.neutral_rank_ic, 3)}（${fix(sg.rank_ic, 3)}）`)}
          {row("分年度 IC 方向一致", pct(sg.year_sign_consistency, 0))}
          {row("高 / 低波动市场 IC", `${fix(sg.regime.high_vol, 3)} / ${fix(sg.regime.low_vol, 3)}`)}
          {row("上涨 / 下跌市场 IC", `${fix(sg.regime.up_market, 3)} / ${fix(sg.regime.down_market, 3)}`)}
          {sg.redundancy.baseline_model != null && row("与起点模型打分相关", fix(sg.redundancy.baseline_model, 2))}
          {row("IC 随持有期（1/5/10/20/40 日）", Object.values(sg.ic_by_horizon).map((v) => fix(v, 3)).join(" / "))}
        </>}
        {!sg && <p className="text-faint">逐股打分尚未导出：重新导出后补齐中性化、稳定性与冗余诊断。</p>}
      </div>
    </details>
  );
}

/** 组合预检摘要（第 14.12 节）：详情与建议审批路径在风控台 */
function RulesLine({ s }: { s: Study }) {
  const r = useAsync(() => api.versionRules(s.research_id, s.version), [s.research_id, s.version]);
  const pf = r.data?.portfolio;
  if (!pf) return null;
  const tone = pf.blocked ? "bad" : pf.counts.warn ? "warn" : "good";
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="label">组合预检</span>
      <Badge tone={tone}>{pf.blocked ? "有阻断" : pf.counts.warn ? "需审批确认" : "未命中"}</Badge>
      <span className="text-muted">阻断 {pf.counts.block} · 警告 {pf.counts.warn} · 提示 {pf.counts.info}</span>
      <span className="text-xs text-faint">{[...new Set(pf.hits.filter((h) => h.severity !== "info").map((h) => h.title))].slice(0, 3).join("；")}</span>
      <Link className="text-accent text-xs ml-auto" to={`/rules?id=${s.research_id}&v=${s.version}`}>在风控台查看命中原因与审批路径 →</Link>
    </div>
  );
}

function TransferBlock({ s }: { s: Study }) {
  const t = s.transfer;
  if (!t) return null;
  const tone = t.label.startsWith("三市场") ? "good" : t.label === "无效" || t.label.startsWith("扣除") ? "bad" : t.label === "待定" ? "muted" : "warn";
  return (
    <div className="flex flex-wrap items-center gap-3 border border-line rounded p-3">
      <span className="label">迁移性（四类）</span>
      <Badge tone={tone}>{t.label}</Badge>
      {t.statistical && t.statistical !== t.label && <span className="text-xs text-muted">统计上：{t.statistical}</span>}
      {t.reason && <span className="text-xs text-muted">{t.reason}</span>}
      <span className="text-xs text-faint">
        {Object.entries(t.markets).map(([m, v]) => `${MARKET_LABEL[m as Market]} t = ${v.t_aligned == null ? "—" : v.t_aligned.toFixed(1)}`).join(" · ")}
        （按假设方向，|t| &gt; {t.threshold} 视为有效）
      </span>
      {s.cross_market && (
        <span className="text-xs text-muted w-full" title={s.cross_market.note}>
          三市场相关结构（逐月 Rank IC，{s.cross_market.months} 个月）：
          {s.cross_market.pairs.map((p) => `${MARKET_LABEL[p.a]}—${MARKET_LABEL[p.b]} ${p.corr == null ? "—" : p.corr.toFixed(2)}`).join(" · ")}
        </span>
      )}
    </div>
  );
}

function ExplainBlock({ s, onDone }: { s: Study; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const e: Explanation | null = s.explanation;
  const run = async () => {
    setBusy(true); setErr(null);
    try { await api.explain(s.research_id, s.version); onDone(); }
    catch (x) { setErr(String((x as Error).message ?? x)); } finally { setBusy(false); }
  };
  const fact = (id: string) => e?.facts.find((f) => f.id === id)?.text ?? "";
  return (
    <div className="border border-line rounded p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">AI 解释市场差异</span>
        <span className="text-xs text-faint">每句必须引用平台事实（F 编号）；AI 只做解释，不改变判定，需研究员确认</span>
        <button className="btn ml-auto" disabled={busy} onClick={run}>{busy ? "生成中…" : e ? "重新生成" : "生成解释"}</button>
      </div>
      {err && <p className="text-xs text-bad">{err}</p>}
      {e && (
        <>
          <ul className="list-disc pl-5 text-sm flex flex-col gap-1">
            {e.sentences.map((x, i) => (
              <li key={i}>{x.text} {x.cites.map((c) => <span key={c} className="num text-accent cursor-help" title={fact(c)}>[{c}]</span>)}</li>
            ))}
          </ul>
          {e.transfer_view && <p className="text-sm"><span className="text-muted">迁移判断：</span>{e.transfer_view}</p>}
          {e.local_adjustments.length > 0 && <p className="text-sm"><span className="text-muted">本地化建议：</span>{e.local_adjustments.join("；")}</p>}
          <details className="text-xs text-faint">
            <summary className="cursor-pointer">事实清单（{e.facts.length} 条）· {e.model} · {e.prompt_version} · {e.created_at.replace("T", " ")}{e.dropped ? ` · 丢弃 ${e.dropped} 句无引用的解释` : ""}</summary>
            <ul className="mt-1 flex flex-col gap-0.5">{e.facts.map((f) => <li key={f.id}><span className="num text-accent">{f.id}</span> {f.text}</li>)}</ul>
          </details>
        </>
      )}
    </div>
  );
}
