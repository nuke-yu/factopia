import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type Job, type Project, type ProjectRun, type ProjectSearch, type ProjectSearchHit } from "../api";
import { cssVar, MARKET_LABEL, pct, useAsync } from "../lib";
import { t, tf } from "../i18n";
import { Badge, ErrorBox, Loading, Notice, Panel } from "../components/ui";
import Globe from "../components/charts/Globe";
import ResumeButton from "../components/ResumeButton";

const STATUS_TONE = { 示例: "muted", 已锁定: "muted", 排队中: "muted", 运行中: "accent", 已完成: "good", 已中止: "warn" } as const;
const JOB_TONE = { queued: "muted", running: "accent", done: "good", failed: "bad", cancelled: "warn", budget_stopped: "warn" } as const;

/** 最近活动时间：已开始运行或已创建任务的最晚时间。 */
function lastActivity(p: Project): string {
  const times = [...p.runs.map((r) => r.started_at), ...p.jobs.map((j) => j.created_at)].filter((x): x is string => !!x);
  times.sort();
  return times[times.length - 1] ?? "";
}

/** 当前项目：URL 的 focus 指定（如从研究 Agent 返回）；没有时取最近有活动的项目。 */
function pickCurrent(items: Project[], focusId: string): Project | null {
  if (items.length === 0) return null;
  return items.find((p) => p.id === focusId)
    ?? [...items].sort((a, b) => lastActivity(b).localeCompare(lastActivity(a)))[0];
}

// 历史项目搜索状态：路由切换会卸载页面，本地 useState 会重置，所以按标签页持久化到 sessionStorage。
// AI 搜索结果一并缓存，返回页面时直接恢复，不重复调用 embedding。
const SEARCH_STATE_KEY = "factopia-projects-search";
type ProjectSearchState = {
  expanded: boolean; query: string; searched: string;
  mode: "keyword" | "ai" | null; ai: ProjectSearch | null;
};
const EMPTY_SEARCH: ProjectSearchState = { expanded: false, query: "", searched: "", mode: null, ai: null };

function readProjectSearch(): ProjectSearchState {
  try {
    const v = JSON.parse(sessionStorage.getItem(SEARCH_STATE_KEY) ?? "null") as Partial<ProjectSearchState> | null;
    if (!v) return EMPTY_SEARCH;
    const ai = v.ai && Array.isArray(v.ai.results) ? v.ai : null;
    const mode = v.mode === "ai" && !ai ? null : v.mode === "keyword" || v.mode === "ai" ? v.mode : null;
    return {
      expanded: !!v.expanded,
      query: typeof v.query === "string" ? v.query : "",
      searched: typeof v.searched === "string" ? v.searched : "",
      mode, ai,
    };
  } catch {                       // 浏览器禁用存储或数据损坏：退回空状态
    return EMPTY_SEARCH;
  }
}

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
  const [params] = useSearchParams();
  const focusId = params.get("focus") ?? "";

  // 当前项目置顶；历史项目默认收起，可按标题（回车）或 AI 语义（embedding）搜索。
  // 初始值从 sessionStorage 恢复，避免切到别的页面再回来时搜索被清空。
  const [savedSearch] = useState(readProjectSearch);
  const [expanded, setExpanded] = useState(savedSearch.expanded);
  const [query, setQuery] = useState(savedSearch.query);
  const [searched, setSearched] = useState(savedSearch.searched);
  const [mode, setMode] = useState<"keyword" | "ai" | null>(savedSearch.mode);
  const [ai, setAi] = useState<ProjectSearch | null>(savedSearch.ai);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);

  useEffect(() => {
    if (aiBusy) return;           // 进行中的 AI 搜索不落盘，避免恢复成空结果
    try {
      sessionStorage.setItem(SEARCH_STATE_KEY, JSON.stringify({ expanded, query, searched, mode, ai }));
    } catch { /* 浏览器禁用存储时仅当前页面有效 */ }
  }, [expanded, query, searched, mode, ai, aiBusy]);

  const current = useMemo(() => pickCurrent(projects.data ?? [], focusId), [projects.data, focusId]);
  const history = useMemo(() => (projects.data ?? []).filter((p) => p.id !== current?.id), [projects.data, current?.id]);
  const keywordHits = useMemo(() => {
    if (mode !== "keyword" || !searched) return history;
    const q = searched.toLocaleLowerCase();
    return history.filter((p) => p.title.toLocaleLowerCase().includes(q));
  }, [history, mode, searched]);
  const aiHits = useMemo(() => {
    if (mode !== "ai" || !ai) return [] as { hit: ProjectSearchHit; p: Project }[];
    const byId = new Map(history.map((p) => [p.id, p]));
    return ai.results.flatMap((hit) => {
      const p = byId.get(hit.id);
      return p ? [{ hit, p }] : [];
    });
  }, [ai, history, mode]);
  // 当前搜索结果显示数 / 历史项目总数；AI 搜索进行中时分子未知，显示 …。
  const shownHistory = mode === "ai" ? (ai ? aiHits.length : null)
    : mode === "keyword" ? keywordHits.length : history.length;

  function searchByTitle() {
    if (aiBusy) return;
    const q = query.trim();
    setSearched(q); setAi(null); setAiError(null);
    if (!q) { setMode(null); setExpanded(true); return; }
    setMode("keyword"); setExpanded(true);
  }

  async function searchByEmbedding() {
    const q = query.trim();
    if (!q) return;
    setAiBusy(true); setAiError(null); setSearched(q); setAi(null); setMode("ai"); setExpanded(true);
    try {
      setAi(await api.projectSearch(q));
    } catch (e) {
      setMode(null); setAiError((e as Error).message);
    } finally { setAiBusy(false); }
  }

  function clearSearch() {
    setQuery(""); setSearched(""); setMode(null); setAi(null); setAiError(null);
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] items-stretch">
        <section className="panel overflow-hidden min-h-[360px] relative">
          <div className="absolute left-5 top-5 z-10 max-w-[34ch] pointer-events-none">
            <div className="label">{t("01 · 研究项目")}</div>
            <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">{t("同一个假设，香港、伦敦、巴黎分别验证")}</h1>
            <p className="text-muted text-sm mt-2">{t("选择一个研究项目继续，或新建研究。每个项目从立项、审核、运行到结果都有记录。")}</p>
          </div>
          {/* 标题在左，地球偏右，互不遮挡；窄屏时地球放到标题下方 */}
          <div className="h-[380px] mt-36 md:mt-0 md:ml-[38%]">{overview.data ? <Globe overview={overview.data} /> : <Loading what={t("市场概况")} />}</div>
        </section>
        <Panel eyebrow={t("数据")} title={t("三地市场")}>
          {overview.data ? (
            <div className="flex flex-col gap-3">
              {(["hk", "uk", "fr"] as const).map((m) => {
                const o = overview.data!.markets[m];
                return (
                  <div key={m} className="border-l-2 pl-3" style={{ borderColor: cssVar(`--${m}`) }}>
                    <div className="flex items-baseline gap-2">
                      <span className="font-medium">{o.name}</span><span className="text-xs text-muted">{o.index} · {o.currency}</span>
                    </div>
                    <div className="num text-xs text-muted mt-0.5">{tf("{n} 只 · {a} 至 {b}", { n: o.stocks, a: o.first, b: o.last })}{o.fx_to_hkd ? ` · 1 ${o.currency} = ${o.fx_to_hkd} HKD` : ""}</div>
                    <div className="num text-xs text-faint">{tf("AI 挖掘可用数据截至 {d}", { d: o.mining_cutoff })}</div>
                  </div>
                );
              })}
            </div>
          ) : <Loading what={t("市场概况")} />}
          <button className="btn btn-primary w-full h-10 justify-center mt-5" onClick={() => navigate("/research")}>{t("新建研究 →")}</button>
        </Panel>
      </div>

      {projects.error && <ErrorBox error={projects.error} />}
      {!projects.data ? (projects.error ? null : <Loading what={t("研究项目")} />) : (
        <>
          {current && (
            <section className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="label">{t("当前项目")}</span>
                {focusId && current.id === focusId
                  ? <Badge tone="accent">{t("已定位")}</Badge>
                  : <span className="text-xs text-faint">{t("最近有更新")}</span>}
                <span className="num text-xs text-faint">{current.id}</span>
                <span className="text-xs text-faint">{t("历史项目默认收起，可用标题或 AI 语义搜索查找。")}</span>
              </div>
              <ProjectCard p={current} current />
            </section>
          )}
          {projects.data.length === 0 && <div className="text-muted">{t('还没有研究项目。点击"新建研究"开始。')}</div>}
          {current && (
            <section className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="label">{tf("历史项目（{n}）", { n: history.length })}</span>
                <button type="button" className="btn h-8 px-3 text-xs" aria-expanded={expanded} aria-controls="project-history"
                  onClick={() => setExpanded((v) => !v)}>
                  <span>{expanded
                    ? tf("收起历史项目（{m}/{n}）", { m: shownHistory ?? "…", n: history.length })
                    : tf("展开历史项目（{n}）", { n: history.length })}</span>
                  <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5"
                    className={`size-3.5 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}>
                    <path d="M4 6l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
                <form className="flex min-w-0 items-center gap-2" onSubmit={(e) => { e.preventDefault(); searchByTitle(); }}>
                  <label htmlFor="project-history-q" className="sr-only">{t("按标题查找历史项目")}</label>
                  <input id="project-history-q" className="field h-8 w-64 min-w-0 max-w-full" value={query} maxLength={200}
                    onChange={(e) => setQuery(e.target.value)} placeholder={t("按标题查找历史项目，回车搜索")} />
                  <button type="button" className="btn h-8 shrink-0 px-3 text-xs" disabled={aiBusy || !query.trim()} onClick={searchByEmbedding}>
                    {aiBusy ? t("AI 搜索中…") : t("AI 搜索")}
                  </button>
                  {mode && <button type="button" className="btn h-8 shrink-0 px-3 text-xs" onClick={clearSearch}>{t("清除")}</button>}
                </form>
              </div>

              {mode === "keyword" && (
                <p className="text-xs text-muted">{tf("标题包含“{q}”的历史项目：{n} 个", { q: searched, n: keywordHits.length })}</p>
              )}
              {mode === "ai" && ai && (
                <div className="flex flex-col gap-1.5">
                  <p className="text-xs text-muted">
                    {tf("AI 语义搜索“{q}”：{n} 个结果，按相似度排序", { q: ai.query, n: aiHits.length })}
                    {ai.model && <span className="text-faint"> · {ai.model}</span>}
                  </p>
                  {ai.method === "keyword" && (
                    <Notice tone="warn" title={t("向量检索不可用，已退回关键词匹配")}>
                      {ai.error || t("检查 .env 中的 EMBEDDING_MODEL 与向量端点配置。")}
                    </Notice>
                  )}
                </div>
              )}
              {aiError && <ErrorBox error={aiError} />}

              {aiBusy ? (
                <p className="text-sm text-muted">{t("AI 搜索中…")}</p>
              ) : expanded ? (
                <div id="project-history" className="flex flex-col gap-4">
                  {mode === "ai" ? (
                    aiHits.length === 0 ? <p className="text-sm text-muted">{t("没有匹配的历史项目。")}</p> :
                      aiHits.map(({ hit, p }) => <ProjectCard key={p.id} p={p} score={hit.score} />)
                  ) : mode === "keyword" ? (
                    keywordHits.length === 0 ? <p className="text-sm text-muted">{t("没有标题包含该关键字的历史项目。")}</p> :
                      keywordHits.map((p) => <ProjectCard key={p.id} p={p} />)
                  ) : (
                    history.length === 0 ? <p className="text-sm text-muted">{t("没有其他历史项目。")}</p> :
                      history.map((p) => <ProjectCard key={p.id} p={p} />)
                  )}
                </div>
              ) : (
                <p className="text-xs text-faint">{t("历史项目已收起；展开后可按标题或 AI 语义搜索。")}</p>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}

function ProjectCard({ p, current = false, score }: { p: Project; current?: boolean; score?: number }) {
  return (
    <Panel className={current ? "ring-1 ring-accent/50" : ""}
      eyebrow={tf("{m} · 预测周期 {h} 个交易日", { m: p.markets.map((m) => MARKET_LABEL[m]).join(" · "), h: p.horizon })} title={p.title || p.id}
      actions={<>
        {current && <Badge tone="accent">{t("当前项目")}</Badge>}
        {score != null && <Badge tone="accent">{tf("匹配度 {s}", { s: score.toFixed(3) })}</Badge>}
        <Badge tone={STATUS_TONE[p.status]}>{t(p.status)}</Badge>
        {p.example ? <Badge>{t("手写示例")}</Badge> : <Badge>v{p.versions.join(" / v")}</Badge>}
        <span className="num text-xs text-faint">{p.id}</span>
      </>}>
      <p className="text-sm text-muted leading-relaxed max-w-[110ch]">{p.hypothesis}</p>
      {!p.example && <StudyLinks p={p} />}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs text-muted">
            <tr>
              <th className="text-left py-1.5 font-normal">{t("运行")}</th><th className="text-left font-normal">{t("市场")}</th><th className="text-left font-normal">{t("状态")}</th>
              <th className="text-right font-normal">{t("轮数 / 采纳")}</th><th className="text-right font-normal">{t("起点 → 最好（扣成本年化）")}</th>
              <th className="text-right font-normal">{t("费用")}</th><th className="text-right font-normal">{t("操作")}</th>
            </tr>
          </thead>
          <tbody>
            {p.jobs.map((j) => <JobRow key={j.job_id} j={j} />)}
            {p.runs.map((r) => <RunRow key={r.run_id} r={r} />)}
          </tbody>
        </table>
        {p.runs.length === 0 && p.jobs.length === 0 && (
          <div className="text-muted text-sm py-3">{t("尚未运行。")}{p.example ? t("这是早期手写的示例规格，可在研究立项页起草新规格。") : t("在审核页锁定后发起运行。")}</div>
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
      <span>{tf("v{v} 研究结论", { v: version })}</span>
      {s ? <Badge tone={OVERALL_TONE[s.overall.state]}>{s.overall.state === "passed" ? t(s.overall.label) : tf("假设{l}", { l: t(s.overall.label) })}</Badge> : <span className="text-faint text-xs">…</span>}
    </Link>
  );
}

function RetryButton({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  return (
    <button className="btn mr-2" onClick={async () => {
      const r = await api.retryJob(jobId);
      navigate(`/run?job=${r.jobs[0].job_id}`);
    }} title={t("新建任务，从第 0 轮重头开始")}>{t("重新运行")}</button>
  );
}

/** 已发起但还没有进度的任务（排队中、准备数据中、启动失败）。 */
function JobRow({ j }: { j: Job }) {
  return (
    <tr className="border-t border-line">
      <td className="py-2 pr-3"><span className="num text-muted">{j.run_id ?? t("等待开始")}</span><span className="num text-xs text-faint ml-1.5">v{j.version}</span>
        {j.error && <div className="text-xs text-bad mt-1 max-w-[60ch] truncate" title={j.error}>{j.error}</div>}</td>
      <td className="pr-3"><span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: cssVar(`--${j.market}`) }} />{MARKET_LABEL[j.market]}</span></td>
      <td className="pr-3"><Badge tone={JOB_TONE[j.state]}>{t(j.state_zh)}</Badge></td>
      <td className="num text-right pr-3 text-faint">0 / {j.loops}</td>
      <td className="num text-right pr-3 text-faint">—</td>
      <td className="num text-right pr-3">${j.cost_usd.toFixed(2)}</td>
      <td className="text-right whitespace-nowrap">
        {["failed", "cancelled", "budget_stopped"].includes(j.state) && <><ResumeButton jobId={j.job_id} /><RetryButton jobId={j.job_id} /></>}
        <Link className="btn" to={`/run?job=${j.job_id}`}>{t("查看任务")}</Link>
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
          {r.context_leak && <Badge tone="bad">{t("受泄漏影响")}</Badge>}
          {r.linked === "推断" && <Badge>{t("按市场与周期关联")}</Badge>}
        </div>
        {r.job_error && <div className="text-xs text-bad mt-1 max-w-[60ch] truncate" title={r.job_error}>{r.job_error}</div>}
      </td>
      <td className="pr-3"><span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: cssVar(`--${r.market}`) }} />{MARKET_LABEL[r.market]}</span></td>
      <td className="pr-3"><Badge tone={STATUS_TONE[r.state]}>{t(r.state)}</Badge></td>
      <td className="num text-right pr-3">{r.rounds} / {r.adopted}</td>
      <td className="num text-right pr-3">{pct(r.baseline_return)} → <span className={r.best_return != null ? "text-good" : "text-faint"}>{pct(r.best_return)}</span></td>
      <td className="num text-right pr-3">${r.cost_usd.toFixed(2)}</td>
      <td className="text-right whitespace-nowrap">
        {r.job_id && r.job_state && ["failed", "cancelled", "budget_stopped"].includes(r.job_state) && <><ResumeButton jobId={r.job_id} /><RetryButton jobId={r.job_id} /></>}
        <Link className="btn mr-2" to={`/run?run=${r.run_id}`}>{r.state === "运行中" ? t("查看过程") : t("回放过程")}</Link>
        <Link className="btn" to={`/results?run=${r.run_id}`}>{t("查看结果")}</Link>
      </td>
    </tr>
  );
}
