import { Link } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { api, type Check, type CredCheck, type Diagnostics, type Explanation, type Market, type PaperDemoResult, type Study, type StudyMarket } from "../api";
import { cssVar, fix, MARKET_LABEL, pct, signed, useAsync } from "../lib";
import { t, tf, joinList, LANG, LOCALE } from "../i18n";
import { Badge, ErrorBox, Loading, Panel } from "./ui";
import StudyScatter from "./charts/StudyScatter";

const OVERALL_TONE = { rejected: "bad", passed: "good", pending: "accent", insufficient: "warn" } as const;
const FINDING_COLOR = { bad: "--bad", warn: "--warn", good: "--good" } as const;
const STATE_ZH: Record<string, string> = {
  queued: t("排队中"), running: t("运行中"), done: t("已完成"), failed: t("失败"), cancelled: t("已停止"), budget_stopped: t("超出预算停止"),
  incomplete: t("未完成"), none: t("尚未运行"), hidden: t("其他地区，无权查看"),
};

/**
 * 研究结论（Spec 第 14.5 节）：同一锁定版本在各市场的结果，按运行前写好的可证伪测试判定。
 * 判定与观察由平台按导出的数据计算（factopia/research/verdict.py），不是 AI 撰写。
 */
export default function StudyPanel({ researchId, version, current }: { researchId: string; version: number; current?: string }) {
  const study = useAsync(() => api.study(researchId, version), [researchId, version]);
  if (study.error) return <ErrorBox error={study.error} />;
  if (!study.data) return <Loading what={t("研究结论")} />;
  const s = study.data;
  const markets = s.rule.markets.map((m) => s.markets[m]).filter(Boolean) as StudyMarket[];
  const anyRounds = markets.some((e) => e.rounds.length > 0);
  const primaryCred = s.markets[s.rule.primary]?.focus?.credibility;
  const textStudy = markets.some((e) => e.kind === "text");

  return (
    <Panel eyebrow={`${t("研究结论")} · ${s.research_id} v${s.version} · ${s.rule.markets.map((m) => MARKET_LABEL[m]).join(" / ")}`}
      title={t("按事先写好的标准判定")}
      actions={<>
        {primaryCred && primaryCred.state !== "unchecked" && <Badge tone={CRED_TONE[primaryCred.state]}>{t("可信度")}{t(primaryCred.label)}</Badge>}
        <Badge tone={OVERALL_TONE[s.overall.state]}>{s.overall.state === "passed" ? t(s.overall.label) : `${t("假设")}${t(s.overall.label)}`}</Badge></>}>
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline gap-3">
              <span className="font-display text-3xl font-semibold whitespace-nowrap shrink-0" style={{ color: cssVar(`--${OVERALL_TONE[s.overall.state]}`) }}>
                {t(s.overall.label)}
              </span>
              <span className="text-sm text-muted">{s.overall.reason}</span>
            </div>
            {primaryCred && primaryCred.state !== "unchecked" && (
              <p className="text-sm">
                <span className="text-muted">{t("可信度检查（主市场）：")}</span>
                <span style={{ color: cssVar(`--${({ credible: "good", doubtful: "bad", pending: "accent", unchecked: "muted" } as const)[primaryCred.state]}`) }}>{t(primaryCred.label)}</span>
                <span className="text-muted">
                  {primaryCred.state === "doubtful" && tf("——未通过：{a}。即使按标准判定通过，也不能当作研究结论。", { a: joinList(primaryCred.checks.filter((c) => c.ok === false).map((c) => t(c.label))) })}
                  {primaryCred.state === "credible" && t("——显著、在 AI 未见过的数据上延续、明显好于随机因子。")}
                </span>
              </p>
            )}
            <p className="text-sm leading-relaxed">{s.title && <span className="font-medium">{s.title}{t("：")}</span>}{s.hypothesis}</p>
          </div>
          <div className="text-xs text-muted leading-relaxed flex flex-col gap-1.5">
            <div>
              <span className="text-text">{t("判定方法：")}</span>
              {tf("主市场（{m}）看{k}，需同时满足 rank IC {sg}、扣成本年化超额高于起点、{d}；其他市场看采纳轮次的 rank IC 方向是否与假设一致。", {
                m: MARKET_LABEL[s.rule.primary],
                k: textStudy ? t("按验证期判定采纳的轮次（文本信号研究）") : " " + t("RD-Agent 最终采纳的轮次"),
                sg: s.rule.ric_sign > 0 ? t("为正") : t("为负"),
                d: textStudy ? t("绝对回撤（扣成本）不比等权持有股票池差") : t("最大回撤不比起点差"),
              })}
              {t("不在各轮中挑最好的一轮，避免多次尝试挑结果。原文中的其他条件（如 t 值门槛、回撤容忍幅度）平台尚未检验，需人工确认。")}
              {!s.rule.sign_from_text && " " + t("原文没有写明 rank IC 方向，按为正判定。")}
            </div>
            <details>
              <summary className="cursor-pointer text-text">{t("可证伪测试原文（运行前锁定，不可修改）")}</summary>
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
        <PaperDemoBlock s={s} />

        {anyRounds && (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
            <div>
              <div className="label mb-1">{t("各轮的 rank IC（横轴）与扣成本年化超额（纵轴）")}</div>
              <StudyScatter study={s} />
              <p className="text-[11px] text-faint mt-1">
                {t("每个点是一轮，大点为 RD-Agent 采纳的轮次，空心菱形为起点（20 个基础因子）。打分真正有效时，点应落在白色一侧的上方；落在红色区域上方的点，是“收益变好、排序能力却与假设相反”。")}
              </p>
            </div>
            <div>
              <div className="label mb-2">{t("平台观察")}</div>
              <ul className="flex flex-col gap-2">
                {s.findings.map((f, i) => (
                  <li key={i} className="flex gap-2 text-sm leading-relaxed">
                    <span className="size-1.5 rounded-full mt-2 shrink-0" style={{ background: cssVar(FINDING_COLOR[f.tone]) }} />
                    <span className={f.tone === "bad" ? "text-text" : "text-muted"}>{f.text}</span>
                  </li>
                ))}
                {s.findings.length === 0 && <li className="text-sm text-faint">{t("运行完成后显示。")}</li>}
              </ul>
              <p className="text-[11px] text-faint mt-3">{t("以上判定与观察由平台按回测数据计算，不是 AI 撰写。测试期只有两年、每个市场只运行一次，结论需在 2024 年以后的数据上复核。")}</p>
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
  const val = c.key === "rank_ic" ? fix(c.value, 4) : `${pct(c.value)}` + tf("（起点 {r}）", { r: pct(c.ref) });
  return (
    <li className="flex items-baseline gap-2 text-xs">
      <span className="num w-3 text-center" style={{ color: cssVar(color) }}>{mark}</span>
      <span className="text-muted">{t(c.label)}</span>
      <span className="num ml-auto">{val}</span>
    </li>
  );
}

const CRED_TONE = { unchecked: "muted", pending: "accent", credible: "good", doubtful: "bad" } as const;

function credValue(c: CredCheck): string {
  const v = c.value;
  if (v == null) return "—";
  if (c.key === "significance") return `t = ${(v as number).toFixed(1)}`;
  if (c.key === "holdout") return v.paired
    ? tf("IC {a} · 中位数 {b}（起点 {c}）· 胜 {w}/{n}", { a: fix(v.rank_ic, 3), b: pct(v.paired.ai_median), c: pct(v.paired.baseline_median), w: v.paired.wins, n: v.paired.n })
    : tf("IC {a} · {b}（起点 {c}）", { a: fix(v.rank_ic, 3), b: pct(v.ai), c: pct(v.baseline) });
  if (c.key === "robust_test") return tf("中位数 {a}（起点 {b}）· 胜 {w}/{n}", { a: pct(v.ai_median), b: pct(v.baseline_median), w: v.wins, n: v.n });
  if (c.key === "ties") return tf("并列 {n} 只（持股 {k}，每天 {d} 种打分 / {s} 只）", { n: v.cutoff_tied_median, k: v.topk, d: v.distinct_median, s: v.stocks_median });
  if (c.key === "placebo") return tf("{p}（{n} 组）", { p: pct(v.share, 0), n: v.n });
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
        <span className="label">{t("可信度")}</span>
        <Badge tone={CRED_TONE[cred.state]}>{running && cred.state === "unchecked" ? (job!.state === "queued" ? t("排队中") : t("检查中")) : t(cred.label)}</Badge>
        {cred.noise?.range && <span className="num text-faint ml-auto" title={t("同一个模型多次回测（打分相同的股票处理顺序不同）的测试期扣成本年化")}>
          {tf("多次回测中位数 {a}（{b} ~ {c}）{d}", { a: pct(cred.noise.median), b: pct(cred.noise.range[0]), c: pct(cred.noise.range[1]), d: cred.noise.deterministic ? "" : t(" · 固定随机性前的检查") })}</span>}
      </div>
      <ul className="flex flex-col gap-1">{cred.checks.map((c) => (
        <li key={c.key} className="flex items-baseline gap-2 text-xs">
          <span className="num w-3 text-center" style={{ color: cssVar(c.ok === true ? "--good" : c.ok === false ? "--bad" : "--muted") }}>{c.ok === true ? "✓" : c.ok === false ? "✗" : "—"}</span>
          <span className="text-muted">{t(c.label)}</span>
          <span className="num ml-auto text-right">{credValue(c)}</span>
        </li>
      ))}</ul>
      {cred.state === "unchecked" && !running && e.run_id && (
        started ? <span className="text-[11px] text-faint">{t("已排队，约 10–15 分钟")}</span> : (
          <button className="btn self-start mt-1" disabled={busy} onClick={async () => {
            setBusy(true);
            try { const j = await api.validate(e.run_id!, f.loop); setStarted(j.job_id); } finally { setBusy(false); }
          }}>{t("运行可信度检查")}</button>)
      )}
      {job?.state === "failed" && cred.state === "unchecked" && <span className="text-[11px] text-bad truncate" title={job.error ?? ""}>{t("上次检查失败：")}{job.error}</span>}
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
        {e.primary && <Badge tone="accent">{t("主市场")}</Badge>}
        {current && <Badge>{t("当前运行")}</Badge>}
        {e.kind === "text" && <Badge tone="accent">{t("文本信号")}</Badge>}
        {e.price_source?.simulated && <Badge tone="warn">{t("模拟数据，仅用于流程演示")}</Badge>}
        <span className="ml-auto">
          {done && e.insufficient ? <Badge tone="warn">{t("数据不足")}</Badge>
            : !done ? <Badge tone={e.state === "running" ? "accent" : "muted"}>{STATE_ZH[e.state] ?? e.state}</Badge>
            : !f ? <Badge tone={e.primary ? "bad" : "muted"}>{t("没有采纳的轮次")}</Badge>
            : f.passed ? <Badge tone="good">{e.primary ? t("通过") : t("方向一致")}</Badge>
            : <Badge tone="bad">{e.primary ? t("未通过") : t("方向相反")}</Badge>}
        </span>
      </div>

      {e.insufficient && <p className="text-xs text-muted">{e.insufficient}</p>}
      {done && f ? (
        <>
          <div className="text-xs text-faint">{tf("第 {i} 轮（{k}）· {f}{more}", { i: f.loop, k: e.kind === "text" ? t("验证期判定采纳") : t("RD-Agent 采纳"), f: joinList(f.factors.slice(0, 3)), more: f.factors.length > 3 ? tf(" 等 {n} 个", { n: f.factors.length }) : "" })}</div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
            <div><dt className="text-faint">{t("扣成本年化")}{f.credibility.noise?.median != null ? t("（单次）") : ""}</dt><dd className="num text-base">{pct(b?.annual_return_with_cost)} → <span className="text-text">{pct(f.metrics.annual_return_with_cost)}</span></dd>
              {f.credibility.noise?.median != null && f.credibility.noise.range && <dd className="num text-[11px] text-warn">
                {tf("多次回测中位数 {a}，区间 {b} ~ {c}", { a: pct(f.credibility.noise.median), b: pct(f.credibility.noise.range[0]), c: pct(f.credibility.noise.range[1]) })}</dd>}</div>
            {e.kind === "text"
              ? <div><dt className="text-faint">{t("最大回撤（绝对）")}</dt><dd className="num text-base">{pct(b?.max_drawdown_abs)} → <span className="text-text">{pct(f.metrics.max_drawdown_abs)}</span></dd></div>
              : <div><dt className="text-faint">{t("最大回撤")}</dt><dd className="num text-base">{pct(b?.max_drawdown_with_cost)} → <span className="text-text">{pct(f.metrics.max_drawdown_with_cost)}</span></dd></div>}
            <div><dt className="text-faint">rank IC</dt><dd className="num text-base">{fix(f.metrics.rank_ic, 4)}</dd></div>
            <div><dt className="text-faint">{tf("第 {n} 组 − 第 1 组", { n: q?.groups ?? 5 })}</dt>
              <dd className="num text-base" style={{ color: q?.spread_mean == null ? undefined : cssVar(q.spread_mean > 0 ? "--good" : "--bad") }}>{signed(q?.spread_mean, 2)}</dd></div>
            <div><dt className="text-faint">{t("同分股票")}</dt><dd className="num">{pct(q?.tie_share, 0)}</dd></div>
            <div><dt className="text-faint">{t("费用 / 耗时")}</dt><dd className="num">{"$"}{tf("{c} / {m} 分钟", { c: (e.cost_usd ?? 0).toFixed(2), m: Math.round((e.duration_s ?? 0) / 60) })}</dd></div>
          </dl>
          <ul className="flex flex-col gap-1 border-t border-line pt-2">{f.checks.map((c) => <CheckRow key={c.key} c={c} />)}</ul>
          <CredibilityBlock e={e} />
          {f.diagnostics && <DiagnosticsBlock d={f.diagnostics} />}
        </>
      ) : done && e.insufficient ? null : done ? (
        <p className="text-xs text-muted">{e.kind === "text"
          ? tf("验证期未满足采纳条件（rank IC 方向与假设一致且扣成本超额高于等权持有股票池），不采纳；起点扣成本年化超额 {p}。", { p: pct(b?.annual_return_with_cost) })
          : tf("RD-Agent 没有采纳任何一轮：新因子都没有超过起点（扣成本年化 {p}）。", { p: pct(b?.annual_return_with_cost) })}</p>
      ) : (
        <p className="text-xs text-muted">{(e.state as string) === "hidden" ? t("当前地区视角不显示其他地区的结果（演示用地区视角）。")
          : e.job ? tf("任务{s}，完成后自动判定。", { s: t(e.job.state_zh) }) : t("这个版本还没有在该市场运行。")}</p>
      )}

      <div className="flex flex-wrap gap-2 mt-auto">
        {e.run_id && !current && <Link className="btn" to={`/results?run=${e.run_id}`}>{t("查看结果")}</Link>}
        {e.run_id && <Link className="btn" to={`/run?run=${e.run_id}`}>{e.state === "running" ? t("查看过程") : t("回放过程")}</Link>}
        {!e.run_id && e.job && <Link className="btn" to={`/run?job=${e.job.job_id}`}>{t("查看任务")}</Link>}
        {e.attempts > 1 && <span className="text-[11px] text-faint self-center">{tf("共运行 {n} 次，取最近一次完成的", { n: e.attempts })}</span>}
      </div>
    </div>
  );
}


const STYLE_ZH: Record<string, string> = { size: t("规模"), momentum: t("动量"), volatility: t("波动"), reversal: t("短期反转") };
const money = (x: number | null | undefined, ccy: string) => (x == null ? "—" : tf("{n} 百万 {c}", { n: (x / 1e6).toLocaleString(LOCALE[LANG], { maximumFractionDigits: 0 }), c: ccy }));

/** 研究结果诊断（第 14.11 节；BR-04、26、27、28）：成本、换手、流动性、容量、汇率、暴露、中性化、稳定性。 */
function DiagnosticsBlock({ d }: { d: Diagnostics }) {
  const c = d.cost, l = d.liquidity, x = d.fx, ex = d.exposure, sg = d.signal;
  const row = (k: string, v: string, tone?: "good" | "bad" | "warn") => (
    <div className="flex justify-between gap-3"><span className="text-faint">{k}</span><span className="num text-right" style={tone ? { color: cssVar(`--${tone}`) } : undefined}>{v}</span></div>
  );
  return (
    <details className="border-t border-line pt-2 text-xs">
      <summary className="cursor-pointer flex flex-wrap items-center gap-2">
        <span className="label">{t("诊断")}</span>
        <Badge tone={d.economic.meaningful ? "good" : "warn"}>{t(d.economic.label)}</Badge>
      </summary>
      <div className="flex flex-col gap-1 mt-2">
        {d.backtest_note && <p className="text-warn">{d.backtest_note}</p>}
        {row(t("扣成本前 → 扣成本后年化超额"), `${pct(c.gross_excess)} → ${pct(c.net_excess)}`, (c.net_excess ?? 0) > 0 ? "good" : "bad")}
        {row(t("成本拖累（其中税费）"), `${pct(c.cost_drag)}（${pct(c.tax_share, 0)}）`)}
        {row(t("成本 2 倍 / 3 倍时超额"), `${pct(c.excess_by_cost_multiple["2"])} / ${pct(c.excess_by_cost_multiple["3"])}`)}
        {row(t("盈亏平衡成本倍数"), c.breakeven_cost_multiple == null ? "—" : tf("{n} 倍", { n: c.breakeven_cost_multiple.toFixed(1) }), (c.breakeven_cost_multiple ?? 0) < 2 ? "warn" : undefined)}
        {row(t("年化换手 / 平均持有"), tf("{a} 倍 / {b} 天", { a: fix(c.turnover_annual, 1), b: fix(c.holding_days, 0) }))}
        {row(t("容量（调仓 ≤ 日均成交额 10%）"), money(l.capacity_base, d.base_currency))}
        {row(t("持仓在低流动性股票的比例"), pct(l.low_liquidity_weight, 0))}
        {!x.same_currency && row(tf("汇率贡献（{a}→{b}）", { a: x.local, b: x.base }), signed(x.fx_contribution))}
        {row(t("对基准 beta"), fix(ex.beta, 2))}
        {row(t("风格暴露"), Object.entries(ex.style).map(([k, v]) => `${STYLE_ZH[k] ?? k} ${v == null ? "—" : v.toFixed(2)}`).join(" · "))}
        {ex.max_sector_weight != null && row(t("最大行业权重"), pct(ex.max_sector_weight, 0))}
        {sg && <>
          {row(t("中性化后 Rank IC（原始）"), `${fix(sg.neutral_rank_ic, 3)}（${fix(sg.rank_ic, 3)}）`)}
          {row(t("分年度 IC 方向一致"), pct(sg.year_sign_consistency, 0))}
          {row(t("高 / 低波动市场 IC"), `${fix(sg.regime.high_vol, 3)} / ${fix(sg.regime.low_vol, 3)}`)}
          {row(t("上涨 / 下跌市场 IC"), `${fix(sg.regime.up_market, 3)} / ${fix(sg.regime.down_market, 3)}`)}
          {sg.redundancy.baseline_model != null && row(t("与起点模型打分相关"), fix(sg.redundancy.baseline_model, 2))}
          {row(t("IC 随持有期（1/5/10/20/40 日）"), Object.values(sg.ic_by_horizon).map((v) => fix(v, 3)).join(" / "))}
        </>}
        {!sg && <p className="text-faint">{t("逐股打分尚未导出：重新导出后补齐中性化、稳定性与冗余诊断。")}</p>}
      </div>
    </details>
  );
}

/** 一键接入模拟盘（研究线 → 结果展示线的连接入口）：按钮名称固定，点击后弹出市场复选下拉。
 *  勾选决定接入范围：只勾香港就只接入港股模拟盘，全选则接入全部；勾选了运行中的市场会重置该市场模拟盘（清空旧状态从头开始），未勾选的保持现状。 */
function PaperDemoBlock({ s }: { s: Study }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<PaperDemoResult | null>(null);
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<Market[] | null>(null);
  const popRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (ev: PointerEvent) => { if (popRef.current && !popRef.current.contains(ev.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", onDoc);
    return () => document.removeEventListener("pointerdown", onDoc);
  }, [open]);
  const wired = useAsync(async () => {
    const r = await api.signals();
    return r.items.filter((e) => e.signal_id.startsWith(`${s.research_id}.v${s.version}.`) && e.paper);
  }, [s.research_id, s.version]);
  const live: Partial<Record<Market, { active: boolean; mode: string }>> = {};
  (wired.data ?? []).forEach((e) => { live[e.market] = e.paper ?? { active: false, mode: "" }; });
  const appr = useAsync(() => api.approvals(s.research_id), [s.research_id]);
  const subs = (appr.data?.items ?? []).filter((x) => x.version === s.version);
  const approvedSub = subs.find((x) => x.status === "approved" || x.status === "approved_restricted");
  const reviewing = subs.find((x) => x.status === "in_review");
  const candidates = ((approvedSub?.approved_markets ?? approvedSub?.markets) ?? s.rule.markets) as Market[];
  const eff = sel ?? candidates;
  const willReset = eff.filter((m) => live[m]?.active);
  const run = async () => {
    setBusy(true); setErr(null);
    try { setDone(await api.paperDemo(s.research_id, s.version, { markets: eff })); wired.reload(); setOpen(false); }
    catch (x) { setErr(String((x as Error).message ?? x)); }
    finally { setBusy(false); }
  };

  // 未获批也放行：后端 paper-demo 会自动补材料、自动提交并走完全部审批（留痕「演示一键接入」）
  if (appr.loading || appr.error) return null;
  return (
    <div className="border border-line rounded p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">{t("接入模拟盘")}</span>
        {approvedSub ? <>
          <Badge tone="good">{t("审批已获批")}</Badge>
          <span className="text-xs text-faint">{t("已获批市场：")}{joinList((approvedSub.approved_markets ?? approvedSub.markets).map((m) => MARKET_LABEL[m as Market] ?? m))}</span>
          <Link className="text-accent text-xs" to={`/approvals?sid=${approvedSub.id}`}>{t("审批台 →")}</Link>
        </> : reviewing ? <>
          <Badge tone="accent">{t("审批中")}</Badge>
          <span className="text-xs text-faint">{t("接入前会自动走完剩余审批步骤")}</span>
          <Link className="text-accent text-xs" to={`/approvals?sid=${reviewing.id}`}>{t("审批台 →")}</Link>
        </> : <>
          <Badge tone="muted">{t("未提交审批")}</Badge>
          <span className="text-xs text-faint">{t("接入前会自动补材料、提交并走完全部审批（留痕「演示一键接入」）")}</span>
        </>}
        <div ref={popRef} className="relative ml-auto">
          <button className="btn" disabled={busy} onClick={() => setOpen((v) => !v)}>
            {busy ? t("接入中…") : t("一键接入模拟盘")}<span className="text-faint text-[10px]">▼</span>
          </button>
          {open && (
            <div className="absolute right-0 top-full mt-1.5 z-30 min-w-60 bg-bg-2 border border-line-2 rounded p-2.5 flex flex-col gap-1.5 shadow-lg">
              {candidates.map((m) => (
                <label key={m} className="flex items-center gap-1.5 cursor-pointer select-none text-sm py-0.5">
                  <input type="checkbox" checked={eff.includes(m)}
                    onChange={(e) => setSel(e.target.checked ? [...eff, m] : eff.filter((x) => x !== m))} />
                  <span className="size-2 rounded-full" style={{ background: cssVar(`--${m}`) }} />
                  {MARKET_LABEL[m]}
                  {live[m]?.active && <Badge tone="warn">{t("运行中")}{live[m]!.mode === "longport" ? t("·长桥") : t("·影子")}</Badge>}
                </label>
              ))}
              <div className="flex items-center gap-2 pt-1.5 border-t border-line">
                <span className="text-[11px] text-faint">
                  {willReset.length > 0 ? tf("将重置：{m}", { m: joinList(willReset.map((m) => MARKET_LABEL[m])) }) : t("未勾选的市场保持现状")}
                </span>
                <button className="btn btn-primary ml-auto" disabled={busy || eff.length === 0} onClick={run}>
                  {busy ? t("接入中…") : tf("接入所选（{n} 个市场）", { n: eff.length })}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
      {err && <p className="text-xs text-bad">{err}</p>}
      {done && (
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone="good">{t("审批")}{t(done.submission.status_zh)}</Badge>
            <span className="text-muted">{done.submission.id}</span>
            <span className="text-xs text-faint">{t("获批市场：")}{joinList(done.submission.markets.map((m) => MARKET_LABEL[m as Market] ?? m))}</span>
            <Link className="text-accent text-xs" to={`/approvals?sid=${done.submission.id}`}>{t("审批台 →")}</Link>
            <Link className="text-accent text-xs" to="/signals">{t("信号目录 →")}</Link>
          </div>
          {done.log.length > 0 && (
            <ul className="text-xs text-muted flex flex-col gap-0.5">{done.log.map((l, i) => <li key={i}>· {l}</li>)}</ul>
          )}
          {done.signals.map((x) => (
            <div key={x.signal_id} className="flex flex-wrap items-center gap-2 text-xs">
              <Badge tone={x.selected === false ? "muted" : x.reset ? "warn" : x.enrolled ? "good" : "bad"}>
                {t(x.market_zh)}{x.selected === false ? t("未选择（保持现状）") : x.reset ? t("已重置（从头开始）") : x.already ? t("已接入（保持现状）") : x.enrolled ? t("已接入") : t("未接入")}
              </Badge>
              <span className="num text-faint">{x.signal_id}</span>
              {x.error && <span className="text-warn">{x.error}</span>}
              {x.job && <span className="text-faint">{tf("推进任务已排队（{s}）", { s: t(x.job.state_zh) })}</span>}
            </div>
          ))}
        </div>
      )}
      <p className="text-[11px] text-faint">
        {t("未提交审批时点击会自动补材料、自动提交并走完全部审批（留痕「演示一键接入」）；审批门槛只看「有采纳轮次」，不要求研究结论通过——判定为否定的版本获批后同样可接入持续观察。")}
        {t("点击「一键接入模拟盘」后勾选市场（可多选或只选一个）：只勾一个就只接入该市场模拟盘，全选则接入全部；勾选了运行中的市场会重置该市场模拟盘（清空旧状态从头开始），未勾选的市场保持现状；接入后约 1–3 分钟出第一轮模拟盘报告，之后每日自动推进。")}
      </p>
    </div>
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
      <span className="label">{t("组合预检")}</span>
      <Badge tone={tone}>{pf.blocked ? t("有阻断") : pf.counts.warn ? t("需审批确认") : t("未命中")}</Badge>
      <span className="text-muted">{tf("阻断 {b} · 警告 {w} · 提示 {i}", { b: pf.counts.block, w: pf.counts.warn, i: pf.counts.info })}</span>
      <span className="text-xs text-faint">{[...new Set(pf.hits.filter((h) => h.severity !== "info").map((h) => h.title))].slice(0, 3).join("；")}</span>
      <Link className="text-accent text-xs ml-auto" to={`/rules?id=${s.research_id}&v=${s.version}`}>{t("在风控台查看命中原因与审批路径 →")}</Link>
    </div>
  );
}

function TransferBlock({ s }: { s: Study }) {
  const tr = s.transfer;
  if (!tr) return null;
  const tone = tr.label.startsWith("三市场") ? "good" : tr.label === "无效" || tr.label.startsWith("扣除") ? "bad" : tr.label === "待定" ? "muted" : "warn";
  return (
    <div className="flex flex-wrap items-center gap-3 border border-line rounded p-3">
      <span className="label">{t("迁移性（四类）")}</span>
      <Badge tone={tone}>{t(tr.label)}</Badge>
      {tr.statistical && tr.statistical !== tr.label && <span className="text-xs text-muted">{t("统计上：")}{t(tr.statistical)}</span>}
      {tr.reason && <span className="text-xs text-muted">{tr.reason}</span>}
      <span className="text-xs text-faint">
        {Object.entries(tr.markets).map(([m, v]) => `${MARKET_LABEL[m as Market]} t = ${v.t_aligned == null ? "—" : v.t_aligned.toFixed(1)}`).join(" · ")}
        {tf("（按假设方向，|t| > {n} 视为有效）", { n: tr.threshold })}
      </span>
      {s.cross_market && (
        <span className="text-xs text-muted w-full" title={s.cross_market.note}>
          {tf("三市场相关结构（逐月 Rank IC，{n} 个月）：", { n: s.cross_market.months })}
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
        <span className="label">{t("AI 解释市场差异")}</span>
        <span className="text-xs text-faint">{t("每句必须引用平台事实（F 编号）；AI 只做解释，不改变判定，需研究员确认")}</span>
        <button className="btn ml-auto" disabled={busy} onClick={run}>{busy ? t("生成中…") : e ? t("重新生成") : t("生成解释")}</button>
      </div>
      {err && <p className="text-xs text-bad">{err}</p>}
      {e && (
        <>
          <ul className="list-disc pl-5 text-sm flex flex-col gap-1">
            {e.sentences.map((x, i) => (
              <li key={i}>{x.text} {x.cites.map((c) => <span key={c} className="num text-accent cursor-help" title={fact(c)}>[{c}]</span>)}</li>
            ))}
          </ul>
          {e.transfer_view && <p className="text-sm"><span className="text-muted">{t("迁移判断：")}</span>{e.transfer_view}</p>}
          {e.local_adjustments.length > 0 && <p className="text-sm"><span className="text-muted">{t("本地化建议：")}</span>{e.local_adjustments.join("；")}</p>}
          <details className="text-xs text-faint">
            <summary className="cursor-pointer">{tf("事实清单（{n} 条）", { n: e.facts.length })} · {e.model} · {e.prompt_version} · {e.created_at.replace("T", " ")}{e.dropped ? tf(" · 丢弃 {n} 句无引用的解释", { n: e.dropped }) : ""}</summary>
            <ul className="mt-1 flex flex-col gap-0.5">{e.facts.map((f) => <li key={f.id}><span className="num text-accent">{f.id}</span> {f.text}</li>)}</ul>
          </details>
        </>
      )}
    </div>
  );
}
