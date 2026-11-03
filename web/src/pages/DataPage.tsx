import { Fragment, useMemo, useState } from "react";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import { api, visibleMarkets, type Market, type MarketRule, type Relation, type Security } from "../api";
import { MARKET_LABEL, marketColor, SHOW_FORMULAS, useAsync } from "../lib";
import { t, tf, joinList } from "../i18n";

// 数据台（Spec 第 14.10 节；BR-13、16、17、18）：证券主数据与跨市场关联、公司行动、口径与规则、时间点一致性

type Tab = "pit" | "securities" | "relations" | "actions" | "dictionary";
const VENUE: Record<string, string> = {
  HK: t("香港"), LN: t("伦敦"), FP: t("巴黎"), NA: t("阿姆斯特丹"), US: t("美国"), CG: t("上海"), CS: t("深圳"), SP: t("新加坡"), SE: t("瑞士"),
  BB: t("布鲁塞尔"), IM: t("米兰"), SM: t("马德里"), AT: t("澳大利亚"), JT: t("东京"),
};

function PitPanel() {
  const { data, error, loading } = useAsync(() => api.pit(), []);
  if (loading) return <Loading what={t("时间点一致性")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  return (
    <Panel title={tf("时间点一致性：{l}", { l: data.label })} eyebrow={t("每个数据域的业务日期、正式发布时间、实际可获取时间")}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="label text-left">
            <tr><th className="py-1 pr-3">{t("数据域")}</th><th className="pr-3">{t("用于回测")}</th><th className="pr-3">{t("业务日期")}</th>
              <th className="pr-3">{t("正式发布")}</th><th className="pr-3">{t("实际可获取")}</th><th className="pr-3">{t("状态")}</th><th>{t("说明")}</th></tr>
          </thead>
          <tbody>
            {data.domains.map((d) => (
              <tr key={d.key} className="border-t border-line align-top">
                <td className="py-2 pr-3 whitespace-nowrap font-medium">{d.name}</td>
                <td className="pr-3">{d.used_in_backtest ? t("是") : <span className="text-faint">{t("否")}</span>}</td>
                <td className="pr-3 text-muted">{d.business_date}</td>
                <td className="pr-3 text-muted">{d.published_at}</td>
                <td className="pr-3 text-muted">{d.available_at}</td>
                <td className="pr-3">{d.status === "ok" ? <Badge tone="good">{t("一致")}</Badge> : <Badge tone="warn">{t("不一致")}</Badge>}</td>
                <td className="text-xs text-muted max-w-[52ch]">{d.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-faint mt-2">
        {tf("股票池：{m}。 成分股起止日期在 configs/universe/membership/，取得数据商的历史成分股后按同一格式替换，Qlib 股票池随之按日生效。", { m: joinList(Object.entries(data.membership).map(([m, v]) => `${MARKET_LABEL[m as Market]} ${v}`)) })}
      </p>
    </Panel>
  );
}

function SecurityDetail({ s, rel, all, onClose }: { s: Security; rel: Relation[]; all: Security[]; onClose: () => void }) {
  const siblings = all.filter((x) => x.issuer_id === s.issuer_id && x.symbol !== s.symbol);
  const rows: [string, string | null | undefined][] = [
    [t("法定名称（GLEIF）"), s.legal_name], [t("LEI"), s.lei ? tf("{l}（{m}）", { l: s.lei, m: s.lei_method ?? "" }) : t("未匹配")],
    [t("注册地"), s.jurisdiction], [t("总部国家"), s.hq_country], [t("FIGI / 股份类别 FIGI"), `${s.figi ?? "—"} / ${s.share_class_figi ?? "—"}`],
    [t("ISIN"), s.isin ?? t("仅法国有（AMF）；其他市场需数据商")], [t("证券类型"), s.security_type],
    [t("行业（Morningstar 口径）"), [s.sector, s.industry].filter(Boolean).join(" · ")],
    [t("财务年度结束月"), s.fiscal_year_end_month ? tf("{n} 月", { n: s.fiscal_year_end_month }) : null],
    [t("财务报告币种 / 交易币种"), `${s.reporting_currency ?? "—"} / ${s.currency}`],
    [t("会计口径"), tf("{a}（{b}）", { a: s.accounting, b: s.accounting_basis })], [t("报告周期"), s.reporting],
    [t("主要上市地"), s.primary_listing ? tf("{v}（{b}）", { v: VENUE[s.primary_listing] ?? s.primary_listing, b: s.primary_listing_basis }) : tf("待确认（{b}）", { b: s.primary_listing_basis })],
    [t("快照时间"), s.snapshot_at],
  ];
  return (
    <Panel title={`${s.long_name ?? s.name}（${s.symbol}）`} eyebrow={`${MARKET_LABEL[s.market]} · ${s.exchange}`}
      actions={<button className="btn" onClick={onClose}>{t("关闭")}</button>}>
      <div className="grid gap-4 lg:grid-cols-2">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          {rows.map(([k, v]) => (<Fragment key={k}><dt className="text-faint">{k}</dt><dd className="num break-all">{v || "—"}</dd></Fragment>))}
        </dl>
        <div className="flex flex-col gap-2">
          <div className="label">{t("同一发行人的其他证券")}</div>
          {siblings.length === 0 && rel.length === 0 && <p className="text-sm text-faint">{t("未发现其他市场的挂牌或存托凭证")}</p>}
          {siblings.map((x) => (
            <div key={x.symbol} className="text-sm flex flex-wrap gap-2 items-center">
              <Badge tone="accent">{t("本平台")} · {MARKET_LABEL[x.market]}</Badge><span className="num">{x.symbol}</span>
              <span className="text-muted">{x.share_class_figi === s.share_class_figi ? t("同一股份多地上市（股份类别 FIGI 一致）") : t("同一发行人的其他股份类别")}</span>
            </div>
          ))}
          {rel.filter((r) => !r.in_universe).map((r) => (
            <div key={`${r.venue}${r.ticker}`} className="text-sm flex flex-wrap gap-2 items-center">
              <Badge tone={r.relation === "depositary_receipt" ? "warn" : "muted"}>{r.venue_zh ?? r.venue}</Badge>
              <span className="num">{r.ticker}</span><span className="text-muted">{r.name}</span>
              <span>{t(r.relation_zh)}</span>{r.note && <span className="text-xs text-faint">{r.note}</span>}
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
  if (loading) return <Loading what={t("证券主数据")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const ql = q.trim().toLowerCase();
  const rows = data.securities.filter((s) => (market === "all" || s.market === market)
    && (!ql || `${s.symbol} ${s.name} ${s.long_name ?? ""} ${s.legal_name ?? ""}`.toLowerCase().includes(ql))
    && (!relationsOnly || s.multi_market || (relBy.get(s.issuer_id) ?? []).some((r) => !r.in_universe)));
  const cur = data.securities.find((s) => s.symbol === open);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-3 items-center">
        <Seg label={t("市场")} value={market} onChange={setMarket}
          options={[{ value: "all", label: t("全部") }, ...visibleMarkets().map((m) => ({ value: m, label: MARKET_LABEL[m] }))]} />
        <input aria-label={t("搜索证券")} className="field max-w-xs" placeholder={t("代码或名称")} value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="text-sm text-muted">{tf("{n} 只", { n: rows.length })}</span>
      </div>
      {cur && <SecurityDetail s={cur} rel={relBy.get(cur.issuer_id) ?? []} all={data.securities} onClose={() => setOpen(null)} />}
      <Panel title={relationsOnly ? t("有跨市场关联的证券") : t("证券主数据")} eyebrow={t("点击一行查看发行人与其他市场的挂牌")}>
        <div className="overflow-x-auto max-h-[600px]">
          <table className="w-full text-sm">
            <thead className="label text-left sticky top-0 bg-bg-2">
              <tr><th className="py-1 pr-3">{t("代码")}</th><th className="pr-3">{t("名称")}</th><th className="pr-3">{t("法律实体 / 注册地")}</th>
                <th className="pr-3">{t("行业")}</th><th className="pr-3">{t("报告币种")}</th><th className="pr-3">{t("年结")}</th><th className="pr-3">{t("主要上市地")}</th><th>{t("跨市场关联")}</th></tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const rel = (relBy.get(s.issuer_id) ?? []).filter((r) => !r.in_universe);
                return (
                  <tr key={s.symbol} className={`border-t border-line cursor-pointer hover:bg-bg-3 ${open === s.symbol ? "bg-accent/10" : ""}`}
                    onClick={() => setOpen(s.symbol)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(s.symbol)}>
                    <td className="py-1.5 pr-3 num whitespace-nowrap" style={{ color: marketColor(s.market) }}>{s.symbol}</td>
                    <td className="pr-3 max-w-[24ch] truncate" title={s.long_name ?? s.name}>{s.long_name ?? s.name}</td>
                    <td className="pr-3 text-xs">{s.lei ? <>{s.legal_name}<span className="text-faint"> · {s.jurisdiction}</span></> : <span className="text-warn">{t("LEI 未匹配")}</span>}</td>
                    <td className="pr-3 text-xs text-muted">{s.industry ?? "—"}</td>
                    <td className="pr-3 num">{s.reporting_currency ?? "—"}{s.reporting_currency && s.reporting_currency !== s.currency && <span className="text-warn"> ≠{s.currency}</span>}</td>
                    <td className="pr-3 num">{s.fiscal_year_end_month ?? "—"}</td>
                    <td className="pr-3 text-xs">{s.primary_listing ? tf("{v}{k}", { v: VENUE[s.primary_listing] ?? s.primary_listing, k: s.is_primary ? t("（本地）") : "" }) : <span className="text-faint">{t("待确认")}</span>}</td>
                    <td className="text-xs">
                      <span className="inline-flex flex-wrap gap-1">
                        {s.multi_market && <Badge tone="accent">{t("本平台多市场")}</Badge>}
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
        <p className="text-xs text-faint mt-2">{t("来源：GLEIF（法律实体，CC0）、OpenFIGI（证券标识）、Yahoo Finance（行业与财务年度，当前快照）。跨市场关联为自动匹配，待数据负责人确认（configs/refdata/overrides.yaml）。")}</p>
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
          <Stat label={t("分红")} value={a.by_type.dividend ?? 0} />
          <Stat label={t("拆股 / 合股")} value={a.by_type.split ?? 0} sub={tf("复权检查 {a}/{b} 通过", { a: a.adjustment_checks.length - a.adjustment_failures.length, b: a.adjustment_checks.length })} />
          <Stat label={t("代码变更")} value={a.by_type.ticker_change ?? 0} sub={t("AMF 历史记录（法国）")} />
          <Stat label={t("疑似停牌")} value={a.by_type.suspension ?? 0} sub={t("连续 ≥3 个交易日无成交")} />
        </div>
      )}
      {a && a.not_covered.length > 0 && <Notice tone="warn" title={t("尚未覆盖")}>{joinList(a.not_covered)}{t("：免费数据源没有可靠记录，需数据商。")}
        {t("导入接口已就绪：按")} {sum.data?.vendor_templates ?? "configs/refdata/vendor_templates/README.md"} {t("的格式导出后运行 python -m factopia.refdata import-actions / import-membership。")}</Notice>}
      {(sum.data?.vendor_imports ?? []).length > 0 && <Notice tone="accent" title={t("数据商导入记录")}>
        {(sum.data?.vendor_imports ?? []).slice(-5).reverse().map((v, i) => <div key={i}>{tf("{d} · {k} · {v} · {n} 行", { d: v.at.slice(0, 10), k: v.kind === "membership" ? tf("历史成分股（{m}）", { m: v.market ?? "" }) : t("公司行动"), v: v.vendor, n: v.rows ?? 0 })}</div>)}
      </Notice>}
      <div className="flex flex-wrap gap-3 items-center">
        <Seg label={t("类型")} value={type} onChange={setType} options={[{ value: "", label: t("全部") }, { value: "dividend", label: t("分红") },
          { value: "split", label: t("拆股") }, { value: "ticker_change", label: t("代码变更") }, { value: "suspension", label: t("疑似停牌") }]} />
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); setSym(symbol.trim()); }}>
          <input aria-label={t("证券代码")} className="field w-40" placeholder={t("如 0005.HK")} value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          <button className="btn" type="submit">{t("筛选")}</button>
        </form>
      </div>
      {loading ? <Loading what={t("公司行动")} /> : error ? <ErrorBox error={error} /> : data && (
        <Panel title={tf("公司行动（{t} 条，显示最近 {n} 条）", { t: data.total, n: data.items.length })} eyebrow={t("行情为复权价；本表用于展示、核对与复权检查")}>
          <div className="overflow-x-auto max-h-[560px]">
            <table className="w-full text-sm">
              <thead className="label text-left sticky top-0 bg-bg-2"><tr><th className="py-1 pr-3">{t("日期")}</th><th className="pr-3">{t("代码")}</th><th className="pr-3">{t("类型")}</th><th className="pr-3">{t("内容")}</th><th>{t("来源")}</th></tr></thead>
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
  ["exchange", t("交易所")], ["timezone", t("时区")], ["currency", t("本地币种")], ["benchmark", t("基准指数")], ["trading_hours", t("交易时段")],
  ["close_local", t("收盘（当地）")], ["eod_delay_min", t("日终数据延迟")], ["price_limits", t("涨跌停")], ["settlement", t("交收")],
  ["short_selling", t("卖空")], ["lot_size", t("每手股数")], ["reporting", t("报告周期")], ["accounting_default", t("会计口径（默认）")],
  ["disclosure_language", t("披露语言")], ["holidays", t("假期与日历")],
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
  if (loading) return <Loading what={t("口径与规则")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const ms = Object.keys(data.markets.markets) as Market[];
  const groups = [...new Set(data.fields.fields.map((f) => f.group ?? t("其他")))];
  const formulaRow = (key: keyof MarketRule) => {
    if (!SHOW_FORMULAS) return null;
    if (key === "eod_delay_min") return ms.map((m) => {
      const c = data.markets.markets[m];
      return <FormulaLine key={m}>{tf("行情可获取 = {c} + {d} 分钟 = {a}（当地）", { c: c.close_local, d: c.eod_delay_min, a: addMin(c.close_local, c.eod_delay_min) })}</FormulaLine>;
    });
    if (key === "close_local") return ms.map((m) => {
      const c = data.markets.markets[m], d = data.text_delay_min?.[m] ?? 60;
      return <FormulaLine key={m}>{tf("公告当天生效 ⇔ 发布时间 + {d} 分钟 ≤ {c}，即发布不晚于 {a}；否则顺延到下一个交易日", { d, c: c.close_local, a: addMin(c.close_local, -d) })}</FormulaLine>;
    });
    return null;
  };
  return (
    <div className="flex flex-col gap-4">
      {SHOW_FORMULAS && <Notice tone="accent" title={t("开发阶段公式说明")}>
        {t("灰色竖线开头的行是计算方法，并代入了实际参数；用于核对每个口径如何得出，进入 MVP 时隐藏。")}
      </Notice>}
      <Panel title={t("市场规则")} eyebrow={tf("版本 {v} · configs/refdata/markets.yaml · 数据负责人维护", { v: data.markets.version })}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3 w-32">{t("规则")}</th>
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
                          {v == null || v === "" ? "—" : key === "eod_delay_min" ? tf("{n} 分钟", { n: v }) : String(v)}</span>
                        {f && f[k]}
                      </td>;
                    })}
                  </tr>
                );
              })}
              <tr className="border-t border-line align-top">
                <td className="py-2 pr-3 text-xs text-faint whitespace-nowrap">{t("股票池成分股")}</td>
                {ms.map((m) => {
                  const mem = data.membership[m];
                  return <td key={m} className="py-2 pr-3 text-xs">
                    {mem ? (mem.point_in_time ? <Badge tone="good">{t("历史成分股")}</Badge> : <Badge tone="warn">{tf("当前成分股 {n}", { n: mem.rows })}</Badge>) : "—"}
                    {SHOW_FORMULAS && mem && <FormulaLine>
                      {tf("来源：{s}；时间点一致 ⇔ 没有来源为 current_list 的行 → {r}", { s: joinList(Object.entries(mem.sources).map(([k, v]) => tf("{k} {v} 行", { k, v }))), r: mem.point_in_time ? t("一致") : t("不一致") })}
                    </FormulaLine>}
                  </td>;
                })}
              </tr>
            </tbody>
          </table>
        </div>
        <div className="grid gap-4 md:grid-cols-2 mt-4">
          {data.markets.accounting_by_jurisdiction && <div>
            <div className="label mb-1">{t("按注册地推断会计口径（覆盖市场默认值）")}</div>
            <table className="w-full text-xs"><tbody>
              {Object.entries(data.markets.accounting_by_jurisdiction).map(([k, v]) => (
                <tr key={k} className="border-t border-line"><td className="py-1 pr-3 num text-faint">{k}</td><td>{v}</td></tr>))}
            </tbody></table>
          </div>}
          {data.markets.cross_listing_venues && <div>
            <div className="label mb-1">{t("跨市场挂牌只检索这些交易所")}</div>
            <div className="flex flex-wrap gap-1">
              {Object.entries(data.markets.cross_listing_venues).map(([k, v]) => <Badge key={k} tone="muted">{v} {k}</Badge>)}
            </div>
            <div className="text-xs text-faint mt-1">{t("德国等二级报价场所不计；名称规范化后一致才采用，标为「自动匹配，待确认」")}</div>
          </div>}
        </div>
      </Panel>
      <Panel title={t("字段口径")} eyebrow={tf("版本 {v} · configs/refdata/fields.yaml · {n} 个字段", { v: data.fields.version, n: data.fields.fields.length })}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">{t("字段")}</th><th className="pr-3">{t("单位")}</th><th className="pr-3">{t("来源")}</th>
              <th className="pr-3">{t("复权 / 调整")}</th><th className="pr-3">{t("可用时间")}</th><th>{t("说明")}</th></tr></thead>
            <tbody>
              {groups.map((g) => (
                <Fragment key={g}>
                  <tr><td colSpan={6} className="pt-4 pb-1 label">{g}</td></tr>
                  {data.fields.fields.filter((f) => (f.group ?? t("其他")) === g).map((f) => (
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
  if (loading) return <Loading what={t("数据台")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const m = data.master;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("数据台")}</div>
          <h1 className="font-display text-xl font-semibold">{t("参考数据与时间点一致性")}</h1>
        </div>
        <span className="text-sm text-muted">{t("发行人与证券 · 公司行动 · 口径与规则 · 三个时间")}</span>
        {data.pit && <span className="ml-auto"><Badge tone={data.pit.point_in_time ? "good" : "warn"}>{data.pit.label}</Badge></span>}
      </div>
      {m ? (
        <div className="panel p-4 grid gap-4 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label={t("证券")} value={m.securities} sub={tf("{n} 个发行人", { n: m.issuers })} />
          <Stat label={t("LEI 覆盖")} value={`${m.with_lei}/${m.securities}`} sub={t("GLEIF 法律实体")} />
          <Stat label={t("FIGI 覆盖")} value={`${m.with_figi}/${m.securities}`} sub={tf("ISIN {n}（仅法国）", { n: m.with_isin })} />
          <Stat label={t("本平台多市场发行人")} value={m.multi_market_issuers} sub={t("如汇丰（香港、伦敦）")} />
          <Stat label={t("其他市场挂牌")} value={(m.relations.same_share_class ?? 0) + (m.relations.other_share_class ?? 0)} sub={t("同股多地 / 其他股份类别")} />
          <Stat label={t("存托凭证")} value={m.relations.depositary_receipt ?? 0} sub={tf("主要上市地待确认 {n}", { n: m.primary_unresolved })} />
        </div>
      ) : <Notice tone="warn" title={t("参考数据尚未生成")}>{t("运行 python -m factopia.refdata all")}</Notice>}
      <Seg label={t("视图")} value={tab} onChange={setTab} options={[
        { value: "pit", label: t("时间点一致性") }, { value: "securities", label: t("证券主数据") }, { value: "relations", label: t("跨市场关联") },
        { value: "actions", label: t("公司行动") }, { value: "dictionary", label: t("口径与规则") },
      ]} />
      {tab === "pit" && <PitPanel />}
      {tab === "securities" && <SecuritiesTab relationsOnly={false} />}
      {tab === "relations" && <SecuritiesTab relationsOnly />}
      {tab === "actions" && <ActionsTab />}
      {tab === "dictionary" && <DictionaryTab />}
    </div>
  );
}
