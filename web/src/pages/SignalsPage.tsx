import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg, Stat } from "../components/ui";
import EChart from "../components/EChart";
import { api, visibleMarkets, type CurrencyView, type MonitorPeriod, type MonitorResult, type SignalEntry } from "../api";
import { cssVar, fix, MARKET_LABEL, marketColor, pct, signed, useAsync, useThemeVersion } from "../lib";

// 信号目录与监控（Spec 第 14.15 节；BR-24、30、31、41、42）

const STATE_TONE: Record<string, "good" | "warn" | "bad" | "accent" | "muted"> = {
  active: "good", paused: "warn", retired: "bad", not_applicable: "muted", not_approved: "bad", in_review: "accent",
};
const SUG_TONE: Record<string, "good" | "warn" | "bad"> = { ok: "good", watch: "warn", pause: "bad" };
const ROLES = ["投资组合经理", "运营 / 技术负责人", "模型风险"];

function Rolling({ m }: { m: MonitorResult }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line"), accent = cssVar("--accent"), bad = cssVar("--bad");
    const th = m.thresholds.rolling_rank_ic_60d_min;
    return {
      animationDuration: 600, grid: { left: 52, right: 16, top: 24, bottom: 28 }, tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: m.rolling.dates, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", axisLabel: { color: muted }, splitLine: { lineStyle: { color: line, opacity: 0.35 } } },
      series: [{
        type: "line", data: m.rolling.values, showSymbol: false, lineStyle: { color: marketColor(m.market), width: 1.8 },
        markArea: { silent: true, itemStyle: { color: accent, opacity: 0.07 }, label: { color: muted, position: "insideTop", fontSize: 11 },
          data: [[{ xAxis: m.rolling.dates.find((d) => d >= m.rolling.live_start), name: "上线期（2024 年起）" }, { xAxis: m.rolling.dates.at(-1) }]] },
        markLine: { symbol: "none", silent: true, label: { color: muted, formatter: "获批阈值", position: "insideEndTop" }, lineStyle: { color: bad, type: "dashed" },
          data: th != null ? [{ yAxis: th }] : [] },
      }],
      title: { text: "滚动 60 日 rank IC", left: 0, top: 0, textStyle: { color: muted, fontSize: 12, fontWeight: "normal" } },
    };
  }, [m, theme]);
  return <EChart option={option} notMerge className="h-[260px]" />;
}

const ROWS: [string, (p: MonitorPeriod) => string][] = [
  ["rank IC（t 值）", (p) => `${fix(p.rank_ic, 4)}（${fix(p.rank_ic_t, 2)}）`],
  ["滚动 60 日 rank IC 最新 / 最低", (p) => `${fix(p.rolling_rank_ic_60d_last, 3)} / ${fix(p.rolling_rank_ic_60d_min, 3)}`],
  ["命中率：rank IC 为正的月份", (p) => pct(p.hit_rate_months, 0)],
  ["命中率：前 20% 跑赢中位数", (p) => pct(p.hit_rate_top, 0)],
  ["前 20% 超额（扣成本前 / 后，年化）", (p) => `${signed(p.excess_gross_ann, 1)} / ${signed(p.excess_net_ann, 1)}`],
  ["相对股票池最大回撤", (p) => pct(p.max_drawdown_vs_universe, 1)],
  ["月度单边换手", (p) => pct(p.turnover_monthly, 0)],
  ["实施偏差：推迟一天执行", (p) => signed(p.implementation_delay_ann, 2)],
  ["拥挤度（组内相关 / 全体）", (p) => fix(p.crowding, 2)],
  ["容量（本地币种，10% 成交额）", (p) => (p.capacity_local == null ? "—" : `${(p.capacity_local / 1e6).toFixed(0)} 百万`)],
  ["暴露：规模 / 动量 / 波动 / 反转", (p) => ["size", "momentum", "volatility", "reversal"].map((k) => fix(p.exposure[k], 2)).join(" / ")],
  ["市场状态：高波动 / 低波动", (p) => `${fix(p.regime.high_vol, 3)} / ${fix(p.regime.low_vol, 3)}`],
  ["市场状态：指数涨 / 跌月份", (p) => `${fix(p.regime.up_months, 3)} / ${fix(p.regime.down_months, 3)}`],
];

// 授权基础币种视角（第 14.22 节）：绝对收益按本地币种与基础币种分别年化，拆出汇率贡献；容量换算为基础币种
function CurrencyTable({ m, ccy }: { m: MonitorResult; ccy: "local" | "base" }) {
  const c = m.currency;
  if (!c) return <p className="text-sm text-muted">该监控结果计算于基础币种视角上线之前，请点击"重新计算监控"。</p>;
  const mil = (x: number | null | undefined) => (x == null ? "—" : `${(x / 1e6).toFixed(0)} 百万`);
  const rows: [string, (v: CurrencyView, p: MonitorPeriod) => string][] = ccy === "base" ? [
    [`前 20% 绝对收益（${c.base}，年化）`, (v) => signed(v.top_base_ann, 1)],
    [`股票池绝对收益（${c.base}，年化）`, (v) => signed(v.universe_base_ann, 1)],
    [`前 20% 超额（${c.base}，扣成本前，年化收益之差）`, (v) => signed(v.excess_base_ann, 1)],
    [`汇率贡献：${c.local} → ${c.base}（年化）`, (v) => signed(v.fx_ann, 1)],
    [`汇率 期初 → 期末（1 ${c.local} = x ${c.base}）`, (v) => `${fix(v.fx_rate_start, 4)} → ${fix(v.fx_rate_end, 4)}`],
    [`容量（${c.base}，10% 成交额）`, (v) => mil(v.capacity_base)],
  ] : [
    [`前 20% 绝对收益（${c.local}，年化）`, (v) => signed(v.top_local_ann, 1)],
    [`股票池绝对收益（${c.local}，年化）`, (v) => signed(v.universe_local_ann, 1)],
    [`前 20% 超额（${c.local}，扣成本前，年化收益之差）`, (v) => signed(v.excess_local_ann, 1)],
    [`容量（${c.local}，10% 成交额）`, (_, p) => mil(p.capacity_local)],
  ];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm num">
        <thead className="label text-left"><tr><th className="py-1 pr-3">指标</th><th className="pr-3">参照期</th><th>上线期</th></tr></thead>
        <tbody>{rows.map(([label, f]) => (
          <tr key={label} className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{label}</td>
            <td className="pr-3">{f(c.reference, m.reference)}</td><td>{f(c.live, m.live)}</td></tr>))}</tbody>
      </table>
      <p className="text-xs text-faint mt-2">授权：{c.mandate_name ?? c.mandate ?? "—"} · 基础币种 {c.base}。同一市场的股票承受同一汇率，相对股票池的超额在两种币种下几乎相同；汇率主要影响绝对收益与容量。本表超额为年化收益之差，与上方"监控指标"表的逐日超额复利口径不同。预警按本地币种计算。</p>
    </div>
  );
}

function Detail({ sid, onChanged }: { sid: string; onChanged: () => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.signal>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [role, setRole] = useState(ROLES[0]);
  const [note, setNote] = useState("");
  const [ccy, setCcy] = useState<"local" | "base">("base");
  const load = () => api.signal(sid).then(setData, (e) => setError((e as Error).message));
  useEffect(() => { setData(null); load(); }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                   // 监控任务进行中：每 5 秒刷新
    if (!data?.job || !["queued", "running"].includes(data.job.state)) return;
    const t = setTimeout(load, 5000);
    return () => clearTimeout(t);
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error && !data) return <ErrorBox error={error} />;
  if (!data) return <Loading what="信号" />;
  const { entry: e, monitor: m, job } = data;
  const act = async (action: string) => {
    setError(null);
    try { await api.signalAct(sid, { action, role, note }); setNote(""); await load(); onChanged(); } catch (x) { setError((x as Error).message); }
  };
  const running = job && ["queued", "running"].includes(job.state);
  return (
    <div className="flex flex-col gap-5">
      <Panel eyebrow={`${e.signal_id} · 来自审批 ${e.submission}`} title={`${e.title} · ${e.market_zh}`}
        actions={<Badge tone={STATE_TONE[e.state]}>{e.state_zh}</Badge>}>
        <div className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
          {e.labels.regional && <div className="sm:col-span-2"><span className="label mr-2">地区适用性</span>
            <Badge tone={e.labels.regional.tags.length ? "warn" : "good"}>{e.labels.applicability}</Badge>
            <span className="ml-2 text-muted">{e.labels.regional.display}</span>
            {e.labels.regional.reasons.length > 0 && <div className="text-xs text-faint mt-0.5">{e.labels.regional.reasons.join("；")}</div>}</div>}
          <div><span className="label mr-2">授权类型</span>{e.labels.mandate}</div>
          <div><span className="label mr-2">适用语言</span>{e.labels.languages.join("、")}</div>
          <div><span className="label mr-2">数据范围</span>{e.labels.data_scope.universe} · {e.labels.data_scope.categories.join("、")} · 快照 <span className="num text-xs">{e.labels.data_scope.snapshot}…</span></div>
          <div><span className="label mr-2">规则版本</span><span className="num text-xs">{e.labels.rule_versions.join(" · ")}</span></div>
          <div><span className="label mr-2">最后验证</span>{e.labels.last_validated ?? "—"}</div>
          <div><span className="label mr-2">下次复核</span>{e.labels.next_review ?? "—"}</div>
          <div><span className="label mr-2">研究结果</span><span className="num text-xs">{e.run.run_id} 第 {e.run.loop} 轮 · rank IC {fix(e.run.rank_ic, 4)} · 可信度 {e.run.credibility ?? "—"}</span></div>
          <div><span className="label mr-2">获批阈值</span><span className="num text-xs">{Object.entries(e.thresholds).map(([k, v]) => `${k} = ${v}`).join("、") || "—"}</span></div>
        </div>
        {e.reason && <p className="text-sm mt-2 text-muted">原因：{e.reason}</p>}
        {e.resubmission && <Notice tone={e.resubmission.status === "in_review" ? "accent" : "warn"} title="该版本之后又提交了审批">
          {e.resubmission.note}。新提交 <Link className="text-accent num" to={`/approvals?sid=${encodeURIComponent(e.resubmission.submission)}`}>{e.resubmission.submission}</Link>
          ，本页状态与监控以已获批的提交 <span className="num">{e.submission}</span> 为准。
        </Notice>}
        {(e.state === "active" || e.state === "paused") && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button className="btn btn-primary" disabled={!!running} onClick={async () => { await api.startMonitor(sid); await load(); onChanged(); }}>
              {running ? `监控计算中（${job!.state_zh}）…` : m ? "重新计算监控" : "运行监控"}</button>
            <select id="sig-role" className="field" style={{ width: "auto" }} value={role} onChange={(x) => setRole(x.target.value)} aria-label="演示身份">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <input id="sig-note" className="field flex-1 min-w-[14rem]" placeholder="原因（必填）" value={note} onChange={(x) => setNote(x.target.value)} />
            {e.state === "active" && <button className="btn" onClick={() => act("pause")}>暂停该市场版本</button>}
            {e.state === "paused" && <button className="btn" onClick={() => act("resume")}>恢复</button>}
            <button className="btn" onClick={() => act("retire")}>停用</button>
          </div>
        )}
        {error && <div className="mt-2"><ErrorBox error={error} /></div>}
        {job?.state === "failed" && <p className="text-xs text-bad mt-2">上次监控失败：{job.error}</p>}
      </Panel>
      {m && (
        <>
          <div className="panel p-4 grid gap-4 grid-cols-2 lg:grid-cols-4">
            <Stat label="平台建议" value={<Badge tone={SUG_TONE[m.suggestion]}>{m.suggestion === "pause" ? "建议暂停" : m.suggestion === "watch" ? "加强观察" : "正常"}</Badge>}
              sub={`${m.suggestion === "pause" ? "统计与经济预警同时成立 · " : ""}计算于 ${m.computed_at.slice(0, 16).replace("T", " ")} UTC`} />
            <Stat label="上线期 rank IC" value={fix(m.live.rank_ic, 4)} sub={`参照期 ${fix(m.reference.rank_ic, 4)} · t ${fix(m.live.rank_ic_t, 2)}`} />
            <Stat label="上线期扣成本超额" value={signed(m.live.excess_net_ann, 1)} sub={`参照期 ${signed(m.reference.excess_net_ann, 1)}`} />
            <Stat label="数据漂移 PSI" value={fix(m.data_drift.psi, 3)} sub={`上线期因子缺失 ${pct(m.data_drift.missing_live, 0)}`} />
          </div>
          <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
            <Panel eyebrow={m.note} title="监控曲线"><Rolling m={m} /></Panel>
            <Panel eyebrow="统计与经济预警同时成立才建议暂停；只对本市场" title={`预警（${m.alerts.length}）`}>
              {m.alerts.length === 0 ? <p className="text-sm text-muted">没有预警。</p> : (
                <ul className="flex flex-col gap-2 text-sm">
                  {m.alerts.map((a) => (
                    <li key={a.title} className={`border-l-2 pl-3 ${a.level === "alert" ? "border-l-bad" : "border-l-warn"}`}>
                      <div className="flex items-center gap-2"><Badge tone={a.level === "alert" ? "bad" : "warn"}>{a.level === "alert" ? "预警" : "观察"}</Badge>
                        <span className="font-medium">{a.title}</span></div>
                      <div className="text-xs text-muted mt-0.5">{a.detail}</div>
                    </li>
                  ))}
                </ul>
              )}
              <div className="label mt-4 mb-1">诊断</div>
              <ul className="text-xs text-muted flex flex-col gap-1">{m.diagnosis.map((d) => <li key={d}>· {d}</li>)}</ul>
            </Panel>
          </div>
          <Panel eyebrow="BR-31：参照期（测试期）与上线期对比" title="监控指标">
            <div className="overflow-x-auto">
              <table className="w-full text-sm num">
                <thead className="label text-left"><tr><th className="py-1 pr-3">指标</th>
                  <th className="pr-3">参照期 {m.reference.start} 至 {m.reference.end}</th><th>上线期 {m.live.start} 至 {m.live.end}</th></tr></thead>
                <tbody>
                  {ROWS.map(([label, f]) => (
                    <tr key={label} className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{label}</td><td className="pr-3">{f(m.reference)}</td><td>{f(m.live)}</td></tr>
                  ))}
                  <tr className="border-t border-line"><td className="py-1.5 pr-3 font-sans">衰减：预测周期 {Object.keys(m.decay.live).join(" / ")} 日</td>
                    <td className="pr-3">{Object.values(m.decay.reference).map((x) => fix(x, 3)).join(" / ")}</td>
                    <td>{Object.values(m.decay.live).map((x) => fix(x, 3)).join(" / ")}</td></tr>
                </tbody>
              </table>
            </div>
            <p className="text-xs text-faint mt-2">因子（冻结代码）：{m.factors.map((f) => `${f.name}（方向 ${f.sign > 0 ? "+" : "−"}，覆盖 ${pct(f.coverage_ref, 0)} → ${pct(f.coverage_live, 0)}）`).join("；")}</p>
          </Panel>
          <Panel eyebrow="试点交付物：按市场、币种和客户授权展示信号表现" title="币种视角"
            actions={<Seg label="币种" value={ccy} onChange={setCcy} options={[{ value: "base", label: `基础币种${m.currency ? ` ${m.currency.base}` : ""}` }, { value: "local", label: `本地币种${m.currency ? ` ${m.currency.local}` : ""}` }]} />}>
            <CurrencyTable m={m} ccy={ccy} />
          </Panel>
        </>
      )}
      {e.events.length > 0 && (
        <Panel eyebrow="只追加，哈希链" title="状态变更记录">
          <ul className="text-sm flex flex-col gap-1">{e.events.map((x) => (
            <li key={x.hash}><span className="num text-xs text-faint mr-2">{x.at.slice(0, 16).replace("T", " ")}</span>{x.role} · {({ pause: "暂停", resume: "恢复", retire: "停用" } as Record<string, string>)[x.action] ?? x.action} · <span className="text-muted">{x.note}</span></li>))}</ul>
        </Panel>
      )}
    </div>
  );
}

export default function SignalsPage() {
  const [params, setParams] = useSearchParams();
  const list = useAsync(() => api.signals(), []);
  if (list.loading) return <Loading what="信号目录" />;
  if (list.error || !list.data) return <ErrorBox error={list.error ?? "无数据"} />;
  const all = list.data.items;
  const fm = params.get("market") ?? "all", fd = params.get("mandate") ?? "all";
  const mandates = [...new Map(all.filter((x) => x.mandate).map((x) => [x.mandate!, x.labels.mandate])).entries()];
  const items = all.filter((x) => (fm === "all" || x.market === fm) && (fd === "all" || x.mandate === fd));
  const setFilter = (k: string, v: string) => { const p = new URLSearchParams(params); if (v === "all") p.delete(k); else p.set(k, v); p.delete("sid"); setParams(p); };
  const sid = params.get("sid") ?? items.find((x) => x.production)?.signal_id ?? items[0]?.signal_id ?? "";
  const groups = items.reduce<Record<string, SignalEntry[]>>((g, x) => { (g[`${x.research_id} v${x.version}`] ??= []).push(x); return g; }, {});
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">信号目录</div>
          <h1 className="font-display text-xl font-semibold">按市场分版本的信号与上线后监控</h1>
        </div>
        <span className="text-sm text-muted">获批版本进入监控层 · 只暂停受影响地区 · 未获批版本保留原因</span>
      </div>
      {all.length === 0 ? <Notice tone="accent" title="目录为空">研究版本提交审批后出现在这里；获批的市场版本才能上线监控。</Notice> : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,4fr)_minmax(0,8fr)]">
          <Panel eyebrow="研究版本 × 市场" title={`目录（${items.length}）`}>
            <div className="flex flex-col gap-2 mb-3">
              <Seg label="市场" value={fm} onChange={(v) => setFilter("market", v)}
                options={[{ value: "all", label: "全部" }, ...visibleMarkets().map((m) => ({ value: m, label: MARKET_LABEL[m] }))]} />
              {mandates.length > 0 && <select id="sig-mandate" aria-label="按授权筛选" className="field text-sm" value={fd} onChange={(x) => setFilter("mandate", x.target.value)}>
                <option value="all">全部授权</option>
                {mandates.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>}
            </div>
            {items.length === 0 && <p className="text-sm text-muted">没有符合筛选条件的信号。</p>}
            <div className="flex flex-col gap-4">
              {Object.entries(groups).map(([k, xs]) => (
                <div key={k}>
                  <div className="text-xs text-faint mb-1 truncate">{k} · {xs[0].title}{xs[0].base_currency ? ` · ${xs[0].base_currency}` : ""}</div>
                  {xs[0].labels.regional && <div className="text-xs mb-1"><Badge tone="accent">{xs[0].labels.regional.headline}</Badge></div>}
                  <ul className="flex flex-col">
                    {xs.map((x) => (
                      <li key={x.signal_id}>
                        <button type="button" onClick={() => setParams({ sid: x.signal_id })}
                          className={`w-full text-left flex items-center gap-2 py-1.5 px-2 rounded ${x.signal_id === sid ? "bg-bg-3" : ""}`}>
                          <span className="w-10 text-sm" style={{ color: marketColor(x.market) }}>{MARKET_LABEL[x.market]}</span>
                          <Badge tone={STATE_TONE[x.state]}>{x.state_zh}</Badge>
                          {x.resubmission?.status === "in_review" && <span className="text-xs text-accent">重新审批中</span>}
                          {x.monitor && <Badge tone={SUG_TONE[x.monitor.suggestion]}>{x.monitor.suggestion === "ok" ? "正常" : x.monitor.suggestion === "watch" ? "观察" : "建议暂停"}</Badge>}
                          {x.monitor_job && ["queued", "running"].includes(x.monitor_job.state) && <span className="text-xs text-accent">计算中</span>}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </Panel>
          {sid && <Detail key={sid} sid={sid} onChanged={list.reload} />}
        </div>
      )}
    </div>
  );
}
