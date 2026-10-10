import { useEffect, useMemo, useState } from "react";
import EChart from "./EChart";
import type { Job, LiveState } from "../api";
import { cssVar, MARKET_LABEL, useThemeVersion } from "../lib";
import { Notice } from "./ui";

const STEPS = [["direct_exp_gen", "提出假设"], ["coding", "编写代码"], ["running", "Qlib 回测"], ["feedback", "评审结果"]] as const;
const KIND_COLOR: Record<string, string> = { step: "--accent", docker: "--uk", exec: "--good", result: "--fr", error: "--bad" };
const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const hhmmss = (t: string | null) => (t ? new Date(t).toLocaleTimeString("zh-CN", { hour12: false }) : "");

/** 每秒更新一次的当前时间，用于运行中的计时。 */
function useNow(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  return now;
}
const since = (t: string | null, now: number) => (t ? Math.max(0, (now - new Date(t).getTime()) / 1000) : 0);

/**
 * 运行中的实时状态（运行器每 3 秒解析一次 RD-Agent 控制台输出）：
 * 阶段进度、当前步骤与计时、正在执行的回测容器、计数、各次回测结果（实时柱状图）、最近活动。
 */
export default function LiveStatus({ job, live }: { job: Job; live: LiveState }) {
  const now = useNow();
  const color = cssVar(`--${job.market}`);
  const current = live.phase === "loop" ? live.loop ?? 0 : -1;   // -1：准备阶段
  const stepIdx = STEPS.findIndex(([k]) => k === live.step);
  const running = job.state === "running";
  const stale = since(live.updated_at ?? null, now) > 30;
  return (
    <section className="panel p-4 flex flex-col gap-4" style={{ borderColor: `color-mix(in oklab, ${color} 45%, var(--line))` }}>
      <div className="flex flex-wrap items-center gap-3">
        <span className={`size-2.5 rounded-full ${running ? "animate-pulse" : ""}`} style={{ background: color, boxShadow: running ? `0 0 12px ${color}` : undefined }} />
        <span className="font-medium">{MARKET_LABEL[job.market]} · {running ? "实时状态" : `最后状态（${job.state_zh}）`}</span>
        <span className="num text-xs text-muted">{job.research_id} v{job.version} · {job.loops} 轮 · 费用 ${job.cost_usd.toFixed(3)} / ${job.budget_usd}</span>
        <span className="num text-xs text-faint ml-auto">{!running ? `最后输出 ${hhmmss(live.last_log_at)}` : stale ? "等待新的输出…" : `更新于 ${hhmmss(live.updated_at ?? null)}`}</span>
      </div>
      {live.fatal && <Notice tone="bad" title="运行中止">{live.fatal}</Notice>}

      {/* 阶段进度：准备 → 第 0 … N-1 轮 */}
      <ol className="flex gap-1.5" aria-label="运行阶段">
        {[-1, ...Array.from({ length: job.loops }, (_, i) => i)].map((i) => {
          const done = i < current, on = i === current;
          return (
            <li key={i} className={`flex-1 min-w-0 text-center text-[11px] py-1.5 rounded border transition-colors ${on ? "text-text" : done ? "text-muted" : "text-faint border-line"}`}
              style={on ? { borderColor: color, background: `color-mix(in oklab, ${color} 22%, transparent)`, boxShadow: `0 0 14px color-mix(in oklab, ${color} 45%, transparent)` }
                : done ? { borderColor: `color-mix(in oklab, ${color} 35%, var(--line))`, background: `color-mix(in oklab, ${color} 8%, transparent)` } : undefined}>
              {i < 0 ? "准备" : `第 ${i} 轮`}
            </li>
          );
        })}
      </ol>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <div className="flex flex-col gap-3">
          {live.phase === "loop" ? (
            <ol className="grid grid-cols-4 gap-1.5" aria-label="当前轮次的步骤">
              {STEPS.map(([k, zh], i) => {
                const on = i === stepIdx, done = i < stepIdx;
                return (
                  <li key={k} className={`rounded border px-2 py-2 text-center ${on ? "" : done ? "text-muted" : "text-faint border-line"}`}
                    style={on ? { borderColor: color, background: `color-mix(in oklab, ${color} 18%, transparent)` } : undefined}>
                    <div className="text-xs">{zh}</div>
                    <div className="num text-[11px] mt-0.5">{on ? (running ? mmss(since(live.step_started_at, now)) : "中止") : done ? "✓" : "—"}</div>
                  </li>
                );
              })}
            </ol>
          ) : (
            <div className="text-sm text-muted">RD-Agent 正在准备：生成该市场的数据文件、检查运行环境，第 0 轮的回测步骤中会先跑起点（20 个基础因子）。</div>
          )}
          {!running ? null : live.docker ? (
            <div className="flex flex-col gap-1">
              <div className="flex text-xs"><span>正在：{live.docker.text}</span><span className="num ml-auto text-muted">{mmss(since(live.docker.started_at, now))}</span></div>
              <div className="h-1.5 rounded live-bar" />
            </div>
          ) : live.step === "coding" ? (
            <div className="flex flex-col gap-1">
              <div className="text-xs">AI 正在编写因子代码，运行检查并修正错误</div>
              <div className="h-1.5 rounded live-bar" />
            </div>
          ) : null}
          <dl className="grid grid-cols-3 gap-2 text-xs">
            <div><dt className="text-faint">AI 调用</dt><dd className="num text-xl">{live.counters.llm}</dd></div>
            <div><dt className="text-faint">代码执行成功</dt><dd className="num text-xl">{live.counters.exec_ok}</dd></div>
            <div><dt className="text-faint">回测次数</dt><dd className="num text-xl">{live.backtests.length}</dd></div>
          </dl>
          <div>
            <div className="label mb-1">最近活动</div>
            <ol className="flex flex-col gap-1 max-h-[220px] overflow-y-auto pr-1" aria-live="polite">
              {[...live.activity].reverse().slice(0, 14).map((a, i) => (
                <li key={`${a.t}-${a.text}-${live.activity.length - i}`} className="live-in flex gap-2 text-xs leading-snug">
                  <span className="num text-faint w-16 shrink-0">{hhmmss(a.t)}</span>
                  <span className="size-1.5 rounded-full mt-1.5 shrink-0" style={{ background: cssVar(KIND_COLOR[a.kind] ?? "--muted") }} />
                  <span className={a.kind === "result" ? "text-text" : a.kind === "error" ? "text-bad" : "text-muted"}>{a.text}</span>
                </li>
              ))}
              {live.activity.length === 0 && <li className="text-xs text-faint">等待 RD-Agent 输出…</li>}
            </ol>
          </div>
        </div>
        <div>
          <div className="label mb-1">各次回测结果（扣成本年化超额，实时）</div>
          <BacktestBars live={live} loops={job.loops} />
          <p className="text-[11px] text-faint mt-1">每完成一次 Qlib 回测增加一根柱子；虚线为起点（20 个基础因子）。是否采纳由随后的 AI 评审决定。</p>
        </div>
      </div>
    </section>
  );
}

function BacktestBars({ live, loops }: { live: LiveState; loops: number }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text");
    const cats = ["起点", ...Array.from({ length: loops }, (_, i) => `第 ${i} 轮`)];
    const byKey = new Map(live.backtests.map((b) => [b.loop == null ? "起点" : `第 ${b.loop} 轮`, b]));
    const base = byKey.get("起点")?.with_cost?.annualized_return;
    return {
      animationDuration: 900, animationEasingUpdate: "cubicOut",
      grid: { left: 48, right: 12, top: 18, bottom: 28 },
      tooltip: {
        trigger: "axis", backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        formatter: (ps: { name: string }[]) => {
          const b = byKey.get(ps[0].name)?.with_cost;
          return b ? `<b>${ps[0].name}</b><br/>扣成本年化 ${((b.annualized_return ?? 0) * 100).toFixed(1)}%<br/>信息比率 ${(b.information_ratio ?? 0).toFixed(2)}<br/>最大回撤 ${((b.max_drawdown ?? 0) * 100).toFixed(1)}%`
            : `${ps[0].name}：尚未回测`;
        },
      },
      xAxis: { type: "category", data: cats, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", axisLabel: { color: muted, formatter: (v: number) => `${Math.round(v * 100)}%` }, splitLine: { lineStyle: { color: line, opacity: 0.4 } } },
      series: [{
        type: "bar", barWidth: "46%",
        data: cats.map((c) => {
          const v = byKey.get(c)?.with_cost?.annualized_return;
          if (v == null) return { value: null };
          const col = c === "起点" ? cssVar("--muted") : v >= (base ?? 0) ? cssVar("--good") : cssVar("--bad");
          return { value: v, itemStyle: { color: col, borderRadius: 2 } };
        }),
        label: { show: true, position: "top", color: text, fontSize: 11, formatter: (p: { value: number | null }) => (p.value == null ? "" : `${(p.value * 100).toFixed(1)}%`) },
        markLine: base == null ? undefined : { symbol: "none", silent: true, data: [{ yAxis: base }], lineStyle: { color: muted, type: "dashed" }, label: { show: false } },
      }],
    };
  }, [live.backtests, loops, theme]);
  return <EChart option={option} className="h-[300px]" />;
}
