import { Fragment, useMemo, useState } from "react";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import { api, MARKETS, type Market, type Relation, type Security } from "../api";
import { MARKET_LABEL, marketColor, useAsync } from "../lib";

// 数据台（Spec 第 14.10 节；BR-13、16、17、18）：证券主数据与跨市场关联、公司行动、数据字典、时间点一致性

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
          options={[{ value: "all", label: "全部" }, ...MARKETS.map((m) => ({ value: m, label: MARKET_LABEL[m] }))]} />
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
      {a && <Notice tone="warn" title="尚未覆盖">{a.not_covered.join("、")}：免费数据源没有可靠记录，需数据商；当前股票池为现有成分股，没有退市样本。</Notice>}
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

function DictionaryTab() {
  const { data, error, loading } = useAsync(() => api.dictionary(), []);
  if (loading) return <Loading what="数据字典" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  return (
    <div className="flex flex-col gap-4">
      <Panel title="市场规则" eyebrow={`版本 ${data.markets.version} · configs/refdata/markets.yaml`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">市场</th><th className="pr-3">交易所 / 时区</th><th className="pr-3">收盘</th>
              <th className="pr-3">日终延迟</th><th className="pr-3">报告周期</th><th className="pr-3">会计口径（默认）</th><th>成分股</th></tr></thead>
            <tbody>
              {(Object.keys(data.markets.markets) as Market[]).map((m) => {
                const c = data.markets.markets[m], mem = data.membership[m];
                return (
                  <tr key={m} className="border-t border-line align-top">
                    <td className="py-2 pr-3 font-medium" style={{ color: marketColor(m) }}>{c.name}</td>
                    <td className="pr-3 text-xs">{c.exchange}<div className="text-faint">{c.timezone} · {c.currency}</div></td>
                    <td className="pr-3 num">{c.close_local}</td>
                    <td className="pr-3 num">{c.eod_delay_min} 分钟</td>
                    <td className="pr-3 text-xs text-muted">{c.reporting}</td>
                    <td className="pr-3 text-xs text-muted">{c.accounting_default}</td>
                    <td className="text-xs">{mem ? (mem.point_in_time ? <Badge tone="good">历史成分股</Badge> : <Badge tone="warn">当前成分股 {mem.rows}</Badge>) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="字段定义" eyebrow={`版本 ${data.fields.version} · configs/refdata/fields.yaml`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">字段</th><th className="pr-3">单位</th><th className="pr-3">来源</th>
              <th className="pr-3">复权 / 调整</th><th className="pr-3">可用时间</th><th>说明</th></tr></thead>
            <tbody>
              {data.fields.fields.map((f) => (
                <tr key={f.name} className="border-t border-line align-top">
                  <td className="py-2 pr-3 whitespace-nowrap"><span className="num">{f.name}</span><div className="text-xs text-faint">{f.zh}</div></td>
                  <td className="pr-3 text-xs">{f.unit}</td><td className="pr-3 text-xs text-muted">{f.source}</td>
                  <td className="pr-3 text-xs text-muted">{f.adjustment}</td><td className="pr-3 text-xs text-muted">{f.available}</td>
                  <td className="text-xs text-faint">{f.note ?? ""}</td>
                </tr>
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
        <span className="text-sm text-muted">发行人与证券 · 公司行动 · 数据字典 · 三个时间</span>
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
        { value: "actions", label: "公司行动" }, { value: "dictionary", label: "数据字典" },
      ]} />
      {tab === "pit" && <PitPanel />}
      {tab === "securities" && <SecuritiesTab relationsOnly={false} />}
      {tab === "relations" && <SecuritiesTab relationsOnly />}
      {tab === "actions" && <ActionsTab />}
      {tab === "dictionary" && <DictionaryTab />}
    </div>
  );
}
