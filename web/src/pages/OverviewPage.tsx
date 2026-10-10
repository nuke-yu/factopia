import { Link } from "react-router-dom";
import { Badge, ErrorBox, Loading, Panel } from "../components/ui";
import { api, MARKETS } from "../api";
import { MARKET_LABEL, marketColor, useAsync } from "../lib";

// 管理总览（Spec 第 14.15 节；BR-48）：按地区查看研究吞吐量、审批瓶颈、信号健康度和异常处置

const dur = (h: number) => (h >= 1 ? `${h.toFixed(1)} 小时` : `${(h * 60).toFixed(1)} 分钟`);

export default function OverviewPage() {
  const { data, error, loading } = useAsync(() => api.management(), []);
  if (loading) return <Loading what="管理总览" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const a = data.approvals;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">管理总览</div>
          <h1 className="font-display text-xl font-semibold">按地区的研究、审批与信号健康</h1>
        </div>
        <span className="text-sm text-muted">研究吞吐量 · 审批瓶颈 · 信号健康度 · 异常处置</span>
        <span className="ml-auto text-xs text-faint">生成于 {data.generated_at.slice(0, 16).replace("T", " ")} UTC</span>
      </div>
      <div className="grid gap-5 lg:grid-cols-3">
        {MARKETS.map((m) => {
          const r = data.regions[m], h = data.signal_health[m];
          return (
            <Panel key={m} eyebrow={m.toUpperCase()} title={MARKET_LABEL[m]}>
              <div className="flex flex-col gap-3 text-sm">
                <div>
                  <div className="label mb-1">研究吞吐量</div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 num">
                    <span className="font-sans text-muted">运行（完成）</span><span>{r.runs}（{r.runs_done}）</span>
                    <span className="font-sans text-muted">采纳轮次</span><span>{r.adopted_rounds}</span>
                    <span className="font-sans text-muted">可信度检查</span><span>{r.validations}</span>
                    <span className="font-sans text-muted">平均用时</span><span>{r.avg_duration_min} 分钟</span>
                    <span className="font-sans text-muted">大模型费用</span><span>${r.cost_usd.toFixed(2)}</span>
                    <span className="font-sans text-muted">失败 / 排队</span><span>{r.jobs_failed} / {r.jobs_queued}</span>
                  </div>
                </div>
                <div>
                  <div className="label mb-1">信号健康度</div>
                  <div className="flex flex-wrap gap-1.5">
                    <Badge tone="good">生产中 {h.active}</Badge>
                    <Badge tone={h.paused ? "warn" : "muted"}>已暂停 {h.paused}</Badge>
                    <Badge tone={h.retired ? "bad" : "muted"}>已停用 {h.retired}</Badge>
                    <Badge tone="muted">未上线 {h.not_production}</Badge>
                    <Badge tone={h.alerts ? "warn" : "muted"}>预警 {h.alerts}</Badge>
                    {h.pause_suggested > 0 && <Badge tone="bad">建议暂停 {h.pause_suggested}</Badge>}
                  </div>
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
      <div className="grid gap-5 xl:grid-cols-2">
        <Panel eyebrow={`共 ${a.total} 次提交 · ${Object.entries(a.by_status).filter(([, v]) => v).map(([k, v]) => `${a.status_zh[k]} ${v}`).join(" · ")}`} title="审批瓶颈">
          <div className="label mb-1">各角色平均处理用时（从上一步到本步决定）</div>
          {a.bottleneck.length === 0 ? <p className="text-sm text-muted">还没有审批记录。</p> : (
            <ul className="flex flex-col gap-1 text-sm">
              {a.bottleneck.map((b) => {
                const max = Math.max(...a.bottleneck.map((x) => x.avg_hours), 0.01);
                return (
                  <li key={b.role} className="grid grid-cols-[9rem_minmax(0,1fr)_7rem] items-center gap-2">
                    <span>{b.role}</span>
                    <span className="h-2 rounded bg-bg-3 overflow-hidden"><span className="block h-full bg-accent" style={{ width: `${Math.max(2, (b.avg_hours / max) * 100)}%` }} /></span>
                    <span className="num text-xs text-right">{dur(b.avg_hours)} · {b.decisions} 次</span>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="label mt-4 mb-1">审批中</div>
          {a.pending.length === 0 ? <p className="text-sm text-muted">没有审批中的提交。</p> : (
            <ul className="text-sm flex flex-col gap-1">
              {a.pending.map((p) => (
                <li key={p.id}><Link className="text-accent" to={`/approvals?sid=${p.id}`}>{p.title}</Link>
                  <span className="text-xs text-muted ml-2">等待「{p.role}」{dur(p.waiting_hours)}</span></li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel eyebrow="监控预警与之后的人工处置" title="异常处置">
          {data.incidents.length === 0 ? <p className="text-sm text-muted">没有监控预警。</p> : (
            <ul className="flex flex-col gap-3 text-sm">
              {data.incidents.map((x) => (
                <li key={x.signal_id} className="border-l-2 pl-3" style={{ borderColor: marketColor(x.market) }}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Link className="font-medium text-accent" to={`/signals?sid=${x.signal_id}`}>{x.signal_id}</Link>
                    <Badge tone={x.handled.length ? "good" : "warn"}>{x.handled.length ? "已处置" : "待处置"}</Badge>
                    <span className="text-xs text-muted">当前：{x.state_zh}</span>
                  </div>
                  <div className="text-xs text-muted mt-0.5">{x.suggestion_zh} · {x.alerts.join("；")}</div>
                  {x.handled.map((h) => <div key={h.at} className="text-xs mt-0.5">{h.at.slice(0, 16).replace("T", " ")} {h.role}{h.action}：{h.note}</div>)}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
