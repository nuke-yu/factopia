import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, MARKETS, type Job, type LiveState, type Market, type Round, type RunEvent } from "../api";
import { cssVar, fix, MARKET_LABEL, pct, reducedMotion, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";
import ResearchTrail from "../components/charts/ResearchTrail";
import NavChart from "../components/charts/NavChart";
import LiveStatus from "../components/LiveStatus";

const PIPELINE = ["提出假设", "编写代码", "Qlib 回测", "评审结果"];
const LIVE_POLL_MS = 5000;
const SPEEDS = [10, 30, 60, 120];
const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export default function RunPage() {
  const [params, setParams] = useSearchParams();
  const jobId = params.get("job");
  const runs = useAsync(() => api.runs(), [params.get("run")]);
  const runId = params.get("run") ?? (jobId ? "" : runs.data?.find((r) => r.rounds >= 3)?.run_id ?? runs.data?.[0]?.run_id ?? "");
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <div className="label">03 · 运行中</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">AI 研究过程</h1>
          <p className="text-muted text-sm mt-1 max-w-[80ch]">每一轮：AI 提出假设 → 写代码并自检修错 → Qlib 回测 → AI 评审并决定下一步。运行中每 5 秒刷新；已完成的运行可按真实时间戳加速回放。</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label htmlFor="run-sel" className="label whitespace-nowrap">运行</label>
          <select id="run-sel" className="field w-auto num text-sm" value={runId} onChange={(e) => setParams({ run: e.target.value })}>
            {!runId && <option value="">等待运行开始…</option>}
            {runs.data?.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {MARKET_LABEL[r.market]} · {r.rounds} 轮</option>)}
          </select>
        </div>
      </div>
      {runs.error && <ErrorBox error={runs.error} />}
      {!runId && jobId && <JobWaiting jobId={jobId} onRun={(rid) => setParams({ run: rid, job: jobId })} />}
      {runId && <Replay key={runId} runId={runId} />}
    </div>
  );
}

/** 周期刷新：enabled 时每隔 ms 毫秒返回新的计数，用作 useAsync 的依赖。 */
function useTick(enabled: boolean, ms: number): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => setTick((x) => x + 1), ms);
    return () => clearInterval(id);
  }, [enabled, ms]);
  return tick;
}

/** 任务排队或 RD-Agent 正在准备数据（还没有进度事件）时：显示任务状态与控制台输出。 */
function JobWaiting({ jobId, onRun }: { jobId: string; onRun: (runId: string) => void }) {
  const [final, setFinal] = useState(false);
  const tick = useTick(!final, 3000);
  const job = useAsync(() => api.job(jobId), [jobId, tick]);
  const queue = useAsync(() => api.jobs(), [tick]);
  const live = useAsync(() => api.jobLive(jobId), [jobId, tick]);
  const j = job.data;
  useEffect(() => {
    if (!j) return;
    if (j.exported && j.run_id) onRun(j.run_id);
    else if (["done", "failed", "cancelled", "budget_stopped"].includes(j.state)) setFinal(true);
  }, [j]); // eslint-disable-line react-hooks/exhaustive-deps
  if (job.error) return <ErrorBox error={job.error} />;
  if (!j) return <Loading what="任务" />;
  const ahead = (queue.data ?? []).filter((x) => x.job_id < j.job_id && (x.state === "queued" || x.state === "running")).length;
  return (
    <div className="flex flex-col gap-4">
      <section className="panel p-4 flex flex-wrap items-center gap-3">
        <Badge tone={j.state === "failed" ? "bad" : j.state === "running" ? "accent" : "muted"}>{j.state_zh}</Badge>
        <span className="font-medium">{j.research_id} v{j.version}</span>
        <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: cssVar(`--${j.market}`) }} />{MARKET_LABEL[j.market]}</span>
        <span className="num text-xs text-muted">{j.loops} 轮 · 上限 ${j.budget_usd}</span>
        {j.state === "queued" && <span className="text-sm text-muted">前面还有 {ahead} 个任务</span>}
        {j.state === "running" && <span className="text-sm text-muted">RD-Agent 正在准备数据与基准回测，第一轮开始后自动切换到过程视图…</span>}
        {(j.state === "queued" || j.state === "running") && <div className="ml-auto"><StopButton jobId={j.job_id} /></div>}
        {["failed", "cancelled", "budget_stopped"].includes(j.state) && <div className="ml-auto"><RetryButton jobId={j.job_id} /></div>}
      </section>
      {j.error && <Notice tone="bad" title="运行失败">{j.error}</Notice>}
      {isLive(live.data) && <LiveStatus job={j} live={live.data} />}
      <details className="panel p-4" open={!isLive(live.data)}>
        <summary className="cursor-pointer label">RD-Agent 原始输出（最后 40 行）</summary>
        <pre className="num text-[11px] whitespace-pre-wrap leading-relaxed text-muted max-h-[420px] overflow-y-auto bg-bg rounded p-3 mt-2">
          {j.log_tail.length ? j.log_tail.join("\n").replace(/\x1b\[[0-9;]*m/g, "") : "等待输出…"}
        </pre>
      </details>
    </div>
  );
}

const isLive = (x: LiveState | Record<string, never> | undefined): x is LiveState => !!x && "phase" in x;

/** 失败或停止的任务：用同一版本、市场与参数重新排队。 */
function RetryButton({ jobId }: { jobId: string }) {
  const [, setParams] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  return (
    <span className="inline-flex items-center gap-2">
      <button className="btn" onClick={async () => {
        try { const r = await api.retryJob(jobId); setParams({ job: r.jobs[0].job_id }); } catch (e) { setError((e as Error).message); }
      }}>重新运行</button>
      {error && <span className="text-xs text-bad">{error}</span>}
    </span>
  );
}

/** 停止运行：点击后再次确认。 */
function StopButton({ jobId }: { jobId: string }) {
  const [step, setStep] = useState<"idle" | "confirm" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);
  if (step === "sent") return <span className="text-sm text-warn">已请求停止，当前步骤结束后停止…</span>;
  return (
    <span className="inline-flex items-center gap-2">
      {step === "confirm" && <span className="text-sm text-warn">已完成的轮次会保留。确认停止？</span>}
      <button className={`btn ${step === "confirm" ? "border-bad text-bad" : ""}`} onClick={async () => {
        if (step === "idle") return setStep("confirm");
        try { await api.cancelJob(jobId); setStep("sent"); } catch (e) { setError((e as Error).message); }
      }}>{step === "confirm" ? "确认停止" : "停止运行"}</button>
      {step === "confirm" && <button className="btn" onClick={() => setStep("idle")}>取消</button>}
      {error && <span className="text-xs text-bad">{error}</span>}
    </span>
  );
}

function Replay({ runId }: { runId: string }) {
  const [running, setLive] = useState(false);
  const tick = useTick(running, LIVE_POLL_MS);
  const detail = useAsync(() => api.run(runId), [runId, tick]);
  const events = useAsync(() => api.events(runId), [runId, tick]);
  const jobId = detail.data?.job?.job_id;
  const liveState = useAsync(() => (jobId && running ? api.jobLive(jobId) : Promise.resolve({})), [jobId, tick]);
  const backtest = useAsync(() => (running ? api.backtest(runId) : Promise.resolve({})), [runId, Math.floor(tick / 3)]);
  const total = events.data?.at(-1)?.t ?? 0;
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState(!reducedMotion());
  const [speed, setSpeed] = useState(30);
  const [follow, setFollow] = useState(true);   // 运行中：进度条跟随最新事件
  const [prompt, setPrompt] = useState<number | null>(null);

  useEffect(() => { if (detail.data) setLive(detail.data.run.state === "运行中"); }, [detail.data]);
  useEffect(() => { if (running) setPlaying(false); }, [running]);
  useEffect(() => { if (running && follow) setClock(total); }, [running, follow, total]);
  useEffect(() => { if (events.data && reducedMotion()) setClock(total); }, [events.data, total]);
  useEffect(() => {
    if (!playing || !events.data) return;
    let raf = 0, last = performance.now();
    const tick = (now: number) => {
      const dt = (now - last) / 1000; last = now;
      setClock((c) => Math.min(total, c + dt * speed));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, speed, total, events.data]);
  useEffect(() => { if (clock >= total && total > 0) setPlaying(false); }, [clock, total]);

  if (!detail.data || !events.data) {
    if (detail.error || events.error) return <ErrorBox error={(detail.error ?? events.error)!} />;
    return <Loading what="运行过程" />;
  }
  const { run, rounds, job, batch } = detail.data;
  const visible = events.data.filter((e) => e.t <= clock);
  const finished = !running && clock >= total;

  return (
    <>
      {run.context_leak && (
        <Notice tone="bad" title="此次运行受数据泄漏影响（MINE-11 修复前），仅用于展示过程">运行时提供给 AI 的已有发现包含测试期的评估结论。</Notice>
      )}
      <section className="panel p-4 flex flex-wrap items-center gap-3">
        {running ? (
          <>
            <span className="inline-flex items-center gap-2 text-sm text-accent"><span className="size-2 rounded-full bg-accent animate-pulse" />实时 · 每 5 秒刷新</span>
            {!follow && <button className="btn" onClick={() => setFollow(true)}>回到最新</button>}
          </>
        ) : (
          <>
            <Badge tone={run.state === "已完成" ? "muted" : "warn"}>{run.state === "已完成" ? "回放" : `${run.state} · 回放`}</Badge>
            <button className="btn w-20 justify-center" onClick={() => { if (clock >= total) setClock(0); setPlaying((p) => !p); }}>
              {playing ? "暂停" : clock >= total ? "重新播放" : "播放"}
            </button>
            <Seg label="回放速度" value={speed} onChange={setSpeed} options={SPEEDS.map((s) => ({ value: s, label: `${s}×` }))} />
          </>
        )}
        <input aria-label="回放进度" type="range" min={0} max={total} step={0.5} value={clock} className="flex-1 min-w-48 accent-[var(--accent)]"
          onChange={(e) => { setPlaying(false); setFollow(false); setClock(Number(e.target.value)); }} />
        <span className="num text-sm text-muted w-28 text-right">{mmss(clock)} / {mmss(total)}</span>
        {running && job ? <StopButton jobId={job.job_id} /> : <button className="btn" onClick={() => { setPlaying(false); setClock(total); }}>跳到结尾</button>}
      </section>
      {job && (
        <div className="text-xs text-muted flex flex-wrap gap-x-4">
          <span>研究规格 <Link to="/projects" className="text-accent underline">{job.research_id} v{job.version}</Link></span>
          <span className="num">任务 {job.job_id}</span>
          <span>{job.loops} 轮 · 费用上限 ${job.budget_usd}</span>
          {job.state !== "running" && <span>任务状态：{job.state_zh}</span>}
        </div>
      )}

      {running && job && isLive(liveState.data) && <LiveStatus job={job} live={liveState.data} />}
      <div className="grid gap-4 md:grid-cols-3">
        {MARKETS.map((m) => {
          if (m === run.market) return <Lane key={m} market={m} loops={job?.loops ?? run.rounds} visible={visible} clock={clock} />;
          const other = batch.find((j) => j.market === m);
          return other ? <BatchLane key={m} job={other} /> : <IdleLane key={m} market={m} />;
        })}
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,6fr)_minmax(0,5fr)]">
        <Panel eyebrow="随轮次生长" title="研究轨迹">
          <div className="h-[560px] -mx-1">
            <ResearchTrail run={run} rounds={trailRounds(rounds, visible)} selected={String(visible.at(-1)?.loop ?? "baseline")} onSelect={() => undefined} />
          </div>
        </Panel>
        <Panel eyebrow="最新在上" title="过程记录">
          <Feed events={visible} onPrompt={setPrompt} />
        </Panel>
      </div>

      {running && backtest.data && Object.keys(backtest.data).length > 0 && (
        <Panel eyebrow="每完成一轮更新" title="各轮回测净值（实时，扣成本后超额）">
          <NavChart backtest={backtest.data} rounds={rounds} selected={String(rounds.filter((r) => r.decision).at(-1)?.loop ?? "")} className="h-[320px]" />
        </Panel>
      )}
      {finished && (
        <Notice tone="accent" title={run.state === "已完成" ? "运行结束" : `运行${run.state}`}>
          共 {run.rounds} 轮，采纳 {run.adopted} 轮，大模型费用 ${run.cost_usd.toFixed(2)}。<Link to={`/results?run=${run.run_id}`} className="text-accent underline">查看结果分析 →</Link>
          {job && <span className="text-muted">（回测明细在运行结束后约 1 分钟内导出）</span>}
        </Notice>
      )}
      {prompt != null && <PromptModal runId={runId} index={prompt} onClose={() => setPrompt(null)} />}
    </>
  );
}

/** 轨迹只显示已完成评审的轮次，正在进行的一轮显示为闪烁节点。 */
function trailRounds(rounds: Round[], visible: RunEvent[]): Round[] {
  const decided = new Set(visible.filter((e) => e.kind === "decision").map((e) => e.loop));
  const current = visible.at(-1)?.loop;
  return rounds.flatMap((r) => decided.has(r.loop) ? [r]
    : r.loop === current ? [{ ...r, status: "running" as const, metrics: null, decision: null }] : []);
}

function Lane({ market, loops, visible, clock }: { market: Market; loops: number; visible: RunEvent[]; clock: number }) {
  const lastStep = [...visible].reverse().find((e) => e.kind === "step" && e.name !== "记录") as Extract<RunEvent, { kind: "step" }> | undefined;
  const loop = visible.at(-1)?.loop ?? 0;
  const done = visible.filter((e) => e.kind === "decision").length;
  const adopted = visible.filter((e) => e.kind === "decision" && e.decision).length;
  const cost = ([...visible].reverse().find((e) => e.kind === "cost") as Extract<RunEvent, { kind: "cost" }> | undefined)?.total_usd ?? 0;
  const calls = visible.filter((e) => e.kind === "llm").length;
  const fixes = visible.filter((e) => e.kind === "check").flatMap((e) => (e as Extract<RunEvent, { kind: "check" }>).items).filter((i) => !i.ok).length;
  // RD-Agent 在步骤结束时才记录开始时间：上一步已结束、下一步尚无记录时，高亮下一步
  const active = !lastStep || done >= loops ? null : lastStep.phase === "start" ? lastStep.name
    : PIPELINE[(PIPELINE.indexOf(lastStep.name) + 1) % PIPELINE.length];
  const color = cssVar(`--${market}`);
  return (
    <section className="panel p-4" style={{ borderColor: `color-mix(in oklab, ${color} 45%, var(--line))` }}>
      <div className="flex items-center gap-2">
        <span className="size-2.5 rounded-full" style={{ background: color, boxShadow: `0 0 10px ${color}` }} />
        <span className="font-medium">{MARKET_LABEL[market]}</span>
        <span className="num text-xs text-muted ml-auto">第 {loop} 轮 · 已完成 {done} / {loops}</span>
      </div>
      <ol className="grid grid-cols-4 gap-1.5 mt-3" aria-label="当前步骤">
        {PIPELINE.map((s) => {
          const on = s === active;
          return (
            <li key={s} className={`text-center text-[11px] py-1.5 rounded border transition-colors ${on ? "text-text" : "text-faint border-line"}`}
              style={on ? { borderColor: color, background: `color-mix(in oklab, ${color} 18%, transparent)`, boxShadow: `0 0 12px color-mix(in oklab, ${color} 40%, transparent)` } : undefined}>
              {s}
            </li>
          );
        })}
      </ol>
      <dl className="grid grid-cols-4 gap-2 mt-3 text-xs">
        <div><dt className="text-faint">采纳</dt><dd className="num text-base">{adopted}</dd></div>
        <div><dt className="text-faint">AI 调用</dt><dd className="num text-base">{calls}</dd></div>
        <div><dt className="text-faint">自检修错</dt><dd className="num text-base">{fixes}</dd></div>
        <div><dt className="text-faint">费用</dt><dd className="num text-base">${cost.toFixed(3)}</dd></div>
      </dl>
      <div className="num text-[11px] text-faint mt-2">已用时 {mmss(clock)}</div>
    </section>
  );
}

/** 同一批次中其他市场的任务：排队、运行中或已结束。 */
function BatchLane({ job }: { job: Job }) {
  const color = cssVar(`--${job.market}`);
  const to = job.run_id ? `/run?run=${job.run_id}&job=${job.job_id}` : `/run?job=${job.job_id}`;
  return (
    <section className={`panel p-4 ${job.state === "queued" ? "opacity-70" : ""}`}>
      <div className="flex items-center gap-2">
        <span className="size-2.5 rounded-full" style={{ background: color }} />
        <span className="font-medium">{MARKET_LABEL[job.market]}</span>
        <span className="ml-auto"><Badge tone={job.state === "running" ? "accent" : job.state === "done" ? "good" : job.state === "queued" ? "muted" : "warn"}>{job.state_zh}</Badge></span>
      </div>
      <p className="text-sm text-muted mt-3">
        {job.state === "queued" ? "同一版本，排在当前市场之后依次运行。" : `${job.rounds} 轮 · 费用 $${job.cost_usd.toFixed(3)}`}
      </p>
      {job.state !== "queued" && <Link to={to} className="text-accent underline text-sm">查看该市场的过程 →</Link>}
    </section>
  );
}

function IdleLane({ market }: { market: Market }) {
  return (
    <section className="panel p-4 opacity-55">
      <div className="flex items-center gap-2">
        <span className="size-2.5 rounded-full" style={{ background: cssVar(`--${market}`) }} />
        <span className="font-medium">{MARKET_LABEL[market]}</span>
      </div>
      <p className="text-sm text-muted mt-3">本次运行未包含该市场。研究规格锁定后，可在三个市场分别运行（RS-09）。</p>
    </section>
  );
}

function Feed({ events, onPrompt }: { events: RunEvent[]; onPrompt: (i: number) => void }) {
  const shown = events.filter((e) => e.kind !== "cost" && !(e.kind === "llm" && e.purpose === "编写 / 检查代码") && !(e.kind === "step" && (e.phase === "end" || e.name === "记录")));
  const recent = shown.slice(-60).reverse();
  return (
    <ol className="flex flex-col gap-2 h-[560px] overflow-y-auto pr-1" aria-live="polite">
      {recent.length === 0 && <li className="text-muted text-sm">等待第一轮开始…</li>}
      {recent.map((e, i) => <FeedItem key={e.index} e={e} fresh={i === 0} onPrompt={onPrompt} />)}
    </ol>
  );
}

function FeedItem({ e, fresh, onPrompt }: { e: RunEvent; fresh: boolean; onPrompt: (i: number) => void }) {
  const head = (label: string, tone?: string) => (
    <div className="flex items-center gap-2 text-xs">
      <span className="num text-faint w-14">{mmss(e.t)}</span>
      <span className="num text-faint">R{e.loop}</span>
      <span style={tone ? { color: tone } : undefined} className={tone ? "" : "text-muted"}>{label}</span>
    </div>
  );
  const box = `border-l-2 pl-3 py-1 ${fresh ? "bg-accent/6" : ""}`;
  switch (e.kind) {
    case "step":
      return <li className={box} style={{ borderColor: cssVar("--line-2") }}>{head(`开始：${e.name}`)}</li>;
    case "llm":
      return (
        <li className={box} style={{ borderColor: cssVar("--accent") }}>
          {head(`AI 调用 · ${e.purpose}`, cssVar("--accent"))}
          <div className="text-xs text-muted mt-0.5 num">提示词 {(e.prompt_chars / 1000).toFixed(1)}k 字 · 用时 {e.seconds ?? "—"} 秒
            {e.has_prompt && <button className="ml-2 text-accent underline" onClick={() => onPrompt(e.index)}>查看提示词</button>}
          </div>
        </li>
      );
    case "hypothesis":
      return (
        <li className={box} style={{ borderColor: cssVar("--accent") }}>
          {head("提出假设", cssVar("--text"))}
          <Typed text={e.text} animate={fresh} />
        </li>
      );
    case "factors":
      return (
        <li className={box} style={{ borderColor: cssVar("--line-2") }}>
          {head(`定义 ${e.factors.length} 个因子`)}
          <div className="flex flex-wrap gap-1.5 mt-1">{e.factors.map((f) => <span key={f.name} title={f.description}><Badge tone="accent">{f.name}</Badge></span>)}</div>
        </li>
      );
    case "check":
      return (
        <li className={box} style={{ borderColor: e.items.every((i) => i.ok) ? cssVar("--good") : cssVar("--warn") }}>
          {head(`代码自检 · 第 ${e.attempt} 次`, e.items.every((i) => i.ok) ? cssVar("--good") : cssVar("--warn"))}
          <ul className="mt-1 flex flex-col gap-0.5">
            {e.items.map((i) => (
              <li key={i.name} className="text-xs flex gap-2">
                <span className="num w-4 shrink-0" style={{ color: i.ok ? cssVar("--good") : cssVar("--bad") }}>{i.ok ? "✓" : "✗"}</span>
                <span className="num shrink-0">{i.name}</span>
                {!i.ok && <span className="text-muted truncate" title={i.message}>{i.message}</span>}
              </li>
            ))}
          </ul>
        </li>
      );
    case "coded":
      return <li className={box} style={{ borderColor: cssVar("--good") }}>{head(`代码完成：${e.passed} / ${e.total} 个因子通过自检`, cssVar("--good"))}</li>;
    case "backtest":
      return (
        <li className={box} style={{ borderColor: cssVar("--uk") }}>
          {head("Qlib 回测完成", cssVar("--text"))}
          <div className="num text-xs text-muted mt-0.5">IC {fix(e.metrics?.ic)} · 扣成本年化 {pct(e.metrics?.annual_return_with_cost)} · 最大回撤 {pct(e.metrics?.max_drawdown_with_cost)}</div>
        </li>
      );
    case "decision":
      return (
        <li className={box} style={{ borderColor: e.decision ? cssVar("--good") : cssVar("--bad") }}>
          <div className="flex items-center gap-2">{head("AI 评审")}{e.decision ? <Badge tone="good">采纳</Badge> : <Badge tone="bad">未采纳</Badge>}</div>
          <p className="text-xs text-muted mt-1 leading-relaxed line-clamp-3" title={e.observations}>{e.observations}</p>
          <p className="text-xs mt-1 leading-relaxed line-clamp-3"><span className="text-faint">下一步：</span>{e.new_hypothesis}</p>
        </li>
      );
    default:
      return null;
  }
}

/** 新出现的假设逐字显示（真实时间约 1.5 秒），之后静态显示。 */
function Typed({ text, animate }: { text: string; animate: boolean }) {
  const [n, setN] = useState(animate && !reducedMotion() ? 0 : text.length);
  useEffect(() => {
    if (n >= text.length) return;
    const id = setTimeout(() => setN((x) => Math.min(text.length, x + Math.ceil(text.length / 45))), 33);
    return () => clearTimeout(id);
  }, [n, text]);
  return <p className="text-sm leading-relaxed mt-1">{text.slice(0, n)}{n < text.length && <span className="text-accent">▍</span>}</p>;
}

function PromptModal({ runId, index, onClose }: { runId: string; index: number; onClose: () => void }) {
  const p = useAsync(() => api.prompt(runId, index), [runId, index]);
  const [tab, setTab] = useState<"system" | "user" | "response">("user");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const text = useMemo(() => (p.data ? (p.data[tab] ?? "") : ""), [p.data, tab]);
  return (
    <div className="fixed inset-0 z-40 bg-black/60 grid place-items-center p-4" role="dialog" aria-modal="true" aria-label="发给 AI 的提示词" onClick={onClose}>
      <div className="panel w-full max-w-5xl max-h-[85vh] flex flex-col p-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-3">
          <h2 className="font-display font-semibold">发给 AI 的提示词（WB-01）</h2>
          <Seg label="内容" value={tab} onChange={setTab} options={[{ value: "system", label: "系统提示" }, { value: "user", label: "用户提示" }, { value: "response", label: "AI 回复" }]} />
          <button className="btn ml-auto" onClick={onClose}>关闭</button>
        </div>
        {p.error ? <ErrorBox error={p.error} /> : !p.data ? <Loading what="提示词" /> : (
          <pre className="num text-xs whitespace-pre-wrap leading-relaxed overflow-y-auto flex-1 bg-bg rounded p-3 text-muted">{text}</pre>
        )}
      </div>
    </div>
  );
}
