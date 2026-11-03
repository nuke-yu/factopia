import { Link } from "react-router-dom";
import { Badge, ErrorBox, Loading, Panel } from "../components/ui";
import { api, MARKETS } from "../api";
import { MARKET_LABEL, marketColor, useAsync } from "../lib";
import { t, tf, joinList } from "../i18n";

// 管理总览（Spec 第 14.15 节；BR-48）：按地区查看研究吞吐量、审批瓶颈、信号健康度和异常处置

const dur = (h: number) => (h >= 1 ? tf("{n} 小时", { n: h.toFixed(1) }) : tf("{n} 分钟", { n: (h * 60).toFixed(1) }));

export default function OverviewPage() {
  const { data, error, loading } = useAsync(() => api.management(), []);
  if (loading) return <Loading what={t("管理总览")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const a = data.approvals;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("管理总览")}</div>
          <h1 className="font-display text-xl font-semibold">{t("按地区的研究、审批与信号健康")}</h1>
        </div>
        <span className="text-sm text-muted">{t("研究吞吐量 · 审批瓶颈 · 信号健康度 · 异常处置")}</span>
        <span className="ml-auto text-xs text-faint">{tf("生成于 {d} UTC", { d: data.generated_at.slice(0, 16).replace("T", " ") })}</span>
      </div>
      <div className="grid gap-5 lg:grid-cols-3">
        {MARKETS.filter((m) => data.regions[m]).map((m) => {
          const r = data.regions[m], h = data.signal_health[m];
          return (
            <Panel key={m} eyebrow={m.toUpperCase()} title={MARKET_LABEL[m]}>
              <div className="flex flex-col gap-3 text-sm">
                <div>
                  <div className="label mb-1">{t("研究吞吐量")}</div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 num">
                    <span className="font-sans text-muted">{t("运行（完成）")}</span><span>{tf("{a}（{b}）", { a: r.runs, b: r.runs_done })}</span>
                    <span className="font-sans text-muted">{t("采纳轮次")}</span><span>{r.adopted_rounds}</span>
                    <span className="font-sans text-muted">{t("可信度检查")}</span><span>{r.validations}</span>
                    <span className="font-sans text-muted">{t("平均用时")}</span><span>{tf("{n} 分钟", { n: r.avg_duration_min })}</span>
                    <span className="font-sans text-muted">{t("大模型费用")}</span><span>{`$${r.cost_usd.toFixed(2)}`}</span>
                    <span className="font-sans text-muted">{t("失败 / 排队")}</span><span>{tf("{a} / {b}", { a: r.jobs_failed, b: r.jobs_queued })}</span>
                  </div>
                </div>
                <div>
                  <div className="label mb-1">{t("信号健康度")}</div>
                  <div className="flex flex-wrap gap-1.5">
                    <Badge tone="good">{tf("生产中 {n}", { n: h.active })}</Badge>
                    <Badge tone={h.paused ? "warn" : "muted"}>{tf("已暂停 {n}", { n: h.paused })}</Badge>
                    <Badge tone={h.retired ? "bad" : "muted"}>{tf("已停用 {n}", { n: h.retired })}</Badge>
                    <Badge tone="muted">{tf("未上线 {n}", { n: h.not_production })}</Badge>
                    <Badge tone={h.alerts ? "warn" : "muted"}>{tf("预警 {n}", { n: h.alerts })}</Badge>
                    {h.pause_suggested > 0 && <Badge tone="bad">{tf("建议暂停 {n}", { n: h.pause_suggested })}</Badge>}
                  </div>
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
      <div className="grid gap-5 xl:grid-cols-2">
        <Panel eyebrow={tf("共 {n} 次提交 · {s}", { n: a.total, s: Object.entries(a.by_status).filter(([, v]) => v).map(([k, v]) => `${t(a.status_zh[k])} ${v}`).join(" · ") })} title={t("审批瓶颈")}>
          <div className="label mb-1">{t("各角色平均处理用时（从上一步到本步决定）")}</div>
          {a.bottleneck.length === 0 ? <p className="text-sm text-muted">{t("还没有审批记录。")}</p> : (
            <ul className="flex flex-col gap-1 text-sm">
              {a.bottleneck.map((b) => {
                const max = Math.max(...a.bottleneck.map((x) => x.avg_hours), 0.01);
                return (
                  <li key={b.role} className="grid grid-cols-[9rem_minmax(0,1fr)_7rem] items-center gap-2">
                    <span>{t(b.role)}</span>
                    <span className="h-2 rounded bg-bg-3 overflow-hidden"><span className="block h-full bg-accent" style={{ width: `${Math.max(2, (b.avg_hours / max) * 100)}%` }} /></span>
                    <span className="num text-xs text-right">{tf("{d} · {n} 次", { d: dur(b.avg_hours), n: b.decisions })}</span>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="label mt-4 mb-1">{t("审批中")}</div>
          {a.pending.length === 0 ? <p className="text-sm text-muted">{t("没有审批中的提交。")}</p> : (
            <ul className="text-sm flex flex-col gap-1">
              {a.pending.map((p) => (
                <li key={p.id}><Link className="text-accent" to={`/approvals?sid=${p.id}`}>{p.title}</Link>
                  <span className="text-xs text-muted ml-2">{tf("等待「{r}」{d}", { r: t(p.role), d: dur(p.waiting_hours) })}</span></li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel eyebrow={t("监控预警与之后的人工处置")} title={t("异常处置")}>
          {data.incidents.length === 0 ? <p className="text-sm text-muted">{t("没有监控预警。")}</p> : (
            <ul className="flex flex-col gap-3 text-sm">
              {data.incidents.map((x) => (
                <li key={x.signal_id} className="border-l-2 pl-3" style={{ borderColor: marketColor(x.market) }}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Link className="font-medium text-accent" to={`/signals?sid=${x.signal_id}`}>{x.signal_id}</Link>
                    <Badge tone={x.handled.length ? "good" : "warn"}>{x.handled.length ? t("已处置") : t("待处置")}</Badge>
                    <span className="text-xs text-muted">{tf("当前：{s}", { s: t(x.state_zh) })}</span>
                  </div>
                  <div className="text-xs text-muted mt-0.5">{t(x.suggestion_zh)} · {joinList(x.alerts)}</div>
                  {x.handled.map((h) => <div key={h.at} className="text-xs mt-0.5">{h.at.slice(0, 16).replace("T", " ")} {t(h.role)}{t(h.action)}{t("：")}{h.note}</div>)}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
