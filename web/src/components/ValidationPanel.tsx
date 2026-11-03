import { useEffect, useMemo, useState } from "react";
import EChart from "./EChart";
import { api, type Job, type Validation, type ValPeriod } from "../api";
import { cssVar, fix, pct, useAsync, useThemeVersion } from "../lib";
import { t, tf, IS_ZH } from "../i18n";
import { Badge, ErrorBox, Loading, Notice, Panel, Stat } from "./ui";

const range = (r?: [number, number] | null) => (r ? tf("{a} 至 {b}", { a: pct(r[0]), b: pct(r[1]) }) : "—");
const tTxt = (t: number | null | undefined) => (t == null ? "—" : `t = ${t.toFixed(1)}`);

/**
 * 可信度检查（Spec 第 14.7 节）：对 RD-Agent 采纳的轮次，在 AI 没见过的 2024 年以后数据上复核、与随机因子比较，
 * 并显示同一个模型多次回测的收益范围（打分并列时选股带有随机性，第 8 章 #21）。
 */
export default function ValidationPanel({ runId, loop }: { runId: string; loop: number | null }) {
  const [tick, setTick] = useState(0);
  const data = useAsync(() => api.validation(runId), [runId, tick]);
  const job = useMemo(() => data.data?.jobs.filter((j) => j.loop === loop).at(-1), [data.data, loop]);
  const v = loop == null ? undefined : data.data?.items[String(loop)];
  const active = job && (job.state === "queued" || job.state === "running");
  useEffect(() => {  // 检查进行中时每 15 秒刷新
    if (!active && v?.status !== "placebo") return;
    const id = setInterval(() => setTick((x) => x + 1), 15000);
    return () => clearInterval(id);
  }, [active, v?.status]);

  if (loop == null) return null;
  return (
    <Panel eyebrow={tf("第 {n} 轮（RD-Agent 采纳）· 不调用大模型", { n: loop })} title={t("可信度检查")}
      actions={<RunButton runId={runId} loop={loop} job={job} hasResult={!!v} onStarted={() => setTick((x) => x + 1)} />}>
      {data.error ? <ErrorBox error={data.error} />
        : !data.data ? <Loading what={t("可信度检查")} />
        : !v ? <Empty job={job} />
        : <Result v={v} job={job} />}
    </Panel>
  );
}

function RunButton({ runId, loop, job, hasResult, onStarted }: { runId: string; loop: number; job?: Job; hasResult: boolean; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (job && (job.state === "queued" || job.state === "running")) {
    return <Badge tone="accent">{job.state === "queued" ? t("排队中") : t("检查中")}</Badge>;
  }
  return (
    <>
      {err && <span className="text-xs text-bad">{err}</span>}
      <button className="btn" disabled={busy} onClick={async () => {
        setBusy(true); setErr(null);
        try { await api.validate(runId, loop); onStarted(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
      }}>{hasResult ? t("重新检查") : t("运行可信度检查")}</button>
    </>
  );
}

function Empty({ job }: { job?: Job }) {
  return (
    <div className="flex flex-col gap-2 text-sm text-muted max-w-[100ch]">
      {job?.state === "failed" && <Notice tone="bad" title={t("上次检查失败")}>{job.error}</Notice>}
      <p>{t("检查三件事，约 10–15 分钟，不产生大模型费用：")}</p>
      <ol className="list-decimal pl-5 flex flex-col gap-1">
        <li><span className="text-text">{t("AI 没见过的数据：")}</span>{t("AI 只能用到 2023 年底的数据，用 2024 年以后的数据重新计算这一轮的因子并回测，看规律是否延续。")}</li>
        <li><span className="text-text">{t("随机因子对照（安慰剂）：")}</span>{t("用同样数量的随机数代替 AI 的因子跑 20 次，看随机因子有多大比例也能达到同样的收益。")}</li>
        <li><span className="text-text">{t("并列顺序敏感性：")}</span>{t("回测中打分相同的股票按股票代码排序（结果可复现）；另换 10 种排序各跑一次，看收益对这个排序有多敏感。")}</li>
      </ol>
    </div>
  );
}

function Result({ v, job }: { v: Validation; job?: Job }) {
  const ai = v.ai, bl = v.baseline, pl = v.placebo;
  const repro = Math.min(...v.factors.map((f) => f.match_share));
  return (
    <div className="flex flex-col gap-5">
      {v.status !== "done" && <Notice tone="accent" title={t("检查进行中")}>{t("未见数据部分已完成，随机因子对照还在运行（每次约 15 秒）。")}</Notice>}
      {job?.state === "failed" && <Notice tone="bad" title={t("最近一次检查失败")}>{job.error}</Notice>}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Stat label={t("2024 年以后 · AI 因子")} value={pct(ai.holdout.annual_return_with_cost)}
          tone={cssVar((ai.holdout.annual_return_with_cost ?? 0) > (bl.holdout.annual_return_with_cost ?? 0) ? "--good" : "--bad")}
          sub={tf("扣成本年化中位数；起点 {b}", { b: pct(bl.holdout.annual_return_with_cost) })} />
        <Stat label={t("2024 年以后 · rank IC")} value={fix(ai.holdout.rank_ic, 3)}
          tone={cssVar((ai.holdout.rank_ic ?? 0) > 0 ? "--good" : "--bad")} sub={tf("{v}（|t| > 2 为显著）", { v: tTxt(ai.holdout.rank_ic_t) })} />
        <Stat label={t("随机因子不低于 AI")} value={pl?.share_ge_ai == null ? "…" : pct(pl.share_ge_ai, 0)}
          tone={pl?.share_ge_ai == null ? undefined : cssVar(pl.share_ge_ai <= 0.1 ? "--good" : "--bad")}
          sub={pl ? tf("{n} 组随机因子（测试期）", { n: pl.n }) : t("运行中")} />
        <Stat label={t("并列顺序敏感性")} value={range(ai.test.annual_return_with_cost_range)}
          sub={tf("测试期扣成本年化：默认排序 {d}，另 {s} 种并列顺序；RD-Agent 报告 {r}", { d: pct(ai.test.annual_return_with_cost_default ?? null), s: v.seeds, r: pct(v.original?.ai?.annual_return_with_cost) })} />
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div>
          <div className="label mb-1">{t("累计超额收益（扣成本）· 每条线是一次回测")}</div>
          <CurveChart v={v} />
          <p className="text-[11px] text-faint mt-1">
            {tf("彩色为 AI 因子 + 20 个基础因子，灰色为起点（只有 20 个基础因子），各 {n} 次：模型与打分完全相同，只是打分相同的股票的先后顺序不同（第一次为默认的按股票代码排序）。", { n: v.seeds + 1 })}
            {t("阴影部分是 AI 从未见过的数据。")}
          </p>
        </div>
        <div>
          <div className="label mb-1">{t("测试期扣成本年化：AI 因子、起点与随机因子")}</div>
          <PlaceboChart v={v} />
          <p className="text-[11px] text-faint mt-1">{t("每个点是一次回测。AI 因子的点应明显在随机因子的右边，才能说明 AI 找到的不是噪声。")}</p>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs text-muted"><tr>
            <th className="text-left py-1.5 font-normal">{t("时间段")}</th><th className="text-left font-normal">{t("模型")}</th>
            <th className="text-right font-normal">{t("扣成本年化（中位数）")}</th><th className="text-right font-normal">{t("多次回测范围")}</th>
            <th className="text-right font-normal">{t("最大回撤（中位数）")}</th><th className="text-right font-normal">rank IC</th>
            <th className="text-right font-normal">{t("显著性")}</th><th className="text-right font-normal">{t("第 5 组 − 第 1 组")}</th>
          </tr></thead>
          <tbody>
            {(["test", "holdout"] as const).flatMap((p) => ([[t("AI 因子"), ai[p]], [t("起点"), bl[p]]] as [string, ValPeriod][]).map(([name, m], i) => (
              <tr key={`${p}-${name}`} className="border-t border-line">
                <td className="py-1.5 pr-3 whitespace-nowrap">{i === 0 ? (
                  <span>{p === "test" ? t("测试期") : t("2024 年以后（未见过）")}<span className="num text-xs text-faint ml-1.5">{tf("{a} 至 {b}", { a: v.periods[p].start, b: v.periods[p].end })}</span></span>) : ""}</td>
                <td className="pr-3">{name}</td>
                <td className="num text-right pr-3">{pct(m.annual_return_with_cost)}</td>
                <td className="num text-right pr-3 text-muted">{range(m.annual_return_with_cost_range)}</td>
                <td className="num text-right pr-3">{pct(m.max_drawdown_with_cost)}</td>
                <td className="num text-right pr-3">{fix(m.rank_ic, 4)}</td>
                <td className="num text-right pr-3">{m.rank_ic_t == null ? "—" : <span className={Math.abs(m.rank_ic_t) > 2 ? "text-text" : "text-faint"}>{tTxt(m.rank_ic_t)}</span>}</td>
                <td className="num text-right">{m.spread_mean == null ? "—" : `${m.spread_mean > 0 ? "+" : ""}${(m.spread_mean * 100).toFixed(2)}%`}</td>
              </tr>
            )))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-muted leading-relaxed max-w-[120ch]">
        {t("复现：用完整数据重新运行 AI 的因子代码，2023 年底之前的值与 RD-Agent 当时的 ")}{repro >= 0.99 ? t("一致") : t("只有部分一致")}{tf("（{p}）", { p: pct(repro, 1) })}
        {repro < 0.99 && t("，代码可能用到了未来数据，结果不可信")}{t("。模型、参数、持股规则与交易成本与该轮完全相同，训练只用 2015–2021 年的数据。")}
        {v.errors.length > 0 && <span className="text-bad"> {tf("{n} 次随机因子回测失败。", { n: v.errors.length })}</span>}
      </p>
    </div>
  );
}

function CurveChart({ v }: { v: Validation }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), accent = cssVar(`--${v.market}`), text = cssVar("--text");
    const series = [
      ...v.curve.baseline.map((d, i) => ({ name: t("起点"), type: "line", data: d, showSymbol: false, silent: true,
        lineStyle: { width: 1, color: muted, opacity: 0.45 }, itemStyle: { color: muted }, z: 1, ...(i === 0 ? {} : { legendHoverLink: false }) })),
      ...v.curve.ai.map((d) => ({ name: t("AI 因子"), type: "line", data: d, showSymbol: false, silent: true,
        lineStyle: { width: 1.3, color: accent, opacity: 0.75 }, itemStyle: { color: accent }, z: 2 })),
    ];
    (series[0] as Record<string, unknown>).markArea = {
      silent: true, itemStyle: { color: accent, opacity: 0.07 },
      label: { color: text, position: "insideTop", fontSize: 11 },
      data: [[{ xAxis: v.curve.dates.find((d) => d >= v.periods.holdout.start), name: t("AI 从未见过的数据") }, { xAxis: v.curve.dates.at(-1) }]],
    };
    (series[0] as Record<string, unknown>).markLine = { symbol: "none", silent: true, data: [{ yAxis: 0 }], lineStyle: { color: muted, type: "dashed" }, label: { show: false } };
    return {
      animationDuration: 900,
      legend: { top: 0, right: 8, textStyle: { color: muted }, data: [t("AI 因子"), t("起点")], itemStyle: { opacity: 1 } },
      grid: { left: 52, right: 16, top: 30, bottom: 28 },
      tooltip: { show: false },
      xAxis: { type: "category", data: v.curve.dates, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", axisLabel: { color: muted, formatter: (x: number) => `${Math.round(x * 100)}%` }, splitLine: { lineStyle: { color: line, opacity: 0.35 } } },
      series,
    };
  }, [v, theme]);
  return <EChart option={option} notMerge className="h-[320px]" />;
}

function PlaceboChart({ v }: { v: Validation }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text");
    const cats = [t("随机因子"), t("起点"), t("AI 因子")];
    const jitter = (i: number) => ((i * 37) % 11) / 11 * 0.5 - 0.25;   // 固定的上下错开，避免重叠
    const pts = (vals: number[] | undefined, y: number) => (vals ?? []).map((x, i) => [x, y + jitter(i)]);
    const placebo = (v.placebo?.runs ?? []).map((r) => r.annual_return_with_cost).filter((x): x is number => x != null);
    const series = [
      { name: t("随机因子"), type: "scatter", data: pts(placebo, 0), symbolSize: 8, itemStyle: { color: muted, opacity: 0.7 } },
      { name: t("起点"), type: "scatter", data: pts(v.baseline.test.annual_return_with_cost_all, 1), symbolSize: 8, itemStyle: { color: "transparent", borderColor: muted, borderWidth: 1.5 } },
      { name: t("AI 因子"), type: "scatter", data: pts(v.ai.test.annual_return_with_cost_all, 2), symbolSize: 9, itemStyle: { color: cssVar(`--${v.market}`) } },
    ];
    const reported = v.original?.ai?.annual_return_with_cost;
    if (reported != null) {
      (series[2] as Record<string, unknown>).markLine = { symbol: "none", silent: true, lineStyle: { color: text, type: "dashed" },
        label: { color: text, formatter: t("RD-Agent 报告"), fontSize: 10 }, data: [{ xAxis: reported }] };
    }
    return {
      animationDuration: 800,
      grid: { left: 64, right: 24, top: 18, bottom: 32 },
      tooltip: { trigger: "item", backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        formatter: (p: { seriesName: string; value: number[] }) => `${p.seriesName}${IS_ZH ? "：" : ": "}${(p.value[0] * 100).toFixed(1)}%` },
      xAxis: { type: "value", axisLabel: { color: muted, formatter: (x: number) => `${Math.round(x * 100)}%` }, splitLine: { lineStyle: { color: line, opacity: 0.35 } } },
      yAxis: { type: "value", min: -0.5, max: 2.5, interval: 0.5, axisTick: { show: false },
        axisLabel: { color: muted, formatter: (y: number) => (Number.isInteger(y) ? cats[y] ?? "" : "") }, splitLine: { show: false } },
      series,
    };
  }, [v, theme]);
  return <EChart option={option} notMerge className="h-[320px]" />;
}
