import { useMemo, useState } from "react";
import { api, type Market, type SeedCandidate, type SeedMode, type SeedProposal, type SeedRef, type SeedsInput } from "../api";
import { useAsync } from "../lib";
import { t, tf } from "../i18n";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "./ui";

const MODES: { value: SeedMode; label: string }[] = [
  { value: "variant", label: t("单因子变体") },
  { value: "combo", label: t("多因子组合") },
  { value: "open", label: t("AI 自主审查") },
];

const SOURCE_TONE = { alpha158: "muted", approved: "good", proprietary: "accent" } as const;

/**
 * 起点因子（Spec 第 16.7 节扩展）：从因子库挑已有因子，让 AI 研究它的变体或组合。
 * 三条入口共用同一份数据：手动选因子（变体 / 组合）、不选因子由 AI 审查因子库（自主审查）、
 * 用一段自然语言描述想要的改动。AI 提出的构想必须引用因子库编号，采用后写入研究规格。
 */
export default function SeedPanel({ markets, horizon, value, onChange, onAdopt }: {
  markets: Market[];
  horizon: number;
  value?: SeedsInput | null;
  onChange: (v: SeedsInput | null) => void;
  onAdopt: (p: SeedProposal) => void;
}) {
  const cand = useAsync(() => api.seedCandidates(markets), [markets.join(",")]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ideas, setIdeas] = useState<SeedProposal[] | null>(null);
  const [meta, setMeta] = useState<{ model: string; dropped: number; at: string } | null>(null);

  const picks = value?.items ?? [];
  const mode: SeedMode = value?.mode ?? "variant";
  const brief = value?.brief ?? "";
  const picked = new Set(picks.map((p) => p.id));

  const rows = useMemo(() => {
    const items = cand.data?.items ?? [];
    const kw = q.trim().toLowerCase();
    return items.filter((x) => !kw || `${x.name} ${x.id} ${x.description}`.toLowerCase().includes(kw));
  }, [cand.data, q]);

  const write = (patch: Partial<SeedsInput>) => {
    const next: SeedsInput = { mode, items: picks, brief, proposal: value?.proposal ?? null, ...patch };
    const empty = !next.items.length && !next.brief.trim() && !next.proposal;
    onChange(empty ? null : next);
  };

  const toggle = (x: SeedCandidate) => {
    const ref: SeedRef = { id: x.id, name: x.name, source: x.source, source_zh: x.source_zh, description: x.description, formulation: x.formulation };
    write({ items: picked.has(x.id) ? picks.filter((p) => p.id !== x.id) : [...picks, ref], proposal: null });
  };

  async function propose() {
    setBusy(true); setError(null); setIdeas(null);
    try {
      const r = await api.seedPropose({ seeds: picks.map((p) => p.id), mode, brief, markets, horizon, n: 4 });
      setIdeas(r.items);
      setMeta({ model: r.model, dropped: r.dropped, at: r.at });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const canPropose = !busy && markets.length > 0 && (picks.length > 0 || mode === "open" || brief.trim().length >= 8);

  return (
    <Panel eyebrow={t("可选")} title={t("起点因子：从因子库的已有因子出发")}>
      <p className="text-sm text-muted max-w-[80ch]">
        {t("从因子库挑一个或几个已有因子，让 AI 研究它的变体（窗口、符号、标准化、条件）或组合（权重、触发、分层）。选定后会写入研究规格，AI 挖掘从这些因子出发，而不是从零开始。不选则按上面的假设自由研究。")}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="label">{t("研究方式")}</span>
        <Seg label={t("研究方式")} value={mode} options={MODES} onChange={(v) => write({ mode: v })} />
        {mode === "open" && <span className="text-xs text-faint">{t("不指定因子，由 AI 先审查因子库再决定起点")}</span>}
        {mode === "combo" && picks.length < 2 && <Badge tone="warn">{t("组合研究建议至少选 2 个因子")}</Badge>}
      </div>

      {picks.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="label">{t("已选")}</span>
          {picks.map((p) => (
            <span key={p.id} className="inline-flex items-center gap-1.5 h-7 pl-2 pr-1 rounded border border-accent/40 bg-accent/10 text-sm">
              <span className="num">{p.name}</span>
              <span className="text-[11px] text-faint">{p.source_zh}</span>
              <button type="button" aria-label={tf("移除 {name}", { name: p.name })} className="px-1 text-faint hover:text-text"
                onClick={() => write({ items: picks.filter((x) => x.id !== p.id), proposal: null })}>×</button>
            </span>
          ))}
        </div>
      )}

      <div className="mt-3">
        <label htmlFor="seed-q" className="label">{t("从因子库选择")}</label>
        <input id="seed-q" className="field mt-1" placeholder={t("按名称或说明搜索因子")} value={q} onChange={(e) => setQ(e.target.value)} />
        {cand.error ? <div className="mt-2"><ErrorBox error={cand.error} /></div>
          : cand.loading ? <Loading what={t("因子库")} />
            : (cand.data?.items.length ?? 0) === 0 ? (
              <div className="mt-2"><Notice title={t("因子库还是空的")}>
                {t("需要先有 Alpha158 评估结果（因子评估步骤）或至少一个获批信号，才能把已有因子作为起点。")}
              </Notice></div>
            ) : (
              <>
                <ul className="mt-2 max-h-56 overflow-auto border border-line rounded divide-y divide-line">
                  {rows.slice(0, 60).map((x) => (
                    <li key={x.id}>
                      <label className="flex items-start gap-2 px-2.5 py-2 cursor-pointer hover:bg-bg-3">
                        <input type="checkbox" className="mt-0.5" checked={picked.has(x.id)} onChange={() => toggle(x)} />
                        <span className="min-w-0 flex-1">
                          <span className="flex flex-wrap items-center gap-2">
                            <span className="num text-sm">{x.name}</span>
                            <Badge tone={SOURCE_TONE[x.source as keyof typeof SOURCE_TONE] ?? "muted"}>{x.source_zh}</Badge>
                            {x.label && <span className="text-[11px] text-faint">{x.label}</span>}
                            {!!x.approved_markets?.length && <span className="text-[11px] text-faint">{tf("已获批：{m}", { m: x.approved_markets.join(" / ").toUpperCase() })}</span>}
                          </span>
                          {!!x.description && <span className="block text-xs text-muted mt-0.5">{x.description}</span>}
                          {!!x.formulation && <span className="block num text-[11px] text-faint mt-0.5 break-all">{x.formulation}</span>}
                        </span>
                      </label>
                    </li>
                  ))}
                  {rows.length === 0 && <li className="px-2.5 py-3 text-sm text-muted">{t("没有匹配的因子")}</li>}
                </ul>
                <p className="text-xs text-faint mt-1">{tf("显示 {a} / 共 {b} 个。", { a: Math.min(rows.length, 60), b: cand.data?.items.length ?? 0 })}
                  {cand.data?.note ? ` ${cand.data.note}` : ""}</p>
              </>
            )}
      </div>

      <div className="mt-3">
        <label htmlFor="seed-brief" className="label">{t("用一段话描述你想要的变体或组合（可选）")}</label>
        <textarea id="seed-brief" className="field mt-1 min-h-20 leading-relaxed" value={brief}
          placeholder={t("例如：把 20 日动量窗口改成 5 / 10 / 40 日，并做行业中性化，看短窗口在港股是否更有效")}
          onChange={(e) => write({ brief: e.target.value })} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="button" className="btn h-9 px-3" disabled={!canPropose} onClick={propose}>
          {busy ? t("AI 构想中…") : t("让 AI 提构想 →")}
        </button>
        <span className="text-xs text-faint">{t("AI 只引用因子库中真实存在的因子；引用不存在的编号的构想会被丢弃。")}</span>
        {!!value?.proposal && <Badge tone="accent">{t("已采用一个 AI 构想")}</Badge>}
      </div>
      {error && <div className="mt-3"><ErrorBox error={error} /></div>}
      {busy && <p className="text-xs text-muted mt-2">{t("正在审查因子库并生成构想，通常需要 10–40 秒。")}</p>}

      {ideas && (
        <div className="mt-4 flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="label">{tf("AI 构想（{n} 个）", { n: ideas.length })}</span>
            {meta && <span className="text-xs text-faint num">
              {tf("{model} · 丢弃 {d} 个不合规", { model: meta.model, d: meta.dropped })}</span>}
          </div>
          {ideas.map((p, i) => (
            <article key={i} className="border border-line rounded p-3 flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={p.kind === "combo" ? "accent" : "muted"}>{p.kind_zh}</Badge>
                <span className="font-medium">{p.title_zh}</span>
                <button type="button" className="btn h-7 px-2 text-xs ml-auto" onClick={() => onAdopt(p)}>{t("采用这个构想")}</button>
              </div>
              <p className="text-sm">{p.hypothesis_zh}</p>
              <dl className="grid sm:grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <div className="flex gap-2"><dt className="text-faint shrink-0">{t("改动")}</dt><dd>{p.change || "—"}</dd></div>
                <div className="flex gap-2"><dt className="text-faint shrink-0">{t("风险")}</dt><dd>{p.risk_zh || "—"}</dd></div>
              </dl>
              {!!p.logic_zh && <p className="text-xs text-muted">{t("经济逻辑：")}{p.logic_zh}</p>}
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-faint">{t("引用因子：")}</span>
                {p.seed_detail.map((s) => <Badge key={s.id} tone="muted">{s.name}</Badge>)}
              </div>
            </article>
          ))}
        </div>
      )}
    </Panel>
  );
}
