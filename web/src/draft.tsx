import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type DraftResult, type Market, type Spec } from "./api";

// 研究立项页填写的输入、AI 起草结果、审核页锁定的版本（Spec 第 16 章）。在页面之间共享，刷新页面后清空。
export type Draft = {
  mandate: string;
  markets: Market[];
  horizon: 1 | 5 | 20 | 60;
  hypothesis: string;
  direction: string;
  groups: string[];          // 证据包中提供给 AI 的因子类型
  includePastRuns: boolean;  // 证据包中是否包含以往运行的假设与采纳结果
};

const DEFAULT: Draft = {
  mandate: "demo_crossborder_hk",
  markets: ["hk", "uk", "fr"],
  horizon: 20,
  hypothesis: "",
  direction: "",
  groups: [],
  includePastRuns: true,
};

type Ctx = {
  draft: Draft;
  update: (patch: Partial<Draft>) => void;
  result: DraftResult | null;          // AI 起草结果（研究立项页 → 审核页）
  setResult: (r: DraftResult | null) => void;
  locked: Spec | null;                 // 审核页锁定的版本
  setLocked: (s: Spec | null) => void;
};
const DraftCtx = createContext<Ctx | null>(null);

export function ResearchDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState<Draft>(DEFAULT);
  const [result, setResult] = useState<DraftResult | null>(null);
  const [locked, setLocked] = useState<Spec | null>(null);
  // 首次打开时，用研究规格示例（configs/research）预填假设，方便演示
  useEffect(() => {
    api.researchExamples().then((xs) => {
      const ex = xs[0];
      if (ex) setDraft((d) => (d.hypothesis ? d : { ...d, hypothesis: ex.hypothesis, direction: ex.direction }));
    }).catch(() => undefined);
  }, []);
  return (
    <DraftCtx.Provider value={{ draft, update: (p) => setDraft((d) => ({ ...d, ...p })), result, setResult, locked, setLocked }}>
      {children}
    </DraftCtx.Provider>
  );
}

export function useDraft() {
  const c = useContext(DraftCtx);
  if (!c) throw new Error("useDraft 须在 ResearchDraftProvider 内使用");
  return c;
}

/** 研究员输入中出现测试期（2022 年起）的年份时提示确认（MINE-11、RS-06）。 */
export function testPeriodMentions(text: string): string[] {
  return Array.from(new Set(text.match(/20(2[2-9]|[3-9]\d)\s*年?/g) ?? []));
}
