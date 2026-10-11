import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, MARKETS, type Market } from "../api";
import { testPeriodMentions, useDraft, type Draft } from "../draft";
import { cssVar, MARKET_LABEL, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";

const HORIZONS: { value: Draft["horizon"]; label: string }[] = [
  { value: 1, label: "1 天" }, { value: 5, label: "1 周" }, { value: 20, label: "1 个月" }, { value: 60, label: "1 个季度" },
];

export default function ResearchPage() {
  const { draft, update, setResult, setLocked } = useDraft();
  const navigate = useNavigate();
  const cube = useAsync(() => api.cube(20), []);
  const mandates = useAsync(() => api.mandates(), []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (cube.data && draft.groups.length === 0) update({ groups: cube.data.groups });
  }, [cube.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const mandate = mandates.data?.find((m) => m.id === draft.mandate);
  const allowed = mandate?.markets ?? MARKETS;
  const mentions = testPeriodMentions(`${draft.hypothesis} ${draft.direction}`);
  const evidence = useMemo(() => {
    if (!cube.data) return [];
    return cube.data.factors.filter((f) => draft.groups.includes(f.group) && f.periods.train.label !== "无效");
  }, [cube.data, draft.groups]);
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const ready = draft.markets.length > 0 && draft.hypothesis.trim().length > 10 && !!mandate && !busy;

  async function startDraft() {
    setBusy(true); setError(null);
    try {
      const r = await api.draft({ hypothesis: draft.hypothesis, direction: draft.direction, markets: draft.markets, horizon: draft.horizon,
        mandate: draft.mandate, groups: draft.groups.length === cube.data?.groups.length ? [] : draft.groups, include_past_runs: draft.includePastRuns,
        idea_id: draft.ideaId ?? null });
      setResult(r); setLocked(null);
      navigate("/audit");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <div>
        <div className="label">02 · 研究立项</div>
        <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">提出假设，选择研究范围</h1>
        <p className="text-muted mt-1 max-w-[70ch]">研究员只需表达想法和范围；AI 会据此起草研究规格（研究计划、候选方向、可证伪测试），在下一步由你审核确认（Spec 第 16 章）。</p>
      </div>
      {draft.ideaId && (
        <div className="panel p-3 flex flex-wrap items-center gap-3 text-sm">
          <span className="label">来自研究线索</span>
          <span className="num text-xs">{draft.ideaId}</span>
          <span className="text-muted">{draft.ideaTitle}</span>
          <span className="text-xs text-faint">锁定版本后，该线索标记为"已转立项"并指向研究版本；防泄漏与文档许可的确认项会出现在审核页</span>
          <button type="button" className="btn h-7 px-2 text-xs ml-auto" onClick={() => update({ ideaId: null, ideaTitle: null })}>不关联线索</button>
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Panel eyebrow="第 1 步" title="客户授权">
            {mandates.error ? <ErrorBox error={mandates.error} /> : !mandates.data ? <Loading what="授权" /> : (
              <>
                <label htmlFor="mandate" className="sr-only">模拟客户授权</label>
                <select id="mandate" className="field" value={draft.mandate} onChange={(e) => {
                  const m = mandates.data!.find((x) => x.id === e.target.value)!;
                  update({ mandate: m.id, markets: draft.markets.filter((x) => m.markets.includes(x)).length ? draft.markets.filter((x) => m.markets.includes(x)) : m.markets });
                }}>
                  {mandates.data.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
                <dl className="grid sm:grid-cols-2 gap-x-6 gap-y-2 mt-3 text-sm">
                  {mandate?.terms.map((t) => (
                    <div key={t.key} className="flex gap-3 border-b border-line pb-1.5">
                      <dt className="text-faint w-24 shrink-0">{t.name}</dt>
                      <dd className="min-w-0">{t.value}<div className="text-[11px] text-faint">{t.usage}</div></dd>
                    </div>
                  ))}
                </dl>
                {!!mandate?.free_text.length && (
                  <p className="text-sm mt-3"><span className="text-faint">需要 AI 解释的条款：</span>{mandate.free_text.join("；")}</p>
                )}
                <p className="text-xs text-faint mt-2">模拟授权，不含真实客户信息。结构化条款由系统直接转换为约束；自由文本条款由 AI 解释，在审核页逐条确认（第 16.5 节）。</p>
              </>
            )}
          </Panel>

          <Panel eyebrow="第 2 步" title="市场与预测周期">
            <div className="flex flex-wrap gap-3">
              {MARKETS.map((m: Market) => {
                const on = draft.markets.includes(m);
                const ok = allowed.includes(m);
                return (
                  <button key={m} type="button" aria-pressed={on} disabled={!ok} title={ok ? undefined : "所选授权不包含该市场"}
                    onClick={() => update({ markets: toggle(draft.markets, m) })}
                    className="flex items-center gap-2 h-10 px-4 rounded border transition-colors disabled:opacity-35 disabled:cursor-not-allowed"
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
              <li className="flex justify-between gap-3"><span className="text-muted">授权约束</span><span className="text-right">{mandate ? `${mandate.constraints.length} 条 + ${mandate.free_text.length} 条待解释` : "—"}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">证据：显著因子</span><span className="num">{evidence.length} 个</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">证据：以往运行</span><span>{draft.includePastRuns ? "包含" : "不包含"}</span></li>
            </ul>
            {evidence.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {evidence.map((f) => <Badge key={f.name} tone="accent">{f.name}</Badge>)}
              </div>
            )}
            <button className="btn btn-primary w-full mt-4 h-10 justify-center" disabled={!ready} onClick={startDraft}>
              {busy ? "AI 起草中…" : "AI 起草研究规格 →"}
            </button>
            {busy && <p className="text-xs text-muted mt-2">正在计算证据包并请大模型起草，通常需要 10–60 秒。</p>}
            {error && <div className="mt-3"><ErrorBox error={error} /></div>}
            {!busy && !error && <p className="text-xs text-faint mt-2">起草结果在下一步由你逐条审核、修改后锁定。</p>}
          </Panel>
        </aside>
      </div>
    </div>
  );
}
