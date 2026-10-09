import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Market } from "../api";
import { MANDATES, testPeriodMentions, useDraft } from "../draft";
import { cssVar, MARKET_LABEL, useAsync } from "../lib";
import { Badge, Notice, Panel, Seg } from "../components/ui";

type Source = "研究员" | "AI 起草" | "系统";
type Field = { key: string; name: string; source: Source; required?: boolean; value: string };

// 示例草稿：大模型起草接口接入前，用于展示审核流程（RS-02 接入后由后端返回）
const EXAMPLE_AI: Record<string, string> = {
  hypothesis: "In Hong Kong, medium-term (about one month) price signals become more stable when adjusted for the stock's own volatility or confirmed by volume, because market-wide swings and sector concentration contaminate raw momentum and reversal.",
  rationale: "Behavioural: investors under-react to persistent stock-specific news, while market-wide shocks add noise; scaling by volatility separates the two.",
  directions: "1. 20-day return scaled by 60-day realised volatility\n2. Momentum confirmed by rising volume\n3. Return relative to the Hang Seng Index",
  exclusions: "Plain medium-term reversal (not significant in Hong Kong over 2015–2021, ROC20 t = -0.66)",
  falsification: "The hypothesis is rejected if the adopted factors do not show a significantly positive rank IC (t < 2) in Hong Kong over the test period, or if after-cost excess return is not above the 20-factor baseline.",
  data: "Daily prices and volume: available in all three markets.\nBenchmark index: available.\nNo text data required.",
};

export default function AuditPage() {
  const { draft } = useDraft();
  const mandate = MANDATES.find((m) => m.id === draft.mandate)!;
  const initial: Field[] = useMemo(() => [
    { key: "original", name: "原始假设", source: "研究员", value: draft.hypothesis },
    { key: "hypothesis", name: "规范化假设", source: "AI 起草", value: EXAMPLE_AI.hypothesis },
    { key: "rationale", name: "经济逻辑", source: "AI 起草", required: true, value: EXAMPLE_AI.rationale },
    { key: "directions", name: "候选特征方向", source: "AI 起草", value: EXAMPLE_AI.directions },
    { key: "exclusions", name: "排除方向", source: "AI 起草", value: EXAMPLE_AI.exclusions },
    { key: "falsification", name: "可证伪测试", source: "AI 起草", required: true, value: EXAMPLE_AI.falsification },
    { key: "data", name: "数据需求与缺口", source: "系统", value: EXAMPLE_AI.data },
    { key: "constraints", name: "约束", source: "系统", value: draft.constraints.join("\n") },
  ], [draft]);
  const [fields, setFields] = useState(initial);
  const [locked, setLocked] = useState(false);
  useEffect(() => { if (!locked) setFields(initial); }, [initial]); // eslint-disable-line react-hooks/exhaustive-deps
  const mentions = testPeriodMentions(fields.map((f) => f.value).join(" "));

  const confirmations = [
    { id: "long", text: `授权约束"只做多"适用于所选的 ${draft.markets.length} 个市场` },
    { id: "excl", text: "排除方向的依据只来自 2015–2021 年的数据" },
    { id: "falsi", text: "可证伪测试的门槛在运行前确定，运行后不再修改" },
    ...(mentions.length ? [{ id: "leak", text: `保留输入中的测试期年份（${mentions.join("、")}），并已知悉其泄漏风险` }] : []),
  ];
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const missing = fields.filter((f) => f.required && !f.value.trim()).map((f) => f.name);
  const pending = confirmations.filter((c) => !checked[c.id]).length;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">02 · 审核</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">审核研究规格</h1>
          <p className="text-muted mt-1 max-w-[70ch]">逐条检查 AI 起草的内容，修改处会与原稿对比留痕；处理完待确认事项后锁定版本，再在所选市场运行。</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Badge tone={locked ? "good" : "warn"}>{locked ? "已锁定 v1" : "草稿 v1 · 未锁定"}</Badge>
          <Badge>{mandate.name}</Badge>
        </div>
      </div>
      <Notice tone="accent" title="当前为示例草稿">大模型起草（RS-02）、版本存储（RS-08）与运行接口（RS-09）将在下一阶段接入；本页先完成审核流程与界面。</Notice>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Panel eyebrow="研究规格" title="逐条审阅">
          <div className="flex flex-col divide-y divide-line">
            {fields.map((f, i) => {
              const edited = f.value !== initial[i].value;
              return (
                <div key={f.key} className="py-3 grid gap-2 md:grid-cols-[150px_1fr]">
                  <div className="flex md:flex-col gap-2 md:gap-1">
                    <div className="font-medium">{f.name}{f.required && <span className="text-bad"> *</span>}</div>
                    <div className="flex gap-1.5 flex-wrap">
                      <Badge tone={f.source === "AI 起草" ? "accent" : "muted"}>{f.source}</Badge>
                      {edited && <Badge tone="warn">已修改</Badge>}
                    </div>
                  </div>
                  <div>
                    <textarea aria-label={f.name} className="field leading-relaxed text-sm" rows={Math.max(2, f.value.split("\n").length + Math.ceil(f.value.length / 140))} disabled={locked || f.source === "系统"}
                      value={f.value} onChange={(e) => setFields((fs) => fs.map((x) => (x.key === f.key ? { ...x, value: e.target.value } : x)))} />
                    {edited && (
                      <details className="mt-1 text-xs">
                        <summary className="text-muted cursor-pointer">查看 AI 原稿</summary>
                        <p className="text-faint mt-1 whitespace-pre-wrap line-through decoration-bad/60">{initial[i].value}</p>
                      </details>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>

        <aside className="flex flex-col gap-5 xl:sticky xl:top-20 self-start">
          <Panel eyebrow="锁定前" title="待确认事项">
            <ul className="flex flex-col gap-2">
              {confirmations.map((c) => (
                <li key={c.id}>
                  <label className="flex gap-2 text-sm leading-snug">
                    <input type="checkbox" disabled={locked} checked={!!checked[c.id]} onChange={(e) => setChecked((x) => ({ ...x, [c.id]: e.target.checked }))} className="mt-0.5" />
                    <span>{c.text}</span>
                  </label>
                </li>
              ))}
            </ul>
          </Panel>
          <Panel eyebrow="MINE-11" title="防泄漏检查">
            <ul className="flex flex-col gap-2 text-sm">
              <Check ok text="证据包只用 2015-01 至 2021-12 的数据" />
              <Check ok text="市场介绍不含因子评估结论与测试期事件" />
              <Check ok={mentions.length === 0} text={mentions.length ? `规格中出现测试期年份：${mentions.join("、")}` : "规格中没有测试期内的年份"} />
              <Check ok={false} warnOnly text="大模型自身可能记得测试期的行情，无法消除（第 8 章 #20）" />
            </ul>
          </Panel>
          <div className="flex flex-col gap-2">
            {missing.length > 0 && <div className="text-xs text-bad">必填字段为空：{missing.join("、")}</div>}
            {pending > 0 && <div className="text-xs text-warn">还有 {pending} 项待确认</div>}
            <button className="btn btn-primary h-10 justify-center" disabled={locked || missing.length > 0 || pending > 0} onClick={() => setLocked(true)}>
              锁定版本并运行
            </button>
            {locked && (
              <Notice tone="accent" title="已锁定（本地演示，尚未保存）">
                运行接口接入后，将在{draft.markets.map((m) => MARKET_LABEL[m]).join("、")}分别启动挖掘。现在可以 <Link to="/results" className="text-accent underline">查看已有运行的结果</Link>。
              </Notice>
            )}
          </div>
        </aside>
      </div>

      <Panel eyebrow="RS-07 · 所见即所发" title="提示词预览">
        <PromptPreview markets={draft.markets} fields={fields} />
      </Panel>
    </div>
  );
}

function Check({ ok, warnOnly, text }: { ok: boolean; warnOnly?: boolean; text: string }) {
  const tone = ok ? "--good" : warnOnly ? "--warn" : "--bad";
  return (
    <li className="flex gap-2 leading-snug">
      <span className="num shrink-0" style={{ color: cssVar(tone) }}>{ok ? "通过" : warnOnly ? "提示" : "注意"}</span>
      <span className={ok ? "text-muted" : ""}>{text}</span>
    </li>
  );
}

function PromptPreview({ markets, fields }: { markets: Market[]; fields: Field[] }) {
  const [market, setMarket] = useState<Market>(markets[0] ?? "hk");
  const [lang, setLang] = useState<"en" | "zh">("en");
  const ctx = useAsync(() => api.context(market, lang), [market, lang]);
  const get = (k: string) => fields.find((f) => f.key === k)?.value ?? "";
  const blocks: { src: string; text: string }[] = [
    { src: "市场介绍（团队审核）", text: ctx.data?.markdown ?? "载入中…" },
    { src: "研究规格 · 规范化假设", text: get("hypothesis") },
    { src: "研究规格 · 经济逻辑", text: get("rationale") },
    { src: "研究规格 · 候选方向 / 排除方向", text: `${get("directions")}\n\nAvoid: ${get("exclusions")}` },
    { src: "研究规格 · 约束", text: get("constraints") },
    { src: "证据包（系统生成，2015-01 至 2021-12）", text: "由后端按测试期开始日计算后插入（scenario.py 已有发现部分）" },
  ];
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2 items-center">
        <Seg label="市场" value={market} onChange={setMarket} options={markets.map((m) => ({ value: m, label: MARKET_LABEL[m] }))} />
        <Seg label="语言" value={lang} onChange={setLang} options={[{ value: "en", label: "英文（实际注入）" }, { value: "zh", label: "中文母版" }]} />
      </div>
      <div className="flex flex-col gap-2">
        {blocks.map((b) => (
          <div key={b.src} className="grid md:grid-cols-[200px_1fr] gap-2 border-l-2 pl-3" style={{ borderColor: cssVar(`--${market}`) }}>
            <div className="label pt-1">{b.src}</div>
            <pre className="num text-xs whitespace-pre-wrap leading-relaxed text-muted max-h-64 overflow-y-auto">{b.text}</pre>
          </div>
        ))}
      </div>
      <p className="text-xs text-faint">完整预览（含已有发现与数据说明）将调用实际运行时同一段代码生成，保证与发给 AI 的内容一致（Spec 第 16.7 节）。</p>
    </div>
  );
}
