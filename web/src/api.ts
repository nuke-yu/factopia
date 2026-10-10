// 后端接口与数据类型。前端只读取后端导出的结果，不做研究计算（Spec UI-07）。

export type Market = "hk" | "uk" | "fr";
export const MARKETS: Market[] = ["hk", "uk", "fr"];

export type Metrics = {
  ic: number | null;
  rank_ic: number | null;
  annual_return_with_cost: number | null;
  annual_return_without_cost: number | null;
  max_drawdown_with_cost: number | null;
  information_ratio_with_cost: number | null;
};

export type Factor = { name: string; description: string; formulation: string; variables: unknown; code: string | null };

export type Round = {
  loop: number;
  status: "done" | "running";
  hypothesis: string | null;
  reason: string | null;
  factors: Factor[];
  coding_attempts: number;
  metrics: Metrics | null;
  sota_before: Metrics | null;
  decision: boolean | null;
  feedback: { observations: string; evaluation: string; new_hypothesis: string; reason: string } | null;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  steps: Record<string, number>;
  started_at: string | null;
  ended_at: string | null;
};

export type Run = {
  run_id: string;
  market: Market;
  horizon: number;
  context?: string | null;
  context_leak: boolean | null;
  settings: Record<string, string>;
  baseline: { metrics: Metrics | null; workspace: string } | null;
  rounds: number;
  adopted: number;
  cost_usd: number;
  started_at: string | null;
  ended_at: string | null;
  status: "done" | "incomplete";
  research?: { id: string; version: number; market: Market; horizon: number; job_id?: string } | null;
};

export type PeriodKey = "train" | "valid" | "test";
export type Cube = {
  horizon: number;
  threshold: number;
  periods: Record<PeriodKey, { label: string; note: string }>;
  groups: string[];
  factors: {
    name: string;
    group: string;
    desc: string;
    periods: Record<PeriodKey, { t: Record<Market, number | null>; ric: Record<Market, number | null>; label: string }>;
  }[];
};

export type Surface = {
  horizon: number;
  test_boundary: string;
  markets: Partial<Record<Market, { months: string[]; factors: string[]; monthly: (number | null)[][]; rolling6: (number | null)[][] }>>;
};

export type MarketInfo = {
  name: string; city: string; index: string; currency: string; lat: number; lon: number;
  stocks: number; excluded: string[]; first: string; last: string; sessions: number; mining_cutoff: string; fx_to_hkd?: number;
};
export type Overview = { markets: Record<Market, MarketInfo>; fx_date: string };

export type Columns = Record<string, (number | string | null)[]>;
/** 按模型打分把股票从低（第 1 组）到高（第 5 组）分组后的未来收益。 */
export type Quantiles = {
  horizon: number; groups: number; days: number;
  mean: (number | null)[]; universe_mean: number | null; size: number[]; empty_days: number[];
  spread_mean: number | null; spread_positive_share: number | null; tie_share: number | null;
  dates: string[]; nav: (number | null)[][]; nav_universe: (number | null)[];
};
export type PeriodReturn = { days: number; excess: number; bench: number };
export type Monthly = { months: (PeriodReturn & { year: number; month: number })[]; years: (PeriodReturn & { year: number })[] };
export type Positions = { dates: string[]; instruments: string[]; weights: number[][] };

export type ResearchSpec = {
  id: string; market: Market; horizon: number; direction: string; hypothesis: string; constraints?: string[];
};

export type ProjectRun = {
  run_id: string; market: Market; rounds: number; adopted: number; cost_usd: number; started_at: string | null;
  state: "已完成" | "运行中" | "已中止"; context_leak: boolean | null; linked: "记录" | "推断"; version: number | null;
  job_id: string | null; job_state: JobState | null; job_error: string | null;
  baseline_return: number | null; best_return: number | null;
};
export type ProjectStatus = "示例" | "已锁定" | "排队中" | "运行中" | "已完成" | "已中止";
export type Project = {
  id: string; title: string; markets: Market[]; horizon: number; hypothesis: string; versions: number[]; example: boolean;
  status: ProjectStatus; runs: ProjectRun[]; jobs: Job[];
};

// ── 研究规格：授权、起草、版本、运行任务（Spec 第 16 章）──
export type Mandate = {
  id: string; name: string; markets: Market[]; constraints: string[]; free_text: string[];
  terms: { key: string; name: string; value: string; usage: string }[];
};
export type DataNeed = { category: string; name: string; detail: string; usable: boolean; status: Partial<Record<Market, string>> };
export type Rationale = { category: "behavioural" | "risk_premium" | "friction" | "flows" | "other"; note: string };
export type EvidenceAlpha = {
  id: string; market: Market; source: string; period: { start: string; end: string }; test_start: string; significant: number;
  factors: { name: string; group: string; desc: string; t: number; ric: number; label: string }[];
};
export type DraftResult = {
  draft: {
    id: string | null; title_zh: string; hypothesis: string; hypothesis_zh: string; rationale: Rationale;
    directions: string[]; exclusions: string[]; falsification: string; data_needs: DataNeed[];
    mandate_interpretations: { term: string; interpretation_zh: string; constraint_en: string | null }[];
    open_questions: string[]; leak_flags: string[]; system_questions: string[]; constraints: string[];
  };
  evidence: { groups: string[]; alpha158: EvidenceAlpha[]; past_runs: { run_id: string; market: Market; loop: number; hypothesis: string; adopted: boolean }[] };
  mandate: Mandate;
  meta: { model: string; prompt_version: string; prompt_tokens: number; completion_tokens: number; seconds: number; at: string };
};
export type DraftInput = {
  hypothesis: string; direction: string; markets: Market[]; horizon: number; mandate: string; groups: string[]; include_past_runs: boolean;
};
export type Question = { text: string; source: "AI" | "系统" | "平台"; confirmed: boolean };
export type Spec = {
  id: string; version?: number; title_zh: string; mandate: string; markets: Market[]; horizon: number;
  hypothesis_original: string; direction_original: string; hypothesis: string; hypothesis_zh: string; rationale: Rationale;
  directions: string[]; exclusions: string[]; falsification: string; data_needs: DataNeed[]; constraints: string[];
  evidence: { groups: string[]; include_past_runs: boolean; items: string[] }; questions: Question[];
  run: { loops: number; budget_usd: number };
  ai_draft: DraftResult["meta"] & { original: Pick<Spec, "hypothesis" | "rationale" | "directions" | "exclusions" | "falsification"> };
  locked_at?: string; locked_by?: string; edits?: { field: string; by: string; at: string }[]; past_runs?: string[];
};
export type JobState = "queued" | "running" | "done" | "failed" | "cancelled" | "budget_stopped";
export type Job = {
  kind?: "validate"; target_run?: string; loop?: number; placebo_n?: number;
  job_id: string; batch_id: string; research_id: string; version: number; market: Market; horizon: number; loops: number;
  budget_usd: number; state: JobState; state_zh: string; created_at: string; started_at: string | null; ended_at: string | null;
  run_id: string | null; cost_usd: number; rounds: number; error: string | null;
};
export type JobDetail = Job & { log_tail: string[]; exported: boolean };
export type LiveMetrics = { annualized_return?: number; information_ratio?: number; max_drawdown?: number };
export type LiveState = {
  phase: "prepare" | "loop"; loop: number | null; step: string | null; step_zh: string | null; step_started_at: string | null;
  docker: { name: string; text: string; started_at: string | null } | null;
  counters: { llm: number; exec_ok: number; docker: number };
  activity: { t: string | null; kind: "step" | "docker" | "exec" | "result" | "error"; text: string }[];
  backtests: { loop: number | null; t: string | null; with_cost?: LiveMetrics; without_cost?: LiveMetrics }[];
  fatal: string | null; last_log_at: string | null; updated_at?: string;
};
// ── 研究结论：按锁定的可证伪测试判定（Spec 第 14.5 节、RS-10）──
export type Check = { key: "rank_ic" | "return" | "drawdown"; label: string; value: number | null; ref: number | null; ok: boolean | null };
export type RoundVerdict = { loop: number; checks: Check[]; passed: boolean | null };
export type VerdictRule = { primary: Market; ric_sign: 1 | -1; sign_from_text: boolean; primary_market: boolean };
// ── 可信度检查（factopia/validate，Spec 第 14.7 节）──
export type Significance = { mean: number | null; t: number | null; n: number; low: number | null; high: number | null };
export type ValPeriod = {
  start: string | null; end: string | null; days: number;
  annual_return_with_cost: number | null; annual_return_with_cost_range?: [number, number] | null; annual_return_with_cost_all?: number[];
  max_drawdown_with_cost: number | null; max_drawdown_with_cost_range?: [number, number] | null;
  information_ratio_with_cost: number | null; annual_return_without_cost: number | null;
  rank_ic: number | null; rank_ic_t: number | null; rank_ic_low: number | null; rank_ic_high: number | null;
  spread_mean?: number | null; spread_positive_share?: number | null; tie_share?: number | null;
};
export type Validation = {
  run_id: string; loop: number; market: Market; horizon: number; status: "placebo" | "done"; seeds: number;
  started_at: string; ended_at?: string; errors: string[];
  periods: { test: { start: string; end: string }; holdout: { start: string; end: string } };
  factors: { name: string; n: number; covered: number; match_share: number; corr: number | null }[];
  ai: { test: ValPeriod; holdout: ValPeriod }; baseline: { test: ValPeriod; holdout: ValPeriod };
  curve: { dates: string[]; ai: number[][]; baseline: number[][] };
  original: { ai: Metrics | null; baseline: Metrics | null } | null;
  placebo?: { n: number; runs: { seed: number; annual_return_with_cost: number | null; max_drawdown_with_cost: number | null; rank_ic: number | null }[];
    share_ge_ai: number | null; share_ge_baseline: number | null; median: number | null };
};
export type CredCheck = { key: "significance" | "holdout" | "reproduce" | "placebo"; label: string; ok: boolean | null; value: any };
export type Credibility = {
  state: "unchecked" | "pending" | "credible" | "doubtful"; label: string; checks: CredCheck[];
  noise: { range: [number, number] | null; seeds: number | null; reported: number | null } | null;
};
export type StudyMarket = {
  market: Market; primary: boolean; state: JobState | "none" | "incomplete"; run_id: string | null; attempts: number; job: Job | null;
  cost_usd?: number; duration_s?: number | null; baseline?: Metrics | null;
  rounds: { loop: number; decision: boolean | null; rank_ic: number | null; rank_ic_t: number | null; return: number | null; drawdown: number | null; passed: boolean | null; hypothesis: string | null }[];
  focus: {
    loop: number; metrics: Metrics; checks: Check[]; passed: boolean | null; hypothesis: string | null; factors: string[];
    quantile: { spread_mean: number | null; spread_positive_share: number | null; tie_share: number | null; groups: number } | null;
    significance: Significance | null; credibility: Credibility; validation_job: Job | null;
    validation: (Pick<Validation, "status" | "periods" | "seeds" | "started_at" | "ended_at" | "errors" | "ai" | "baseline"> & {
      placebo: { n: number; share_ge_ai: number | null; share_ge_baseline: number | null; median: number | null } | null }) | null;
  } | null;
};
export type Study = {
  research_id: string; version: number; hypothesis: string; title: string | null;
  rule: { primary: Market; markets: Market[]; ric_sign: 1 | -1; sign_from_text: boolean; text: string };
  overall: { state: "pending" | "rejected" | "passed"; label: string; reason: string };
  markets: Partial<Record<Market, StudyMarket>>;
  findings: { tone: "bad" | "warn" | "good"; text: string }[];
};
export type RunnerStatus = { paused: { reason: string; at: string } | null; queued: number; running: number };
export type Preview = { market: Market; test_start: string; dates: Record<string, string>; text: string; chars: number };

type EventBase = { index: number; t: number; loop: number; step: string; has_prompt: boolean };
export type RunEvent = EventBase & (
  | { kind: "step"; name: string; phase: "start" | "end" }
  | { kind: "hypothesis"; text: string; reason: string }
  | { kind: "factors"; factors: { name: string; description: string }[] }
  | { kind: "llm"; purpose: string; seconds: number | null; prompt_chars: number; response: string }
  | { kind: "check"; attempt: number; items: { name: string; ok: boolean; message: string }[] }
  | { kind: "coded"; passed: number; total: number }
  | { kind: "backtest"; metrics: Metrics | null }
  | { kind: "decision"; decision: boolean; observations: string; new_hypothesis: string }
  | { kind: "cost"; total_usd: number }
);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join("；") : body.detail;
    throw new Error(detail ?? `请求失败：${res.status} ${path}`);
  }
  return res.json() as Promise<T>;
}
const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  overview: () => get<Overview>("/overview"),
  cube: (h: number) => get<Cube>(`/alpha158/${h}/cube`),
  surface: (h: number, m: Market) => get<Surface>(`/alpha158/${h}/surface?market=${m}`),
  runs: () => get<Run[]>("/runs"),
  run: (id: string) => get<{
    run: Run & { state: ProjectRun["state"] }; rounds: Round[]; job: Job | null; batch: Job[];
    verdicts: RoundVerdict[] | null; rule: VerdictRule | null;
  }>(`/runs/${id}`),
  validation: (runId: string) => get<{ items: Record<string, Validation>; jobs: Job[] }>(`/runs/${runId}/validation`),
  validate: (runId: string, loop?: number) => post<Job>(`/runs/${runId}/validate`, loop == null ? {} : { loop }),
  study: (id: string, version: number) => get<Study>(`/research/${id}/versions/${version}/study`),
  backtest: (id: string) => get<Record<string, Columns>>(`/runs/${id}/backtest`),
  monthly: (id: string) => get<Record<string, Monthly>>(`/runs/${id}/monthly`),
  quantiles: (id: string) => get<Record<string, Quantiles>>(`/runs/${id}/quantiles`),
  positions: (id: string, key: string, step = 5) => get<Positions>(`/runs/${id}/positions/${key}?step=${step}`),
  projects: () => get<Project[]>("/projects"),
  events: (id: string) => get<RunEvent[]>(`/runs/${id}/events`),
  prompt: (id: string, index: number) => get<{ system: string; user: string; response: string | null }>(`/runs/${id}/events/${index}/prompt`),
  researchExamples: () => get<ResearchSpec[]>("/research/examples"),
  mandates: () => get<Mandate[]>("/mandates"),
  draft: (body: DraftInput) => post<DraftResult>("/research/draft", body),
  preview: (spec: Partial<Spec>, market: Market) => post<Preview>("/research/preview", { spec, market }),
  lock: (spec: Spec) => post<Spec>("/research/versions", spec),
  versions: (id: string) => get<Spec[]>(`/research/${id}/versions`),
  startRuns: (id: string, version: number, body: { markets: Market[]; loops: number; budget_usd: number }) =>
    post<{ batch_id: string; jobs: Job[] }>(`/research/${id}/versions/${version}/runs`, body),
  jobs: (batch?: string) => get<Job[]>(`/jobs${batch ? `?batch=${batch}` : ""}`),
  job: (id: string) => get<JobDetail>(`/jobs/${id}`),
  cancelJob: (id: string) => post<{ ok: boolean }>(`/jobs/${id}/cancel`, {}),
  jobLive: (id: string) => get<LiveState | Record<string, never>>(`/jobs/${id}/live`),
  retryJob: (id: string) => post<{ batch_id: string; jobs: Job[] }>(`/jobs/${id}/retry`, {}),
  runner: () => get<RunnerStatus>("/runner"),
  resumeRunner: () => post<RunnerStatus>("/runner/resume", {}),
  context: (m: Market, lang: "zh" | "en" = "zh") => get<{ markdown: string }>(`/context/${m}?lang=${lang}`),
};
