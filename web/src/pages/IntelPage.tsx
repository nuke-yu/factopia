import { Fragment, useEffect, useMemo, useState } from "react";
import EChart from "../components/EChart";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import {
  api, type GlossaryCandidate, type ReviewAccuracy, type ReviewItem, type ReviewTier, type TextCompareRow, type TextConcept, type TextDataset, type TextDoc, type TextFiling,
  type TextHit, type TextOverview, type TextSummary,
} from "../api";
import { cssVar, fix, MARKET_LABEL, marketColor, SHOW_FORMULAS, signed, useAsync, useThemeVersion } from "../lib";

// 情报台（Spec 第 11 章；BR-08 至 12、15、49）：多语言公告 → 概念标签 / 译文 / 置信度 → 复核 → 文本信号 → 三市场检验

const SIM = "模拟数据，仅用于流程演示";
const LANG: Record<string, string> = { zh: "中文", en: "英文", fr: "法文" };
const REVIEW_ZH: Record<string, string> = {
  pending: "待复核", not_required: "无需复核", confirm: "已确认", correct: "已修正", reject: "判定不涉及风险",
};
type Tab = "filings" | "search" | "review" | "glossary";
const TIER_TONE: Record<ReviewTier, "bad" | "accent" | "warn" | "muted"> = { must: "bad", sample: "accent", impact: "warn", other: "muted" };
const TIER_HINT: Record<ReviewTier, string> = {
  must: "低置信度翻译。业务文档要求人工复核；未复核时，使用该数据集的研究不能提交审批。",
  sample: "按固定种子随机抽取，用来估计 AI 判断的准确率：抽到的不论是否触发复核都要看，看完全部样本才有代表性。另有一部分是术语表没命中、没送 AI 的段落，用来估计漏检率。",
  impact: "这段的判断一旦相反，所在公告的「风险变化」就会变号，对信号影响最大。",
  other: "触发了复核条件（否定句、低置信度、监管措辞、财务术语等），但不在以上三组。有空再看，不看不影响信号与提交审批。",
};
const pctOrDash = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);

const time = (iso: string | null | undefined) => (iso ? iso.replace("T", " ").replace(/(:\d\d)(\+00:00|Z)$/, "$1 UTC") : "—");

function SimBadge({ simulated }: { simulated: boolean }) {
  return simulated ? <Badge tone="warn">{SIM}</Badge> : <Badge tone="good">真实公告</Badge>;
}

function ConceptChips({ concepts }: { concepts: TextConcept[] }) {
  if (!concepts.length) return <span className="text-xs text-faint">不涉及四类风险</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {concepts.map((c, k) => (
        <Badge key={k} tone={c.direction > 0 ? "bad" : "good"}>
          {c.name ?? c.concept} {c.direction > 0 ? "↑加重" : "↓缓解"}{c.negated ? " · 否定句" : ""} · {fix(c.confidence, 2)}
        </Badge>
      ))}
    </span>
  );
}

/** 一条引用：来源、段落、三个时间。 */
function Cite({ ds, simulated, title, company, paragraph, published, available, effective, url }: {
  ds: string; simulated: boolean; title: string; company: string; paragraph: number; published: string; available: string;
  effective: string | null; url?: string | null;
}) {
  return (
    <div className="text-xs text-faint flex flex-wrap gap-x-3 gap-y-0.5">
      <span>{company} · {title} · 第 {paragraph + 1} 段</span>
      <span>正式发布 <span className="num">{time(published)}</span></span>
      <span>实际可获取 <span className="num">{time(available)}</span></span>
      <span>生效交易日 <span className="num">{effective ?? "—"}</span></span>
      {url && <a className="text-accent underline" href={url} target="_blank" rel="noreferrer">原始 PDF</a>}
      {simulated && <span className="text-warn">{SIM}</span>}
      <span className="sr-only">{ds}</span>
    </div>
  );
}

function SourceCard({ d }: { d: TextDataset }) {
  const e = d.evaluation;
  return (
    <div className="panel p-4 flex flex-col gap-3" style={{ borderTop: `2px solid ${marketColor(d.market)}` }}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-display font-semibold">{d.market_zh}</span>
        <SimBadge simulated={d.simulated} />
      </div>
      <div className="text-xs text-muted">来源：{d.source} · 许可：{d.licence}</div>
      {!d.ready ? <div className="text-sm text-faint">尚未生成（python -m factopia.text all）</div> : (
        <div className="grid grid-cols-3 gap-3">
          <Stat label="公告" value={d.n_filings} sub={`${d.n_companies} 家公司`} />
          <Stat label="逐日 Rank IC" value={fix(e?.rank_ic_mean, 3)} sub={`t = ${fix(e?.rank_ic_t, 2)}`} />
          <Stat label="公告后事件" value={fix(e?.events.diff_t, 2)} sub="悲观 − 乐观 的 t 值" />
        </div>
      )}
      {d.truth && <div className="text-xs text-warn">植入规律：{d.truth.rule}；预期：{d.truth.expected}</div>}
      <div className="text-xs text-faint">处理延迟 {d.processing_delay_min} 分钟计入可获取时间；分数沿用 {d.hold_days ?? "—"} 个交易日</div>
    </div>
  );
}

// 敏感性检验（第 11.10 节）：处理延迟 × 信号沿用期，结论是否稳定
function Sensitivity({ datasets }: { datasets: TextDataset[] }) {
  const ds = datasets.filter((d) => d.sensitivity?.rows.length);
  if (!ds.length) return null;
  return (
    <div className="mt-4">
      <div className="label mb-1">敏感性检验：处理延迟（分钟）× 信号沿用期（交易日）</div>
      <div className="grid gap-4 lg:grid-cols-3">
        {ds.map((d) => {
          const s = d.sensitivity!;
          const delays = [...new Set(s.rows.map((r) => r.delay_min))], holds = [...new Set(s.rows.map((r) => r.hold_days))];
          return (
            <div key={d.dataset} className="border border-line rounded p-2 overflow-x-auto">
              <div className="flex items-center gap-2 text-sm mb-1">{d.market_zh}<SimBadge simulated={d.simulated} />
                <Badge tone={s.stable ? "good" : "warn"}>{s.stable ? "结论稳定" : "结论随参数变化"}</Badge></div>
              <table className="w-full text-xs num">
                <thead className="text-faint"><tr><th className="text-left font-normal">延迟 \ 沿用</th>{holds.map((h) => <th key={h} className="text-right font-normal">{h} 天</th>)}</tr></thead>
                <tbody>{delays.map((dl) => (
                  <tr key={dl} className="border-t border-line"><td>{dl} 分钟</td>
                    {holds.map((h) => {
                      const r = s.rows.find((x) => x.delay_min === dl && x.hold_days === h);
                      const ok = r?.verdict.startsWith("有效");
                      return <td key={h} className={`text-right ${r?.baseline ? "font-semibold" : ""}`} title={r?.verdict}>
                        <span style={{ color: `var(--${ok ? "good" : "muted"})` }}>{fix(r?.rank_ic_t, 2)}</span></td>;
                    })}</tr>))}</tbody>
              </table>
              <div className="text-[11px] text-faint mt-1">数字为 Rank IC 的 t 值；绿色 = 判定有效；粗体 = 当前参数（60 分钟、63 天）</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// 开发阶段公式说明（SHOW_FORMULAS）：每个市场一行，代入该市场的实际数值；口径与 factopia/text/signal.py 的 evaluate / judge 一致
function verdictWhy(r: TextCompareRow): string {
  const t = r.rank_ic_t, et = r.event_diff_t ?? null;
  if (!r.n_days || t == null) return `有效交易日 = ${r.n_days}，主口径无法计算 → 数据不足`;
  const ev = et == null ? "事件 t 未计算" : et < -2 ? `事件 t = ${fix(et, 2)} < −2（同向显著）` : et > 2 ? `事件 t = ${fix(et, 2)} > 2（反向显著）` : `事件 t = ${fix(et, 2)}（不显著）`;
  const main = t < -2 ? `主口径 t = ${fix(t, 2)} < −2` : t > 2 ? `主口径 t = ${fix(t, 2)} > 2` : `主口径 −2 ≤ t = ${fix(t, 2)} ≤ 2`;
  return `${main}；${ev} → ${r.verdict}`;
}

function FormulaRow({ r, horizon }: { r: TextCompareRow; horizon: number }) {
  const ic = r.rank_ic_mean, t = r.rank_ic_t;
  const p = r.event_pessimistic, o = r.event_optimistic, et = r.event_diff_t ?? null;
  const noDays = !r.n_days || ic == null;
  const items: [string, string][] = [
    ["公告", `${r.n_filings} 篇 = ${r.n_companies} 家公司在样本期内的公告数（filings.parquet 行数）`],
    ["Rank IC", noDays ? "有效交易日 = 0（每天有信号的股票都少于 5 只）→ 无法计算"
      : `IC_d = Spearman(当天信号, 当天起未来 ${horizon} 日收益)，只算 ≥ 5 只股票有信号的交易日；Rank IC = 平均(IC_d) = ${fix(ic, 3)}（共 ${r.n_days} 个交易日）`],
    ["t 值", noDays || t == null ? "无法计算"
      : `t = 平均(IC_d) ÷ Newey-West 标准误（滞后 ${horizon} 日）= ${fix(ic, 4)} ÷ ${fix(ic / t, 4)} = ${fix(t, 2)}`],
    ["三分组收益差", r.tercile_spread_mean == null ? "无法计算（同上）"
      : `每天按信号排序分三组：差_d = 信号最高 1/3 的平均收益 − 最低 1/3 的平均收益；平均(差_d) = ${signed(r.tercile_spread_mean, 2)}`],
    ["悲观 / 乐观组超额", `超额 = 个股生效日起 ${horizon} 日收益 − 同市场等权平均；悲观组 = 风险变化 > 0 的 ${p?.n ?? 0} 次公告，平均 ${signed(p?.mean_excess, 2)}；`
      + `乐观组 = 风险变化 < 0 的 ${o?.n ?? 0} 次公告，平均 ${signed(o?.mean_excess, 2)}`],
    ["事件 t 值", et == null || p?.mean_excess == null || o?.mean_excess == null
      ? `事件数不足（总数需 ≥ 10、每组需 > 2；目前悲观 ${p?.n ?? 0}、乐观 ${o?.n ?? 0}）→ 未计算`
      : `t = (悲观均值 − 乐观均值) ÷ √(s²悲/n悲 + s²乐/n乐) = (${signed(p.mean_excess, 2)} − ${signed(o.mean_excess, 2)}) ÷ ${signed((p.mean_excess - o.mean_excess) / et, 2).replace("+", "")} = ${fix(et, 2)}（Welch）`],
    ["结论", verdictWhy(r)],
  ];
  return (
    <tr>
      <td colSpan={10} className="pb-2 pl-3 font-sans">
        <div className="border-l-2 border-line pl-3 grid gap-0.5 text-xs text-muted" style={{ gridTemplateColumns: "max-content 1fr" }}>
          {items.map(([k, v]) => <Fragment key={k}><span className="text-faint pr-3">{k}</span><span className="num">{v}</span></Fragment>)}
        </div>
      </td>
    </tr>
  );
}

function FormulaDefs({ horizon, datasets }: { horizon: number; datasets: TextDataset[] }) {
  const hold = datasets.find((d) => d.hold_days)?.hold_days ?? 63;
  const delay = datasets.find((d) => d.processing_delay_min)?.processing_delay_min ?? 60;
  return (
    <div className="mt-2 text-xs text-muted border border-dashed border-line rounded p-2 grid gap-0.5">
      <div className="text-faint">开发阶段公式说明（进入 MVP 时隐藏）· 各市场下方灰色行为代入实际数值的计算过程</div>
      <div>风险语气 = Σ(方向 × 置信度) ÷ 公告段落数 × 100（方向：加重 +1 / 缓解 −1；只计入复核后生效的标注）</div>
      <div>信号 = 本篇风险语气 − 同一系列上一篇（同公司、同类型、同语言）的风险语气；从生效日起沿用，最长 {hold} 个交易日</div>
      <div>生效日 = 发布时间 + {delay} 分钟（处理延迟）之后的第一个收盘所在交易日</div>
      <div>未来收益_d = 收盘价(d + 1 + {horizon}) ÷ 收盘价(d + 1) − 1（从信号日的下一个交易日起算 {horizon} 个交易日）</div>
    </div>
  );
}

function CompareTable({ rows, note, horizon, rule, datasets }: { rows: TextCompareRow[]; note: string; horizon: number; rule?: string; datasets: TextDataset[] }) {
  return (
    <Panel title="三个市场分别检验" eyebrow={`演示主线 · 管理层风险表述变化 → 未来 ${horizon} 个交易日相对收益`}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left">
            <tr><th className="py-1 pr-3">市场</th><th className="pr-3">数据</th><th className="pr-3 text-right">公告</th>
              <th className="pr-3 text-right">Rank IC</th><th className="pr-3 text-right">t 值</th><th className="pr-3 text-right">三分组收益差</th>
              <th className="pr-3 text-right">悲观组超额</th><th className="pr-3 text-right">乐观组超额</th><th className="pr-3 text-right">事件 t 值</th><th>结论</th></tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => [
              <tr key={r.dataset} className="border-t border-line">
                <td className="py-1.5 pr-3 font-sans">{MARKET_LABEL[r.market]}</td>
                <td className="pr-3 font-sans">{r.simulated ? <Badge tone="warn">模拟</Badge> : <Badge tone="good">真实</Badge>}</td>
                <td className="pr-3 text-right">{r.n_filings}</td>
                <td className="pr-3 text-right">{fix(r.rank_ic_mean, 3)}</td>
                <td className="pr-3 text-right">{fix(r.rank_ic_t, 2)}</td>
                <td className="pr-3 text-right">{signed(r.tercile_spread_mean, 2)}</td>
                <td className="pr-3 text-right">{signed(r.event_pessimistic?.mean_excess, 2)}</td>
                <td className="pr-3 text-right">{signed(r.event_optimistic?.mean_excess, 2)}</td>
                <td className="pr-3 text-right">{fix(r.event_diff_t, 2)}</td>
                <td className="font-sans">{r.verdict}</td>
              </tr>,
              SHOW_FORMULAS && <FormulaRow key={r.dataset + "-f"} r={r} horizon={horizon} />,
            ])}
          </tbody>
        </table>
      </div>
      {SHOW_FORMULAS && <FormulaDefs horizon={horizon} datasets={datasets} />}
      <p className="text-xs text-faint mt-2">
        假设方向：风险表述越悲观，未来收益越低（Rank IC 与事件 t 值为负）。t 值绝对值大于 2 视为显著。{note}
      </p>
      {rule && <p className="text-xs text-muted mt-1">判定规则：{rule}</p>}
      <Sensitivity datasets={datasets} />
    </Panel>
  );
}

function IcChart({ monthly }: { monthly: Record<string, number> }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const keys = Object.keys(monthly), muted = cssVar("--muted"), line = cssVar("--line-2");
    return {
      grid: { left: 44, right: 12, top: 16, bottom: 28 },
      tooltip: { trigger: "axis", valueFormatter: (v: number) => v.toFixed(3) },
      xAxis: { type: "category", data: keys, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", axisLabel: { color: muted }, splitLine: { lineStyle: { color: line, opacity: 0.4 } } },
      series: [{ type: "bar", data: keys.map((k) => ({ value: monthly[k], itemStyle: { color: monthly[k] < 0 ? cssVar("--good") : cssVar("--bad") } })) }],
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monthly, theme]);
  return <EChart option={option} notMerge className="h-[200px]" />;
}

function DocView({ ds, id, onClose }: { ds: string; id: string; onClose: () => void }) {
  const { data, error, loading } = useAsync<TextDoc>(() => api.textDoc(ds, id), [ds, id]);
  const [onlyLabelled, setOnlyLabelled] = useState(true);
  if (loading) return <Loading what="公告" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const m = data.meta;
  const paras = data.paragraphs.filter((p) => !onlyLabelled || p.concepts.length || p.flags.length);
  return (
    <Panel title={m?.title ?? id} eyebrow={`${m?.company ?? ""} · ${LANG[m?.language ?? ""] ?? ""}原文`}
      actions={<>
        <Seg label="段落" value={onlyLabelled ? "y" : "n"} onChange={(v) => setOnlyLabelled(v === "y")}
          options={[{ value: "y", label: "只看标注段落" }, { value: "n", label: "全部段落" }]} />
        <button className="btn" onClick={onClose}>关闭</button>
      </>}>
      <div className="flex flex-col gap-2 mb-3">
        <SimBadge simulated={data.simulated} />
        <div className="text-xs text-muted">
          理解模型 <span className="num">{data.model}</span> · 提示词 <span className="num">{data.prompt_version}</span> ·
          术语表 <span className="num">{data.glossary_version}</span> · 标注时间 <span className="num">{time(data.labelled_at)}</span>
          {data.pages_total ? <> · 读取 {data.pages_read}/{data.pages_total} 页</> : null}
        </div>
        {m && <div className="text-xs text-muted">
          业务日期 <span className="num">{m.business_date}</span> · 正式发布 <span className="num">{time(m.published_at)}</span> ·
          实际可获取 <span className="num">{time(m.available_at)}</span> · 生效交易日 <span className="num">{m.effective_date}</span>
          {m.after_close && <span className="text-warn"> · 收盘后发布，顺延到下一交易日</span>}
          {m.url && <> · <a className="text-accent underline" href={m.url} target="_blank" rel="noreferrer">原始 PDF</a></>}
        </div>}
      </div>
      <div className="flex flex-col divide-y divide-line">
        {paras.map((p) => (
          <div key={p.i} className="py-2.5 grid gap-2 md:grid-cols-2">
            <div>
              <div className="label mb-1">第 {p.i + 1} 段 · 原文</div>
              <p className="text-sm leading-relaxed">{p.text}</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="label">机器翻译{p.translation_confidence != null && <> · 置信度 {fix(p.translation_confidence, 2)}</>}</div>
              {Object.keys(p.translation).length ? Object.entries(p.translation).map(([k, v]) => (
                <p key={k} className="text-sm text-muted"><span className="text-faint">{LANG[k]}：</span>{v}</p>
              )) : <p className="text-xs text-faint">{p.method === "lexicon" ? "词典法标注，无译文" : "—"}</p>}
              <ConceptChips concepts={p.concepts} />
              <div className="flex flex-wrap gap-1">
                {p.review_status && <Badge tone={p.review_status === "pending" ? "warn" : "muted"}>{REVIEW_ZH[p.review_status]}</Badge>}
                {p.flags.map((f) => <Badge key={f.code} tone="accent">{f.label}</Badge>)}
              </div>
            </div>
          </div>
        ))}
        {!paras.length && <div className="text-sm text-faint py-3">本篇没有与四类风险相关的段落</div>}
      </div>
    </Panel>
  );
}

function FilingsTab({ ov }: { ov: TextOverview }) {
  const ready = ov.datasets.filter((d) => d.ready);
  const [ds, setDs] = useState(ready[0]?.dataset ?? "fr");
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, loading } = useAsync(() => api.textFilings(ds), [ds]);
  const cur = ov.datasets.find((d) => d.dataset === ds);
  return (
    <div className="flex flex-col gap-4">
      <Seg label="数据集" value={ds} onChange={(v) => { setDs(v); setOpen(null); }}
        options={ov.datasets.map((d) => ({ value: d.dataset, label: `${d.market_zh}${d.simulated ? "（模拟）" : ""}` }))} />
      {cur?.evaluation && Object.keys(cur.evaluation.ic_monthly).length > 0 && (
        <Panel title="逐月 Rank IC" eyebrow="风险表述变化 vs 未来收益 · 绿色 = 方向符合假设（负相关）">
          <IcChart monthly={cur.evaluation.ic_monthly} />
        </Panel>
      )}
      {open && <DocView ds={ds} id={open} onClose={() => setOpen(null)} />}
      {loading ? <Loading what="公告列表" /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={`公告与风险表述打分（${data.total} 篇）`} eyebrow={`${data.source} · ${data.licence}`}>
          <div className="overflow-x-auto max-h-[560px]">
            <table className="w-full text-sm">
              <thead className="label text-left sticky top-0 bg-bg-2">
                <tr><th className="py-1 pr-3">公司</th><th className="pr-3">公告</th><th className="pr-3">语言</th>
                  <th className="pr-3">正式发布</th><th className="pr-3">实际可获取</th><th className="pr-3">生效交易日</th>
                  <th className="pr-3 text-right">风险语气</th><th className="pr-3 text-right">较上期变化</th><th>待复核</th></tr>
              </thead>
              <tbody>
                {data.items.map((f: TextFiling) => (
                  <tr key={f.id} className={`border-t border-line cursor-pointer hover:bg-bg-3 ${open === f.id ? "bg-accent/10" : ""}`}
                    onClick={() => setOpen(f.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(f.id)}>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{f.company}<div className="num text-xs text-faint">{f.instrument}</div></td>
                    <td className="pr-3 max-w-[28ch] truncate" title={f.title}>{f.title}</td>
                    <td className="pr-3">{LANG[f.language] ?? f.language}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{time(f.published_at)}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{time(f.available_at)}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{f.effective_date}{f.after_close && <span className="text-warn"> 顺延</span>}</td>
                    <td className="pr-3 num text-right">{fix(f.tone_total, 2)}</td>
                    <td className="pr-3 num text-right" style={{ color: f.change == null ? undefined : f.change > 0 ? cssVar("--bad") : cssVar("--good") }}>
                      {f.change == null ? "首期" : `${f.change > 0 ? "+" : ""}${f.change.toFixed(2)}`}</td>
                    <td>{f.pending_review ? <Badge tone="warn">{f.pending_review}</Badge> : <span className="text-faint">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-faint mt-2">风险语气 = 每百段的净风险表述（加重为正、缓解或否定为负，按置信度加权，使用复核后的结果）。点击一行查看原文、译文与引用。</p>
        </Panel>
      )}
    </div>
  );
}

function SearchTab({ ov }: { ov: TextOverview }) {
  const [q, setQ] = useState("需求转弱");
  const [concepts, setConcepts] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState({ q: "需求转弱", concepts: [] as string[] });
  const { data, error, loading } = useAsync(() => api.textSearch(submitted.q, [], submitted.concepts), [submitted]);
  const [summary, setSummary] = useState<TextSummary | null>(null);
  const [sumBusy, setSumBusy] = useState(false);
  const [sumErr, setSumErr] = useState<string | null>(null);
  useEffect(() => { setSummary(null); setSumErr(null); }, [submitted]);
  const summarize = async () => {
    setSumBusy(true); setSumErr(null);
    try { setSummary(await api.textSummary(submitted.q, submitted.concepts)); }
    catch (e) { setSumErr(String((e as Error).message ?? e)); } finally { setSumBusy(false); }
  };
  return (
    <div className="flex flex-col gap-4">
      <form className="panel p-4 flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); setSubmitted({ q, concepts }); }}>
        <label className="label" htmlFor="intel-q">用任意语言提问（中文 / English / Français）</label>
        <div className="flex flex-wrap gap-2">
          <input id="intel-q" className="field flex-1 min-w-[16rem]" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="如：demand weakened / la demande s'affaiblit / 成本压力" />
          <button className="btn btn-primary" type="submit">检索</button>
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          <span className="text-xs text-faint">或按概念：</span>
          {ov.concepts.map((c) => (
            <button key={c.concept} type="button" aria-pressed={concepts.includes(c.concept)}
              className={`btn h-7 text-xs ${concepts.includes(c.concept) ? "border-accent text-accent" : ""}`}
              onClick={() => setConcepts((s) => s.includes(c.concept) ? s.filter((x) => x !== c.concept) : [...s, c.concept])}>
              {c.zh}
            </button>
          ))}
        </div>
      </form>
      {data && data.results.length > 0 && (
        <Panel title="AI 摘要" eyebrow="每句结论必须引用检索结果；没有引用的句子会被丢弃"
          actions={<button className="btn btn-primary" disabled={sumBusy || !submitted.q} onClick={summarize}>{sumBusy ? "生成中…" : summary ? "重新生成" : "生成摘要"}</button>}>
          {sumErr && <Notice tone="bad" title="摘要生成失败">{sumErr}</Notice>}
          {!summary && !sumErr && <p className="text-sm text-faint">基于前 12 条检索结果，用中文概括各市场、各公司的相关表述。</p>}
          {summary && (
            <div className="flex flex-col gap-3">
              <ul className="flex flex-col gap-1.5 text-sm list-disc pl-5">
                {summary.sentences.map((s, k) => (
                  <li key={k}>{s.text} {s.cites.map((c) => <a key={c} href={`#cite-${c}`} className="num text-accent">[{c}]</a>)}</li>
                ))}
              </ul>
              <div className="flex flex-col gap-1 border-t border-line pt-2">
                {summary.citations.map((c) => (
                  <div key={c.n} id={`cite-${c.n}`} className="text-xs text-faint">
                    <span className="num text-accent">[{c.n}]</span> {c.company} · {c.title} · 第 {c.paragraph + 1} 段 · 正式发布 <span className="num">{time(c.published_at)}</span> ·
                    实际可获取 <span className="num">{time(c.available_at)}</span>{c.simulated && <span className="text-warn"> · {SIM}</span>}
                    {c.url && <> · <a className="text-accent underline" href={c.url} target="_blank" rel="noreferrer">原始 PDF</a></>}
                  </div>
                ))}
              </div>
              <p className="text-xs text-faint">模型 {summary.model}{summary.dropped ? ` · 丢弃了 ${summary.dropped} 句没有引用的结论` : ""}</p>
            </div>
          )}
        </Panel>
      )}
      {loading ? <Loading what="检索结果" /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={`${data.results.length}${data.truncated ? "+" : ""} 条结果`}
          eyebrow={data.concepts.length ? `提问映射到概念：${data.concepts.map((c) => `${c.name}${c.terms.length ? `（${c.terms.join("、")}）` : ""}`).join("；")}` : "未映射到概念，按关键词匹配原文与译文"}>
          <div className="flex flex-col divide-y divide-line">
            {data.results.map((h: TextHit, k) => (
              <div key={k} className="py-2.5 flex flex-col gap-1.5">
                <div className="flex flex-wrap gap-2 items-center">
                  <Badge tone="accent">{MARKET_LABEL[ov.datasets.find((d) => d.dataset === h.dataset)?.market ?? "fr"]}</Badge>
                  <SimBadge simulated={h.simulated} />
                  <span className="text-xs text-muted">{h.language_zh}原文 · 匹配：{h.matched.join("、")} · {REVIEW_ZH[h.review_status]}</span>
                </div>
                <p className="text-sm">{h.original}</p>
                {Object.entries(h.translation).map(([k2, v]) => <p key={k2} className="text-sm text-muted"><span className="text-faint">{LANG[k2]}译文：</span>{v}</p>)}
                <ConceptChips concepts={h.concepts} />
                <Cite ds={h.dataset} simulated={h.simulated} title={h.title} company={h.company} paragraph={h.paragraph}
                  published={h.published_at} available={h.available_at} effective={h.effective_date} url={h.url} />
              </div>
            ))}
            {!data.results.length && <div className="text-sm text-faint py-3">没有找到相关段落</div>}
          </div>
        </Panel>
      )}
    </div>
  );
}

function ReviewCard({ item, conceptsList, reviewer, onDone }: {
  item: ReviewItem; conceptsList: TextOverview["concepts"]; reviewer: string; onDone: () => void;
}) {
  const [mode, setMode] = useState<"view" | "correct">("view");
  const [picked, setPicked] = useState<Record<string, number>>(
    Object.fromEntries(item.concepts.map((c) => [c.concept, c.direction])));
  const [zh, setZh] = useState(item.translation.zh ?? "");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const send = async (action: string) => {
    setBusy(true); setErr(null);
    try {
      await api.textDecide({
        dataset: item.dataset, doc_id: item.doc_id, i: item.i, action, reviewer, note,
        concepts: action === "correct" ? Object.entries(picked).map(([concept, direction]) => ({ concept, direction, negated: direction < 0 })) : null,
        translation: action === "correct" && zh && zh !== item.translation.zh ? { zh } : null,
      });
      onDone();
    } catch (e) { setErr(String((e as Error).message ?? e)); } finally { setBusy(false); }
  };
  return (
    <div className="py-3 flex flex-col gap-2">
      <div className="flex flex-wrap gap-2 items-center">
        <Badge tone={TIER_TONE[item.tier]}>{item.sample_kind === "unscreened" ? "漏检抽样" : item.tier_zh}</Badge>
        <SimBadge simulated={item.simulated} />
        <span className="text-xs text-muted">{item.company} · {item.title} · 第 {item.i + 1} 段 · {item.language_zh} · {time(item.published_at)}</span>
        {item.flags.map((f) => <Badge key={f.code} tone="accent">{f.label}</Badge>)}
      </div>
      <p className="text-sm">{item.original}</p>
      {Object.entries(item.translation).map(([k, v]) => <p key={k} className="text-sm text-muted"><span className="text-faint">{LANG[k]}译文：</span>{v}</p>)}
      {item.sample_kind === "unscreened" ? (
        <div className="text-xs text-warn">这段没有命中术语表，没有送 AI 判断，平台按"不涉及风险"处理。请判断它是否其实表达了四类风险之一：是的话点"修正"标出概念（记为漏检），否则点"不涉及风险"。</div>
      ) : <>
        <div className="text-xs text-faint">模型判断（{item.method === "lexicon" ? "词典法" : item.model}）：</div>
        <ConceptChips concepts={item.model_concepts} />
      </>}
      {item.decision && <div className="text-xs text-muted">
        复核记录：{item.history.map((h) => `${h.at.replace("T", " ")} ${h.reviewer} ${REVIEW_ZH[h.action]}${h.note ? `（${h.note}）` : ""}`).join("；")}
      </div>}
      {!item.decision && mode === "view" && (
        <div className="flex flex-wrap gap-2">
          {item.sample_kind !== "unscreened" && <button className="btn btn-primary" disabled={busy || !reviewer} onClick={() => send("confirm")}>确认模型判断</button>}
          <button className="btn" disabled={busy || !reviewer} onClick={() => setMode("correct")}>修正</button>
          <button className="btn" disabled={busy || !reviewer} onClick={() => send("reject")}>不涉及风险</button>
          <input aria-label="复核理由" className="field flex-1 min-w-[12rem]" placeholder="理由（可选）" value={note} onChange={(e) => setNote(e.target.value)} />
          {!reviewer && <span className="text-sm text-warn w-full">请先在上方"复核人"里填写姓名，三个按钮才能使用（复核记录要写明是谁做的）</span>}
        </div>
      )}
      {mode === "correct" && (
        <div className="flex flex-col gap-2 border border-line rounded p-3">
          <div className="flex flex-wrap gap-2">
            {conceptsList.map((c) => {
              const v = picked[c.concept];
              const next = v === undefined ? 1 : v > 0 ? -1 : undefined;
              return (
                <button key={c.concept} type="button" className={`btn h-7 text-xs ${v === undefined ? "" : v > 0 ? "border-bad text-bad" : "border-good text-good"}`}
                  onClick={() => setPicked((p) => { const n = { ...p }; if (next === undefined) delete n[c.concept]; else n[c.concept] = next; return n; })}>
                  {c.zh}{v === undefined ? "" : v > 0 ? " ↑加重" : " ↓缓解/否定"}
                </button>
              );
            })}
          </div>
          <label className="label" htmlFor={`zh-${item.key}`}>中文译文（可修改）</label>
          <textarea id={`zh-${item.key}`} className="field min-h-16" value={zh} onChange={(e) => setZh(e.target.value)} />
          <input aria-label="修正理由" className="field" placeholder="修正理由" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-2">
            <button className="btn btn-primary" disabled={busy} onClick={() => send("correct")}>保存修正</button>
            <button className="btn" onClick={() => setMode("view")}>取消</button>
          </div>
          <p className="text-xs text-faint">点击概念切换：加重 → 缓解/否定 → 不选。</p>
        </div>
      )}
      {err && <Notice tone="bad" title="提交失败">{err}</Notice>}
    </div>
  );
}

function AccuracyPanel({ tick }: { tick: number }) {
  const { data, error } = useAsync(() => api.textAccuracy(), [tick]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  return (
    <>
      {data.datasets.map((a: ReviewAccuracy) => (
        <Panel key={a.dataset} eyebrow={`抽样质检 · ${a.dataset} · 从 ${a.pool.toLocaleString()} 段 AI 判断中固定抽取 ${a.sample_size} 段`} title="AI 判断准确率">
          <div className="grid gap-4 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="label text-left"><tr><th className="py-1 pr-3">分组</th><th className="pr-3 text-right">已复核</th><th className="pr-3 text-right">判断正确</th><th className="pr-3 text-right">准确率</th><th className="text-right">95% 区间</th></tr></thead>
                <tbody>
                  {a.rows.length === 0 && <tr><td colSpan={5} className="py-2 text-faint">还没有复核抽样段落。在下方切到"抽样质检"组开始复核。</td></tr>}
                  {a.rows.map((r) => (
                    <tr key={r.group} className="border-t border-line">
                      <td className="py-1.5 pr-3">{r.group_zh}</td><td className="pr-3 text-right num">{r.reviewed}</td><td className="pr-3 text-right num">{r.correct}</td>
                      <td className="pr-3 text-right num font-medium">{pctOrDash(r.accuracy)}</td>
                      <td className="text-right num text-muted">{r.ci95 ? `${pctOrDash(r.ci95[0])} – ${pctOrDash(r.ci95[1])}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-sm flex flex-col gap-1.5">
              <div className="label">术语表漏检（没送 AI 的段落）</div>
              {a.screening === "full" ? <p className="text-muted">当前为全文模式，所有段落都送 AI，不做漏检抽样。</p> : <>
                <div><span className="font-display text-xl num">{pctOrDash(a.unscreened.miss_rate)}</span>
                  <span className="text-xs text-muted ml-2">已复核 {a.unscreened.reviewed} / {a.unscreened.sample_size} 段，漏检 {a.unscreened.missed} 段
                    {a.unscreened.ci95 && `（95% 区间 ${pctOrDash(a.unscreened.ci95[0])} – ${pctOrDash(a.unscreened.ci95[1])}）`}</span></div>
                <p className="text-xs text-faint">漏检多时：先用"术语表扩充"让 AI 找漏掉的说法；仍不够再在正式业务中打开全文模式（configs/text/pipeline.yaml）。</p>
              </>}
              <p className="text-xs text-faint">{a.note}。判断正确 = 确认；或判定不涉及风险而 AI 本来也没标概念；或只改译文、概念与方向不变。</p>
            </div>
          </div>
        </Panel>
      ))}
    </>
  );
}

function ReviewTab({ ov, onChanged }: { ov: TextOverview; onChanged: () => void }) {
  const [status, setStatus] = useState<"pending" | "done">("pending");
  const [ds, setDs] = useState<string>("");
  const [tier, setTier] = useState<ReviewTier | "all">("must");
  const [tick, setTick] = useState(0);
  const [reviewer, setReviewer] = useState(() => { try { return localStorage.getItem("factopia-reviewer") ?? ""; } catch { return ""; } });
  useEffect(() => { try { localStorage.setItem("factopia-reviewer", reviewer); } catch { /* 忽略 */ } }, [reviewer]);
  const { data, error, loading } = useAsync(() => api.textReview(status, ds || undefined, tier), [status, ds, tier, tick]);
  const tc = data?.counts.tiers;
  const n = (t: ReviewTier) => (tc ? tc[t][status] : "");
  return (
    <div className="flex flex-col gap-4">
      <AccuracyPanel tick={tick} />
      <div className="panel p-4 flex flex-wrap gap-3 items-center">
        <Seg label="分组" value={tier} onChange={setTier} options={[
          { value: "must", label: `必须复核 ${n("must")}` }, { value: "sample", label: `抽样质检 ${n("sample")}` },
          { value: "impact", label: `高影响 ${n("impact")}` }, { value: "other", label: `其他 ${n("other")}` }, { value: "all", label: "全部" },
        ]} />
        <Seg label="状态" value={status} onChange={setStatus}
          options={[{ value: "pending", label: `待复核 ${data?.counts.pending ?? ""}` }, { value: "done", label: `已复核 ${data?.counts.done ?? ""}` }]} />
        <Seg label="数据集" value={ds} onChange={setDs}
          options={[{ value: "", label: "全部" }, ...ov.datasets.map((d) => ({ value: d.dataset, label: `${d.market_zh}${d.simulated ? "（模拟）" : ""}` }))]} />
        <label className="text-sm text-muted flex items-center gap-2 whitespace-nowrap" htmlFor="intel-reviewer">复核人
          <input id="intel-reviewer" className="field w-40" value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="姓名（必填）" />
        </label>
      </div>
      <Notice tone="accent" title={tier === "all" ? "四个分组" : `${({ must: "必须复核", sample: "抽样质检", impact: "高影响", other: "其他" } as const)[tier]}`}>
        {tier === "all" ? "必须复核 → 抽样质检 → 高影响 → 其他，按这个顺序排列。只有「必须复核」未完成会阻止提交审批；其余组看多少由你决定。复核结果只追加记录不覆盖，打分与检索使用复核后的结果。"
          : TIER_HINT[tier]}
      </Notice>
      {loading ? <Loading what="复核队列" /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={status === "pending" ? "待复核段落" : "已复核段落"} eyebrow={`显示前 ${data.items.length} 条`}>
          <div className="flex flex-col divide-y divide-line">
            {data.items.map((it) => (
              <ReviewCard key={it.key} item={it} conceptsList={ov.concepts} reviewer={reviewer}
                onDone={() => { setTick((t) => t + 1); onChanged(); }} />
            ))}
            {!data.items.length && <div className="text-sm text-faint py-3">没有{status === "pending" ? "待复核" : "已复核"}的段落</div>}
          </div>
        </Panel>
      )}
    </div>
  );
}

function CandidateRow({ c, by, onDone }: { c: GlossaryCandidate; by: string; onDone: () => void }) {
  const [term, setTerm] = useState(c.term);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const go = async (action: "approve" | "reject") => {
    setBusy(true); setErr(null);
    try { await api.glossaryDecide(c.id, { action, by, term: action === "approve" ? term : null }); onDone(); }
    catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <tr className="border-t border-line align-top">
      <td className="py-2 pr-3 whitespace-nowrap">{c.concept_zh}<div className="text-xs text-faint">{LANG[c.lang] ?? c.lang} · 出现 {c.count} 次</div></td>
      <td className="py-2 pr-3 min-w-[12rem]">
        {c.status === "pending" ? <input aria-label="候选术语" id={`gc-${c.id}`} className="field h-8" value={term} onChange={(e) => setTerm(e.target.value)} />
          : <span className="font-medium">{c.final_term ?? c.term}</span>}
        {c.examples.slice(0, 2).map((x) => <blockquote key={`${x.doc_id}-${x.i}`} className="text-xs text-faint border-l-2 border-line pl-2 mt-1">{x.text}</blockquote>)}
      </td>
      <td className="py-2 whitespace-nowrap">
        {c.status === "pending" ? (
          <div className="flex gap-1.5">
            <button className="btn btn-primary h-7 px-2 text-xs" disabled={busy || !by} onClick={() => go("approve")}>批准</button>
            <button className="btn h-7 px-2 text-xs" disabled={busy || !by} onClick={() => go("reject")}>拒绝</button>
          </div>
        ) : <Badge tone={c.status === "approved" ? "good" : "muted"}>{c.status === "approved" ? "已批准" : "已拒绝"} · {c.decided_by}</Badge>}
        {err && <div className="text-xs text-bad mt-1">{err}</div>}
      </td>
    </tr>
  );
}

function GlossaryExpansion({ ov }: { ov: TextOverview }) {
  const [tick, setTick] = useState(0);
  const { data, error } = useAsync(() => api.glossaryCandidates(), [tick]);
  const [by, setBy] = useState(() => { try { return localStorage.getItem("factopia-reviewer") ?? ""; } catch { return ""; } });
  useEffect(() => { try { localStorage.setItem("factopia-reviewer", by); } catch { /* 忽略 */ } }, [by]);
  const [show, setShow] = useState<"pending" | "done">("pending");
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading what="候选术语" />;
  const list = data.candidates.filter((c) => (show === "pending" ? c.status === "pending" : c.status !== "pending"));
  const last = data.runs[data.runs.length - 1];
  return (
    <Panel eyebrow={`术语表扩充 · 当前版本 ${data.glossary_version} · 已批准新增 ${data.additions.length} 条`} title="AI 找到的候选术语"
      actions={<label className="text-sm text-muted flex items-center gap-2 whitespace-nowrap" htmlFor="gc-by">审批人<input id="gc-by" className="field h-8 w-32" value={by} onChange={(e) => setBy(e.target.value)} placeholder="姓名" /></label>}>
      <p className="text-sm text-muted mb-2">AI 从术语表没命中的段落里，找出其实表达了风险、但用了术语表里没有的说法，原样摘出短语作为候选。批准后加入术语表（可先修改短语），再运行补做，只判断新纳入的段落：
        <code className="num text-xs ml-1">python -m factopia.text understand --dataset fr --refresh</code>（先显示预计花费，加 --yes 才运行）。</p>
      <p className="text-xs text-faint mb-3">{last ? `最近一次：${time(last.at)}，抽取 ${last.sampled} 段，其中 ${last.with_risk} 段表达了风险，得到 ${last.candidates} 个候选，花费 $${last.usd}${last.stopped ? `（${last.stopped}）` : ""}。` : "还没有运行过。"}
        生成候选：<code className="num">python -m factopia.text glossary-suggest --dataset fr</code>（先显示预计花费，加 --yes 才运行）。预筛方式：{ov.pipeline?.screening === "full" ? "全文" : "术语表预筛"}。</p>
      <Seg label="显示" value={show} onChange={setShow} options={[
        { value: "pending", label: `待审批 ${data.candidates.filter((c) => c.status === "pending").length}` },
        { value: "done", label: `已处理 ${data.candidates.filter((c) => c.status !== "pending").length}` }]} />
      <div className="overflow-x-auto mt-3">
        <table className="w-full text-sm">
          <thead className="label text-left"><tr><th className="py-1 pr-3">概念</th><th className="pr-3">候选短语与原文例句</th><th>审批</th></tr></thead>
          <tbody>
            {list.map((c) => <CandidateRow key={c.id} c={c} by={by.trim()} onDone={() => setTick((t) => t + 1)} />)}
            {!list.length && <tr><td colSpan={3} className="py-2 text-faint">没有{show === "pending" ? "待审批" : "已处理"}的候选。</td></tr>}
          </tbody>
        </table>
      </div>
      {!by.trim() && <p className="text-xs text-warn mt-2">请先填写审批人，批准 / 拒绝按钮才能使用。</p>}
    </Panel>
  );
}

function GlossaryTab({ ov }: { ov: TextOverview }) {
  return (
    <div className="flex flex-col gap-4">
    <GlossaryExpansion ov={ov} />
    <Panel title="统一概念标签与三语术语表" eyebrow={`版本 ${ov.glossary_version} · configs/text/glossary.yaml`}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left"><tr><th className="py-1 pr-3">概念</th><th className="pr-3">中文</th><th className="pr-3">英文</th><th>法文</th></tr></thead>
          <tbody>
            {ov.concepts.map((c) => (
              <tr key={c.concept} className="border-t border-line align-top">
                <td className="py-2 pr-3 whitespace-nowrap"><div className="font-medium">{c.zh}</div><div className="text-xs text-faint">{c.en} · {c.fr}</div></td>
                {(["zh", "en", "fr"] as const).map((l) => <td key={l} className="pr-3 py-2 text-xs text-muted">{(c.terms[l] ?? []).join("、")}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-faint mt-2">术语表用于预筛段落、跨语言检索和未配置大模型时的词典法标注；每条理解结果都记录所用版本。
        {ov.glossary_additions?.length ? ` 其中 ${ov.glossary_additions.length} 条为人工批准的新增术语（${ov.glossary_additions.map((a) => a.term).join("、")}）。` : ""}</p>
    </Panel>
    </div>
  );
}

export default function IntelPage() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync(() => api.textOverview(), [tick]);
  const [tab, setTab] = useState<Tab>("filings");
  if (loading && !data) return <Loading what="情报台" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">情报台</div>
          <h1 className="font-display text-xl font-semibold">多语言公告与文本信号</h1>
        </div>
        <span className="text-sm text-muted">原文优先 · 译文与概念标签另存 · 按实际可获取时间生效</span>
        <span className="ml-auto flex flex-wrap gap-2" title="只有真实数据「必须复核」组（低置信度翻译）未完成会阻止研究提交审批；其余待复核不阻止；模拟数据的复核要求豁免">
          <Badge tone={data.review_counts.must_real ? "bad" : "good"}>必须复核 {data.review_counts.must_real ?? 0}</Badge>
          <Badge tone="muted">真实数据其他待复核 {Math.max(0, (data.review_counts.pending_real ?? 0) - (data.review_counts.must_real ?? 0))}（不阻止）</Badge>
          {data.review_counts.pending_simulated ? <Badge tone="muted">模拟数据待复核 {data.review_counts.pending_simulated}（豁免）</Badge> : null}
          <Badge tone="muted">预筛：{data.pipeline?.screening === "full" ? "全文" : "术语表"}</Badge>
        </span>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {data.datasets.map((d) => <SourceCard key={d.dataset} d={d} />)}
      </div>
      {data.compare && <CompareTable rows={data.compare.markets} note={data.compare.note} horizon={data.compare.horizon} rule={data.compare.rule} datasets={data.datasets} />}
      <Seg label="视图" value={tab} onChange={setTab} options={[
        { value: "filings", label: "公告与打分" }, { value: "search", label: "跨语言检索" },
        { value: "review", label: `语言专家复核` }, { value: "glossary", label: "术语表" },
      ]} />
      {tab === "filings" && <FilingsTab ov={data} />}
      {tab === "search" && <SearchTab ov={data} />}
      {tab === "review" && <ReviewTab ov={data} onChanged={() => setTick((t) => t + 1)} />}
      {tab === "glossary" && <GlossaryTab ov={data} />}
    </div>
  );
}
