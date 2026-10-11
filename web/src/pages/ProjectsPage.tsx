import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Job, type Project, type ProjectRun } from "../api";
import { cssVar, MARKET_LABEL, pct, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Panel } from "../components/ui";
import Globe from "../components/charts/Globe";

const STATUS_TONE = { 示例: "muted", 已锁定: "muted", 排队中: "muted", 运行中: "accent", 已完成: "good", 已中止: "warn" } as const;
const JOB_TONE = { queued: "muted", running: "accent", done: "good", failed: "bad", cancelled: "warn", budget_stopped: "warn" } as const;

export default function ProjectsPage() {
  const [tick, setTick] = useState(0);
  const projects = useAsync(() => api.projects(), [tick]);
  const active = projects.data?.some((p) => p.status === "运行中" || p.status === "排队中");
  useEffect(() => {  // 有任务在排队或运行时，每 10 秒刷新状态
    if (!active) return;
    const id = setInterval(() => setTick((x) => x + 1), 10000);
    return () => clearInterval(id);
  }, [active]);
  const overview = useAsync(() => api.overview(), []);
  const navigate = useNavigate();

  return (
    <div className="flex flex-col gap-5">
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] items-stretch">
        <section className="panel overflow-hidden min-h-[360px] relative">
          <div className="absolute left-5 top-5 z-10 max-w-[34ch] pointer-events-none">
            <div className="label">01 · 研究项目</div>
            <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">同一个假设，香港、伦敦、巴黎分别验证</h1>
            <p className="text-muted text-sm mt-2">选择一个研究项目继续，或新建研究。每个项目从立项、审核、运行到结果都有记录。</p>
          </div>
          {/* 标题在左，地球偏右，互不遮挡；窄屏时地球放到标题下方 */}
          <div className="h-[380px] mt-36 md:mt-0 md:ml-[38%]">{overview.data ? <Globe overview={overview.data} /> : <Loading what="市场概况" />}</div>
        </section>
        <Panel eyebrow="数据" title="三地市场">
          {overview.data ? (
            <div className="flex flex-col gap-3">
              {(["hk", "uk", "fr"] as const).map((m) => {
                const o = overview.data!.markets[m];
                return (
                  <div key={m} className="border-l-2 pl-3" style={{ borderColor: cssVar(`--${m}`) }}>
                    <div className="flex items-baseline gap-2">
                      <span className="font-medium">{o.name}</span><span className="text-xs text-muted">{o.index} · {o.currency}</span>
                    </div>
                    <div className="num text-xs text-muted mt-0.5">{o.stocks} 只 · {o.first} 至 {o.last}{o.fx_to_hkd ? ` · 1 ${o.currency} = ${o.fx_to_hkd} HKD` : ""}</div>
                    <div className="num text-xs text-faint">AI 挖掘可用数据截至 {o.mining_cutoff}</div>
                  </div>
                );
              })}
            </div>
          ) : <Loading what="市场概况" />}
          <button className="btn btn-primary w-full h-10 justify-center mt-5" onClick={() => navigate("/research")}>新建研究 →</button>
        </Panel>
      </div>

      {projects.error && <ErrorBox error={projects.error} />}
      {!projects.data ? (projects.error ? null : <Loading what="研究项目" />) : (
        <div className="flex flex-col gap-4">
          {projects.data.map((p) => <ProjectCard key={p.id} p={p} />)}
          {projects.data.length === 0 && <div className="text-muted">还没有研究项目。点击"新建研究"开始。</div>}
        </div>
      )}
    </div>
  );
}

function ProjectCard({ p }: { p: Project }) {
  return (
    <Panel eyebrow={`${p.markets.map((m) => MARKET_LABEL[m]).join(" · ")} · 预测周期 ${p.horizon} 个交易日`} title={p.title || p.id}
      actions={<>
        <Badge tone={STATUS_TONE[p.status]}>{p.status}</Badge>
        {p.example ? <Badge>手写示例</Badge> : <Badge>v{p.versions.join(" / v")}</Badge>}
        <span className="num text-xs text-faint">{p.id}</span>
      </>}>
      <p className="text-sm text-muted leading-relaxed max-w-[110ch]">{p.hypothesis}</p>
      {!p.example && <StudyLinks p={p} />}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs text-muted">
            <tr>
              <th className="text-left py-1.5 font-normal">运行</th><th className="text-left font-normal">市场</th><th className="text-left font-normal">状态</th>
              <th className="text-right font-normal">轮数 / 采纳</th><th className="text-right font-normal">起点 → 最好（扣成本年化）</th>
              <th className="text-right font-normal">费用</th><th className="text-right font-normal">操作</th>
            </tr>
          </thead>
          <tbody>
            {p.jobs.map((j) => <JobRow key={j.job_id} j={j} />)}
            {p.runs.map((r) => <RunRow key={r.run_id} r={r} />)}
          </tbody>
        </table>
        {p.runs.length === 0 && p.jobs.length === 0 && (
          <div className="text-muted text-sm py-3">尚未运行。{p.example ? "这是早期手写的示例规格，可在研究立项页起草新规格。" : "在审核页锁定后发起运行。"}</div>
        )}
      </div>
    </Panel>
  );
}

const OVERALL_TONE = { rejected: "bad", passed: "good", pending: "accent", insufficient: "warn" } as const;

/** 每个已运行的版本：按可证伪测试的研究结论，点击进入结果页查看三市场对比。 */
function StudyLinks({ p }: { p: Project }) {
  const versions = [...new Set(p.runs.map((r) => r.version).filter((v): v is number => v != null))].sort((a, b) => b - a);
  if (versions.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 mt-3">
      {versions.map((v) => <StudyLink key={v} p={p} version={v} />)}
    </div>
  );
}

function StudyLink({ p, version }: { p: Project; version: number }) {
  const study = useAsync(() => api.study(p.id, version), [p.id, version, p.runs.length]);
  const s = study.data;
  const run = s ? (s.markets[s.rule.primary]?.run_id ?? Object.values(s.markets).find((e) => e?.run_id)?.run_id) : null;
  return (
    <Link to={run ? `/results?run=${run}` : "#"} className="btn gap-2" aria-disabled={!run}>
      <span>v{version} 研究结论</span>
      {s ? <Badge tone={OVERALL_TONE[s.overall.state]}>{s.overall.state === "passed" ? s.overall.label : `假设${s.overall.label}`}</Badge> : <span className="text-faint text-xs">…</span>}
    </Link>
  );
}

function RetryButton({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  return (
    <button className="btn mr-2" onClick={async () => {
      const r = await api.retryJob(jobId);
      navigate(`/run?job=${r.jobs[0].job_id}`);
    }}>重新运行</button>
  );
}

/** 已发起但还没有进度的任务（排队中、准备数据中、启动失败）。 */
function JobRow({ j }: { j: Job }) {
  return (
    <tr className="border-t border-line">
      <td className="py-2 pr-3"><span className="num text-muted">{j.run_id ?? "等待开始"}</span><span className="num text-xs text-faint ml-1.5">v{j.version}</span>
        {j.error && <div className="text-xs text-bad mt-1 max-w-[60ch] truncate" title={j.error}>{j.error}</div>}</td>
      <td className="pr-3"><span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: cssVar(`--${j.market}`) }} />{MARKET_LABEL[j.market]}</span></td>
      <td className="pr-3"><Badge tone={JOB_TONE[j.state]}>{j.state_zh}</Badge></td>
      <td className="num text-right pr-3 text-faint">0 / {j.loops}</td>
      <td className="num text-right pr-3 text-faint">—</td>
      <td className="num text-right pr-3">${j.cost_usd.toFixed(2)}</td>
      <td className="text-right whitespace-nowrap">
        {["failed", "cancelled", "budget_stopped"].includes(j.state) && <RetryButton jobId={j.job_id} />}
        <Link className="btn" to={`/run?job=${j.job_id}`}>查看任务</Link>
      </td>
    </tr>
  );
}

function RunRow({ r }: { r: ProjectRun }) {
  return (
    <tr className="border-t border-line">
      <td className="py-2 pr-3">
        <span className="num">{r.run_id}</span>{r.version != null && <span className="num text-xs text-faint ml-1.5">v{r.version}</span>}
        <div className="flex gap-1.5 mt-1">
          {r.context_leak && <Badge tone="bad">受泄漏影响</Badge>}
          {r.linked === "推断" && <Badge>按市场与周期关联</Badge>}
        </div>
        {r.job_error && <div className="text-xs text-bad mt-1 max-w-[60ch] truncate" title={r.job_error}>{r.job_error}</div>}
      </td>
      <td className="pr-3"><span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: cssVar(`--${r.market}`) }} />{MARKET_LABEL[r.market]}</span></td>
      <td className="pr-3"><Badge tone={STATUS_TONE[r.state]}>{r.state}</Badge></td>
      <td className="num text-right pr-3">{r.rounds} / {r.adopted}</td>
      <td className="num text-right pr-3">{pct(r.baseline_return)} → <span className={r.best_return != null ? "text-good" : "text-faint"}>{pct(r.best_return)}</span></td>
      <td className="num text-right pr-3">${r.cost_usd.toFixed(2)}</td>
      <td className="text-right whitespace-nowrap">
        {r.job_id && r.job_state && ["failed", "cancelled", "budget_stopped"].includes(r.job_state) && <RetryButton jobId={r.job_id} />}
        <Link className="btn mr-2" to={`/run?run=${r.run_id}`}>{r.state === "运行中" ? "查看过程" : "回放过程"}</Link>
        <Link className="btn" to={`/results?run=${r.run_id}`}>查看结果</Link>
      </td>
    </tr>
  );
}
