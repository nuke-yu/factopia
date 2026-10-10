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
  locked_at?: string; locked_by?: string; precheck_setup?: Precheck; edits?: { field: string; by: string; at: string }[]; past_runs?: string[];
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
  annual_return_with_cost_default?: number | null;
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
export type Diagnostics = {
  run_id: string; round: string; market: Market; horizon: number; base_currency: string; backtest_note: string;
  cost: { gross_excess: number | null; net_excess: number | null; cost_drag: number | null; tax_drag: number | null; tax_share: number | null;
    excess_by_cost_multiple: Record<string, number | null>; breakeven_cost_multiple: number | null;
    turnover_daily: number | null; turnover_annual: number | null; holding_days: number | null };
  liquidity: { holdings_adv_weighted: number | null; low_liquidity_weight: number | null; avg_holdings: number | null;
    capacity_local: number | null; capacity_base: number | null; capacity_rule: string };
  fx: { base: string; local: string; same_currency: boolean; fx_contribution: number | null; portfolio_local?: number | null;
    portfolio_base?: number | null; excess_local?: number | null; excess_base?: number | null };
  exposure: { beta: number | null; style: Record<string, number | null>; sector_weights: Record<string, number>;
    max_sector_weight: number | null; sector_hhi: number | null; style_note: string };
  economic: { meaningful: boolean; reasons: string[]; label: string };
  signal: { rank_ic: number | null; rank_ic_t: number | null; neutral_rank_ic: number | null; neutral_rank_ic_t: number | null;
    neutral_retained: number | null; neutral_controls: string[]; ic_by_year: Record<string, number | null>;
    year_sign_consistency: number | null; ic_by_horizon: Record<string, number | null>;
    regime: { high_vol: number | null; low_vol: number | null; up_market: number | null; down_market: number | null };
    redundancy: Record<string, number | null> } | null;
};
export type Transfer = { label: string; statistical?: string; reason: string; threshold: number;
  markets: Record<string, { t_aligned: number | null; effective: boolean; economic: Diagnostics["economic"] | null }> };
export type Explanation = { sentences: { text: string; cites: string[] }[]; dropped: number; transfer_view: string;
  local_adjustments: string[]; facts: { id: string; market: Market; key: string; text: string }[];
  model: string; prompt_version: string; created_at: string; note: string };
export type StudyMarket = {
  market: Market; primary: boolean; state: JobState | "none" | "incomplete"; run_id: string | null; attempts: number; job: Job | null;
  cost_usd?: number; duration_s?: number | null; baseline?: Metrics | null;
  rounds: { loop: number; decision: boolean | null; rank_ic: number | null; rank_ic_t: number | null; return: number | null; drawdown: number | null; passed: boolean | null; hypothesis: string | null }[];
  focus: {
    loop: number; metrics: Metrics; checks: Check[]; passed: boolean | null; hypothesis: string | null; factors: string[];
    quantile: { spread_mean: number | null; spread_positive_share: number | null; tie_share: number | null; groups: number } | null;
    significance: Significance | null; credibility: Credibility; validation_job: Job | null;
    diagnostics: Diagnostics | null; baseline_diagnostics: Diagnostics | null;
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
  transfer: Transfer; explanation: Explanation | null;
  cross_market: { months: number; pairs: { a: Market; b: Market; corr: number | null }[]; note: string } | null;
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


// ---------------------------------------------------------------- 情报台：多语言公告与文本信号
export type TextConcept = { concept: string; name?: string; direction: number; negated: boolean; confidence: number; evidence?: string };
export type TextFlag = { code: string; label: string };
export type TextEval = {
  horizon: number; n_days: number; rank_ic_mean: number | null; rank_ic_t: number | null; positive_ratio: number | null;
  tercile_spread_mean: number | null; tercile_spread_t: number | null; avg_coverage: number | null;
  events: { pessimistic: { n: number; mean_excess: number | null }; optimistic: { n: number; mean_excess: number | null };
    rank_corr: number | null; diff_t: number | null };
  ic_monthly: Record<string, number>;
};
export type TextDataset = {
  dataset: string; market: Market; market_zh: string; simulated: boolean; source: string; licence: string;
  processing_delay_min: number; ready: boolean; n_filings: number; n_companies: number; n_changes: number;
  hold_days: number | null; evaluation: TextEval | null;
  truth: { label: string; rule: string; expected: string; companies: number; filings: number } | null;
};
export type TextCompareRow = {
  dataset: string; market: Market; simulated: boolean; n_filings: number; n_companies: number;
  rank_ic_mean: number | null; rank_ic_t: number | null; tercile_spread_mean: number | null; n_days: number;
  event_pessimistic?: { n: number; mean_excess: number | null }; event_optimistic?: { n: number; mean_excess: number | null };
  event_diff_t?: number | null; verdict: string;
};
export type TextOverview = {
  datasets: TextDataset[]; compare: { horizon: number; markets: TextCompareRow[]; note: string } | null;
  glossary_version: string; review_counts: { pending: number; done: number };
  concepts: { concept: string; zh: string; en: string; fr: string; terms: Record<string, string[]> }[];
};
export type TextCitation = { i: number; concept: string; name: string; value: number; negated: boolean; evidence: string; review: string; text: string };
export type TextFiling = {
  id: string; instrument: string; company: string; title: string; language: string; subtype: string | null; url: string | null;
  business_date: string; published_at: string; available_at: string; effective_date: string | null; after_close: boolean;
  tone: Record<string, number>; tone_total: number; change: number | null; prev_id: string | null;
  changes: Record<string, number | null>; pending_review: number; model: string; glossary_version: string;
  prompt_version: string; citations: TextCitation[];
};
export type TextParagraph = {
  i: number; text: string; concepts: TextConcept[]; translation: Record<string, string>; translation_confidence: number | null;
  flags: TextFlag[]; review_status: string | null; method: string | null;
};
export type TextDoc = {
  dataset: string; simulated: boolean; meta: TextFiling | null; model: string | null; prompt_version: string | null;
  glossary_version: string | null; labelled_at: string | null; pages_total: number | null; pages_read: number | null;
  paragraphs: TextParagraph[];
};
export type TextHit = {
  dataset: string; simulated: boolean; source: string; licence: string; doc_id: string; paragraph: number; company: string;
  instrument: string; title: string; url: string | null; language: string; language_zh: string; business_date: string;
  published_at: string; available_at: string; effective_date: string | null; original: string;
  translation: Record<string, string>; translation_confidence: number | null; concepts: TextConcept[]; matched: string[];
  review_status: string; model: string; glossary_version: string;
};
export type TextSummary = {
  sentences: { text: string; cites: number[] }[]; dropped: number; model: string | null;
  citations: { n: number; dataset: string; doc_id: string; paragraph: number; company: string; title: string;
    published_at: string; available_at: string; simulated: boolean; url: string | null }[];
};
// ---------------------------------------------------------------- 数据台：参考数据与时间点一致性
export type PitDomain = { key: string; name: string; used_in_backtest: boolean; business_date: string; published_at: string;
  available_at: string; status: "ok" | "warn"; note: string };
export type PitStatus = { built_at: string; point_in_time: boolean; label: string; reasons: string[]; domains: PitDomain[];
  membership: Record<string, string> };
export type Security = {
  symbol: string; market: Market; name: string; long_name: string | null; exchange: string; currency: string;
  figi: string | null; composite_figi: string | null; share_class_figi: string | null; security_type: string | null;
  isin: string | null; lei: string | null; legal_name: string | null; lei_method: string | null; jurisdiction: string | null;
  sector: string | null; industry: string | null; hq_country: string | null; reporting_currency: string | null;
  fiscal_year_end_month: number | null; accounting: string; accounting_basis: string; reporting: string;
  issuer_id: string; primary_listing: string | null; primary_listing_basis: string; is_primary: boolean | null;
  snapshot_at: string | null; multi_market: boolean;
};
export type Relation = { issuer_id: string; venue: string; venue_zh: string | null; ticker: string; name: string | null;
  security_type: string | null; relation: string; relation_zh: string; in_universe: boolean; status: string; note: string | null };
export type CorpAction = { symbol: string; market: Market; date: string; type: string; type_zh: string; value: number | null;
  detail: string; source: string };
export type RefSummary = {
  master: { securities: number; issuers: number; with_lei: number; with_figi: number; with_isin: number;
    relations: Record<string, number>; multi_market_issuers: number; primary_unresolved: number; built_at: string } | null;
  actions: { total: number; by_type: Record<string, number>; symbols_with_actions: number;
    adjustment_checks: { symbol: string; date: string; ratio: number; adjusted_return: number; raw_jump: number; ok: boolean }[];
    adjustment_failures: unknown[]; not_covered: string[]; built_at: string } | null;
  pit: PitStatus | null;
};
export type DictField = { name: string; zh: string; unit: string; source: string; adjustment: string; available: string; note?: string };
export type RefDictionary = {
  fields: { version: string; fields: DictField[] };
  markets: { version: string; markets: Record<Market, { name: string; exchange: string; timezone: string; currency: string;
    close_local: string; eod_delay_min: number; reporting: string; accounting_default: string }> };
  membership: Record<string, { rows: number; sources: Record<string, number>; point_in_time: boolean }>;
};
export type ReviewDecision = { key: string; action: string; reviewer: string; note: string; at: string; concepts: TextConcept[] | null; translation: Record<string, string> | null };
export type ReviewItem = {
  key: string; dataset: string; doc_id: string; i: number; simulated: boolean; company: string; instrument: string; title: string;
  published_at: string; language: string; language_zh: string; original: string; translation: Record<string, string>;
  translation_confidence: number | null; model: string; glossary_version: string; method: string;
  concepts: TextConcept[]; model_concepts: TextConcept[]; flags: TextFlag[]; decision: ReviewDecision | null; history: ReviewDecision[];
};
export type ReviewIn = { dataset: string; doc_id: string; i: number; action: string; reviewer: string; note?: string;
  concepts?: { concept: string; direction: number; negated?: boolean }[] | null; translation?: Record<string, string> | null };

// 地区规则包与预检（Spec 第 14.12 节；BR-19、20、21、39）
export type Severity = "block" | "warn" | "info";
export type RuleHit = {
  rule_id: string; title: string; severity: Severity; severity_zh: string; stage: string; stage_zh: string; market: string; market_zh: string;
  reason: string; evidence: Record<string, unknown>; pack: string; pack_version: string; pack_status: string; owner: string;
  source: string; impact: string; approvers: string[];
};
export type Precheck = {
  checked_at: string; stages: string[]; stages_zh: string[]; blocked: boolean; counts: Record<Severity, number>; hits: RuleHit[];
  packs: { pack: string; version: string; status: string; owner: string; file: string }[]; sources_version: string;
  approval_path: { steps: { role: string; reasons: string[] }[]; blocked: boolean; note: string }; disclaimer: string;
};
export type Rule = { id: string; title: string; severity: Severity; stages: string[]; check: Record<string, unknown> & { type: string };
  source: string; approvers: string[]; impact: string };
export type RulePack = { pack: string; market: string; version: string; owner: string; effective_date: string; last_review: string;
  status: string; file: string; rules: Rule[]; costs?: Record<"buy" | "sell", { tax: number; commission: number; note?: string }> };
export type DataSource = { name: string; status: string; licence: string; note?: string; regions?: string[]; cross_border?: boolean };
export type RulePacks = {
  packs: RulePack[]; disclaimer: string; severity_zh: Record<string, string>; stage_zh: Record<string, string>;
  category_zh: Record<string, string>; status_zh: Record<string, string>;
  sources: { version: string; owner: string; last_review: string; sources: Record<string, DataSource>;
    category_sources: Record<string, Partial<Record<Market, string>>>; always: string[] };
};
export type VersionRules = { research_id: string; version: number; setup: Precheck; portfolio: Precheck | null; portfolio_note: string | null;
  focus: Record<string, { run_id: string | null; loop: number | string | null }>; overrides: Record<string, unknown>;
  portfolio_source?: Record<string, string> };

// 授权组合与报告（Spec 第 14.13 节；BR-03、14、32、40）
export type PerfStats = { return_ann: number; return_total: number; vol: number; sharpe: number | null; max_drawdown: number;
  bench_return_ann?: number; excess_ann?: number; tracking_error?: number; info_ratio?: number | null };
export type FeedbackSuggestion = { market: Market; action: string; action_zh: string; reasons: string[]; signal: { run_id: string | null; loop: number | string | null } };
export type FeedbackDecision = { market: Market; action: string; action_zh: string; note: string; by: string; at: string };
export type PortfolioSummary = {
  research_id: string; version: number; mandate: string; mandate_name: string; built_at: string; markets: Market[];
  base_currency: string; report_currency: string;
  benchmark: { weights: Record<string, number>; indices: Record<string, string>; label: string };
  period: { start: string; end: string; days: number; rebalances: number };
  terms: { max_tracking_error: number; max_turnover: number; holdings_per_market: number; max_stock_weight: number; long_only: boolean;
    market_limits: Record<string, number>; benchmark_weights: Record<string, number>; restricted: string[] };
  signals: Record<string, { run_id: string | null; loop: number | string | null; factors?: string[]; rank_ic_t?: number | null; note?: string }>;
  constraints: { name: string; value: string; source: string; status: string }[];
  metrics: { total: PerfStats; avg_turnover: number | null; markets: Record<string, { local: PerfStats; base: PerfStats; avg_weight: number; cost_ann: number }> };
  fx_split: Record<string, { local: number; fx: number; base: number; cross: number; identity_max_error: number }>;
  attribution: { total_excess: number; sum_of_effects: number; linking_error: number; daily_identity_max_error: number; method: string;
    markets: Record<string, { allocation: number; selection: number; fx_interaction: number }>;
    by_effect: { allocation: number; selection: number; fx_interaction: number }; fx_contribution_abs: Record<string, number>;
    local_contribution_abs: Record<string, number> };
  feedback: FeedbackSuggestion[]; backtest_note: string;
};
export type PortfolioResp = { summary: PortfolioSummary | null; feedback: FeedbackDecision[] };
export type PortfolioSeries = { dates: string[]; port: number[]; bench: number[]; weights: Record<string, number[]> };
export type ReportRef = { name: string; url: string; bytes: number; has_portfolio?: boolean };

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
  pit: () => get<PitStatus>("/pit"),
  rulePacks: () => get<RulePacks>("/rules/packs"),
  portfolio: (id: string, v: number) => get<PortfolioResp>(`/research/${id}/versions/${v}/portfolio`),
  buildPortfolio: (id: string, v: number) => post<PortfolioResp>(`/research/${id}/versions/${v}/portfolio`, {}),
  portfolioSeries: (id: string, v: number) => get<PortfolioSeries>(`/research/${id}/versions/${v}/portfolio/series`),
  portfolioFeedback: (id: string, v: number, body: { market: Market; action: string; note: string }) =>
    post<FeedbackDecision[]>(`/research/${id}/versions/${v}/portfolio/feedback`, body),
  makeReport: (id: string, v: number) => post<ReportRef>(`/research/${id}/versions/${v}/report`, {}),
  reports: (id: string, v: number) => get<ReportRef[]>(`/research/${id}/versions/${v}/reports`),
  precheck: (spec: Partial<Spec>, overrides: Record<string, unknown> = {}) => post<Precheck>("/rules/precheck", { spec, overrides }),
  versionRules: (id: string, version: number, overrides?: Record<string, unknown>) => overrides
    ? post<VersionRules>(`/research/${id}/versions/${version}/rules`, { overrides })
    : get<VersionRules>(`/research/${id}/versions/${version}/rules`),
  refSummary: () => get<RefSummary>("/refdata/summary"),
  securities: (market?: Market) => get<{ securities: Security[]; relations: Relation[] }>(`/refdata/securities${market ? `?market=${market}` : ""}`),
  actions: (symbol?: string, type?: string) => {
    const p = new URLSearchParams({ limit: "800" });
    if (symbol) p.set("symbol", symbol);
    if (type) p.set("type", type);
    return get<{ total: number; counts: Record<string, number>; items: CorpAction[] }>(`/refdata/actions?${p}`);
  },
  dictionary: () => get<RefDictionary>("/refdata/dictionary"),
  textOverview: () => get<TextOverview>("/text/overview"),
  textFilings: (ds: string, instrument?: string) =>
    get<{ dataset: string; simulated: boolean; source: string; licence: string; total: number; items: TextFiling[] }>(
      `/text/${ds}/filings${instrument ? `?instrument=${encodeURIComponent(instrument)}` : ""}`),
  textDoc: (ds: string, id: string) => get<TextDoc>(`/text/${ds}/filings/${id}`),
  textSearch: (q: string, datasets: string[] = [], concepts: string[] = []) => {
    const p = new URLSearchParams({ q, limit: "60" });
    datasets.forEach((d) => p.append("dataset", d));
    concepts.forEach((c) => p.append("concept", c));
    return get<{ query: string; concepts: { concept: string; name: string; terms: string[] }[]; results: TextHit[]; truncated: boolean }>(`/text/search?${p}`);
  },
  textSummary: (q: string, concepts: string[] = []) => post<TextSummary>("/text/summary", { q, concept: concepts.length ? concepts : null }),
  textReview: (status: "pending" | "done" | "all", dataset?: string) =>
    get<{ items: ReviewItem[]; counts: { pending: number; done: number }; actions: Record<string, string> }>(
      `/text/review?status=${status}&limit=60${dataset ? `&dataset=${dataset}` : ""}`),
  textDecide: (body: ReviewIn) => post<ReviewDecision>("/text/review", body),
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
  explain: (id: string, version: number) => post<Explanation>(`/research/${id}/versions/${version}/explain`, {}),
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
