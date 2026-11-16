import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api, MARKETS, type Agent, type AgentJob, type AgentRound, type AgentTrace, type Market, type ResearchTermination } from "../api";
import { fix, pct, useAsync, MARKET_LABEL } from "../lib";
import { Badge, ErrorBox, Loading, Notice, Panel, Stat, Toast } from "../components/ui";
import { t, tf } from "../i18n";

const RUNNING = new Set(["queued", "observing", "diagnosing", "planning", "acting", "waiting_children", "evaluating"]);
const POLLING = new Set([...RUNNING, "awaiting_human"]);
const TONE: Record<string, "good" | "bad" | "warn" | "muted" | "accent"> = {
  queued: "accent", running: "accent", observing: "accent", diagnosing: "accent", planning: "accent", acting: "accent",
  waiting_children: "accent", evaluating: "accent", awaiting_human: "warn", done: "good", stopped: "muted",
  failed: "bad", cancelled: "muted", budget_stopped: "warn",
};
const STATE_LABEL: Record<string, string> = {
  queued: t("排队中"), observing: t("观察结果"), diagnosing: t("诊断中"), planning: t("规划中"),
  awaiting_human: t("等待人工批准"), acting: t("执行中"), waiting_children: t("等待子任务"),
  evaluating: t("评估中"), done: t("已完成"), stopped: t("已停止"), failed: t("失败"),
  cancelled: t("已取消"), budget_stopped: t("超出预算停止"),
};
const VERDICT_TONE: Record<string, "good" | "bad" | "warn" | "muted"> = {
  improved: "good", not_improved: "bad", inconclusive: "warn", stopped: "muted",
};
const VERDICT_LABEL: Record<string, string> = {
  improved: t("改进"), not_improved: t("未改进"), inconclusive: t("无法判定"), stopped: t("已停止"),
};

type NoticeKind = "starting" | "children" | "validating" | "evaluating" | "evaluated"
  | "awaiting_human" | "saved" | "rejected" | "stopped" | "failed";
type Stage = { key: string; kind: NoticeKind; text: string };

function StateBadge({ state, flash = false }: { state: string; flash?: boolean }) {
  return <span className={flash ? "state-flash" : "inline-flex"}><Badge tone={TONE[state] ?? "muted"}>{STATE_LABEL[state] ?? state}</Badge></span>;
}

function approvalLabel(decision?: string) {
  return decision === "approved" ? t("已批准") : decision === "rejected" ? t("已拒绝") : "—";
}

/** 从服务端状态推导当前阶段提示：启动 → 子任务 → 可信度检查 → 评估 → 完成。 */
function computeStage(agent: Agent, rounds: AgentRound[], children: AgentJob[]): Stage | null {
  if (agent.state === "awaiting_human" && !agent.approved) {
    return { key: `awaiting:${agent.current_round}:${agent.current_version}`, kind: "awaiting_human",
      text: t("下一版草稿已生成，等待人工批准。请审改后点击「批准并运行」或「只保存版本」。") };
  }
  if (!agent.approved || agent.approved.decision !== "approved") return null;
  if (agent.approved.run === false) {
    return { key: `saved:${agent.approved.saved_version}`, kind: "saved",
      text: tf("已保存新版本 v{n}，未启动运行。", { n: agent.approved.saved_version ?? "" }) };
  }
  const round = rounds.find((r) => r.n === agent.current_round);
  if (round?.evaluation) {
    const v = String(round.evaluation.verdict || "");
    const continuing = POLLING.has(agent.state) && agent.state !== "awaiting_human";
    const next = continuing
      ? tf("Agent 正在继续下一轮（{state}）…", { state: STATE_LABEL[agent.state] ?? agent.state })
      : v === "improved"
        ? t("Agent 已停止。下一步：在结果页查看结果并继续研究，或提交审批。")
        : t("Agent 已停止。下一步：在结果页查看诊断与可信度检查，再决定继续研究或把新发现存入线索池。");
    return { key: `evaluated:${round.n}:${v}:${continuing ? "continuing" : agent.state}`, kind: "evaluated",
      text: tf("评估完成：{v}。{next}", { v: VERDICT_LABEL[v] ?? v, next }) };
  }
  if (agent.state === "stopped") return { key: `stopped:${agent.current_round}`, kind: "stopped", text: t("Agent 已停止。") };
  if (agent.state === "failed") return { key: `failed:${agent.current_round}`, kind: "failed", text: t("Agent 失败，请查看错误与 trace。") };
  const current = children.filter((j) => j.version === agent.current_version);
  const runs = current.filter((j) => j.kind !== "validate");
  const vals = current.filter((j) => j.kind === "validate");
  const final = new Set(["done", "failed", "cancelled", "budget_stopped"]);
  const active = (j: AgentJob) => j.state === "queued" || j.state === "running";
  if (vals.some(active)) {
    return { key: `validating:${vals.map((j) => j.job_id).join(",")}`, kind: "validating",
      text: t("子任务已完成，正在可信度检查…") };
  }
  if (vals.length && vals.every((j) => final.has(j.state))) {
    return { key: `evaluating:${vals.map((j) => j.job_id).join(",")}`, kind: "evaluating",
      text: t("可信度检查完成，Agent 正在评估…") };
  }
  if (runs.length && runs.every((j) => final.has(j.state))) {
    return { key: `prepare:${runs.map((j) => j.job_id).join(",")}`, kind: "validating",
      text: t("子任务已完成，准备可信度检查…") };
  }
  if (runs.length) {
    const markets = runs.map((j) => MARKET_LABEL[j.market as Market] ?? j.market).join(" / ");
    return { key: `children:${runs.map((j) => j.job_id).join(",")}`, kind: "children",
      text: tf("已批准：{n} 个子任务已启动（{markets}）。页面每 5 秒自动刷新；点击「运行编号」查看过程，无需其他操作。",
        { n: runs.length, markets }) };
  }
  return { key: "starting", kind: "starting", text: t("已批准，正在启动子任务…") };
}

export default function AgentsPage() {
  const { id } = useParams<{ id?: string }>();
  return id ? <AgentDetailView id={id} /> : <AgentList />;
}

function AgentList() {
  const list = useAsync(() => api.agents(), []);
  if (list.error) return <ErrorBox error={list.error} />;
  if (!list.data) return <Loading what={t("研究 Agent")} />;
  return (
    <div className="flex flex-col gap-5">
      <div>
        <div className="label">{t("研究 Agent")}</div>
        <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">{t("AI 研究迭代闭环")}</h1>
        <p className="text-muted text-sm mt-1 max-w-[90ch]">
          {t("Agent 自动观察结果、诊断问题、规划下一版本；人工审改并批准后，自动运行新版本、做可信度检查并评估。")}
        </p>
      </div>
      {list.data.length === 0 ? (
        <Notice tone="accent" title={t("还没有 Agent")}>
          {t("在结果页点“继续研究 → AI 生成下一版假设并自动运行”即可启动；Agent 会先停在人工闸门。")}
        </Notice>
      ) : (
        <Panel eyebrow={t("全部 Agent")} title={t("按创建时间倒序")}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted"><tr>
                <th className="text-left py-1.5">{t("Agent")}</th><th className="text-left">{t("根研究")}</th>
                <th className="text-left">{t("状态")}</th><th className="text-right">{t("版本")}</th>
                <th className="text-right">{t("轮次")}</th><th className="text-right">{t("费用")}</th>
                <th className="text-left pl-3">{t("停止原因")}</th>
              </tr></thead>
              <tbody>
                {list.data.map((a) => (
                  <tr key={a.agent_id} className="border-t border-line align-top">
                    <td className="py-2"><Link className="text-accent num text-xs" to={`/agents/${a.agent_id}`}>{a.agent_id}</Link></td>
                    <td className="num text-xs">{a.root.research_id} v{a.root.version}<div className="text-faint">{a.root.markets.map((m) => MARKET_LABEL[m]).join(" / ")}</div></td>
                    <td><StateBadge state={a.state} flash={POLLING.has(a.state) && a.state !== "awaiting_human"} /><div className="text-[11px] text-faint">{a.job_state}</div></td>
                    <td className="num text-right">{a.current_version}</td>
                    <td className="num text-right">{a.current_round} / {a.round_limit}</td>
                    <td className="num text-right">${a.cost_usd.toFixed(4)}</td>
                    <td className="text-xs text-muted max-w-[30ch]">{a.stop_reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

function AgentDetailView({ id }: { id: string }) {
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [noticeKind, setNoticeKind] = useState<NoticeKind | null>(null);
  const [dismissedStage, setDismissedStage] = useState<string | null>(null);
  const [confirmTerminate, setConfirmTerminate] = useState(false);
  const [terminatedLocal, setTerminatedLocal] = useState<ResearchTermination | null>(null);
  const [showWorkflow, setShowWorkflow] = useState(true);
  const [showTrace, setShowTrace] = useState(false);
  const lastStage = useRef<string | null>(null);
  const childJobsRef = useRef<HTMLDivElement>(null);
  const detail = useAsync(() => api.agent(id), [id, tick]);
  const trace = useAsync(() => api.agentTrace(id), [id, tick]);
  useEffect(() => {
    // 每个 Agent 默认展开 AI 生成过程；切换 Agent 时重置。
    setShowWorkflow(true);
    setShowTrace(false);
  }, [id]);
  useEffect(() => {
    const state = detail.data?.agent.state;
    if (!state || !POLLING.has(state)) return;
    const timer = setInterval(() => setTick((x) => x + 1), 5000);
    return () => clearInterval(timer);
  }, [detail.data?.agent.state]);

  useEffect(() => {
    if (!detail.data) return;
    const stage = computeStage(detail.data.agent, detail.data.rounds, detail.data.children);
    if (!stage || stage.key === lastStage.current || stage.key === dismissedStage) return;
    lastStage.current = stage.key;
    setNotice(stage.text);
    setNoticeKind(stage.kind);
    if (stage.kind === "children" || stage.kind === "validating" || stage.kind === "evaluating") {
      requestAnimationFrame(() => childJobsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }, [detail.data, dismissedStage]);

  if (detail.error) return <ErrorBox error={detail.error} />;
  if (!detail.data) return <Loading what={t("研究 Agent")} />;
  const { agent, rounds, job, report, children: childJobs } = detail.data;
  const terminated = detail.data.terminated ?? terminatedLocal;
  const latest = rounds[rounds.length - 1];
  const evaluation = [...rounds].reverse().find((r) => r.evaluation)?.evaluation as Record<string, any> | undefined;

  async function approve(draft: Record<string, unknown>, run: boolean, by: string) {
    setBusy(true); setActionError(null);
    try {
      const r = await api.approveAgent(id, { draft, run, by });
      setNotice(run ? t("已批准，正在启动子任务…") : tf("已保存新版本 v{n}，未启动运行。", { n: r.saved.version ?? "" }));
      setNoticeKind(run ? "starting" : "saved");
      setTick((x) => x + 1);
    } catch (e) {
      setActionError((e as Error).message);
      throw e;
    } finally { setBusy(false); }
  }

  async function reject(reason: string, by: string) {
    setBusy(true); setActionError(null);
    try {
      await api.rejectAgent(id, { reason, by });
      setNotice(t("已拒绝本轮草稿：没有保存版本，也没有启动运行。"));
      setNoticeKind("rejected");
      setTick((x) => x + 1);
    } catch (e) {
      setActionError((e as Error).message);
      throw e;
    } finally { setBusy(false); }
  }

  async function stop() {
    setBusy(true); setActionError(null);
    try {
      await api.stopAgent(id, { reason: t("页面人工停止") });
      setNotice(t("已停止 Agent；仍在运行的子任务会收到取消请求。"));
      setNoticeKind("stopped");
      setTick((x) => x + 1);
    } catch (e) { setActionError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function terminateResearch() {
    setBusy(true); setActionError(null);
    try {
      const r = await api.terminateResearch(agent.root.research_id, {
        version: agent.root.version, reason: "人工终止当前研究（研究 Agent 页）",
      });
      setTerminatedLocal(r.terminated);
      setConfirmTerminate(false);
      setNotice(t("已终止当前研究；该研究不能再启动 Agent。"));
      setNoticeKind("stopped");
      setTick((x) => x + 1);
    } catch (e) { setActionError((e as Error).message); }
    finally { setBusy(false); }
  }

  const canStop = RUNNING.has(agent.state) || agent.state === "awaiting_human";
  const noticePulse = ["starting", "children", "validating", "evaluating"].includes(noticeKind ?? "");
  const noticeTitle = noticeKind === "starting" ? t("已批准，正在启动")
    : noticeKind === "awaiting_human" ? t("等待人工批准")
      : ["children", "validating", "evaluating"].includes(noticeKind ?? "") ? t("进度")
        : noticeKind === "evaluated" ? t("评估完成") : t("提示");
  const dismissNotice = () => {
    setNotice(null);
    setNoticeKind(null);
    setDismissedStage(lastStage.current);
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <div className="label">{t("研究 Agent")}</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">{agent.agent_id}</h1>
          <p className="text-sm text-muted mt-1">
            {agent.root.research_id} v{agent.root.version} · {agent.root.markets.map((m) => MARKET_LABEL[m]).join(" / ")} ·
            {" "}{tf("{n} 个交易日", { n: agent.root.horizon })}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <StateBadge state={agent.state} flash={POLLING.has(agent.state) && agent.state !== "awaiting_human"} />
          {agent.web_search && <Badge tone="accent">{t("联网")}</Badge>}
          {agent.deep_think && <Badge tone="warn">{t("深度思考")}</Badge>}
          <button className="btn h-8 px-3 text-xs" disabled={!!terminated || busy}
            style={terminated ? { cursor: "not-allowed" } : undefined}
            title={terminated ? t("当前研究已终止") : t("人工终止当前研究；终止后该研究不能再启动 Agent")}
            onClick={() => { if (confirmTerminate) void terminateResearch(); else setConfirmTerminate(true); }}>
            {terminated ? t("当前研究已终止") : confirmTerminate ? t("确认终止") : t("终止当前研究")}
          </button>
          {confirmTerminate && !terminated && (
            <button className="btn h-8 px-2 text-xs" disabled={busy} onClick={() => setConfirmTerminate(false)}>{t("取消")}</button>
          )}
          {canStop && <button className="btn h-8 px-3 text-xs" disabled={busy} title={t("仅用于中途停止，不是下一步")} onClick={stop}>{t("中止 Agent")}</button>}
          <Link className="btn h-8 px-3 text-xs" to={`/projects?focus=${encodeURIComponent(agent.root.research_id)}`}>{t("返回研究项目")}</Link>
        </div>
      </div>

      <Toast open={!!notice} onClose={dismissNotice} duration={null}
        tone={noticeKind === "rejected" || noticeKind === "stopped" || noticeKind === "failed" ? "warn" : "accent"}
        title={noticeTitle}>
        <div className="flex items-start gap-3">
          {noticePulse && <span className="mt-1.5 size-2 shrink-0 rounded-full bg-accent animate-pulse" aria-hidden />}
          <span className="flex-1">{notice}</span>
        </div>
      </Toast>
      {agent.last_error && <Notice tone="bad" title={t("Agent 失败")}>{agent.last_error}</Notice>}
      {actionError && <ErrorBox error={actionError} />}
      {agent.stop_reason && <Notice tone="accent" title={t("当前状态")}>{agent.stop_reason}</Notice>}
      {terminated && (
        <Notice tone="accent" title={t("研究已终止")}>
          {tf("由 {by} 于 {at} 人工终止", { by: terminated.by, at: terminated.at.slice(0, 16).replace("T", " ") })}
          {terminated.reason && <span>{tf("原因：{r}", { r: terminated.reason })}</span>}
        </Notice>
      )}

      <Panel eyebrow={t("运行概览")} title={t("根研究与预算")}>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label={t("根研究")} value={`v${agent.root.version}`} sub={agent.root.research_id} />
          <Stat label={t("当前版本")} value={`v${agent.current_version}`} sub={agent.parent_version !== agent.current_version ? tf("父版本 v{n}", { n: agent.parent_version }) : t("尚未生成新版本")} />
          <Stat label={t("轮次")} value={`${agent.current_round} / ${agent.round_limit}`} sub={agent.job_state || "—"} />
          <Stat label={t("费用")} value={`$${agent.cost_usd.toFixed(4)}`} sub={tf("{a} 输入 / {b} 输出 token", { a: agent.usage.prompt_tokens, b: agent.usage.completion_tokens })} />
        </div>
        <p className="text-xs text-muted mt-3">
          {t("子任务：")}{childJobs.length ? tf("{n} 个（见下表）", { n: childJobs.length }) : t("尚未创建")} ·
          {" "}{t("人工审批：")}{agent.approved ? `${approvalLabel(agent.approved.decision)} · ${agent.approved.by} · v${agent.approved.saved_version ?? "—"}` : t("尚未审批")}
          {job?.state ? ` · ${t("任务状态")} ${STATE_LABEL[job.state] ?? job.state}` : ""}
        </p>
      </Panel>

      <AgentTraceControls showTrace={showTrace} showWorkflow={showWorkflow}
        onToggleTrace={() => setShowTrace((v) => !v)}
        onToggleWorkflow={() => setShowWorkflow((v) => !v)} />

      {showWorkflow && (
        <div id="agent-workflow" className="scroll-mt-20">
          <WorkflowProgress agent={agent} rounds={rounds} jobs={childJobs} />
        </div>
      )}

      {showTrace && <TracePanel items={trace.data?.items || []} error={trace.error} />}

      <div ref={childJobsRef} className="scroll-mt-20">
        <ChildJobsPanel items={childJobs} />
      </div>

      {rounds.length === 0 && <Notice tone="accent" title={t("Agent 正在准备")}>{t("runner 已接手，正在观察结果与诊断；每 5 秒自动刷新。")}</Notice>}
      {rounds.map((r) => (
        <RoundCard key={r.n} agent={agent} round={r} busy={busy}
          onApprove={approve} onReject={reject} awaiting={agent.state === "awaiting_human" && r.n === latest?.n} />
      ))}

      {evaluation && <NextStepsPanel agent={agent} evaluation={evaluation} childJobs={childJobs} terminated={!!terminated} />}
      {evaluation && <OverlapPanel id={id} version={agent.current_version} />}

      {report && (
        <Panel eyebrow={t("报告")} title={t("迭代报告")}>
          <details><summary className="cursor-pointer text-sm text-text">{t("展开 report.md")}</summary>
            <pre className="num text-xs whitespace-pre-wrap mt-2">{report}</pre></details>
        </Panel>
      )}
    </div>
  );
}

type StepStatus = "done" | "active" | "pending" | "failed" | "skipped";
const STEP_TONE: Record<StepStatus, "good" | "accent" | "muted" | "bad"> = {
  done: "good", active: "accent", pending: "muted", failed: "bad", skipped: "muted",
};
const STEP_LABEL: Record<StepStatus, string> = {
  done: t("已完成"), active: t("进行中"), pending: t("等待"), failed: t("失败"), skipped: t("跳过"),
};

function WorkflowStep({ title, status, children }: { title: string; status: StepStatus; children: ReactNode }) {
  const dot = { done: "bg-good", active: "bg-accent state-flash", pending: "bg-line-2", failed: "bg-bad", skipped: "bg-faint" }[status];
  return (
    <li className="flex gap-3" data-status={status}>
      <span className={`mt-1.5 size-2.5 shrink-0 rounded-full ${dot}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`text-sm font-medium ${status === "pending" ? "text-muted" : status === "skipped" ? "text-faint" : status === "failed" ? "text-bad" : "text-text"}`}>{title}</span>
          <Badge tone={STEP_TONE[status]}>{STEP_LABEL[status]}</Badge>
        </div>
        <div className="text-xs text-muted mt-0.5">{children}</div>
      </div>
    </li>
  );
}

function runText(j: AgentJob) {
  const m = MARKET_LABEL[j.market as Market] ?? j.market;
  const phrase = j.state === "running" ? t("正在运行中")
    : j.state === "done" ? t("已完成运行")
      : j.state === "failed" ? t("失败")
        : j.state === "queued" ? t("排队中")
          : j.state === "cancelled" ? t("已取消")
            : j.state === "budget_stopped" ? t("超出预算停止") : String(j.state);
  return tf("{m}市场{p}", { m, p: phrase });
}

function validationText(j: AgentJob) {
  const m = MARKET_LABEL[j.market as Market] ?? j.market;
  const phrase = j.state === "running" ? t("进行中")
    : j.state === "done" ? t("已完成")
      : j.state === "failed" ? t("失败")
        : j.state === "queued" ? t("排队中") : String(j.state);
  return tf("{m}市场可信度检查：{p}", { m, p: phrase });
}

/** 当前轮次的业务流程：每一步在做什么、结果是什么；RD-Agent 日志留在「04 运行中」。 */
function WorkflowProgress({ agent, rounds, jobs }: { agent: Agent; rounds: AgentRound[]; jobs: AgentJob[] }) {
  const round = rounds.find((r) => r.n === agent.current_round) ?? rounds[rounds.length - 1] ?? null;
  const facts = round?.facts;
  const diagnosis = round?.diagnosis as Record<string, any> | null;
  const external = round?.external;
  const plan = round?.plan as Record<string, any> | null;
  const draft = round?.next_version_draft as Record<string, any> | null;
  const approval = round?.approval ?? null;
  const action = round?.action as Record<string, any> | null;
  const evaluation = round?.evaluation as Record<string, any> | null;
  const summary = (facts?.summary || {}) as Record<string, any>;
  const actionIds = new Set<string>((action?.child_jobs || []) as string[]);
  const savedVersion = approval?.saved_version ?? null;
  const runJobs = jobs.filter((j) => j.kind !== "validate" &&
    (actionIds.size ? actionIds.has(j.job_id) : savedVersion != null && j.version === savedVersion));
  const runIds = new Set(runJobs.map((j) => j.run_id).filter((x): x is string => !!x));
  const valJobs = jobs.filter((j) => j.kind === "validate" &&
    ((j.target_run && runIds.has(j.target_run)) || (savedVersion != null && j.version === savedVersion)));
  const markets = [...new Set((plan?.args?.changes?.markets ?? plan?.args?.markets ?? agent.root.markets) as Market[])];
  const actionLabel: Record<string, string> = {
    new_version: t("生成下一版"), stop: t("停止"), run_rdagent: t("运行已保存版本"),
    run_text: t("运行文本版本"), validate: t("可信度检查"), diagnose: t("重新诊断"),
  };
  const needed = !plan || plan.action === "new_version";
  const runActive = runJobs.some((j) => ["queued", "running"].includes(j.state));
  const runFailed = runJobs.some((j) => j.state === "failed");
  const runDone = runJobs.length > 0 && runJobs.every((j) => j.state === "done");
  const valActive = valJobs.some((j) => ["queued", "running"].includes(j.state));
  const valFailed = valJobs.some((j) => j.state === "failed");
  const valDone = valJobs.length > 0 && valJobs.every((j) => j.state === "done");

  const observe: StepStatus = facts ? "done" : ["queued", "observing"].includes(agent.state) ? "active" : "pending";
  const diagnose: StepStatus = diagnosis ? "done" : agent.state === "diagnosing" ? "active" : "pending";
  const web: StepStatus = external ? "done" : agent.state === "diagnosing" && diagnosis ? "active" : "pending";
  const planning: StepStatus = plan ? "done" : agent.state === "planning" ? "active" : "pending";
  const deep: StepStatus = plan?.deep_think ? "done" : agent.state === "planning" && plan ? "active" : "pending";
  const draftStatus: StepStatus = !needed ? "skipped" : draft ? "done" : agent.state === "planning" && plan ? "active" : "pending";
  const approvalStatus: StepStatus = !needed ? "skipped" : approval
    ? approval.decision === "rejected" ? "failed" : "done"
    : agent.state === "awaiting_human" ? "active" : "pending";
  const runStatus: StepStatus = !needed || approval?.run === false || approval?.decision === "rejected" ? "skipped"
    : runJobs.length ? runFailed ? "failed" : runActive ? "active" : "done"
      : approval?.decision === "approved" ? "active" : "pending";
  const valStatus: StepStatus = !needed || approval?.run === false || approval?.decision === "rejected" ? "skipped"
    : valJobs.length ? valFailed ? "failed" : valActive ? "active" : valDone ? "done" : "active"
      : runDone ? "active" : "pending";
  const evalStatus: StepStatus = !needed ? "skipped" : evaluation ? "done" : agent.state === "evaluating" ? "active" : "pending";
  const finishStatus: StepStatus = agent.state === "failed" ? "failed"
    : ["stopped", "done"].includes(agent.state) ? "done"
      : agent.state === "queued" && evaluation ? "active" : "pending";

  const workflowListRef = useRef<HTMLOListElement>(null);
  const focusKey = [observe, diagnose, web, planning, deep, draftStatus, approvalStatus,
    runStatus, valStatus, evalStatus, finishStatus, agent.state].join("|");
  useEffect(() => {
    const list = workflowListRef.current;
    if (!list) return;
    const lastDone = [...list.querySelectorAll<HTMLElement>('li[data-status="done"]')].at(-1);
    const target = list.querySelector<HTMLElement>('li[data-status="active"]')
      ?? list.querySelector<HTMLElement>('li[data-status="failed"]')
      ?? lastDone
      ?? (list.lastElementChild as HTMLElement | null);
    if (!target) return;
    const top = target.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
    const behavior = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
    list.scrollTo({ top: Math.max(0, top - list.clientHeight / 2 + target.clientHeight / 2), behavior });
  }, [focusKey]);

  return (
    <Panel eyebrow={tf("第 {n} 轮", { n: round?.n ?? (agent.current_round || 1) })}
      title={<span className={RUNNING.has(agent.state) ? "state-flash" : ""}>{t("AI生成假设，思考中...")}</span>}>
      <p className="text-xs text-faint mb-2">{t("RD-Agent 原始日志请在「04 运行中」查看；这里只显示流程状态与结果。")}</p>
      <ol ref={workflowListRef} className="workflow-scroll flex flex-col gap-2 pr-1">
        <WorkflowStep title={t("观察事实")} status={observe}>
          {facts ? tf("事实 {n} 条 · 平台结论：{a} · 迁移：{b}", {
            n: facts.facts.length, a: t(String(summary.overall?.label ?? "—")), b: t(String(summary.transfer?.label ?? "—")),
          }) : observe === "active" ? t("正在生成事实包…") : t("等待开始")}
        </WorkflowStep>

        <WorkflowStep title={t("诊断问题")} status={diagnose}>
          {diagnosis ? <>{diagnosis.primary_cause?.text || "—"}<span className="text-faint"> · {tf("置信度 {c}", { c: diagnosis.confidence || "—" })}</span></>
            : diagnose === "active" ? t("正在诊断…") : t("等待事实包")}
        </WorkflowStep>

        {agent.web_search && (
          <WorkflowStep title={t("联网检索")} status={web}>
            {external ? external.configured
              ? tf("外部资料 {n} 条（{d} 前）", { n: external.results?.length ?? 0, d: external.before ?? "—" })
              : t("未配置或不可用")
              : web === "active" ? t("正在检索外部资料…") : t("等待诊断完成")}
          </WorkflowStep>
        )}

        <WorkflowStep title={t("规划下一步")} status={planning}>
          {plan ? <>{tf("动作：{a}", { a: actionLabel[String(plan.action)] ?? String(plan.action) })}{plan.expected_outcome ? ` · ${plan.expected_outcome}` : ""}</>
            : planning === "active" ? t("正在规划下一步…") : t("等待诊断完成")}
        </WorkflowStep>

        {agent.deep_think && (
          <WorkflowStep title={t("深度思考评审")} status={deep}>
            {plan?.deep_think
              ? plan.deep_think.critique
                ? <>{plan.deep_think.critique.verdict === "accept" ? t("通过评审") : t("需要修订")} · {plan.deep_think.revised ? t("已按评审修订") : t("保留原计划")}{plan.deep_think.critique.model ? ` · ${plan.deep_think.critique.model}` : ""}{plan.deep_think.revision_external && <span className="text-faint"> · {plan.deep_think.revision_external.error ? t("补充检索失败") : tf("补充检索 {n} 条", { n: plan.deep_think.revision_external.results?.length ?? 0 })}</span>}</>
                : t("评审未返回，已保留原计划")
              : deep === "active" ? t("Critic 正在评审计划…") : t("等待 Planner 草案")}
          </WorkflowStep>
        )}

        <WorkflowStep title={t("生成下一版草稿")} status={draftStatus}>
          {draftStatus === "skipped" ? t("本轮不需要下一版") : draft
            ? tf("下一版草稿：{t}", { t: draft.draft?.title_zh || draft.draft?.title || "—" })
            : draftStatus === "active" ? t("正在生成下一版草稿…") : t("等待计划")}
        </WorkflowStep>

        <WorkflowStep title={t("人工批准")} status={approvalStatus}>
          {approvalStatus === "skipped" ? t("本轮不需要人工批准") : approval
            ? <>{approvalLabel(approval.decision)} · {approval.by} · v{approval.saved_version ?? "—"} · {approval.run ? t("自动运行") : t("只保存版本")}</>
            : approvalStatus === "active" ? t("等待人工批准") : t("等待草稿")}
        </WorkflowStep>

        <WorkflowStep title={t("运行新版本")} status={runStatus}>
          {runStatus === "skipped" ? (approval?.run === false ? t("下一版已保存，未启动运行") : t("本轮不需要运行"))
            : runJobs.length ? (
              <div className="flex flex-col gap-0.5">
                {markets.map((m) => {
                  const j = runJobs.find((x) => x.market === m);
                  return <div key={m}>{j ? runText(j) : tf("{m}市场{p}", { m: MARKET_LABEL[m], p: t("等待运行") })}</div>;
                })}
              </div>
            ) : approval?.decision === "approved" ? t("正在创建子运行任务…") : t("等待人工批准")}
        </WorkflowStep>

        <WorkflowStep title={t("可信度检查")} status={valStatus}>
          {valStatus === "skipped" ? t("本轮不需要可信度检查")
            : valJobs.length ? (
              <div className="flex flex-col gap-0.5">
                {markets.map((m) => {
                  const j = valJobs.find((x) => x.market === m);
                  return <div key={m}>{j ? validationText(j) : tf("{m}市场可信度检查：{p}", { m: MARKET_LABEL[m], p: t("等待") })}</div>;
                })}
              </div>
            ) : runDone ? t("子运行已完成，准备可信度检查") : t("等待子运行完成")}
        </WorkflowStep>

        <WorkflowStep title={t("评估与回流")} status={evalStatus}>
          {evalStatus === "skipped" ? t("本轮不需要评估")
            : evaluation ? <>{VERDICT_LABEL[evaluation.verdict] ?? evaluation.verdict} · {evaluation.reason || "—"}
              {evaluation.precheck && <> · {tf("阻断 {b} / 警告 {w} / 提示 {i}", {
                b: evaluation.precheck.counts?.block ?? 0, w: evaluation.precheck.counts?.warn ?? 0, i: evaluation.precheck.counts?.info ?? 0,
              })}</>}
              {evaluation.idea_id && <> · <Link className="text-accent hover:underline" to={`/ideas?id=${evaluation.idea_id}`}>{tf("自动回流线索 {id}", { id: evaluation.idea_id })}</Link></>}
            </> : evalStatus === "active" ? t("正在评估…") : t("等待可信度检查")}
        </WorkflowStep>

        <WorkflowStep title={t("本轮结束 / 下一轮")} status={finishStatus}>
          {finishStatus === "failed" ? agent.last_error || t("Agent 失败")
            : finishStatus === "done" ? agent.stop_reason || t("本轮结束")
              : finishStatus === "active" ? t("准备下一轮") : t("等待评估")}
        </WorkflowStep>
      </ol>
    </Panel>
  );
}

function ChildJobsPanel({ items }: { items: AgentJob[] }) {
  return (
    <Panel eyebrow={t("子任务")} title={tf("子运行（{n}）", { n: items.length })}>
      {items.length === 0 ? <p className="text-sm text-muted">{t("子任务尚未创建：Agent 还没批准新版本，或队列尚未启动。")}</p> : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted"><tr>
              <th className="text-left py-1.5">{t("市场")}</th><th className="text-left">{t("类型")}</th>
              <th className="text-left">{t("状态")}</th><th className="text-right">{t("轮次")}</th>
              <th className="text-right">{t("费用")}</th><th className="text-left pl-3">{t("运行编号")}</th><th className="text-left pl-3">{t("错误")}</th>
            </tr></thead>
            <tbody>
              {items.map((j) => {
                const failed = j.state === "failed";
                return (
                  <tr key={j.job_id} className="border-t border-line"
                    style={failed ? { background: "color-mix(in oklab, var(--bad) 8%, transparent)" } : undefined}>
                    <td className="py-1.5">{MARKET_LABEL[j.market as Market] ?? j.market}</td>
                    <td>{j.kind === "validate" ? t("可信度检查") : j.kind === "text" ? t("文本信号") : t("RD-Agent")}</td>
                    <td><span className={j.state === "running" ? "state-flash" : "inline-flex"}><Badge tone={TONE[j.state] ?? "muted"}>{j.state_zh ? t(j.state_zh) : STATE_LABEL[j.state] ?? j.state}</Badge></span></td>
                    <td className="num text-right">{j.rounds}</td>
                    <td className="num text-right">${Number(j.cost_usd || 0).toFixed(4)}</td>
                    <td className="pl-3" style={j.run_id && !failed
                      ? { background: "color-mix(in oklab, var(--accent) 8%, transparent)" }
                      : undefined}>{j.run_id
                      ? <Link className="text-accent num text-xs"
                        to={j.state === "done" ? `/results?run=${j.run_id}` : `/run?run=${j.run_id}&job=${j.job_id}`}>{j.run_id}</Link>
                      : "—"}</td>
                    <td className="pl-3 text-xs text-bad">{j.error || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function NextStepsPanel({ agent, evaluation, childJobs, terminated }: {
  agent: Agent; evaluation: Record<string, any>; childJobs: AgentJob[]; terminated: boolean;
}) {
  const run = childJobs.find((j) => j.kind !== "validate" && j.state === "done" && j.run_id)
    || childJobs.find((j) => j.kind !== "validate" && j.run_id);
  const version = agent.current_version;
  const verdict = VERDICT_LABEL[evaluation.verdict] ?? String(evaluation.verdict ?? "");
  const title = tf("{id} v{v} 评估：{verdict}", { id: agent.root.research_id, v: version, verdict });
  const body = [evaluation.reason, evaluation.note].filter(Boolean).join("\n") || title;
  const params = new URLSearchParams({
    prefill_title: title, prefill_body: body, prefill_markets: agent.root.markets.join(","),
    prefill_research: `${agent.root.research_id}.v${version}`,
    ...(run?.run_id ? { prefill_run: run.run_id } : {}),
  });
  const stopped = ["stopped", "done", "failed"].includes(agent.state);
  return (
    <Panel eyebrow={t("下一步")} title={t("评估完成后的操作")}>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {run?.run_id
          ? <Link className="btn justify-center" to={`/results?run=${run.run_id}`}>{t("查看结果")}</Link>
          : <span className="btn justify-center opacity-40 cursor-not-allowed">{t("查看结果")}</span>}
        {terminated
          ? <span className="btn justify-center opacity-40 cursor-not-allowed" title={t("该研究已终止，不能生成新版本")}>{t("基于本版本继续研究")}</span>
          : <Link className="btn justify-center" to={`/research?based_on=${agent.root.research_id}.v${version}`}>{t("基于本版本继续研究")}</Link>}
        {evaluation.idea_id
          ? <Link className="btn justify-center" to={`/ideas?id=${evaluation.idea_id}`}>{t("查看已自动回流的线索")}</Link>
          : <Link className="btn justify-center" to={`/ideas?${params.toString()}`}>{t("把新发现存入线索池")}</Link>}
        <Link className="btn justify-center" to={`/approvals?id=${agent.root.research_id}&v=${version}`}>{t("提交审批")}</Link>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs">
        <Link className="text-accent hover:underline" to={`/rules?id=${agent.root.research_id}&v=${version}`}>{t("规则预检")}</Link>
        <Link className="text-accent hover:underline" to={`/portfolio?id=${agent.root.research_id}&v=${version}`}>{t("构建组合与报告")}</Link>
      </div>
      <p className="text-xs text-muted mt-2">
        {stopped
          ? t("Agent 已停止；以上操作由人决定。提交审批前通常需要先生成组合和报告。")
          : t("Agent 仍在继续下一轮；你也可以基于当前版本人工继续或提交审批。")}
      </p>
    </Panel>
  );
}

function OverlapPanel({ id, version }: { id: string; version: number }) {
  const data = useAsync(() => api.agentOverlap(id), [id, version]);
  if (data.error) return <ErrorBox error={data.error} />;
  if (!data.data) return <Loading what={t("因子库重叠")} />;
  const markets = Object.entries(data.data.markets);
  return (
    <Panel eyebrow={t("因子库")} title={t("与已获批 / 历史版本的重叠")}>
      <p className="text-xs text-muted mb-2">
        {t("打分相关性的绝对值 ≥ 0.7 视为冗余；候选为各研究版本的采纳轮次，统计期为测试期。")}
      </p>
      {markets.length === 0 ? <p className="text-sm text-muted">{t("没有可比较的市场。")}</p> : (
        <div className="grid gap-3 lg:grid-cols-3">
          {markets.map(([m, x]) => (
            <div key={m} className="panel p-3 flex flex-col gap-1.5">
              <div className="flex items-center gap-2"><span className="font-medium">{MARKET_LABEL[m as Market]}</span>
                {x.ok ? (x.redundant ? <Badge tone="bad">{t("存在冗余")}</Badge> : <Badge tone="good">{t("未发现高重叠")}</Badge>) : null}
              </div>
              {x.ok ? <>
                <p className="text-xs text-muted">{t("当前版本：")}rank IC {fix(x.current?.rank_ic, 4)} · IR {fix(x.current?.ir, 2)}</p>
                <p className="text-xs text-muted">{t("最大相关性：")}{fix(x.max_abs, 2)}</p>
                <ul className="flex flex-col gap-0.5 text-xs">
                  {(x.pairs || []).slice(0, 6).map((p) => (
                    <li key={p.key} className="flex flex-wrap items-center gap-1">
                      {p.approved && <Badge tone="accent">{t("已获批")}</Badge>}
                      <span className="num">{p.key}</span>
                      <span className="text-faint">corr {fix(p.corr, 2)}</span>
                      {p.redundant && <span className="text-bad">{t("冗余")}</span>}
                    </li>
                  ))}
                </ul>
                {x.complement && <p className="text-xs text-faint">{x.complement}</p>}
              </> : <p className="text-sm text-muted">{x.reason || t("无法比较")}</p>}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function RoundCard({ agent, round, busy, awaiting, onApprove, onReject }: {
  agent: Agent; round: AgentRound; busy: boolean; awaiting: boolean;
  onApprove: (draft: Record<string, unknown>, run: boolean, by: string) => Promise<void>;
  onReject: (reason: string, by: string) => Promise<void>;
}) {
  const facts = round.facts?.facts || [];
  const summary = (round.facts?.summary || {}) as Record<string, any>;
  const external = round.external;
  const diagnosis = round.diagnosis as Record<string, any> | null;
  const plan = round.plan as Record<string, any> | null;
  const draft = round.next_version_draft?.draft || null;
  const action = round.action as Record<string, any> | null;
  const evaluation = round.evaluation as Record<string, any> | null;
  return (
    <Panel eyebrow={tf("第 {n} 轮", { n: round.n })} title={plan?.action ? t(plan.action) : t("进行中")}
      actions={round.approval ? <Badge tone={round.approval.decision === "approved" ? "good" : "bad"}>{approvalLabel(round.approval.decision)}</Badge> : undefined}>
      <div className="flex flex-col gap-4">
        <details className="text-sm">
          <summary className="cursor-pointer text-text">
            {tf("事实（{n}）", { n: facts.length })} · {t("平台结论")} {summary.overall?.label || "—"} · {t("迁移")} {summary.transfer?.label || "—"}
          </summary>
          <ul className="mt-2 flex flex-col gap-1 text-xs">
            {facts.map((f) => <li key={f.id}><span className="num text-accent">{f.id}</span> <span className="text-faint">{f.period}</span> · {f.text}</li>)}
          </ul>
        </details>

        {external && (
          <details className="text-sm">
            <summary className="cursor-pointer text-text">
              {t("联网检索")} · {external.configured ? tf("{n} 条结果", { n: external.results?.length ?? 0 }) : t("未配置")}
              {external.model && <span className="text-faint"> · {external.model}</span>}
            </summary>
            <p className="text-xs text-faint mt-1">{tf("查询：{q}", { q: external.query })}</p>
            {external.warning && <p className="text-xs text-warn">{t(external.warning)}</p>}
            {external.error && <p className="text-xs text-warn">{external.error}</p>}
            <ul className="mt-1 flex flex-col gap-0.5 text-xs">
              {(external.results || []).map((r) => (
                <li key={r.url || r.title}>
                  {r.url
                    ? <a href={r.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">{r.title}</a>
                    : <span>{r.title}</span>}
                  <span className="text-faint"> · {r.published || "—"}</span>
                </li>
              ))}
            </ul>
          </details>
        )}

        {diagnosis && (
          <div className="text-sm">
            <div className="label mb-1">{t("诊断")} · {diagnosis.confidence}</div>
            <p className="leading-relaxed">{diagnosis.primary_cause?.text}</p>
            {(diagnosis.market_findings || []).map((x: any, i: number) => <p key={i} className="text-muted mt-1">· {x.market}：{x.text}</p>)}
            {(diagnosis.risks || []).map((x: any, i: number) => <p key={i} className="text-xs text-warn mt-1">{t("风险：")}{x.text}</p>)}
          </div>
        )}

        {plan && (
          <div className="text-sm">
            <div className="label mb-1">{t("计划")} · {plan.action}</div>
            <p className="leading-relaxed">{plan.expected_outcome || "—"}</p>
            {plan.success_criteria && <p className="text-xs text-muted mt-1">{t("预注册标准：")}{JSON.stringify(plan.success_criteria)}</p>}
            {plan.rationale && <p className="text-xs text-muted mt-1">{plan.rationale.text} <span className="num text-faint">[{plan.rationale.cites?.join(", ")}]</span></p>}
            {plan.stop_condition && <p className="text-xs text-faint mt-1">{t("停止条件：")}{plan.stop_condition}</p>}
            {plan.deep_think?.critique && (
              <details className="mt-2 border-t border-line pt-2">
                <summary className="cursor-pointer text-sm">
                  {t("深度思考 · 多智能体评审")} · {plan.deep_think.critique.verdict} ·{" "}
                  {plan.deep_think.revised ? t("已按评审修订") : t("保留原计划")}
                  {plan.deep_think.critique.model && <span className="text-faint"> · {plan.deep_think.critique.model}</span>}
                  {plan.deep_think.revision_external && (
                    <span className="text-faint"> · {plan.deep_think.revision_external.error
                      ? t("补充检索失败")
                      : tf("补充检索 {n} 条", { n: plan.deep_think.revision_external.results?.length ?? 0 })}</span>
                  )}
                </summary>
                <ul className="mt-1 flex flex-col gap-1 text-xs">
                  {(plan.deep_think.critique.issues || []).map((x: any, i: number) => (
                    <li key={i} className="flex flex-wrap items-baseline gap-1.5">
                      <Badge tone={x.severity === "block" ? "bad" : "warn"}>{x.severity === "block" ? t("阻断") : t("提醒")}</Badge>
                      <span className="text-muted">{x.text}</span>
                      {!!x.cites?.length && <span className="num text-faint">[{x.cites.join(", ")}]</span>}
                    </li>
                  ))}
                </ul>
                {!!plan.deep_think.critique.required_changes?.length && (
                  <ul className="mt-1 list-disc pl-4 text-xs text-muted">
                    {plan.deep_think.critique.required_changes.map((x: string, i: number) => <li key={i}>{x}</li>)}
                  </ul>
                )}
                {plan.deep_think.revision_external && (
                  <div className="mt-2 text-xs">
                    <div className="label mb-1">{t("修订前补充检索")}</div>
                    {plan.deep_think.revision_external.error
                      ? <p className="text-warn">{plan.deep_think.revision_external.error}</p>
                      : (
                        <ul className="flex flex-col gap-0.5">
                          {(plan.deep_think.revision_external.results || []).map((x: any, i: number) => (
                            <li key={x.url || i}>
                              {x.url
                                ? <a href={x.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">{x.title || "—"}</a>
                                : <span>{x.title || "—"}</span>}
                              <span className="text-faint"> · {x.published || "—"}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    {plan.deep_think.revision_external.warning && (
                      <p className="text-warn mt-1">{t(plan.deep_think.revision_external.warning)}</p>
                    )}
                  </div>
                )}
              </details>
            )}
          </div>
        )}

        {draft && (awaiting
          ? <DraftEditor key={`${agent.agent_id}-${round.n}`} agent={agent} draft={draft} busy={busy} onApprove={onApprove} onReject={onReject} />
          : <div className="text-sm"><div className="label mb-1">{t("下一版草稿")}</div><p className="font-medium">{draft.title_zh || "—"}</p><p className="text-muted mt-1">{draft.hypothesis}</p>{(draft.directions || []).map((x: string, i: number) => <p key={i} className="text-xs text-faint mt-1">· {x}</p>)}</div>
        )}

        {round.approval && (
          <p className="text-xs text-muted">{t("人工审批：")}{approvalLabel(round.approval.decision)} · {round.approval.by} · v{round.approval.saved_version ?? "—"} · {t("自动运行")} {String(round.approval.run)}{round.approval.reason ? ` · ${round.approval.reason}` : ""}</p>
        )}
        {action && (
          <p className="text-xs text-muted">{t("动作：")}{action.status || "—"}{(action.child_jobs || []).length ? ` · ${t("子任务")} ${(action.child_jobs || []).length}` : ""}{action.path ? ` · ${action.path}` : ""}</p>
        )}
        {evaluation && <EvaluationView e={evaluation} />}
      </div>
    </Panel>
  );
}

function EvaluationView({ e }: { e: Record<string, any> }) {
  const parent = e.parent?.metrics || {};
  const child = e.child?.metrics || {};
  return (
    <div className="text-sm">
      <div className="flex items-center gap-2"><span className="label">{t("评估")}</span>
        <Badge tone={VERDICT_TONE[e.verdict] ?? "muted"}>{VERDICT_LABEL[e.verdict] ?? t(e.verdict)}</Badge>
        <span className="text-muted">{e.reason}</span>
      </div>
      <p className="text-xs text-muted mt-1">
        {e.verdict === "improved"
          ? t("下一步：在结果页继续研究或提交审批。")
          : t("下一步：查看诊断与可信度检查，决定继续研究或存入线索池。")}
      </p>
      {e.precheck && (
        <div className="text-xs mt-1">
          <span className="label">{t("规则预检")}</span>{" "}
          {e.precheck.error ? <span className="text-bad">{e.precheck.error}</span> : (
            <>
              <span className={e.precheck.blocked ? "text-bad" : "text-muted"}>
                {e.precheck.blocked
                  ? t("存在阻断规则，不能提交审批")
                  : tf("阻断 {b} / 警告 {w} / 提示 {i}", {
                      b: e.precheck.counts?.block ?? 0,
                      w: e.precheck.counts?.warn ?? 0,
                      i: e.precheck.counts?.info ?? 0,
                    })}
              </span>
              {e.precheck.approval_path?.steps?.length ? (
                <span className="text-faint"> · {t("建议审批路径：")}{e.precheck.approval_path.steps.map((x: any) => t(x.role)).join(" → ")}</span>
              ) : null}
            </>
          )}
        </div>
      )}
      <table className="w-full text-xs mt-2">
        <thead className="text-muted"><tr><th className="text-left py-1">{t("指标")}</th><th className="text-right">rank IC</th><th className="text-right">{t("扣成本年化")}</th><th className="text-right">{t("最大回撤")}</th></tr></thead>
        <tbody>
          <tr><td className="py-1">{t("父版本")}</td><td className="num text-right">{fix(parent.rank_ic, 4)}</td><td className="num text-right">{pct(parent.annual_return_with_cost)}</td><td className="num text-right">{pct(parent.max_drawdown_with_cost)}</td></tr>
          <tr><td className="py-1">{t("子版本")}</td><td className="num text-right">{fix(child.rank_ic, 4)}</td><td className="num text-right">{pct(child.annual_return_with_cost)}</td><td className="num text-right">{pct(child.max_drawdown_with_cost)}</td></tr>
        </tbody>
      </table>
      {e.success_criteria && <p className="text-xs text-faint mt-1">{t("预注册标准：")}{JSON.stringify(e.success_criteria)}</p>}
      {e.note && <p className="text-xs text-faint mt-1">{e.note}</p>}
      <details className="mt-1"><summary className="cursor-pointer text-xs text-muted">{t("完整评估 JSON")}</summary><pre className="num text-[11px] whitespace-pre-wrap mt-1">{JSON.stringify(e, null, 1)}</pre></details>
    </div>
  );
}

function DraftEditor({ agent, draft, busy, onApprove, onReject }: {
  agent: Agent; draft: Record<string, any>; busy: boolean;
  onApprove: (draft: Record<string, unknown>, run: boolean, by: string) => Promise<void>;
  onReject: (reason: string, by: string) => Promise<void>;
}) {
  const [title, setTitle] = useState(String(draft.title_zh || ""));
  const [hypothesis, setHypothesis] = useState(String(draft.hypothesis || ""));
  const [hypothesisZh, setHypothesisZh] = useState(String(draft.hypothesis_zh || ""));
  const [directions, setDirections] = useState(String((draft.directions || []).join("\n")));
  const [exclusions, setExclusions] = useState(String((draft.exclusions || []).join("\n")));
  const [falsification, setFalsification] = useState(String(draft.falsification || ""));
  const [note, setNote] = useState(String(draft.rationale?.note || ""));
  const [markets, setMarkets] = useState<Market[]>((draft.markets || agent.root.markets) as Market[]);
  const [horizon, setHorizon] = useState(String(draft.horizon || agent.root.horizon));
  const [by, setBy] = useState("研究员（演示，未登录）");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const toggle = (m: Market) => setMarkets((xs) => xs.includes(m) ? xs.filter((x) => x !== m) : [...xs, m]);
  const payload = () => ({
    ...draft, title_zh: title.trim(), hypothesis: hypothesis.trim(), hypothesis_zh: hypothesisZh.trim(),
    directions: directions.split("\n").map((x) => x.trim()).filter(Boolean),
    exclusions: exclusions.split("\n").map((x) => x.trim()).filter(Boolean),
    falsification: falsification.trim(), rationale: { ...(draft.rationale || {}), note: note.trim() },
    markets, horizon: Number(horizon),
  });
  async function approve(run: boolean) {
    setErr(null);
    try { await onApprove(payload(), run, by.trim() || "研究员（演示，未登录）"); }
    catch (e) { setErr((e as Error).message); }
  }
  async function reject() {
    setErr(null);
    try { await onReject(reason.trim() || t("不同意本轮研究规格"), by.trim() || "研究员（演示，未登录）"); }
    catch (e) { setErr((e as Error).message); }
  }
  return (
    <div className="border border-accent/40 bg-accent/5 rounded p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{t("人工闸门")}</Badge>
        <span className="text-xs text-muted">{t("审改 vN+1 草稿；hypothesis / falsification / directions / rationale.note 必须是英文（发给 RD-Agent）。")}</span>
      </div>
      <label className="text-sm">{t("标题")}<input className="field mt-1" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="text-sm">{t("假设（英文）")}<textarea className="field mt-1 min-h-24 num text-xs" value={hypothesis} onChange={(e) => setHypothesis(e.target.value)} /></label>
      <label className="text-sm">{t("中文假设（可选）")}<textarea className="field mt-1 min-h-16 text-xs" value={hypothesisZh} onChange={(e) => setHypothesisZh(e.target.value)} /></label>
      <label className="text-sm">{t("研究方向（每行一条，英文）")}<textarea className="field mt-1 min-h-24 num text-xs" value={directions} onChange={(e) => setDirections(e.target.value)} /></label>
      <label className="text-sm">{t("排除方向（每行一条）")}<textarea className="field mt-1 min-h-16 text-xs" value={exclusions} onChange={(e) => setExclusions(e.target.value)} /></label>
      <label className="text-sm">{t("可证伪测试（英文）")}<textarea className="field mt-1 min-h-20 num text-xs" value={falsification} onChange={(e) => setFalsification(e.target.value)} /></label>
      <label className="text-sm">{t("经济逻辑（英文）")}<input className="field mt-1 num text-xs" value={note} onChange={(e) => setNote(e.target.value)} /></label>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="label">{t("市场")}</span>
        {MARKETS.map((m) => (
          <label key={m} className="flex items-center gap-1"><input type="checkbox" checked={markets.includes(m)} onChange={() => toggle(m)} />{MARKET_LABEL[m]}</label>
        ))}
        <span className="label ml-2">{t("周期")}</span>
        <select className="field w-auto" value={horizon} onChange={(e) => setHorizon(e.target.value)}>
          {[1, 5, 20, 60].map((h) => <option key={h} value={h}>{tf("{n} 日", { n: h })}</option>)}
        </select>
      </div>
      <label className="text-sm">{t("审批人（演示，未登录）")}<input className="field mt-1" value={by} onChange={(e) => setBy(e.target.value)} /></label>
      {err && <div className="text-xs text-bad">{err}</div>}
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn btn-primary h-9 px-3" disabled={busy} onClick={() => approve(true)}>{busy ? t("提交中…") : t("批准并运行")}</button>
        <button className="btn h-9 px-3" disabled={busy} onClick={() => approve(false)}>{t("只保存版本")}</button>
        <input className="field w-64" placeholder={t("拒绝原因")} value={reason} onChange={(e) => setReason(e.target.value)} />
        <button className="btn h-9 px-3" disabled={busy} onClick={reject}>{t("拒绝")}</button>
      </div>
      <p className="text-[11px] text-faint">{t("批准并运行会把草稿锁定为同一研究的新版本，并创建所选市场的运行任务；费用受预算上限约束。")}</p>
    </div>
  );
}

function TraceRows({ items, error }: { items: AgentTrace[]; error?: string }) {
  if (error) return <ErrorBox error={error} />;
  const rows = [...items].reverse().slice(0, 120);
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {rows.map((x, i) => (
        <li key={i} className="border-b border-line/60 py-1">
          <span className="num text-faint">{String(x.at || "").slice(11, 19)}</span>{" "}
          <Badge tone={x.kind === "llm" ? "accent" : x.kind === "state" ? "muted" : "warn"}>{x.kind}</Badge>{" "}
          {x.role && <span className="text-muted">{x.role}</span>}{" "}
          {x.model && <span className="text-faint">{x.model}</span>}{" "}
          {x.from && x.to && <span className="num">{x.from} → {x.to}</span>}{" "}
          {x.tool && <span className="num">{x.tool}</span>}{" "}
          {x.usage?.usd != null && <span className="num text-faint">${Number(x.usage.usd).toFixed(4)}</span>}
          {x.prompt && (
            <details className="mt-1"><summary className="cursor-pointer text-faint">{t("提示词 / 回答")}</summary>
              <pre className="num text-[11px] whitespace-pre-wrap mt-1 max-h-40 overflow-auto">{String(x.prompt)}</pre>
              <pre className="num text-[11px] whitespace-pre-wrap mt-1 max-h-40 overflow-auto">{String(x.response || "")}</pre>
            </details>
          )}
        </li>
      ))}
      {rows.length === 0 && <li className="text-faint">{t("还没有 trace。")}</li>}
    </ul>
  );
}

function AgentTraceControls({ showTrace, showWorkflow, onToggleTrace, onToggleWorkflow }: {
  showTrace: boolean; showWorkflow: boolean; onToggleTrace: () => void; onToggleWorkflow: () => void;
}) {
  return (
    <Panel eyebrow={t("可观测性")} title={t("AI 生成与决策轨迹")}>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={`btn h-8 ${showTrace ? "border-accent text-accent" : ""}`}
          aria-pressed={showTrace} onClick={onToggleTrace}>
          {showTrace ? t("隐藏决策轨迹") : t("显示决策轨迹")}
        </button>
        <button type="button" className={`btn h-8 ${showWorkflow ? "border-accent text-accent" : ""}`}
          aria-pressed={showWorkflow} onClick={onToggleWorkflow}>
          {showWorkflow ? t("隐藏生成下一个AI的思考过程") : t("显示生成下一个AI的思考过程")}
        </button>
      </div>
    </Panel>
  );
}

function TracePanel({ items, error }: { items: AgentTrace[]; error?: string }) {
  return (
    <Panel eyebrow={t("可观测性")} title={t("决策轨迹（trace）")}>
      <p className="text-xs text-muted mb-2">{t("每次 LLM 调用、工具调用和状态迁移；完整内容在 runtime/agents/<id>/trace.jsonl。")}</p>
      <div className="workflow-scroll pr-1"><TraceRows items={items} error={error} /></div>
    </Panel>
  );
}
