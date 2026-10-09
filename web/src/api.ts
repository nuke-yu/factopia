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
export type Positions = { dates: string[]; instruments: string[]; weights: number[][] };

export type ResearchSpec = {
  id: string; market: Market; horizon: number; direction: string; hypothesis: string; constraints?: string[];
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `请求失败：${res.status} ${path}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  overview: () => get<Overview>("/overview"),
  cube: (h: number) => get<Cube>(`/alpha158/${h}/cube`),
  surface: (h: number, m: Market) => get<Surface>(`/alpha158/${h}/surface?market=${m}`),
  runs: () => get<Run[]>("/runs"),
  run: (id: string) => get<{ run: Run; rounds: Round[] }>(`/runs/${id}`),
  backtest: (id: string) => get<Record<string, Columns>>(`/runs/${id}/backtest`),
  positions: (id: string, key: string, step = 5) => get<Positions>(`/runs/${id}/positions/${key}?step=${step}`),
  researchExamples: () => get<ResearchSpec[]>("/research/examples"),
  context: (m: Market, lang: "zh" | "en" = "zh") => get<{ markdown: string }>(`/context/${m}?lang=${lang}`),
};
