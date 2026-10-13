import { Fragment, useState } from "react";
import { Badge, Panel } from "./ui";
import type { IdeaSuggestion } from "../api";
import { MARKET_LABEL } from "../lib";

// AI 建议的生成依据（Spec 第 17.11 节）：用了哪些数据、明确不用哪些、19 个方向的逐维度打分、发给 AI 的全部证据与完整提示词

const pts = (p: number) => (p === 0 ? "0" : p > 0 ? `+${p}` : `${p}`);
const tone = (p: number) => (p > 0 ? "text-good" : p < 0 ? "text-bad" : "text-faint");

export default function SuggestionBasis({ s }: { s: IdeaSuggestion }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!s.table || !s.inputs || !s.dimensions) return null;
  const cited = (s.evidence ?? []).filter((e) => e.cited).length;
  const sel = s.table.find((r) => r.selected);
  return (
    <section id="suggestion-basis">
      <Panel eyebrow={`${s.id} · ${s.model} · 提示词 ${s.prompt_version}`} title="AI 建议的生成依据">
        <div className="flex flex-col gap-6 text-sm">
          <p className="text-muted max-w-[90ch]">
            {s.seed
              ? <>本条以研究员的想法「{s.seed}」为主线生成；下方打分表照常计算，只供参考，没有用来选方向。</>
              : <>平台先给下方 {s.table.length} 个方向逐项打分，再从得分最高的 {s.candidates?.length ?? 4} 个里按得分加权抽取。本次抽中「{s.angle}」（侧重{s.focus_market ? MARKET_LABEL[s.focus_market] : "全部市场"}，{sel?.chance != null ? `抽中概率 ${Math.round(sel.chance * 100)}%` : ""}）。AI 拿到 {s.evidence?.length ?? 0} 条证据，引用了 {cited} 条。</>}
          </p>

          <div>
            <h3 className="label mb-2">一 · 用了哪些数据（{s.inputs.used.length} 个维度）</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-xs border-collapse min-w-[48rem]">
                <thead><tr className="text-left text-faint border-b border-line"><th className="py-1.5 pr-3">数据</th><th className="pr-3">来源</th><th className="pr-3 text-right">数量</th><th>怎么用</th></tr></thead>
                <tbody>
                  {s.inputs.used.map((u) => (
                    <tr key={u.name} className="border-b border-line/60 align-top">
                      <td className="py-1.5 pr-3 whitespace-nowrap text-text">{u.name}</td>
                      <td className="pr-3 text-muted">{u.source}</td>
                      <td className="pr-3 text-right num">{u.count}</td>
                      <td className="text-muted">{u.use}
                        {u.items.length > 0 && <details className="mt-1"><summary className="cursor-pointer text-faint">逐条查看（{u.items.length}）</summary>
                          <ul className="list-disc pl-4 mt-1">{u.items.map((x, i) => <li key={i}>{x}</li>)}</ul></details>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-faint mt-1">避开清单共 {s.inputs.avoid_n} 个标题（以往研究、已有线索、最近的建议、本次页面已看过的 {s.inputs.session_avoid_n} 条），AI 的建议必须与它们明显不同；生成后按词语重合度查重，超过 0.45 重试一次。</p>
            <h4 className="text-xs text-faint mt-3 mb-1">明确不用的数据</h4>
            <ul className="text-xs flex flex-col gap-0.5">{s.inputs.not_used.map((n) => <li key={n.name}><span className="text-text">{n.name}</span><span className="text-muted">：{n.why}</span></li>)}</ul>
          </div>

          <div>
            <h3 className="label mb-2">二 · 方向打分表（{s.table.length} 个方向 × {s.dimensions.length} 个维度）</h3>
            <ul className="text-xs text-muted grid gap-x-6 gap-y-0.5 sm:grid-cols-2 mb-2">
              {s.dimensions.map((d) => <li key={d.key}><span className="text-text">{d.label}</span>：{d.rule}</li>)}
            </ul>
            <p className="text-xs text-faint mb-2">每个方向在每个市场分别计分，取最高的市场作为侧重；方向按关键词粗略归类以往研究、轮次、线索与排除方向。点一行看每一项的计分原因。</p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs border-collapse min-w-[64rem]">
                <thead>
                  <tr className="text-faint border-b border-line">
                    <th className="text-left py-1.5 pr-2">方向</th><th className="text-left pr-2">侧重</th>
                    {s.dimensions.map((d) => <th key={d.key} className="px-1 font-normal text-right">{d.label}</th>)}
                    <th className="px-2 text-right">总分</th>
                    <th className="px-1 font-normal text-right">港 / 英 / 法</th><th className="pl-2 font-normal text-right">抽中概率</th>
                  </tr>
                </thead>
                <tbody>
                  {s.table.map((r) => (
                    <Fragment key={r.angle}>
                      <tr onClick={() => setOpen(open === r.angle ? null : r.angle)}
                        className={`border-b border-line/60 cursor-pointer hover:bg-accent/5 ${r.selected ? "bg-accent/10" : ""} ${r.top ? "" : "text-muted"}`}>
                        <td className="py-1.5 pr-2">
                          <span className={r.selected ? "text-accent font-medium" : r.top ? "text-text" : ""}>{r.angle}</span>
                          {r.selected && <span className="ml-1"><Badge tone="accent">本次</Badge></span>}
                          {r.skipped && <span className="ml-1"><Badge>"换一个"跳过</Badge></span>}
                        </td>
                        <td className="pr-2 whitespace-nowrap">{MARKET_LABEL[r.market]}</td>
                        {s.dimensions!.map((d) => <td key={d.key} className={`px-1 text-right num ${tone(r.detail[d.key]?.points ?? 0)}`} title={r.detail[d.key]?.note}>{pts(r.detail[d.key]?.points ?? 0)}</td>)}
                        <td className="px-2 text-right num font-medium text-text">{r.score}</td>
                        <td className="px-1 text-right num whitespace-nowrap">{(["hk", "uk", "fr"] as const).map((m) => r.by_market[m] ?? "–").join(" / ")}</td>
                        <td className="pl-2 text-right num">{r.chance != null ? `${Math.round(r.chance * 100)}%` : r.top && s.seed ? "未抽取" : "—"}</td>
                      </tr>
                      {open === r.angle && (
                        <tr className="border-b border-line/60 bg-accent/5"><td colSpan={s.dimensions!.length + 5} className="p-2">
                          <ul className="grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
                            {s.dimensions!.map((d) => <li key={d.key}><span className={`num ${tone(r.detail[d.key]?.points ?? 0)}`}>{pts(r.detail[d.key]?.points ?? 0)}</span> <span className="text-text">{d.label}</span>：<span className="text-muted">{r.detail[d.key]?.note}</span></li>)}
                          </ul>
                          <p className="text-faint mt-1">归类结果（全部市场）：以往研究 {r.counts.studies} 个，相关轮次 {r.counts.rounds} 轮（采纳 {r.counts.adopted}），排除方向 {r.counts.exclusions} 条，相关线索 {r.counts.ideas} 条。Alpha158 分组：{r.groups.join("、") || "无"}。关键词：<code>{r.keywords}</code></p>
                        </td></tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div>
            <h3 className="label mb-2">三 · 发给 AI 的证据（共 {s.evidence?.length ?? 0} 条，AI 引用 {cited} 条）</h3>
            <ul className="text-xs flex flex-col gap-1">
              {(s.evidence ?? []).map((e) => (
                <li key={e.id} className="flex gap-2 items-start">
                  <span className="num text-faint w-8 shrink-0">{e.id}</span>
                  <span className="shrink-0"><Badge tone={e.cited ? "accent" : "muted"}>{e.cited ? "已引用" : "未引用"}</Badge></span>
                  <span className="text-faint shrink-0 w-20">{e.kind}</span>
                  <span className={e.cited ? "text-text" : "text-muted"}>{e.zh}</span>
                </li>
              ))}
            </ul>
          </div>

          {s.prompt && <div>
            <h3 className="label mb-2">四 · 发给 AI 的完整提示词</h3>
            <details><summary className="cursor-pointer text-xs text-faint">展开原文（英文，{s.prompt.length.toLocaleString()} 字符；这就是本次请求的全部输入）</summary>
              <pre className="mt-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words bg-bg/60 border border-line rounded p-3 max-h-[32rem] overflow-y-auto">{s.prompt}</pre>
            </details>
          </div>}
        </div>
      </Panel>
    </section>
  );
}
