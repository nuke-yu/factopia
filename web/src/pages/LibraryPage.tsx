import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Badge, ErrorBox, Loading, Panel, Seg } from "../components/ui";
import { api, visibleMarkets, type KnowledgeSearch, type LibraryItem } from "../api";
import { MARKET_LABEL, useAsync } from "../lib";

// 因子库与知识检索（Spec 第 14.18 节；BR-25、35）：内部因子库（Alpha158、获批信号、团队专有因子接口）
// 与跨中 / 英 / 法的语义检索（因子库、内部研究、数据字典、经批准的外部公告）

type Src = "all" | LibraryItem["source"];
const SOURCES: { value: Src; label: string }[] = [
  { value: "all", label: "全部" }, { value: "alpha158", label: "Alpha158 公开基准" },
  { value: "approved", label: "获批信号" }, { value: "proprietary", label: "团队专有" },
];
const KIND_TONE = { factor: "accent", research: "good", field: "muted", filing: "warn" } as const;
const EXAMPLES = ["波动率调整后的动量", "management risk disclosure", "risque de dépréciation", "成交量 换手"];

function tCell(t: number | null | undefined) {
  if (t == null) return <span className="text-faint">—</span>;
  const strong = Math.abs(t) > 3;
  return <span className={strong ? "text-text font-medium" : "text-muted"} style={strong ? { color: `var(--${t > 0 ? "good" : "bad"})` } : undefined}>{t.toFixed(2)}</span>;
}

function Search() {
  const [q, setQ] = useState("");
  const [llm, setLlm] = useState(true);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<KnowledgeSearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function run(text = q) {
    if (!text.trim()) return;
    setQ(text); setBusy(true); setError(null);
    try { setRes(await api.knowledge(text.trim(), llm)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <Panel eyebrow="中文 / English / Français 任意语言提问" title="知识检索">
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <input id="kq" aria-label="检索内容" className="field flex-1 min-w-[14rem]" value={q} placeholder="例：波动率调整后的动量"
          onChange={(e) => setQ(e.target.value)} />
        <label className="flex items-center gap-1.5 text-xs text-muted"><input id="kllm" type="checkbox" checked={llm} onChange={(e) => setLlm(e.target.checked)} />大模型改写提问</label>
        <button className="btn btn-primary" disabled={busy || !q.trim()}>{busy ? "检索中…" : "检索"}</button>
      </form>
      <div className="flex flex-wrap gap-1.5 mt-2 text-xs">
        {EXAMPLES.map((x) => <button key={x} type="button" className="btn h-7 px-2 text-xs" onClick={() => run(x)}>{x}</button>)}
      </div>
      {error && <div className="mt-3"><ErrorBox error={error} /></div>}
      {res && (
        <div className="mt-4 flex flex-col gap-3">
          <div className="text-xs text-muted">
            {res.method} · 检索范围：{Object.entries(res.corpus).map(([k, v]) => `${({ factor: "因子", research: "研究", field: "字段", filing: "公告" } as Record<string, string>)[k] ?? k} ${v}`).join("、")}
            {res.expansion.method === "llm" && <div className="mt-1">改写：{(["zh", "en", "fr"] as const).map((l) => (res.expansion.terms[l] ?? []).join(" / ")).filter(Boolean).join(" ｜ ")}</div>}
          </div>
          {res.results.length === 0 ? <p className="text-sm text-muted">没有找到相关内容。</p> : (
            <div className="grid gap-4 lg:grid-cols-2">
              {(["factor", "research", "field", "filing"] as const).map((k) => {
                const rs = res.results.filter((r) => r.kind === k).slice(0, 6);
                if (!rs.length) return null;
                return (
                  <div key={k}>
                    <div className="label mb-1">{rs[0].kind_zh}</div>
                    <ul className="flex flex-col divide-y divide-line">
                      {rs.map((r) => (
                        <li key={r.id} className="py-2 flex flex-col gap-0.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone={KIND_TONE[r.kind]}>{r.kind_zh}</Badge>
                            {r.kind === "research"
                              ? <Link className="text-accent text-sm" to="/projects" title={`研究项目 ${r.ref}`}>{r.title}</Link>
                              : <span className="text-sm">{r.title}</span>}
                            <span className="ml-auto num text-xs text-faint">得分 {r.score}</span>
                          </div>
                          <div className="text-xs text-muted line-clamp-2">{r.snippet}</div>
                          <div className="text-[11px] text-faint">命中：{r.matched.join("、")}</div>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

export default function LibraryPage() {
  const { data, error, loading } = useAsync(() => api.library(), []);
  const [src, setSrc] = useState<Src>("all");
  const [filter, setFilter] = useState("");
  const items = useMemo(() => (data?.items ?? []).filter((i) => (src === "all" || i.source === src)
    && (!filter || `${i.name} ${i.group ?? ""} ${i.description ?? ""}`.toLowerCase().includes(filter.toLowerCase()))), [data, src, filter]);
  if (loading) return <Loading what="因子库" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">因子库</div>
          <h1 className="font-display text-xl font-semibold">内部因子库与知识检索</h1>
        </div>
        <span className="text-sm text-muted">{Object.entries(data.summary.by_source).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>
      </div>
      <Search />
      <Panel eyebrow={`共 ${data.summary.total} 个 · 专有因子库接入：${data.summary.proprietary_dir}`} title="因子库">
        <div className="flex flex-wrap items-center gap-3 mb-3">
          <Seg label="来源" value={src} options={SOURCES} onChange={setSrc} />
          <input id="lf" aria-label="筛选因子" className="field max-w-xs" placeholder="按名称、因子组或说明筛选" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <span className="text-xs text-faint">t 值：Alpha158 为测试期之前（2015–2021）；获批信号为获批时的测试期（2022–2023）。|t| &gt; 3 着色</span>
        </div>
        <div className="overflow-x-auto max-h-[36rem] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="text-muted sticky top-0 bg-bg-2"><tr>
              <th className="text-left font-normal py-1 pr-2">因子</th>
              <th className="text-left font-normal pr-2">来源 / 状态</th>
              <th className="text-left font-normal pr-2">说明</th>
              {visibleMarkets().map((m) => <th key={m} className="text-right font-normal pr-2">{MARKET_LABEL[m]} t</th>)}
              <th className="text-left font-normal pr-2">迁移标签</th>
            </tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id} className="border-t border-line align-top">
                  <td className="py-1 pr-2 num whitespace-nowrap">{i.name}</td>
                  <td className="pr-2 whitespace-nowrap"><Badge tone={i.source === "approved" ? "good" : i.source === "proprietary" ? "accent" : "muted"}>{i.status}</Badge>
                    <div className="text-faint mt-0.5">{i.group}</div></td>
                  <td className="pr-2 min-w-[16rem]">{i.description}
                    {i.source === "approved" && <div className="text-faint mt-0.5">{i.period_note}</div>}</td>
                  {visibleMarkets().map((m) => {
                    const x = i.markets[m];
                    return <td key={m} className="pr-2 text-right num whitespace-nowrap">{tCell(x?.t)}
                      {x?.state && <div className="text-faint">{x.state}</div>}</td>;
                  })}
                  <td className="pr-2">{i.label ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
