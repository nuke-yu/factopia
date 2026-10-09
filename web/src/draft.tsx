import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type Market } from "./api";

// 研究立项页填写、审核页确认的研究规格草稿（Spec 第 16.4 节）。锁定与存储将在接入后端后实现。
export type Draft = {
  mandate: string;
  markets: Market[];
  horizon: 1 | 5 | 20 | 60;
  hypothesis: string;
  direction: string;
  groups: string[];          // 证据包中提供给 AI 的因子类型
  includePastRuns: boolean;  // 证据包中是否包含以往运行的假设与采纳结果
  constraints: string[];
};

const DEFAULT: Draft = {
  mandate: "demo_crossborder_hk",
  markets: ["hk", "uk", "fr"],
  horizon: 20,
  hypothesis: "",
  direction: "",
  groups: [],
  includePastRuns: true,
  constraints: [],
};

const Ctx = createContext<{ draft: Draft; update: (patch: Partial<Draft>) => void } | null>(null);

export function ResearchDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState<Draft>(DEFAULT);
  // 首次打开任一页面时，用研究规格示例（configs/research）预填
  useEffect(() => {
    api.researchExamples().then((xs) => {
      const ex = xs[0];
      if (ex) setDraft((d) => (d.hypothesis ? d : { ...d, hypothesis: ex.hypothesis, direction: ex.direction, constraints: ex.constraints ?? [] }));
    }).catch(() => undefined);
  }, []);
  return <Ctx.Provider value={{ draft, update: (p) => setDraft((d) => ({ ...d, ...p })) }}>{children}</Ctx.Provider>;
}

export function useDraft() {
  const c = useContext(Ctx);
  if (!c) throw new Error("useDraft 须在 ResearchDraftProvider 内使用");
  return c;
}

export const MANDATES = [
  {
    id: "demo_crossborder_hk",
    name: "模拟跨境授权 · 香港服务",
    terms: [
      ["客户服务地区", "香港（香港、伦敦、巴黎团队共同支持）"],
      ["目标投资市场", "香港、英国、法国"],
      ["基础 / 报告币种", "HKD / HKD"],
      ["投资基准", "恒生指数、富时 100、CAC 40 复合基准"],
      ["是否允许卖空", "不允许（只做多）"],
      ["受限证券", "无（示例）"],
      ["风险预算", "跟踪误差 ≤ 6%（组合层使用）"],
      ["报告语言", "中文主报告，英文 / 法文附件"],
    ] as [string, string][],
  },
];

/** 研究员输入中出现测试期（2022 年起）的年份时提示确认（MINE-11、RS-06）。 */
export function testPeriodMentions(text: string): string[] {
  return Array.from(new Set(text.match(/20(2[2-9]|[3-9]\d)\s*年?/g) ?? []));
}
