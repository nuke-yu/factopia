import { Fragment, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import EChart from "../components/EChart";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import {
  api, type DigRound, type GlossaryCandidate, type ReviewAccuracy, type ReviewItem, type ReviewTier, type TextCompareRow, type TextConcept, type TextDataset, type TextDig, type TextDoc, type TextFiling,
  type TextHit, type TextOverview, type TextSummary,
} from "../api";
import { cssVar, fix, MARKET_LABEL, marketColor, SHOW_FORMULAS, signed, useAsync, useThemeVersion } from "../lib";
import { IS_ZH, joinList, t, tf } from "../i18n";

// 情报台（Spec 第 11 章；BR-08 至 12、15、49）：多语言公告 → 概念标签 / 译文 / 置信度 → 复核 → 文本信号 → 三市场检验

const SIM = t("模拟数据，仅用于流程验证");
const LANG: Record<string, string> = { zh: t("中文"), en: t("英文"), fr: t("法文") };
const REVIEW_ZH: Record<string, string> = {
  pending: t("待复核"), not_required: t("无需复核"), confirm: t("已确认"), correct: t("已修正"), reject: t("判定不涉及风险"),
};
type Tab = "filings" | "search" | "review" | "glossary";
const TIER_TONE: Record<ReviewTier, "bad" | "accent" | "warn" | "muted"> = { must: "bad", sample: "accent", impact: "warn", other: "muted" };
const TIER_HINT: Record<ReviewTier, string> = {
  must: t("低置信度翻译。业务文档要求人工复核；未复核时，使用该数据集的研究不能提交审批。"),
  sample: t("按固定种子随机抽取，用来估计 AI 判断的准确率：抽到的不论是否触发复核都要看，看完全部样本才有代表性。另有一部分是术语表没命中、没送 AI 的段落，用来估计漏检率。"),
  impact: t("这段的判断一旦相反，所在公告的「风险变化」就会变号，对信号影响最大。"),
  other: t("触发了复核条件（否定句、低置信度、监管措辞、财务术语等），但不在以上三组。有空再看，不看不影响信号与提交审批。"),
};
const TIER_LABEL: Record<ReviewTier, string> = { must: t("必须复核"), sample: t("抽样质检"), impact: t("高影响"), other: t("其他") };
const FLAG_LABEL: Record<string, string> = {
  low_confidence: t("概念判断置信度低"), low_translation_confidence: t("翻译置信度低"), negation: t("含否定句"),
  regulatory_wording: t("含监管措辞"), key_financial_terms: t("含关键财务术语"), lexicon_only: t("仅词典法标注，未经大模型"),
};

// i18n-9：后端把结论/排除原因改为语言中立码，前端按码表本地化
const VERDICT_LABEL: Record<string, string> = {
  insufficient: t("数据不足（每天少于 5 只股票有信号）"), inconsistent: t("结论不一致（事件检验方向相反）"),
  valid: t("有效（方向符合假设）"), opposite: t("方向相反"), aux: t("不显著（仅事件检验显著，作辅助证据）"),
  aux_opposite: t("不显著（事件检验方向相反）"), insignificant: t("不显著"),
};
const verdictLabel = (v: string) => VERDICT_LABEL[v] ?? v;
const exclReason = (r: any) => typeof r === "string" ? r
  : r?.code === "few_paragraphs" ? tf("段落过少（{n} 段 < {m}）：通知或 PDF 抽不出文字", { n: r.n, m: r.min })
  : r?.code === "notice" ? tf("通知类公告（标题为报告发布 / 提供查阅通知，{n} 段）", { n: r.n }) : String(r?.code ?? r);
const COMPARE_NOTE = t("只有法国是真实数据；香港、英国为模拟数据（植入已知规律），用于验证流程能否找出规律，不构成研究结论");
const JUDGE_RULE = t("结论以逐日 Rank IC 的 t 值（Newey-West）为主口径：t < −2 且事件检验不反向 → 有效；主口径不显著时，事件检验显著只作辅助证据，结论仍为不显著（2026-10-11 收紧，原规则为两个口径任一显著即有效）");

const ACC_GROUP: Record<string, string> = { all: t("全部抽样"), flagged: t("触发复核的段落"), unflagged: t("未触发复核的段落") };
const pctOrDash = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);

const time = (iso: string | null | undefined) => (iso ? iso.replace("T", " ").replace(/(:\d\d)(\+00:00|Z)$/, "$1 UTC") : "—");

function SimBadge({ simulated }: { simulated: boolean }) {
  return simulated ? <Badge tone="warn">{SIM}</Badge> : <Badge tone="good">{t("真实公告")}</Badge>;
}

function ConceptChips({ concepts }: { concepts: TextConcept[] }) {
  if (!concepts.length) return <span className="text-xs text-faint">{t("不涉及四类风险")}</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {concepts.map((c, k) => (
        <Badge key={k} tone={c.direction > 0 ? "bad" : "good"}>
          {c.name ?? c.concept} {c.direction > 0 ? t("↑加重") : t("↓缓解")}{c.negated ? t(" · 否定句") : ""} · {fix(c.confidence, 2)}
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
      <span>{company} · {title} · {tf("第 {i} 段", { i: paragraph + 1 })}</span>
      <span>{t("正式发布")} <span className="num">{time(published)}</span></span>
      <span>{t("实际可获取")} <span className="num">{time(available)}</span></span>
      <span>{t("生效交易日")} <span className="num">{effective ?? "—"}</span></span>
      {url && <a className="text-accent underline" href={url} target="_blank" rel="noreferrer">{t("原始 PDF")}</a>}
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
        <span className="font-display font-semibold">{MARKET_LABEL[d.market]}</span>
        <SimBadge simulated={d.simulated} />
      </div>
      <div className="text-xs text-muted">{t("来源：")}{d.source} · {t("许可：")}{d.licence}</div>
      {!d.ready ? <div className="text-sm text-faint">{t("尚未生成（python -m factopia.text all）")}</div> : (
        <div className="grid grid-cols-3 gap-3">
          <Stat label={t("公告")} value={d.n_filings} sub={tf("{n} 家公司", { n: d.n_companies })} />
          <Stat label={t("逐日 Rank IC")} value={fix(e?.rank_ic_mean, 3)} sub={`t = ${fix(e?.rank_ic_t, 2)}`} />
          <Stat label={t("公告后事件")} value={fix(e?.events.diff_t, 2)} sub={t("悲观 − 乐观 的 t 值")} />
        </div>
      )}
      {d.truth && <div className="text-xs text-warn">{t("植入规律：")}{tf("风险水平每上升 1 级，可交易的第一天起 {d} 个交易日个股累计少涨 {e}", { d: d.truth.effect_days, e: `${(d.truth.effect * 100).toFixed(0)}%` })}；{t("预期：")}{t("风险表述变化与未来 20 日收益负相关（Rank IC < 0）")}</div>}
      <div className="text-xs text-faint">{tf("处理延迟 {m} 分钟计入可获取时间；分数沿用 {h} 个交易日", { m: d.processing_delay_min, h: d.hold_days ?? "—" })}</div>
    </div>
  );
}

// 敏感性检验（第 11.10 节）：处理延迟 × 信号沿用期，结论是否稳定
function Sensitivity({ datasets }: { datasets: TextDataset[] }) {
  const ds = datasets.filter((d) => d.sensitivity?.rows.length);
  if (!ds.length) return null;
  return (
    <div className="mt-4">
      <div className="label mb-1">{t("敏感性检验：处理延迟（分钟）× 信号沿用期（交易日）")}</div>
      <div className="grid gap-4 lg:grid-cols-3">
        {ds.map((d) => {
          const s = d.sensitivity!;
          const delays = [...new Set(s.rows.map((r) => r.delay_min))], holds = [...new Set(s.rows.map((r) => r.hold_days))];
          return (
            <div key={d.dataset} className="border border-line rounded p-2 overflow-x-auto">
              <div className="flex items-center gap-2 text-sm mb-1">{MARKET_LABEL[d.market]}<SimBadge simulated={d.simulated} />
                <Badge tone={s.stable ? "good" : "warn"}>{s.stable ? t("结论稳定") : t("结论随参数变化")}</Badge></div>
              <table className="w-full text-xs num">
                <thead className="text-faint"><tr><th className="text-left font-normal">{t("延迟 \\ 沿用")}</th>{holds.map((h) => <th key={h} className="text-right font-normal">{tf("{h} 天", { h })}</th>)}</tr></thead>
                <tbody>{delays.map((dl) => (
                  <tr key={dl} className="border-t border-line"><td>{tf("{d} 分钟", { d: dl })}</td>
                    {holds.map((h) => {
                      const r = s.rows.find((x) => x.delay_min === dl && x.hold_days === h);
                      const ok = r?.verdict === "valid";
                      return <td key={h} className={`text-right ${r?.baseline ? "font-semibold" : ""}`} title={r ? verdictLabel(r.verdict) : undefined}>
                        <span style={{ color: `var(--${ok ? "good" : "muted"})` }}>{fix(r?.rank_ic_t, 2)}</span></td>;
                    })}</tr>))}</tbody>
              </table>
              <div className="text-[11px] text-faint mt-1">{t("数字为 Rank IC 的 t 值；绿色 = 判定有效；粗体 = 当前参数（60 分钟、63 天）")}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// 开发阶段公式说明（SHOW_FORMULAS）：每个市场一行，代入该市场的实际数值；口径与 factopia/text/signal.py 的 evaluate / judge 一致
function verdictWhy(r: TextCompareRow): string {
  const tv = r.rank_ic_t, et = r.event_diff_t ?? null;
  if (!r.n_days || tv == null) return tf("有效交易日 = {n}，主口径无法计算 → 数据不足", { n: r.n_days });
  const ev = et == null ? t("事件 t 未计算") : et < -2 ? tf("事件 t = {v} < −2（同向显著）", { v: fix(et, 2) }) : et > 2 ? tf("事件 t = {v} > 2（反向显著）", { v: fix(et, 2) }) : tf("事件 t = {v}（不显著）", { v: fix(et, 2) });
  const main = tv < -2 ? tf("主口径 t = {v} < −2", { v: fix(tv, 2) }) : tv > 2 ? tf("主口径 t = {v} > 2", { v: fix(tv, 2) }) : tf("主口径 −2 ≤ t = {v} ≤ 2", { v: fix(tv, 2) });
  return `${main}；${ev} → ${verdictLabel(r.verdict)}`;
}

function FormulaRow({ r, horizon }: { r: TextCompareRow; horizon: number }) {
  const ic = r.rank_ic_mean, tv = r.rank_ic_t;
  const p = r.event_pessimistic, o = r.event_optimistic, et = r.event_diff_t ?? null;
  const noDays = !r.n_days || ic == null;
  const items: [string, string][] = [
    [t("公告"), tf("{f} 篇 = {c} 家公司在样本期内的公告数（filings.parquet 行数）", { f: r.n_filings, c: r.n_companies })],
    [t("Rank IC"), noDays ? t("有效交易日 = 0（每天有信号的股票都少于 5 只）→ 无法计算")
      : tf("IC_d = Spearman(当天信号, 当天起未来 {h} 日收益)，只算 ≥ 5 只股票有信号的交易日；Rank IC = 平均(IC_d) = {v}（共 {n} 个交易日）", { h: horizon, v: fix(ic, 3), n: r.n_days })],
    [t("t 值"), noDays || tv == null ? t("无法计算")
      : tf("t = 平均(IC_d) ÷ Newey-West 标准误（滞后 {h} 日）= {a} ÷ {b} = {v}", { h: horizon, a: fix(ic, 4), b: fix(ic / tv, 4), v: fix(tv, 2) })],
    [t("三分组收益差"), r.tercile_spread_mean == null ? t("无法计算（同上）")
      : tf("每天按信号排序分三组：差_d = 信号最高 1/3 的平均收益 − 最低 1/3 的平均收益；平均(差_d) = {v}", { v: signed(r.tercile_spread_mean, 2) })],
    [t("悲观 / 乐观组超额"), tf("超额 = 个股生效日起 {h} 日收益 − 同市场等权平均；悲观组 = 风险变化 > 0 的 {pn} 次公告，平均 {pm}；乐观组 = 风险变化 < 0 的 {on} 次公告，平均 {om}", { h: horizon, pn: p?.n ?? 0, pm: signed(p?.mean_excess, 2), on: o?.n ?? 0, om: signed(o?.mean_excess, 2) })],
    [t("事件 t 值"), et == null || p?.mean_excess == null || o?.mean_excess == null
      ? tf("事件数不足（总数需 ≥ 10、每组需 > 2；目前悲观 {pn}、乐观 {on}）→ 未计算", { pn: p?.n ?? 0, on: o?.n ?? 0 })
      : tf("t = (悲观均值 − 乐观均值) ÷ √(s²悲/n悲 + s²乐/n乐) = ({a} − {b}) ÷ {c} = {v}（Welch）", { a: signed(p.mean_excess, 2), b: signed(o.mean_excess, 2), c: signed((p.mean_excess - o.mean_excess) / et, 2).replace("+", ""), v: fix(et, 2) })],
    [t("结论"), verdictWhy(r)],
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
      <div className="text-faint">{t("开发阶段公式说明（进入 MVP 时隐藏）· 各市场下方灰色行为代入实际数值的计算过程")}</div>
      <div>{t("风险语气 = Σ(方向 × 置信度) ÷ 公告段落数 × 100（方向：加重 +1 / 缓解 −1；只计入复核后生效的标注）")}</div>
      <div>{tf("信号 = 本篇风险语气 − 同一系列上一篇（同公司、同类型、同语言）的风险语气；从生效日起沿用，最长 {h} 个交易日", { h: hold })}</div>
      <div>{tf("生效日 = 发布时间 + {d} 分钟（处理延迟）之后的第一个收盘所在交易日", { d: delay })}</div>
      <div>{tf("未来收益_d = 收盘价(d + 1 + {h}) ÷ 收盘价(d + 1) − 1（从信号日的下一个交易日起算 {h} 个交易日）", { h: horizon })}</div>
    </div>
  );
}

function CompareTable({ rows, note, horizon, rule, datasets }: { rows: TextCompareRow[]; note: string; horizon: number; rule?: string; datasets: TextDataset[] }) {
  return (
    <Panel title={t("三个市场分别检验")} eyebrow={tf("示例主线 · 管理层风险表述变化 → 未来 {h} 个交易日相对收益", { h: horizon })}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left">
            <tr><th className="py-1 pr-3">{t("市场")}</th><th className="pr-3">{t("数据")}</th><th className="pr-3 text-right">{t("公告")}</th>
              <th className="pr-3 text-right">Rank IC</th><th className="pr-3 text-right">{t("t 值")}</th><th className="pr-3 text-right">{t("三分组收益差")}</th>
              <th className="pr-3 text-right">{t("悲观组超额")}</th><th className="pr-3 text-right">{t("乐观组超额")}</th><th className="pr-3 text-right">{t("事件 t 值")}</th><th>{t("结论")}</th></tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => [
              <tr key={r.dataset} className="border-t border-line">
                <td className="py-1.5 pr-3 font-sans">{MARKET_LABEL[r.market]}</td>
                <td className="pr-3 font-sans">{r.simulated ? <Badge tone="warn">{t("模拟")}</Badge> : <Badge tone="good">{t("真实")}</Badge>}</td>
                <td className="pr-3 text-right">{r.n_filings}</td>
                <td className="pr-3 text-right">{fix(r.rank_ic_mean, 3)}</td>
                <td className="pr-3 text-right">{fix(r.rank_ic_t, 2)}</td>
                <td className="pr-3 text-right">{signed(r.tercile_spread_mean, 2)}</td>
                <td className="pr-3 text-right">{signed(r.event_pessimistic?.mean_excess, 2)}</td>
                <td className="pr-3 text-right">{signed(r.event_optimistic?.mean_excess, 2)}</td>
                <td className="pr-3 text-right">{fix(r.event_diff_t, 2)}</td>
                <td className="font-sans">{verdictLabel(r.verdict)}</td>
              </tr>,
              SHOW_FORMULAS && <FormulaRow key={r.dataset + "-f"} r={r} horizon={horizon} />,
            ])}
          </tbody>
        </table>
      </div>
      {SHOW_FORMULAS && <FormulaDefs horizon={horizon} datasets={datasets} />}
      <p className="text-xs text-faint mt-2">
        {t("假设方向：风险表述越悲观，未来收益越低（Rank IC 与事件 t 值为负）。t 值绝对值大于 2 视为显著。")}{note}
      </p>
      {rule && <p className="text-xs text-muted mt-1">{t("判定规则：")}{rule}</p>}
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

function DocView({ ds, id, onClose, onDig }: { ds: string; id: string; onClose: () => void; onDig?: () => void }) {
  const { data, error, loading } = useAsync<TextDoc>(() => api.textDoc(ds, id), [ds, id]);
  const [onlyLabelled, setOnlyLabelled] = useState(true);
  if (loading) return <Loading what={t("公告")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const m = data.meta;
  const paras = data.paragraphs.filter((p) => !onlyLabelled || p.concepts.length || p.flags.length);
  return (
    <Panel title={m?.title ?? id} eyebrow={`${m?.company ?? ""} · ${tf("{l}原文", { l: LANG[m?.language ?? ""] ?? "" })}`}
      actions={<>
        <Seg label={t("段落")} value={onlyLabelled ? "y" : "n"} onChange={(v) => setOnlyLabelled(v === "y")}
          options={[{ value: "y", label: t("只看标注段落") }, { value: "n", label: t("全部段落") }]} />
        {onDig && <button className="btn btn-primary" onClick={onDig}>{t("以本篇为起点深挖")}</button>}
        <button className="btn" onClick={onClose}>{t("关闭")}</button>
      </>}>
      <div className="flex flex-col gap-2 mb-3">
        <SimBadge simulated={data.simulated} />
        <div className="text-xs text-muted">
          {t("理解模型")} <span className="num">{data.model}</span> · {t("提示词")} <span className="num">{data.prompt_version}</span> ·
          {t("术语表")} <span className="num">{data.glossary_version}</span> · {t("标注时间")} <span className="num">{time(data.labelled_at)}</span>
          {data.pages_total ? <> · {tf("读取 {a}/{b} 页", { a: data.pages_read ?? 0, b: data.pages_total })}</> : null}
        </div>
        {m && <div className="text-xs text-muted">
          {t("业务日期")} <span className="num">{m.business_date}</span> · {t("正式发布")} <span className="num">{time(m.published_at)}</span> ·
          {t("实际可获取")} <span className="num">{time(m.available_at)}</span> · {t("生效交易日")} <span className="num">{m.effective_date}</span>
          {m.after_close && <span className="text-warn"> · {t("收盘后发布，顺延到下一交易日")}</span>}
          {m.url && <> · <a className="text-accent underline" href={m.url} target="_blank" rel="noreferrer">{t("原始 PDF")}</a></>}
        </div>}
      </div>
      <div className="flex flex-col divide-y divide-line">
        {paras.map((p) => (
          <div key={p.i} className="py-2.5 grid gap-2 md:grid-cols-2">
            <div>
              <div className="label mb-1">{tf("第 {i} 段 · 原文", { i: p.i + 1 })}</div>
              <p className="text-sm leading-relaxed">{p.text}</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="label">{t("机器翻译")}{p.translation_confidence != null && <> · {t("置信度")} {fix(p.translation_confidence, 2)}</>}</div>
              {Object.keys(p.translation).length ? Object.entries(p.translation).map(([k, v]) => (
                <p key={k} className="text-sm text-muted"><span className="text-faint">{LANG[k]}{t("：")}</span>{v}</p>
              )) : <p className="text-xs text-faint">{p.method === "lexicon" ? t("词典法标注，无译文") : "—"}</p>}
              <ConceptChips concepts={p.concepts} />
              <div className="flex flex-wrap gap-1">
                {p.review_status && <Badge tone={p.review_status === "pending" ? "warn" : "muted"}>{REVIEW_ZH[p.review_status]}</Badge>}
                {p.flags.map((f) => <Badge key={f.code} tone="accent">{FLAG_LABEL[f.code] ?? f.label}</Badge>)}
              </div>
            </div>
          </div>
        ))}
        {!paras.length && <div className="text-sm text-faint py-3">{t("本篇没有与四类风险相关的段落")}</div>}
      </div>
    </Panel>
  );
}

// ── 公告深挖（WF-04，docs/Agentic-workflow-情报台-深挖.md）──────────────────
const DIG_RUNNING = new Set(["queued", "planning", "rewriting", "searching", "annotating", "summarizing", "extracting", "concluding"]);
const DIG_TOOL_ZH: Record<string, string> = { query: t("检索并提取"), finish: t("收尾出结论"), give_up: t("数据不足放弃") };
const DIG_STATE_ZH: Record<string, string> = {
  queued: t("排队中"), web_searching: t("联网检索公开资料"), planning: t("规划下一步"), rewriting: t("改写查询"),
  searching: t("检索相关公告"), annotating: t("补概念标注"), summarizing: t("生成检索摘要"), extracting: t("提取风险事实"),
  concluding: t("生成结论"), done: t("已完成"), insufficient: t("数据不足"), stopped: t("已停止"), failed: t("失败"),
};
const DIG_DROP_ZH: Record<string, string> = {
  no_valid_cites: t("引用段落编号无效"), unknown_concept: t("概念不在术语表"), bad_direction: t("方向值无效"),
  empty_statement: t("事实正文为空"), evidence_not_verbatim: t("证据不是原文片段"), no_date: t("引用公告没有日期"),
  not_object: t("格式不是对象"),
};
const DIG_MARKET: Record<string, "hk" | "uk" | "fr"> = { fr: "fr", demo_hk: "hk", demo_uk: "uk" };

function DigPanel({ ds, instrument, company, filingId, onClose }:
  { ds: string; instrument: string; company: string; filingId: string | null; onClose: () => void }) {
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [webSearch, setWebSearch] = useState(false);
  const navigate = useNavigate();
  const { data, error, loading } = useAsync<TextDig>(() => api.textDigs(ds, instrument), [ds, instrument, tick]);
  useEffect(() => {
    if (!data || !DIG_RUNNING.has(data.dig.state)) return;
    const timer = setInterval(() => setTick((t) => t + 1), 5000);
    return () => clearInterval(timer);
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = async () => {
    setBusy(true); setErr(null);
    try { await api.textDigCreate({ dataset: ds, instrument, company, filing_id: filingId ?? undefined, web_search: webSearch }); setTick((t) => t + 1); }
    catch (e) { setErr(String((e as Error).message ?? e)); } finally { setBusy(false); }
  };
  const cancel = async () => {
    if (!data?.dig.job_id) return;
    setBusy(true); setErr(null);
    try { await api.cancelJob(data.dig.job_id); setTick((t) => t + 1); } catch (e) { setErr(String((e as Error).message ?? e)); } finally { setBusy(false); }
  };
  const toIdea = async () => {
    const b = data?.dig; const c = b?.conclusion;
    if (!b || !c) return;
    setBusy(true); setErr(null);
    const body = [c.summary,
      (c.groups ?? []).map((g) => `${g.name}：${g.summary}`).join("\n"),
      (c.timeline ?? []).map((tl) => `${tl.date} ${tl.statement}`).join("\n"),
      c.signal_note].filter(Boolean).join("\n\n");
    try {
      const r = await api.createIdea({ channel: "hypothesis", title: `${b.company} 公告深挖风险画像`,
        body, markets: [DIG_MARKET[ds] ?? "fr"], submitted_by: t("研究员（未登录）"), confirm_no_client_data: true });
      navigate(`/ideas?id=${r.id}`);                 // 跳线索池并自动定位到新线索
    } catch (e) { setErr(String((e as Error).message ?? e)); } finally { setBusy(false); }
  };
  const b = data?.dig;
  const state = b?.state ?? "";
  const tone = state === "done" ? "good" : state === "insufficient" ? "warn" : state === "failed" ? "bad" : state === "stopped" ? "muted" : "accent";
  return (
    <Panel title={`${t("公告深挖")} · ${b?.company ?? company}`} eyebrow={`${instrument} · ${t("有界检索-提取循环：AI 自行决定查什么、提取什么、何时收尾")}`}
      actions={<>
        <label className="text-xs text-muted flex items-center gap-1 cursor-pointer whitespace-nowrap"
          title={t("勾选后启动时先联网检索该公司公开资料（门户网站、彭博社、经济论坛等）；仅作启发，来源需人工核验")}>
          <input type="checkbox" checked={webSearch} disabled={busy || DIG_RUNNING.has(state)}
            onChange={(e) => setWebSearch(e.target.checked)} />
          {t("联网搜索")}
        </label>
        {!b && !loading && !error && <button className="btn btn-primary" disabled={busy} onClick={start}>{busy ? t("启动中…") : t("启动深挖")}</button>}
        {b && DIG_RUNNING.has(state) && <button className="btn" disabled={busy} onClick={cancel}>{t("取消深挖")}</button>}
        {b && !DIG_RUNNING.has(state) && <button className="btn" disabled={busy} onClick={start}>{busy ? t("启动中…") : t("重新深挖")}</button>}
        <button className="btn" onClick={onClose}>{t("关闭")}</button>
      </>}>
      <div className="flex flex-wrap items-center gap-2 mb-2">
        {b && <Badge tone={tone}>{DIG_STATE_ZH[state] ?? state}</Badge>}
        {b && <span className="num text-xs text-faint">{tf("第 {n}/{m} 轮", { n: b.round, m: b.max_rounds })} · ${b.cost_usd.toFixed(4)} · {b.facts.length} {t("条事实")}</span>}
        {b?.seed.simulated && <SimBadge simulated />}
      </div>
      {loading && !data && <p className="text-sm text-muted">{t("加载深挖记录…")}</p>}
      {error && <div className="text-sm text-muted">{t("该公司还没有公告深挖记录。")}
        <button className="btn h-7 px-2 text-xs ml-2" disabled={busy} onClick={start}>{t("启动深挖")}</button></div>}
      {err && <p className="text-xs text-bad">{err}</p>}
      {b?.last_error && <p className="text-xs text-bad">{b.last_error}</p>}
      {b?.stop_reason && !DIG_RUNNING.has(state) && <p className="text-xs text-muted">{b.stop_reason}</p>}
      {data && data.rounds.length > 0 && (
        <ul className="flex flex-col gap-1.5 text-sm">
          {data.rounds.map((r: DigRound) => (
            <li key={r.round} className="flex flex-col gap-0.5 border-l-2 border-line pl-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="num text-xs text-faint">{tf("第 {n} 轮", { n: r.round })}</span>
                {r.plan?.action && <Badge tone="muted">{DIG_TOOL_ZH[r.plan.action] ?? r.plan.action}</Badge>}
                {r.plan?.model && <span className="text-xs text-faint">{r.plan.model}</span>}
                {r.search?.hits.length != null && <span className="text-xs text-muted">{tf("命中 {n} 段", { n: r.search.hits.length })}</span>}
                {r.search && r.search.scoped_out > 0 && <span className="text-xs text-faint">{tf("过滤别家公司 {n}", { n: r.search.scoped_out })}</span>}
                {r.annotate && !r.annotate.skipped && r.annotate.labelled > 0 && <span className="text-xs text-muted">{tf("补标注 {n} 段（已进复核队列）", { n: r.annotate.labelled })}</span>}
                {r.annotate?.skipped && <span className="text-xs text-faint">{t("标注跳过")}</span>}
                {r.summarize?.sentences.length != null && r.summarize.sentences.length > 0 && <span className="text-xs text-muted">{tf("摘要 {n} 句", { n: r.summarize.sentences.length })}</span>}
                {r.extract && !r.extract.skipped && <span className="text-xs text-muted">{tf("新增 {n} 条事实", { n: r.extract.added_facts.length })}</span>}
                {r.extract?.skipped && <span className="text-xs text-faint">{t("本轮无命中，跳过提取")}</span>}
              </div>
              {r.plan?.error && <span className="text-xs text-bad">{r.plan.error}</span>}
              {r.plan?.args?.query != null && <span className="text-xs text-muted">{tf("查询：{q}", { q: String(r.plan.args.query) })}</span>}
              {r.rewrite && r.rewrite.terms.length > 0 && <span className="text-xs text-faint">{tf("改写：{t}", { t: joinList(r.rewrite.terms) })}</span>}
              {r.rewrite?.error && <span className="text-xs text-warn">{tf("改写失败，回退原文查询：{e}", { e: r.rewrite.error })}</span>}
              {r.plan?.rationale?.text && <span className="text-xs text-muted">{r.plan.rationale.text}</span>}
              {r.search?.error && <span className="text-xs text-warn">{tf("检索不可用：{e}", { e: r.search.error })}</span>}
              {r.extract?.dropped.length ? <span className="text-xs text-faint">{tf("丢弃 {n} 条：{r}", { n: r.extract.dropped.length, r: joinList([...new Set(r.extract.dropped.map((d) => DIG_DROP_ZH[d.reason] ?? d.reason))]) })}</span> : null}
              {r.extract?.error && <span className="text-xs text-warn">{r.extract.error}</span>}
              {r.conclusion && <span className="text-xs text-accent">{r.conclusion.give_up ? t("本轮收尾：数据不足结论") : t("本轮收尾：风险画像")}</span>}
            </li>
          ))}
        </ul>
      )}
      {b?.web?.results.length ? (
        <details className="text-sm mt-2">
          <summary className="cursor-pointer text-warn">{tf("联网资料（{n} 条 · 仅作启发，来源需人工核验）", { n: b.web.results.length })}</summary>
          {b.web.warning && <p className="text-xs text-warn mt-1">{b.web.warning}</p>}
          <ul className="mt-1.5 flex flex-col gap-1">
            {b.web.results.map((r, k) => (
              <li key={k} className="text-xs flex flex-col gap-0.5 border-b border-line pb-1">
                <span><span className="num text-faint mr-1">W{k + 1}</span>
                  <span className="font-medium">{r.title}</span>
                  {r.source && <span className="text-faint ml-1">· {r.source}</span>}
                  {r.published && <span className="text-faint ml-1">{r.published}</span>}</span>
                <span className="text-muted">{r.snippet}</span>
                {r.url && <a className="text-accent break-all" href={r.url} target="_blank" rel="noreferrer">{r.url}</a>}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {b?.web?.error && !b.web.results.length && <p className="text-xs text-warn mt-2">{tf("联网检索不可用：{e}", { e: b.web.error })}</p>}
      {b && b.facts.length > 0 && (
        <details className="text-sm mt-2"><summary className="cursor-pointer text-accent">{tf("事实清单（{n} 条）", { n: b.facts.length })}</summary>
          <ul className="mt-1.5 flex flex-col gap-1">
            {b.facts.map((f) => (
              <li key={f.id} className="text-xs flex flex-col gap-0.5 border-b border-line pb-1">
                <span><span className="num text-faint mr-1">{f.id}</span>
                  <Badge tone="muted">{f.name}</Badge>
                  <Badge tone={f.direction === 1 ? "bad" : "good"}>{f.direction === 1 ? t("风险加重") : t("风险缓解")}</Badge>
                  {f.review_status === "pending" && <Badge tone="warn">{t("引用待复核段落")}</Badge>}
                  <span className="text-faint ml-1">{f.date}</span></span>
                <span className="text-muted">{f.statement}</span>
                <span className="text-faint">「{f.evidence}」 {f.refs.map((r) => `${r.title} · ${tf("第 {i} 段", { i: r.paragraph + 1 })}`).join("；")}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {b?.conclusion?.give_up && (
        <div className="text-sm mt-2 border border-line rounded p-3">
          <div className="label">{t("数据不足结论")}</div>
          <p className="text-muted mt-1">{b.conclusion.give_up.reason}</p>
          {b.conclusion.give_up.evidence_gaps.length > 0 && <p className="text-xs text-faint mt-1">{t("缺什么：")}{joinList(b.conclusion.give_up.evidence_gaps)}</p>}
          {b.conclusion.give_up.suggestion && <p className="text-xs text-faint">{t("建议：")}{b.conclusion.give_up.suggestion}</p>}
        </div>
      )}
      {b?.conclusion && !b.conclusion.give_up && (
        <div className="text-sm mt-2 flex flex-col gap-2 border border-line rounded p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="label">{t("深挖结论 · 风险画像")}</div>
            <button className="btn h-7 px-2 text-xs" disabled={busy} onClick={toIdea}>{t("转为线索")}</button>
            {b.conclusion.fallback && <Badge tone="muted">{t("结论模型不可用，按事实清单兜底")}</Badge>}
          </div>
          <p className="text-muted">{b.conclusion.summary}</p>
          {b.conclusion.groups?.length ? (
            <div className="flex flex-col gap-1">
              {b.conclusion.groups.map((g, k) => (
                <p key={k} className="text-xs text-muted"><Badge tone="muted">{g.name}</Badge> {g.summary}
                  <span className="num text-faint"> [{g.cites.join(" ")}]</span></p>
              ))}
            </div>
          ) : null}
          {b.conclusion.timeline?.length ? (
            <details><summary className="cursor-pointer text-accent text-xs">{tf("风险时间线（{n} 条）", { n: b.conclusion.timeline.length })}</summary>
              <ul className="mt-1.5 flex flex-col gap-0.5">
                {b.conclusion.timeline.map((tl, k) => (
                  <li key={k} className="text-xs text-muted"><span className="num text-faint">{tl.date}</span> {tl.statement}
                    <span className="num text-faint"> [{tl.cites.join(" ")}]</span></li>
                ))}
              </ul>
            </details>
          ) : null}
          {b.conclusion.signal_note && <p className="text-xs text-faint">{t("与文本信号对照：")}{b.conclusion.signal_note}</p>}
          {b.conclusion.web_note && <p className="text-xs text-warn">{t("外部资料要点（来源需人工核验）：")}{b.conclusion.web_note}</p>}
          {b.conclusion.follow_ups?.length ? <p className="text-xs text-muted">{t("待深挖线索：")}{joinList(b.conclusion.follow_ups)}</p> : null}
        </div>
      )}
    </Panel>
  );
}

function FilingsTab({ ov }: { ov: TextOverview }) {
  const ready = ov.datasets.filter((d) => d.ready);
  const [ds, setDs] = useState(ready[0]?.dataset ?? "fr");
  const [open, setOpen] = useState<string | null>(null);
  const [dig, setDig] = useState<{ instrument: string; company: string; filingId: string | null } | null>(null);
  const { data, error, loading } = useAsync(() => api.textFilings(ds), [ds]);
  const cur = ov.datasets.find((d) => d.dataset === ds);
  return (
    <div className="flex flex-col gap-4">
      <Seg label={t("数据集")} value={ds} onChange={(v) => { setDs(v); setOpen(null); setDig(null); }}
        options={ov.datasets.map((d) => ({ value: d.dataset, label: `${MARKET_LABEL[d.market]}${d.simulated ? t("（模拟）") : ""}` }))} />
      {cur?.evaluation && Object.keys(cur.evaluation.ic_monthly).length > 0 && (
        <Panel title={t("逐月 Rank IC")} eyebrow={t("风险表述变化 vs 未来收益 · 绿色 = 方向符合假设（负相关）")}>
          <IcChart monthly={cur.evaluation.ic_monthly} />
        </Panel>
      )}
      {open && <DocView ds={ds} id={open} onClose={() => setOpen(null)} onDig={() => {
        const f = data?.items.find((x) => x.id === open) ?? data?.excluded?.find((x) => x.id === open);
        if (f) { setOpen(null); setDig({ instrument: f.instrument, company: f.company, filingId: open }); }
      }} />}
      {dig && <DigPanel ds={ds} instrument={dig.instrument} company={dig.company} filingId={dig.filingId} onClose={() => setDig(null)} />}
      {loading ? <Loading what={t("公告列表")} /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={tf("公告与风险表述打分（{n} 篇）", { n: data.total })} eyebrow={`${data.source} · ${data.licence}`}>
          <div className="overflow-x-auto max-h-[560px]">
            <table className="w-full text-sm">
              <thead className="label text-left sticky top-0 bg-bg-2">
                <tr><th className="py-1 pr-3">{t("公司")}</th><th className="pr-3">{t("公告")}</th><th className="pr-3">{t("语言")}</th>
                  <th className="pr-3">{t("正式发布")}</th><th className="pr-3">{t("实际可获取")}</th><th className="pr-3">{t("生效交易日")}</th>
                  <th className="pr-3 text-right">{t("风险语气")}</th><th className="pr-3 text-right">{t("较上期变化")}</th><th>{t("待复核")}</th></tr>
              </thead>
              <tbody>
                {data.items.map((f: TextFiling) => (
                  <tr key={f.id} className={`border-t border-line cursor-pointer hover:bg-bg-3 ${open === f.id ? "bg-accent/10" : ""}`}
                    onClick={() => setOpen(f.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(f.id)}>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{f.company}<div className="num text-xs text-faint">{f.instrument}</div>
                      <button className="btn h-6 px-1.5 text-xs mt-0.5" onClick={(e) => { e.stopPropagation(); setDig({ instrument: f.instrument, company: f.company, filingId: null }); }}>{t("公告深挖")}</button></td>
                    <td className="pr-3 max-w-[28ch] truncate" title={f.title}>{f.title}</td>
                    <td className="pr-3">{LANG[f.language] ?? f.language}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{time(f.published_at)}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{time(f.available_at)}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{f.effective_date}{f.after_close && <span className="text-warn"> {t("顺延")}</span>}</td>
                    <td className="pr-3 num text-right">{fix(f.tone_total, 2)}</td>
                    <td className="pr-3 num text-right" style={{ color: f.change == null ? undefined : f.change > 0 ? cssVar("--bad") : cssVar("--good") }}>
                      {f.change == null ? t("首期") : `${f.change > 0 ? "+" : ""}${f.change.toFixed(2)}`}</td>
                    <td>{f.pending_review ? <Badge tone="warn">{f.pending_review}</Badge> : <span className="text-faint">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-faint mt-2">{t("风险语气 = 每百段的净风险表述（加重为正、缓解或否定为负，按置信度加权，使用复核后的结果）。点击一行查看原文、译文与引用。")}
            {data.cleaning?.table_rows.enabled && <> {tf("以数字为主的表格行不计入（分子分母都不计，本数据集共 {n} 段）。", { n: data.cleaning.table_paragraphs.toLocaleString() })}</>}</p>
        </Panel>
      )}
      {data && !!data.excluded?.length && (
        <Panel title={tf("不参与系列比较的公告（{n} 篇）", { n: data.excluded.length })} eyebrow={t("数据清理 · configs/text/pipeline.yaml series_filter")}>
          <p className="text-sm text-muted mb-2">{tf('"报告已发布 / 提供查阅"一类的通知，以及 PDF 抽不出文字的短文件，风险语气接近 0；混进同公司报告系列会制造假的"变乐观 / 变悲观"，所以不参与比较、不进入信号。规则：少于 {a} 段，或标题像通知且少于 {b} 段。', { a: data.cleaning?.series_filter.min_paragraphs ?? 0, b: data.cleaning?.series_filter.notice_max_paragraphs ?? 0 })}</p>
          <div className="overflow-x-auto max-h-[320px]">
            <table className="w-full text-sm">
              <thead className="label text-left sticky top-0 bg-bg-2"><tr><th className="py-1 pr-3">{t("公司")}</th><th className="pr-3">{t("公告")}</th><th className="pr-3">{t("正式发布")}</th><th className="pr-3 text-right">{t("段落")}</th><th>{t("原因")}</th></tr></thead>
              <tbody>
                {data.excluded.map((f) => (
                  <tr key={f.id} className="border-t border-line cursor-pointer hover:bg-bg-3" onClick={() => setOpen(f.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(f.id)}>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{f.company}<div className="num text-xs text-faint">{f.instrument}</div></td>
                    <td className="pr-3 max-w-[36ch] truncate" title={f.title}>{f.title}</td>
                    <td className="pr-3 num text-xs whitespace-nowrap">{time(f.published_at)}</td>
                    <td className="pr-3 num text-right">{f.n_paragraphs}</td>
                    <td className="text-xs text-muted">{exclReason(f.reason)}</td>
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

function SearchTab({ ov }: { ov: TextOverview }) {
  const [q, setQ] = useState(t("需求转弱"));
  const [concepts, setConcepts] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState({ q: t("需求转弱"), concepts: [] as string[] });
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
        <label className="label" htmlFor="intel-q">{t("用任意语言提问（中文 / English / Français）")}</label>
        <div className="flex flex-wrap gap-2">
          <input id="intel-q" className="field flex-1 min-w-[16rem]" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("如：demand weakened / la demande s'affaiblit / 成本压力")} />
          <button className="btn btn-primary" type="submit">{t("检索")}</button>
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          <span className="text-xs text-faint">{t("或按概念：")}</span>
          {ov.concepts.map((c) => (
            <button key={c.concept} type="button" aria-pressed={concepts.includes(c.concept)}
              className={`btn h-7 text-xs ${concepts.includes(c.concept) ? "border-accent text-accent" : ""}`}
              onClick={() => setConcepts((s) => s.includes(c.concept) ? s.filter((x) => x !== c.concept) : [...s, c.concept])}>
              {t(c.zh)}
            </button>
          ))}
        </div>
      </form>
      {data && data.results.length > 0 && (
        <Panel title={t("AI 摘要")} eyebrow={t("每句结论必须引用检索结果；没有引用的句子会被丢弃")}
          actions={<button className="btn btn-primary" disabled={sumBusy || !submitted.q} onClick={summarize}>{sumBusy ? t("生成中…") : summary ? t("重新生成") : t("生成摘要")}</button>}>
          {sumErr && <Notice tone="bad" title={t("摘要生成失败")}>{sumErr}</Notice>}
          {!summary && !sumErr && <p className="text-sm text-faint">{t("基于前 12 条检索结果，按当前界面语言概括各市场、各公司的相关表述。")}</p>}
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
                    <span className="num text-accent">[{c.n}]</span> {c.company} · {c.title} · {tf("第 {i} 段", { i: c.paragraph + 1 })} · {t("正式发布")} <span className="num">{time(c.published_at)}</span> ·{" "}
                    {t("实际可获取")} <span className="num">{time(c.available_at)}</span>{c.simulated && <span className="text-warn"> · {SIM}</span>}
                    {c.url && <> · <a className="text-accent underline" href={c.url} target="_blank" rel="noreferrer">{t("原始 PDF")}</a></>}
                  </div>
                ))}
              </div>
              <p className="text-xs text-faint">{t("模型")} {summary.model}{summary.dropped ? <> · {tf("丢弃了 {n} 句没有引用的结论", { n: summary.dropped })}</> : ""}</p>
            </div>
          )}
        </Panel>
      )}
      {loading ? <Loading what={t("检索结果")} /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={tf("{n} 条结果", { n: `${data.results.length}${data.truncated ? "+" : ""}` })}
          eyebrow={data.concepts.length ? `${t("提问映射到概念：")}${data.concepts.map((c) => `${c.name}${c.terms.length ? (IS_ZH ? `（${c.terms.join("、")}）` : ` (${c.terms.join(", ")})`) : ""}`).join(IS_ZH ? "；" : "; ")}` : t("未映射到概念，按关键词匹配原文与译文")}>
          <div className="flex flex-col divide-y divide-line">
            {data.results.map((h: TextHit, k) => (
              <div key={k} className="py-2.5 flex flex-col gap-1.5">
                <div className="flex flex-wrap gap-2 items-center">
                  <Badge tone="accent">{MARKET_LABEL[ov.datasets.find((d) => d.dataset === h.dataset)?.market ?? "fr"]}</Badge>
                  <SimBadge simulated={h.simulated} />
                  <span className="text-xs text-muted">{tf("{l}原文", { l: LANG[h.language] ?? h.language_zh })} · {t("匹配：")}{joinList(h.matched.map((m) => (m === "关键词" ? t("关键词") : m)))} · {REVIEW_ZH[h.review_status]}</span>
                </div>
                <p className="text-sm">{h.original}</p>
                {Object.entries(h.translation).map(([k2, v]) => <p key={k2} className="text-sm text-muted"><span className="text-faint">{LANG[k2]}{t("译文：")}</span>{v}</p>)}
                <ConceptChips concepts={h.concepts} />
                <Cite ds={h.dataset} simulated={h.simulated} title={h.title} company={h.company} paragraph={h.paragraph}
                  published={h.published_at} available={h.available_at} effective={h.effective_date} url={h.url} />
              </div>
            ))}
            {!data.results.length && <div className="text-sm text-faint py-3">{t("没有找到相关段落")}</div>}
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
        <Badge tone={TIER_TONE[item.tier]}>{item.sample_kind === "unscreened" ? t("漏检抽样") : TIER_LABEL[item.tier]}</Badge>
        <SimBadge simulated={item.simulated} />
        <span className="text-xs text-muted">{item.company} · {item.title} · {tf("第 {i} 段", { i: item.i + 1 })} · {LANG[item.language] ?? item.language_zh} · {time(item.published_at)}</span>
        {item.flags.map((f) => <Badge key={f.code} tone="accent">{FLAG_LABEL[f.code] ?? f.label}</Badge>)}
      </div>
      <p className="text-sm">{item.original}</p>
      {Object.entries(item.translation).map(([k, v]) => <p key={k} className="text-sm text-muted"><span className="text-faint">{LANG[k]}{t("译文：")}</span>{v}</p>)}
      {item.sample_kind === "unscreened" ? (
        <div className="text-xs text-warn">{t('这段没有命中术语表，没有送 AI 判断，平台按"不涉及风险"处理。请判断它是否其实表达了四类风险之一：是的话点"修正"标出概念（记为漏检），否则点"不涉及风险"。')}</div>
      ) : <>
        <div className="text-xs text-faint">{tf("模型判断（{m}）：", { m: item.method === "lexicon" ? t("词典法") : (item.model ?? "") })}</div>
        <ConceptChips concepts={item.model_concepts} />
      </>}
      {item.decision && <div className="text-xs text-muted">
        {t("复核记录：")}{item.history.map((h) => `${h.at.replace("T", " ")} ${h.reviewer} ${REVIEW_ZH[h.action]}${h.note ? (IS_ZH ? `（${h.note}）` : ` (${h.note})`) : ""}`).join(IS_ZH ? "；" : "; ")}
      </div>}
      {!item.decision && mode === "view" && (
        <div className="flex flex-wrap gap-2">
          {item.sample_kind !== "unscreened" && <button className="btn btn-primary" disabled={busy || !reviewer} onClick={() => send("confirm")}>{t("确认模型判断")}</button>}
          <button className="btn" disabled={busy || !reviewer} onClick={() => setMode("correct")}>{t("修正")}</button>
          <button className="btn" disabled={busy || !reviewer} onClick={() => send("reject")}>{t("不涉及风险")}</button>
          <input aria-label={t("复核理由")} className="field flex-1 min-w-[12rem]" placeholder={t("理由（可选）")} value={note} onChange={(e) => setNote(e.target.value)} />
          {!reviewer && <span className="text-sm text-warn w-full">{t('请先在上方"复核人"里填写姓名，三个按钮才能使用（复核记录要写明是谁做的）')}</span>}
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
                  {t(c.zh)}{v === undefined ? "" : v > 0 ? " " + t("↑加重") : " " + t("↓缓解/否定")}
                </button>
              );
            })}
          </div>
          <label className="label" htmlFor={`zh-${item.key}`}>{t("中文译文（可修改）")}</label>
          <textarea id={`zh-${item.key}`} className="field min-h-16" value={zh} onChange={(e) => setZh(e.target.value)} />
          <input aria-label={t("修正理由")} className="field" placeholder={t("修正理由")} value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-2">
            <button className="btn btn-primary" disabled={busy} onClick={() => send("correct")}>{t("保存修正")}</button>
            <button className="btn" onClick={() => setMode("view")}>{t("取消")}</button>
          </div>
          <p className="text-xs text-faint">{t("点击概念切换：加重 → 缓解/否定 → 不选。")}</p>
        </div>
      )}
      {err && <Notice tone="bad" title={t("提交失败")}>{err}</Notice>}
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
        <Panel key={a.dataset} eyebrow={tf("抽样质检 · {ds} · 从 {p} 段 AI 判断中固定抽取 {s} 段", { ds: a.dataset, p: a.pool.toLocaleString(), s: a.sample_size })} title={t("AI 判断准确率")}>
          <div className="grid gap-4 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="label text-left"><tr><th className="py-1 pr-3">{t("分组")}</th><th className="pr-3 text-right">{t("已复核")}</th><th className="pr-3 text-right">{t("判断正确")}</th><th className="pr-3 text-right">{t("准确率")}</th><th className="text-right">{t("95% 区间")}</th></tr></thead>
                <tbody>
                  {a.rows.length === 0 && <tr><td colSpan={5} className="py-2 text-faint">{t('还没有复核抽样段落。在下方切到"抽样质检"组开始复核。')}</td></tr>}
                  {a.rows.map((r) => (
                    <tr key={r.group} className="border-t border-line">
                      <td className="py-1.5 pr-3">{ACC_GROUP[r.group] ?? FLAG_LABEL[r.group.replace(/^flag:/, "")] ?? r.group_zh}</td><td className="pr-3 text-right num">{r.reviewed}</td><td className="pr-3 text-right num">{r.correct}</td>
                      <td className="pr-3 text-right num font-medium">{pctOrDash(r.accuracy)}</td>
                      <td className="text-right num text-muted">{r.ci95 ? `${pctOrDash(r.ci95[0])} – ${pctOrDash(r.ci95[1])}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-sm flex flex-col gap-1.5">
              <div className="label">{t("术语表漏检（没送 AI 的段落）")}</div>
              {a.screening === "full" ? <p className="text-muted">{t("当前为全文模式，所有段落都送 AI，不做漏检抽样。")}</p> : <>
                <div><span className="font-display text-xl num">{pctOrDash(a.unscreened.miss_rate)}</span>
                  <span className="text-xs text-muted ml-2">{tf("已复核 {r} / {s} 段，漏检 {m} 段", { r: a.unscreened.reviewed, s: a.unscreened.sample_size, m: a.unscreened.missed })}
                    {a.unscreened.ci95 ? <> {tf("（95% 区间 {a} – {b}）", { a: pctOrDash(a.unscreened.ci95[0]), b: pctOrDash(a.unscreened.ci95[1]) })}</> : ""}</span></div>
                <p className="text-xs text-faint">{t('漏检多时：先用"术语表扩充"让 AI 找漏掉的说法；仍不够再在正式业务中打开全文模式（configs/text/pipeline.yaml）。')}</p>
              </>}
              <p className="text-xs text-faint">{a.note}{t("。判断正确 = 确认；或判定不涉及风险而 AI 本来也没标概念；或只改译文、概念与方向不变。")}</p>
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
        <Seg label={t("分组")} value={tier} onChange={setTier} options={[
          { value: "must", label: tf("必须复核 {n}", { n: n("must") }) }, { value: "sample", label: tf("抽样质检 {n}", { n: n("sample") }) },
          { value: "impact", label: tf("高影响 {n}", { n: n("impact") }) }, { value: "other", label: tf("其他 {n}", { n: n("other") }) }, { value: "all", label: t("全部") },
        ]} />
        <Seg label={t("状态")} value={status} onChange={setStatus}
          options={[{ value: "pending", label: tf("待复核 {n}", { n: data?.counts.pending ?? "" }) }, { value: "done", label: tf("已复核 {n}", { n: data?.counts.done ?? "" }) }]} />
        <Seg label={t("数据集")} value={ds} onChange={setDs}
          options={[{ value: "", label: t("全部") }, ...ov.datasets.map((d) => ({ value: d.dataset, label: `${MARKET_LABEL[d.market]}${d.simulated ? t("（模拟）") : ""}` }))]} />
        <label className="text-sm text-muted flex items-center gap-2 whitespace-nowrap" htmlFor="intel-reviewer">{t("复核人")}
          <input id="intel-reviewer" className="field w-40" value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder={t("姓名（必填）")} />
        </label>
      </div>
      <Notice tone="accent" title={tier === "all" ? t("四个分组") : TIER_LABEL[tier]}>
        {tier === "all" ? t("必须复核 → 抽样质检 → 高影响 → 其他，按这个顺序排列。只有「必须复核」未完成会阻止提交审批；其余组看多少由你决定。复核结果只追加记录不覆盖，打分与检索使用复核后的结果。")
          : TIER_HINT[tier]}
      </Notice>
      {loading ? <Loading what={t("复核队列")} /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={status === "pending" ? t("待复核段落") : t("已复核段落")} eyebrow={tf("显示前 {n} 条", { n: data.items.length })}>
          <div className="flex flex-col divide-y divide-line">
            {data.items.map((it) => (
              <ReviewCard key={it.key} item={it} conceptsList={ov.concepts} reviewer={reviewer}
                onDone={() => { setTick((t) => t + 1); onChanged(); }} />
            ))}
            {!data.items.length && <div className="text-sm text-faint py-3">{status === "pending" ? t("没有待复核的段落") : t("没有已复核的段落")}</div>}
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
      <td className="py-2 pr-3 whitespace-nowrap">{c.concept_zh}<div className="text-xs text-faint">{LANG[c.lang] ?? c.lang} · {tf("出现 {n} 次", { n: c.count })}</div></td>
      <td className="py-2 pr-3 min-w-[12rem]">
        {c.status === "pending" ? <input aria-label={t("候选术语")} id={`gc-${c.id}`} className="field h-8" value={term} onChange={(e) => setTerm(e.target.value)} />
          : <span className="font-medium">{c.final_term ?? c.term}</span>}
        {c.examples.slice(0, 2).map((x) => <blockquote key={`${x.doc_id}-${x.i}`} className="text-xs text-faint border-l-2 border-line pl-2 mt-1">{x.text}</blockquote>)}
      </td>
      <td className="py-2 whitespace-nowrap">
        {c.status === "pending" ? (
          <div className="flex gap-1.5">
            <button className="btn btn-primary h-7 px-2 text-xs" disabled={busy || !by} onClick={() => go("approve")}>{t("批准")}</button>
            <button className="btn h-7 px-2 text-xs" disabled={busy || !by} onClick={() => go("reject")}>{t("拒绝")}</button>
          </div>
        ) : <Badge tone={c.status === "approved" ? "good" : "muted"}>{c.status === "approved" ? t("已批准") : t("已拒绝")} · {c.decided_by}</Badge>}
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
  if (!data) return <Loading what={t("候选术语")} />;
  const list = data.candidates.filter((c) => (show === "pending" ? c.status === "pending" : c.status !== "pending"));
  const last = data.runs[data.runs.length - 1];
  return (
    <Panel eyebrow={tf("术语表扩充 · 当前版本 {v} · 已批准新增 {n} 条", { v: data.glossary_version, n: data.additions.length })} title={t("AI 找到的候选术语")}
      actions={<label className="text-sm text-muted flex items-center gap-2 whitespace-nowrap" htmlFor="gc-by">{t("审批人")}<input id="gc-by" className="field h-8 w-32" value={by} onChange={(e) => setBy(e.target.value)} placeholder={t("姓名")} /></label>}>
      <p className="text-sm text-muted mb-2">{t("AI 从术语表没命中的段落里，找出其实表达了风险、但用了术语表里没有的说法，原样摘出短语作为候选。批准后加入术语表（可先修改短语），再运行补做，只判断新纳入的段落：")}
        <code className="num text-xs ml-1">python -m factopia.text understand --dataset fr --refresh</code>{t("（先显示预计花费，加 --yes 才运行）。")}</p>
      <p className="text-xs text-faint mb-3">{last ? <> {tf("最近一次：{at}，抽取 {s} 段，其中 {r} 段表达了风险，得到 {c} 个候选，花费 ${u}{stop}。", { at: time(last.at), s: last.sampled, r: last.with_risk, c: last.candidates, u: last.usd, stop: last.stopped ? (IS_ZH ? `（${last.stopped}）` : ` (${last.stopped})`) : "" })}</> : t("还没有运行过。")}
        {t("生成候选：")}<code className="num">python -m factopia.text glossary-suggest --dataset fr</code>{t("（先显示预计花费，加 --yes 才运行）。")}{t("预筛方式：")}{ov.pipeline?.screening === "full" ? t("全文") : t("术语表预筛")}</p>
      <Seg label={t("显示")} value={show} onChange={setShow} options={[
        { value: "pending", label: tf("待审批 {n}", { n: data.candidates.filter((c) => c.status === "pending").length }) },
        { value: "done", label: tf("已处理 {n}", { n: data.candidates.filter((c) => c.status !== "pending").length }) }]} />
      <div className="overflow-x-auto mt-3">
        <table className="w-full text-sm">
          <thead className="label text-left"><tr><th className="py-1 pr-3">{t("概念")}</th><th className="pr-3">{t("候选短语与原文例句")}</th><th>{t("审批")}</th></tr></thead>
          <tbody>
            {list.map((c) => <CandidateRow key={c.id} c={c} by={by.trim()} onDone={() => setTick((t) => t + 1)} />)}
            {!list.length && <tr><td colSpan={3} className="py-2 text-faint">{show === "pending" ? t("没有待审批的候选。") : t("没有已处理的候选。")}</td></tr>}
          </tbody>
        </table>
      </div>
      {!by.trim() && <p className="text-xs text-warn mt-2">{t("请先填写审批人，批准 / 拒绝按钮才能使用。")}</p>}
    </Panel>
  );
}

function GlossaryTab({ ov }: { ov: TextOverview }) {
  return (
    <div className="flex flex-col gap-4">
    <GlossaryExpansion ov={ov} />
    <Panel title={t("统一概念标签与三语术语表")} eyebrow={tf("版本 {v} · configs/text/glossary.yaml", { v: ov.glossary_version })}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left"><tr><th className="py-1 pr-3">{t("概念")}</th><th className="pr-3">{t("中文")}</th><th className="pr-3">{t("英文")}</th><th>{t("法文")}</th></tr></thead>
          <tbody>
            {ov.concepts.map((c) => (
              <tr key={c.concept} className="border-t border-line align-top">
                <td className="py-2 pr-3 whitespace-nowrap"><div className="font-medium">{c.zh}</div><div className="text-xs text-faint">{c.en} · {c.fr}</div></td>
                {(["zh", "en", "fr"] as const).map((l) => <td key={l} className="pr-3 py-2 text-xs text-muted">{joinList(c.terms[l] ?? [])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-faint mt-2">{t("术语表用于预筛段落、跨语言检索和未配置大模型时的词典法标注；每条理解结果都记录所用版本。")}
        {ov.glossary_additions?.length ? <> {tf("其中 {n} 条为人工批准的新增术语（{terms}）。", { n: ov.glossary_additions.length, terms: joinList(ov.glossary_additions.map((a) => a.term)) })}</> : ""}</p>
    </Panel>
    </div>
  );
}

export default function IntelPage() {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useAsync(() => api.textOverview(), [tick]);
  const [tab, setTab] = useState<Tab>("filings");
  if (loading && !data) return <Loading what={t("情报台")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("情报台")}</div>
          <h1 className="font-display text-xl font-semibold">{t("多语言公告与文本信号")}</h1>
        </div>
        <span className="text-sm text-muted">{t("原文优先 · 译文与概念标签另存 · 按实际可获取时间生效")}</span>
        <span className="ml-auto flex flex-wrap gap-2" title={t("只有真实数据「必须复核」组（低置信度翻译）未完成会阻止研究提交审批；其余待复核不阻止；模拟数据的复核要求豁免")}>
          <Badge tone={data.review_counts.must_real ? "bad" : "good"}>{tf("必须复核 {n}", { n: data.review_counts.must_real ?? 0 })}</Badge>
          <Badge tone="muted">{tf("真实数据其他待复核 {n}（不阻止）", { n: Math.max(0, (data.review_counts.pending_real ?? 0) - (data.review_counts.must_real ?? 0)) })}</Badge>
          {data.review_counts.pending_simulated ? <Badge tone="muted">{tf("模拟数据待复核 {n}（豁免）", { n: data.review_counts.pending_simulated })}</Badge> : null}
          <Badge tone="muted">{t("预筛：")}{data.pipeline?.screening === "full" ? t("全文") : t("术语表")}</Badge>
        </span>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {data.datasets.map((d) => <SourceCard key={d.dataset} d={d} />)}
      </div>
      {data.compare && <CompareTable rows={data.compare.markets} note={data.compare.markets.some((r) => r.simulated) ? COMPARE_NOTE : ""} horizon={data.compare.horizon} rule={JUDGE_RULE} datasets={data.datasets} />}
      <Seg label={t("视图")} value={tab} onChange={setTab} options={[
        { value: "filings", label: t("公告与打分") }, { value: "search", label: t("跨语言检索") },
        { value: "review", label: t("语言专家复核") }, { value: "glossary", label: t("术语表") },
      ]} />
      {tab === "filings" && <FilingsTab ov={data} />}
      {tab === "search" && <SearchTab ov={data} />}
      {tab === "review" && <ReviewTab ov={data} onChanged={() => setTick((t) => t + 1)} />}
      {tab === "glossary" && <GlossaryTab ov={data} />}
    </div>
  );
}
