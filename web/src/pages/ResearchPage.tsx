import { useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { api, MARKETS, type Market } from "../api";
import { MANDATES, testPeriodMentions, useDraft, type Draft } from "../draft";
import { cssVar, MARKET_LABEL, useAsync } from "../lib";
import { Badge, Notice, Panel, Seg } from "../components/ui";

const HORIZONS: { value: Draft["horizon"]; label: string }[] = [
  { value: 1, label: "1 天" }, { value: 5, label: "1 周" }, { value: 20, label: "1 个月" }, { value: 60, label: "1 个季度" },
];

export default function ResearchPage() {
  const { draft, update } = useDraft();
  const navigate = useNavigate();
  const cube = useAsync(() => api.cube(20), []);

  useEffect(() => {
    if (cube.data && draft.groups.length === 0) update({ groups: cube.data.groups });
  }, [cube.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const mandate = MANDATES.find((m) => m.id === draft.mandate)!;
  const mentions = testPeriodMentions(`${draft.hypothesis} ${draft.direction}`);
  const evidence = useMemo(() => {
    if (!cube.data) return [];
    return cube.data.factors.filter((f) => draft.groups.includes(f.group) && f.periods.train.label !== "无效");
  }, [cube.data, draft.groups]);
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const ready = draft.markets.length > 0 && draft.hypothesis.trim().length > 10;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <div className="label">01 · 研究立项</div>
        <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">提出假设，选择研究范围</h1>
        <p className="text-muted mt-1 max-w-[70ch]">研究员只需表达想法和范围；AI 会据此起草研究规格（研究计划、候选方向、可证伪测试），在下一步由你审核确认（Spec 第 16 章）。</p>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Panel eyebrow="第 1 步" title="客户授权">
            <label htmlFor="mandate" className="sr-only">模拟客户授权</label>
            <select id="mandate" className="field" value={draft.mandate} onChange={(e) => update({ mandate: e.target.value })}>
              {MANDATES.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            <dl className="grid sm:grid-cols-2 gap-x-6 gap-y-2 mt-3 text-sm">
              {mandate.terms.map(([k, v]) => (
                <div key={k} className="flex gap-3 border-b border-line pb-1.5"><dt className="text-faint w-24 shrink-0">{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
            <p className="text-xs text-faint mt-2">模拟授权，不含真实客户信息。授权解析将在接入后端后由系统完成（第 16.5 节）。</p>
          </Panel>

          <Panel eyebrow="第 2 步" title="市场与预测周期">
            <div className="flex flex-wrap gap-3">
              {MARKETS.map((m: Market) => {
                const on = draft.markets.includes(m);
                return (
                  <button key={m} type="button" aria-pressed={on} onClick={() => update({ markets: toggle(draft.markets, m) })}
                    className="flex items-center gap-2 h-10 px-4 rounded border transition-colors"
                    style={{ borderColor: on ? cssVar(`--${m}`) : cssVar("--line-2"), background: on ? `color-mix(in oklab, ${cssVar(`--${m}`)} 14%, transparent)` : "transparent" }}>
                    <span className="size-2.5 rounded-full" style={{ background: cssVar(`--${m}`) }} />
                    <span>{MARKET_LABEL[m]}</span>
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-muted mt-2">同一份研究规格在所选市场分别独立运行，再比较迁移性。</p>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <span className="label">预测周期</span>
              <Seg label="预测周期" value={draft.horizon} onChange={(v) => update({ horizon: v })} options={HORIZONS} />
              {draft.horizon !== 20 && <Badge tone="warn">目前只有 20 日周期的评估结果</Badge>}
            </div>
          </Panel>

          <Panel eyebrow="第 3 步" title="投资假设">
            <label className="label" htmlFor="hyp">假设（中文、英文或法文）</label>
            <textarea id="hyp" className="field mt-1 min-h-28 leading-relaxed" value={draft.hypothesis} onChange={(e) => update({ hypothesis: e.target.value })} />
            <label className="label mt-3 block" htmlFor="dir">研究方向</label>
            <input id="dir" className="field mt-1" value={draft.direction} onChange={(e) => update({ direction: e.target.value })} />
            {mentions.length > 0 && (
              <div className="mt-3">
                <Notice title={`输入中出现了测试期内的年份：${mentions.join("、")}`}>
                  RD-Agent 回测测试期从 2022 年开始。描述测试期内发生的事等于提前透露考题（MINE-11）。如确需保留，请在审核页确认。
                </Notice>
              </div>
            )}
          </Panel>

          <Panel eyebrow="第 4 步" title="证据筛选">
            <div className="label mb-2">提供给 AI 的因子类型</div>
            <div className="flex flex-wrap gap-2">
              {cube.data?.groups.map((g) => {
                const on = draft.groups.includes(g);
                return (
                  <button key={g} type="button" aria-pressed={on} onClick={() => update({ groups: toggle(draft.groups, g) })}
                    className={`h-8 px-3 rounded-full border text-sm transition-colors ${on ? "border-accent text-text bg-accent/10" : "border-line-2 text-muted"}`}>
                    {g}
                  </button>
                );
              })}
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-4 text-sm">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={draft.includePastRuns} onChange={(e) => update({ includePastRuns: e.target.checked })} />
                包含以往运行的假设与采纳结果
              </label>
              <span className="text-muted">评估区间：<span className="num text-text">2015-01 至 2021-12</span>（测试期之前，固定）</span>
            </div>
          </Panel>
        </div>

        <aside className="xl:sticky xl:top-20 self-start flex flex-col gap-4">
          <Panel eyebrow="摘要" title="将提供给 AI 的内容">
            <ul className="flex flex-col gap-2 text-sm">
              <li className="flex justify-between gap-3"><span className="text-muted">市场</span><span>{draft.markets.map((m) => MARKET_LABEL[m]).join("、") || "未选择"}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">预测周期</span><span className="num">{draft.horizon} 个交易日</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">约束</span><span className="text-right">{draft.constraints.length} 条（来自授权与示例）</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">证据：显著因子</span><span className="num">{evidence.length} 个</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">证据：以往运行</span><span>{draft.includePastRuns ? "包含" : "不包含"}</span></li>
            </ul>
            {evidence.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {evidence.map((f) => <Badge key={f.name} tone="accent">{f.name}</Badge>)}
              </div>
            )}
            <button className="btn btn-primary w-full mt-4 h-10 justify-center" disabled={!ready} onClick={() => navigate("/audit")}>
              AI 起草研究规格 →
            </button>
            <p className="text-xs text-faint mt-2">当前阶段：起草结果为示例草稿；大模型起草接口将在下一阶段接入（RS-02）。</p>
          </Panel>
        </aside>
      </div>
    </div>
  );
}
