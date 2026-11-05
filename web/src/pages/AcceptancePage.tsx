import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel, Stat } from "../components/ui";
import { api, type RedTeam } from "../api";
import { pct, useAsync } from "../lib";
import { t as tt, tf } from "../i18n";

// 验收（Spec 第 14.16 节；BR-52、53）：8 条 POC 成功标准、4 个待验证量化目标、红队测试

const STAGE_ZH: Record<string, string> = {
  spec: tt("立项"), data_code_backtest: tt("数据、代码与三市场回测"), comparison: tt("三市场比较与解释"), compliance: tt("合规预检"),
  portfolio_report: tt("组合与报告"), approval: tt("跨地区审批"),
};
const hours = (h: number | null | undefined) => (h == null ? "—" : h >= 24 ? tf("{n} 天", { n: (h / 24).toFixed(1) }) : tf("{n} 小时", { n: h.toFixed(1) }));

function Target({ label, value, target, ok, sub }: { label: string; value: string; target: string; ok: boolean | null; sub: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="label">{label}</div>
      <div className="flex items-baseline gap-2"><span className="num text-2xl font-semibold">{value}</span>
        {ok != null && <Badge tone={ok ? "good" : "bad"}>{ok ? tt("达到") : tt("未达到")}</Badge>}</div>
      <div className="text-xs text-muted">{tf("目标 {v}", { v: target })}</div>
      <div className="text-xs text-faint">{sub}</div>
    </div>
  );
}

export default function AcceptancePage() {
  const { data, error, loading, reload } = useAsync(() => api.acceptance(), []);
  const [rt, setRt] = useState<{ busy: boolean; data?: RedTeam; error?: string }>({ busy: false });
  if (loading) return <Loading what={tt("验收")} />;
  if (error || !data) return <ErrorBox error={error ?? tt("无数据")} />;
  const t = data.targets;
  const red = rt.data ?? data.redteam;
  const okCount = data.criteria.filter((c) => c.ok).length;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{tt("验收")}</div>
          <h1 className="font-display text-xl font-semibold">{tt("POC 成功标准与量化目标")}</h1>
        </div>
        <span className="text-sm text-muted">{tt("按平台上的实际记录自动核对 · 红队测试")}</span>
        <span className="ml-auto text-xs text-faint">{tf("计算于 {d} UTC", { d: t.computed_at.slice(0, 16).replace("T", " ") })}</span>
      </div>
      <Panel eyebrow={tt("业务 spec 原文 8 条，按平台记录自动核对")} title={tf("成功标准（{a} / {b}）", { a: okCount, b: data.criteria.length })}>
        <ol className="flex flex-col divide-y divide-line">
          {data.criteria.map((c) => (
            <li key={c.no} className="py-2.5 grid gap-1 sm:grid-cols-[2rem_minmax(0,1fr)_auto] items-start">
              <span className="num text-faint">{c.no}</span>
              <div><div className="font-medium">{c.text}</div><div className="text-xs text-muted mt-0.5">{c.evidence}</div></div>
              <div className="flex items-center gap-2"><Badge tone={c.ok ? "good" : "warn"}>{c.ok ? tt("满足") : tt("未满足")}</Badge>
                <Link className="text-xs text-accent" to={c.link}>{tt("查看")}</Link></div>
            </li>
          ))}
        </ol>
      </Panel>
      <Panel eyebrow={tt("BR-53：待验证量化目标（非预先承诺收益）")} title={tt("量化目标")}>
        <div className="grid gap-6 sm:grid-cols-2 xl:grid-cols-4">
          <Target label={tt("研究周期缩短")} value={pct(t.cycle.reduction, 0)} target="30%–50%" ok={t.cycle.reduction == null ? null : t.cycle.reduction >= 0.3}
            sub={tf("平台 {p} vs 人工基线 {b}（{s}）", { p: hours(t.cycle.platform_hours), b: hours(t.cycle.baseline_hours), s: t.cycle.baseline_status })} />
          <Target label={tt("重复研究工作减少")} value={pct(t.repeated_work.reduction, 0)} target="20%–40%" ok={t.repeated_work.reduction == null ? null : t.repeated_work.reduction >= 0.2}
            sub={tf("重复小时 {a} → {b}（按任务清单估算）", { a: t.repeated_work.manual_repeated_hours, b: t.repeated_work.platform_repeated_hours })} />
          <Target label={tt("关键结论来源引用覆盖率")} value={pct(t.citation.coverage, 0)} target="100%" ok={t.citation.coverage == null ? null : t.citation.coverage >= 1}
            sub={tf("展示的 AI 结论 {a}/{b} 句带引用；模型原始输出被丢弃 {c} 句", { a: t.citation.cited, b: t.citation.shown, c: t.citation.model_dropped })} />
          <Target label={tt("获批实验可复现率")} value={pct(t.reproducibility.rate, 0)} target="≥ 95%" ok={t.reproducibility.rate == null ? null : t.reproducibility.rate >= 0.95}
            sub={t.reproducibility.criteria} />
        </div>
        <div className="grid gap-5 xl:grid-cols-2 mt-5">
          <div>
            <div className="label mb-1">{tt("研究周期：各环节（日历时间）")}</div>
            <table className="w-full text-sm num">
              <thead className="label text-left"><tr><th className="py-1">{tt("环节")}</th><th>{tt("人工基线")}</th>
                {t.cycle.versions.filter((v) => v.complete).map((v) => <th key={`${v.research_id}${v.version}`}>{v.research_id} v{v.version}</th>)}</tr></thead>
              <tbody>
                {Object.entries(t.cycle.baseline_stages).map(([k, h]) => (
                  <tr key={k} className="border-t border-line"><td className="py-1.5 font-sans">{STAGE_ZH[k] ?? k}</td><td>{hours(h)}</td>
                    {t.cycle.versions.filter((v) => v.complete).map((v) => <td key={`${v.research_id}${v.version}`}>{hours(v.stages[k])}</td>)}</tr>
                ))}
              </tbody>
            </table>
            <p className="text-xs text-faint mt-1">{tt("平台数字按实际时间戳计算（含等待）。基线为假设值，试点前须换成三地团队的真实项目记录。")}</p>
          </div>
          <div>
            <div className="label mb-1">{tt("可复现：获批的市场版本")}</div>
            {t.reproducibility.items.length === 0 ? <p className="text-sm text-muted">{tt("还没有获批的版本。")}</p> : (
              <ul className="text-sm flex flex-col gap-1">
                {t.reproducibility.items.map((i) => (
                  <li key={i.signal} className="flex flex-wrap gap-2 items-baseline"><Badge tone={i.ok ? "good" : "bad"}>{i.ok ? tt("可复现") : tt("不满足")}</Badge>
                    <span className="num text-xs">{i.signal}</span><span className="text-xs text-muted">{tf("因子一致 {v}", { v: pct(i.factor_match, 1) })}{i.reason ? ` · ${i.reason}` : ""}</span>
                    {i.tie_range && i.tie_range[0] != null && <span className="text-xs text-faint w-full pl-1">{tf("并列敏感性（稳健性，不计入可复现率）：多个并列排序下扣成本年化 {a} ～ {b}", { a: pct(i.tie_range[0], 1), b: pct(i.tie_range[1], 1) })}</span>}</li>
                ))}
              </ul>
            )}
            <p className="text-xs text-faint mt-2">{tt("可复现 = 重新运行得到与获批时相同的结果。训练已固定随机种子与确定性模式（2026-10-13）；此前运行的检查需重新运行才能度量。并列敏感性计入可信度检查（第 14.7 节）。")}</p>
          </div>
        </div>
      </Panel>
      <Panel eyebrow={tt("对抗输入检查防线；审批与目录用例在临时目录中进行，不影响真实记录")} title={tt("红队测试")}
        actions={<button className="btn btn-primary" disabled={rt.busy} onClick={async () => {
          setRt({ busy: true });
          try { const d = await api.redteam(); setRt({ busy: false, data: d }); reload(); } catch (e) { setRt({ busy: false, error: (e as Error).message }); }
        }}>{rt.busy ? tt("运行中…") : tt("运行红队测试")}</button>}>
        {rt.error && <ErrorBox error={rt.error} />}
        {!red ? <Notice tone="accent" title={tt("还没有运行")}>{tt("点击右上角运行（约数秒）。")}</Notice> : (
          <>
            <div className="flex items-center gap-3 mb-3">
              <Stat label={tt("通过")} value={`${red.passed} / ${red.total}`} sub={tf("运行于 {d} UTC", { d: red.ran_at.slice(0, 16).replace("T", " ") })} />
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="label text-left"><tr><th className="py-1 pr-3">{tt("编号")}</th><th className="pr-3">{tt("结果")}</th><th className="pr-3">{tt("防线")}</th><th className="pr-3">{tt("攻击方式")}</th><th>{tt("证据")}</th></tr></thead>
                <tbody>
                  {red.results.map((r) => (
                    <tr key={r.id} className="border-t border-line align-top">
                      <td className="py-1.5 pr-3 num text-xs">{r.id}</td>
                      <td className="pr-3"><Badge tone={r.passed ? "good" : "bad"}>{r.passed ? tt("通过") : tt("失败")}</Badge></td>
                      <td className="pr-3">{r.title}</td><td className="pr-3 text-xs text-muted">{r.attack}</td>
                      <td className="text-xs text-faint max-w-[46ch]">{r.evidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}
