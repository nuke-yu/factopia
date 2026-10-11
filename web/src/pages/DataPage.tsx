import { Fragment, useMemo, useState } from "react";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import { api, visibleMarkets, type Market, type MarketRule, type Relation, type Security } from "../api";
import { MARKET_LABEL, marketColor, SHOW_FORMULAS, useAsync } from "../lib";

// 数据台（Spec 第 14.10 节；BR-13、16、17、18）：证券主数据与跨市场关联、公司行动、口径与规则、时间点一致性

type Tab = "pit" | "securities" | "relations" | "actions" | "dictionary";
const VENUE: Record<string, string> = {
  HK: "香港", LN: "伦敦", FP: "巴黎", NA: "阿姆斯特丹", US: "美国", CG: "上海", CS: "深圳", SP: "新加坡", SE: "瑞士",
  BB: "布鲁塞尔", IM: "米兰", SM: "马德里", AT: "澳大利亚", JT: "东京",
};

function PitPanel() {
  const { data, error, loading } = useAsync(() => api.pit(), []);
  if (loading) return <Loading what="时间点一致性" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  return (
    <Panel title={`时间点一致性：${data.label}`} eyebrow="每个数据域的业务日期、正式发布时间、实际可获取时间">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left">
            <tr><th className="py-1 pr-3">数据域</th><th className="pr-3">用于回测</th><th className="pr-3">业务日期</th>
              <th className="pr-3">正式发布</th><th className="pr-3">实际可获取</th><th className="pr-3">状态</th><th>说明</th></tr>
          </thead>
          <tbody>
            {data.domains.map((d) => (
              <tr key={d.key} className="border-t border-line align-top">
                <td className="py-2 pr-3 whitespace-nowrap font-medium">{d.name}</td>
                <td className="pr-3">{d.used_in_backtest ? "是" : <span className="text-faint">否</span>}</td>
                <td className="pr-3 text-muted">{d.business_date}</td>
                <td className="pr-3 text-muted">{d.published_at}</td>
                <td className="pr-3 text-muted">{d.available_at}</td>
                <td className="pr-3">{d.status === "ok" ? <Badge tone="good">一致</Badge> : <Badge tone="warn">不一致</Badge>}</td>
                <td className="text-xs text-muted max-w-[52ch]">{d.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-faint mt-2">
        股票池：{Object.entries(data.membership).map(([m, v]) => `${MARKET_LABEL[m as Market]} ${v}`).join("、")}。
        成分股起止日期在 configs/universe/membership/，取得数据商的历史成分股后按同一格式替换，Qlib 股票池随之按日生效。
      </p>
    </Panel>
  );
}

function SecurityDetail({ s, rel, all, onClose }: { s: Security; rel: Relation[]; all: Security[]; onClose: () => void }) {
  const siblings = all.filter((x) => x.issuer_id === s.issuer_id && x.symbol !== s.symbol);
  const rows: [string, string | null | undefined][] = [
    ["法定名称（GLEIF）", s.legal_name], ["LEI", s.lei ? `${s.lei}（${s.lei_method ?? ""}）` : "未匹配"],
    ["注册地", s.jurisdiction], ["总部国家", s.hq_country], ["FIGI / 股份类别 FIGI", `${s.figi ?? "—"} / ${s.share_class_figi ?? "—"}`],
    ["ISIN", s.isin ?? "仅法国有（AMF）；其他市场需数据商"], ["证券类型", s.security_type],
    ["行业（Morningstar 口径）", [s.sector, s.industry].filter(Boolean).join(" · ")],
    ["财务年度结束月", s.fiscal_year_end_month ? `${s.fiscal_year_end_month} 月` : null],
    ["财务报告币种 / 交易币种", `${s.reporting_currency ?? "—"} / ${s.currency}`],
    ["会计口径", `${s.accounting}（${s.accounting_basis}）`], ["报告周期", s.reporting],
    ["主要上市地", s.primary_listing ? `${VENUE[s.primary_listing] ?? s.primary_listing}（${s.primary_listing_basis}）` : `待确认（${s.primary_listing_basis}）`],
    ["快照时间", s.snapshot_at],
  ];
  return (
    <Panel title={`${s.long_name ?? s.name}（${s.symbol}）`} eyebrow={`${MARKET_LABEL[s.market]} · ${s.exchange}`}
      actions={<button className="btn" onClick={onClose}>关闭</button>}>
      <div className="grid gap-4 lg:grid-cols-2">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          {rows.map(([k, v]) => (<Fragment key={k}><dt className="text-faint">{k}</dt><dd className="num break-all">{v || "—"}</dd></Fragment>))}
        </dl>
        <div className="flex flex-col gap-2">
          <div className="label">同一发行人的其他证券</div>
          {siblings.length === 0 && rel.length === 0 && <p className="text-sm text-faint">未发现其他市场的挂牌或存托凭证</p>}
          {siblings.map((x) => (
            <div key={x.symbol} className="text-sm flex flex-wrap gap-2 items-center">
              <Badge tone="accent">本平台 · {MARKET_LABEL[x.market]}</Badge><span className="num">{x.symbol}</span>
              <span className="text-muted">{x.share_class_figi === s.share_class_figi ? "同一股份多地上市（股份类别 FIGI 一致）" : "同一发行人的其他股份类别"}</span>
            </div>
          ))}
          {rel.filter((r) => !r.in_universe).map((r) => (
            <div key={`${r.venue}${r.ticker}`} className="text-sm flex flex-wrap gap-2 items-center">
              <Badge tone={r.relation === "depositary_receipt" ? "warn" : "muted"}>{r.venue_zh ?? r.venue}</Badge>
              <span className="num">{r.ticker}</span><span className="text-muted">{r.name}</span>
              <span>{r.relation_zh}</span>{r.note && <span className="text-xs text-faint">{r.note}</span>}
              <span className="text-xs text-faint">· {r.status}</span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

function SecuritiesTab({ relationsOnly }: { relationsOnly: boolean }) {
  const [market, setMarket] = useState<Market | "all">("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, loading } = useAsync(() => api.securities(), []);
  const relBy = useMemo(() => {
    const m = new Map<string, Relation[]>();
    for (const r of data?.relations ?? []) m.set(r.issuer_id, [...(m.get(r.issuer_id) ?? []), r]);
    return m;
  }, [data]);
  if (loading) return <Loading what="证券主数据" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const ql = q.trim().toLowerCase();
  const rows = data.securities.filter((s) => (market === "all" || s.market === market)
    && (!ql || `${s.symbol} ${s.name} ${s.long_name ?? ""} ${s.legal_name ?? ""}`.toLowerCase().includes(ql))
    && (!relationsOnly || s.multi_market || (relBy.get(s.issuer_id) ?? []).some((r) => !r.in_universe)));
  const cur = data.securities.find((s) => s.symbol === open);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-3 items-center">
        <Seg label="市场" value={market} onChange={setMarket}
          options={[{ value: "all", label: "全部" }, ...visibleMarkets().map((m) => ({ value: m, label: MARKET_LABEL[m] }))]} />
        <input aria-label="搜索证券" className="field max-w-xs" placeholder="代码或名称" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="text-sm text-muted">{rows.length} 只</span>
      </div>
      {cur && <SecurityDetail s={cur} rel={relBy.get(cur.issuer_id) ?? []} all={data.securities} onClose={() => setOpen(null)} />}
      <Panel title={relationsOnly ? "有跨市场关联的证券" : "证券主数据"} eyebrow="点击一行查看发行人与其他市场的挂牌">
        <div className="overflow-x-auto max-h-[600px]">
          <table className="w-full text-sm">
            <thead className="label text-left sticky top-0 bg-bg-2">
              <tr><th className="py-1 pr-3">代码</th><th className="pr-3">名称</th><th className="pr-3">法律实体 / 注册地</th>
                <th className="pr-3">行业</th><th className="pr-3">报告币种</th><th className="pr-3">年结</th><th className="pr-3">主要上市地</th><th>跨市场关联</th></tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const rel = (relBy.get(s.issuer_id) ?? []).filter((r) => !r.in_universe);
                return (
                  <tr key={s.symbol} className={`border-t border-line cursor-pointer hover:bg-bg-3 ${open === s.symbol ? "bg-accent/10" : ""}`}
                    onClick={() => setOpen(s.symbol)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(s.symbol)}>
                    <td className="py-1.5 pr-3 num whitespace-nowrap" style={{ color: marketColor(s.market) }}>{s.symbol}</td>
                    <td className="pr-3 max-w-[24ch] truncate" title={s.long_name ?? s.name}>{s.long_name ?? s.name}</td>
                    <td className="pr-3 text-xs">{s.lei ? <>{s.legal_name}<span className="text-faint"> · {s.jurisdiction}</span></> : <span className="text-warn">LEI 未匹配</span>}</td>
                    <td className="pr-3 text-xs text-muted">{s.industry ?? "—"}</td>
                    <td className="pr-3 num">{s.reporting_currency ?? "—"}{s.reporting_currency && s.reporting_currency !== s.currency && <span className="text-warn"> ≠{s.currency}</span>}</td>
                    <td className="pr-3 num">{s.fiscal_year_end_month ?? "—"}</td>
                    <td className="pr-3 text-xs">{s.primary_listing ? `${VENUE[s.primary_listing] ?? s.primary_listing}${s.is_primary ? "（本地）" : ""}` : <span className="text-faint">待确认</span>}</td>
                    <td className="text-xs">
                      <span className="inline-flex flex-wrap gap-1">
                        {s.multi_market && <Badge tone="accent">本平台多市场</Badge>}
                        {rel.slice(0, 4).map((r) => <Badge key={`${r.venue}${r.ticker}`} tone={r.relation === "depositary_receipt" ? "warn" : "muted"}>{r.venue_zh ?? r.venue} {r.ticker}</Badge>)}
                        {rel.length > 4 && <span className="text-faint">+{rel.length - 4}</span>}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-faint mt-2">来源：GLEIF（法律实体，CC0）、OpenFIGI（证券标识）、Yahoo Finance（行业与财务年度，当前快照）。跨市场关联为自动匹配，待数据负责人确认（configs/refdata/overrides.yaml）。</p>
      </Panel>
    </div>
  );
}

function ActionsTab() {
  const [type, setType] = useState("");
  const [symbol, setSymbol] = useState("");
  const [sym, setSym] = useState("");
  const { data, error, loading } = useAsync(() => api.actions(sym || undefined, type || undefined), [sym, type]);
  const sum = useAsync(() => api.refSummary(), []);
  const a = sum.data?.actions;
  return (
    <div className="flex flex-col gap-4">
      {a && (
        <div className="panel p-4 grid gap-4 sm:grid-cols-4">
          <Stat label="分红" value={a.by_type.dividend ?? 0} />
          <Stat label="拆股 / 合股" value={a.by_type.split ?? 0} sub={`复权检查 ${a.adjustment_checks.length - a.adjustment_failures.length}/${a.adjustment_checks.length} 通过`} />
          <Stat label="代码变更" value={a.by_type.ticker_change ?? 0} sub="AMF 历史记录（法国）" />
          <Stat label="疑似停牌" value={a.by_type.suspension ?? 0} sub="连续 ≥3 个交易日无成交" />
        </div>
      )}
      {a && a.not_covered.length > 0 && <Notice tone="warn" title="尚未覆盖">{a.not_covered.join("、")}：免费数据源没有可靠记录，需数据商。
        导入接口已就绪：按 {sum.data?.vendor_templates ?? "configs/refdata/vendor_templates/README.md"} 的格式导出后运行 python -m factopia.refdata import-actions / import-membership。</Notice>}
      {(sum.data?.vendor_imports ?? []).length > 0 && <Notice tone="accent" title="数据商导入记录">
        {(sum.data?.vendor_imports ?? []).slice(-5).reverse().map((v, i) => <div key={i}>{v.at.slice(0, 10)} · {v.kind === "membership" ? `历史成分股（${v.market}）` : "公司行动"} · {v.vendor} · {v.rows} 行</div>)}
      </Notice>}
      <div className="flex flex-wrap gap-3 items-center">
        <Seg label="类型" value={type} onChange={setType} options={[{ value: "", label: "全部" }, { value: "dividend", label: "分红" },
          { value: "split", label: "拆股" }, { value: "ticker_change", label: "代码变更" }, { value: "suspension", label: "疑似停牌" }]} />
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); setSym(symbol.trim()); }}>
          <input aria-label="证券代码" className="field w-40" placeholder="如 0005.HK" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          <button className="btn" type="submit">筛选</button>
        </form>
      </div>
      {loading ? <Loading what="公司行动" /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={`公司行动（${data.total} 条，显示最近 ${data.items.length} 条）`} eyebrow="行情为复权价；本表用于展示、核对与复权检查">
          <div className="overflow-x-auto max-h-[560px]">
            <table className="w-full text-sm">
              <thead className="label text-left sticky top-0 bg-bg-2"><tr><th className="py-1 pr-3">日期</th><th className="pr-3">代码</th><th className="pr-3">类型</th><th className="pr-3">内容</th><th>来源</th></tr></thead>
              <tbody>
                {data.items.map((x, k) => (
                  <tr key={k} className="border-t border-line">
                    <td className="py-1.5 pr-3 num whitespace-nowrap">{x.date}</td>
                    <td className="pr-3 num" style={{ color: marketColor(x.market) }}>{x.symbol}</td>
                    <td className="pr-3"><Badge tone={x.type === "suspension" ? "warn" : x.type === "split" ? "accent" : "muted"}>{x.type_zh}</Badge></td>
                    <td className="pr-3 text-muted">{x.detail}</td>
                    <td className="text-xs text-faint">{x.source}</td>
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

// 口径与规则（BR-17）：市场规则（markets.yaml）与字段口径（fields.yaml）；开发阶段显示公式行（SHOW_FORMULAS）
const RULE_ROWS: [keyof MarketRule, string][] = [
  ["exchange", "交易所"], ["timezone", "时区"], ["currency", "本地币种"], ["benchmark", "基准指数"], ["trading_hours", "交易时段"],
  ["close_local", "收盘（当地）"], ["eod_delay_min", "日终数据延迟"], ["price_limits", "涨跌停"], ["settlement", "交收"],
  ["short_selling", "卖空"], ["lot_size", "每手股数"], ["reporting", "报告周期"], ["accounting_default", "会计口径（默认）"],
  ["disclosure_language", "披露语言"], ["holidays", "假期与日历"],
];

const addMin = (hhmm: string, min: number) => {
  const [h, m] = hhmm.split(":").map(Number), t = h * 60 + m + min;
  return `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};

function FormulaLine({ children }: { children: React.ReactNode }) {
  return <div className="text-xs text-muted border-l-2 border-line pl-2 mt-1 num">{children}</div>;
}

function DictionaryTab() {
  const { data, error, loading } = useAsync(() => api.dictionary(), []);
  if (loading) return <Loading what="口径与规则" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const ms = Object.keys(data.markets.markets) as Market[];
  const groups = [...new Set(data.fields.fields.map((f) => f.group ?? "其他"))];
  const formulaRow = (key: keyof MarketRule) => {
    if (!SHOW_FORMULAS) return null;
    if (key === "eod_delay_min") return ms.map((m) => {
      const c = data.markets.markets[m];
      return <FormulaLine key={m}>行情可获取 = {c.close_local} + {c.eod_delay_min} 分钟 = {addMin(c.close_local, c.eod_delay_min)}（当地）</FormulaLine>;
    });
    if (key === "close_local") return ms.map((m) => {
      const c = data.markets.markets[m], d = data.text_delay_min?.[m] ?? 60;
      return <FormulaLine key={m}>公告当天生效 ⇔ 发布时间 + {d} 分钟 ≤ {c.close_local}，即发布不晚于 {addMin(c.close_local, -d)}；否则顺延到下一个交易日</FormulaLine>;
    });
    return null;
  };
  return (
    <div className="flex flex-col gap-4">
      {SHOW_FORMULAS && <Notice tone="accent" title="开发阶段公式说明">
        灰色竖线开头的行是计算方法，并代入了实际参数；用于核对每个口径如何得出，进入 MVP 时隐藏。
      </Notice>}
      <Panel title="市场规则" eyebrow={`版本 ${data.markets.version} · configs/refdata/markets.yaml · 数据负责人维护`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3 w-32">规则</th>
              {ms.map((m) => <th key={m} className="pr-3" style={{ color: marketColor(m) }}>{data.markets.markets[m].name}</th>)}</tr></thead>
            <tbody>
              {RULE_ROWS.map(([key, label]) => {
                const f = formulaRow(key);
                return (
                  <tr key={key} className="border-t border-line align-top">
                    <td className="py-2 pr-3 text-xs text-faint whitespace-nowrap">{label}</td>
                    {ms.map((m, k) => {
                      const v = data.markets.markets[m][key];
                      return <td key={m} className="py-2 pr-3 text-xs">
                        <span className={key === "close_local" || key === "eod_delay_min" ? "num" : ""}>
                          {v == null || v === "" ? "—" : key === "eod_delay_min" ? `${v} 分钟` : String(v)}</span>
                        {f && f[k]}
                      </td>;
                    })}
                  </tr>
                );
              })}
              <tr className="border-t border-line align-top">
                <td className="py-2 pr-3 text-xs text-faint whitespace-nowrap">股票池成分股</td>
                {ms.map((m) => {
                  const mem = data.membership[m];
                  return <td key={m} className="py-2 pr-3 text-xs">
                    {mem ? (mem.point_in_time ? <Badge tone="good">历史成分股</Badge> : <Badge tone="warn">当前成分股 {mem.rows}</Badge>) : "—"}
                    {SHOW_FORMULAS && mem && <FormulaLine>
                      来源：{Object.entries(mem.sources).map(([k, v]) => `${k} ${v} 行`).join("、")}；时间点一致 ⇔ 没有来源为 current_list 的行 → {mem.point_in_time ? "一致" : "不一致"}
                    </FormulaLine>}
                  </td>;
                })}
              </tr>
            </tbody>
          </table>
        </div>
        <div className="grid gap-4 md:grid-cols-2 mt-4">
          {data.markets.accounting_by_jurisdiction && <div>
            <div className="label mb-1">按注册地推断会计口径（覆盖市场默认值）</div>
            <table className="w-full text-xs"><tbody>
              {Object.entries(data.markets.accounting_by_jurisdiction).map(([k, v]) => (
                <tr key={k} className="border-t border-line"><td className="py-1 pr-3 num text-faint">{k}</td><td>{v}</td></tr>))}
            </tbody></table>
          </div>}
          {data.markets.cross_listing_venues && <div>
            <div className="label mb-1">跨市场挂牌只检索这些交易所</div>
            <div className="flex flex-wrap gap-1">
              {Object.entries(data.markets.cross_listing_venues).map(([k, v]) => <Badge key={k} tone="muted">{v} {k}</Badge>)}
            </div>
            <div className="text-xs text-faint mt-1">德国等二级报价场所不计；名称规范化后一致才采用，标为"自动匹配，待确认"</div>
          </div>}
        </div>
      </Panel>
      <Panel title="字段口径" eyebrow={`版本 ${data.fields.version} · configs/refdata/fields.yaml · ${data.fields.fields.length} 个字段`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">字段</th><th className="pr-3">单位</th><th className="pr-3">来源</th>
              <th className="pr-3">复权 / 调整</th><th className="pr-3">可用时间</th><th>说明</th></tr></thead>
            <tbody>
              {groups.map((g) => (
                <Fragment key={g}>
                  <tr><td colSpan={6} className="pt-4 pb-1 label">{g}</td></tr>
                  {data.fields.fields.filter((f) => (f.group ?? "其他") === g).map((f) => (
                    <Fragment key={f.name}>
                      <tr className="border-t border-line align-top">
                        <td className="py-2 pr-3 whitespace-nowrap"><span className="num">{f.name}</span><div className="text-xs text-faint">{f.zh}</div></td>
                        <td className="pr-3 text-xs">{f.unit}</td><td className="pr-3 text-xs text-muted">{f.source}</td>
                        <td className="pr-3 text-xs text-muted">{f.adjustment}</td><td className="pr-3 text-xs text-muted">{f.available}</td>
                        <td className="text-xs text-faint">{f.note ?? ""}</td>
                      </tr>
                      {SHOW_FORMULAS && f.formula && <tr><td colSpan={6} className="pb-2"><FormulaLine>{f.formula}</FormulaLine></td></tr>}
                    </Fragment>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

export default function DataPage() {
  const { data, error, loading } = useAsync(() => api.refSummary(), []);
  const [tab, setTab] = useState<Tab>("pit");
  if (loading) return <Loading what="数据台" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const m = data.master;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">数据台</div>
          <h1 className="font-display text-xl font-semibold">参考数据与时间点一致性</h1>
        </div>
        <span className="text-sm text-muted">发行人与证券 · 公司行动 · 口径与规则 · 三个时间</span>
        {data.pit && <span className="ml-auto"><Badge tone={data.pit.point_in_time ? "good" : "warn"}>{data.pit.label}</Badge></span>}
      </div>
      {m ? (
        <div className="panel p-4 grid gap-4 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="证券" value={m.securities} sub={`${m.issuers} 个发行人`} />
          <Stat label="LEI 覆盖" value={`${m.with_lei}/${m.securities}`} sub="GLEIF 法律实体" />
          <Stat label="FIGI 覆盖" value={`${m.with_figi}/${m.securities}`} sub={`ISIN ${m.with_isin}（仅法国）`} />
          <Stat label="本平台多市场发行人" value={m.multi_market_issuers} sub="如汇丰（香港、伦敦）" />
          <Stat label="其他市场挂牌" value={(m.relations.same_share_class ?? 0) + (m.relations.other_share_class ?? 0)} sub="同股多地 / 其他股份类别" />
          <Stat label="存托凭证" value={m.relations.depositary_receipt ?? 0} sub={`主要上市地待确认 ${m.primary_unresolved}`} />
        </div>
      ) : <Notice tone="warn" title="参考数据尚未生成">运行 python -m factopia.refdata all</Notice>}
      <Seg label="视图" value={tab} onChange={setTab} options={[
        { value: "pit", label: "时间点一致性" }, { value: "securities", label: "证券主数据" }, { value: "relations", label: "跨市场关联" },
        { value: "actions", label: "公司行动" }, { value: "dictionary", label: "口径与规则" },
      ]} />
      {tab === "pit" && <PitPanel />}
      {tab === "securities" && <SecuritiesTab relationsOnly={false} />}
      {tab === "relations" && <SecuritiesTab relationsOnly />}
      {tab === "actions" && <ActionsTab />}
      {tab === "dictionary" && <DictionaryTab />}
    </div>
  );
}
