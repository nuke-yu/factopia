import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, MARKETS, type Market } from "../api";
import { testPeriodMentions, useDraft, type Draft } from "../draft";
import { cssVar, MARKET_LABEL, useAsync } from "../lib";
import { t, tf, joinList, IS_ZH } from "../i18n";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";

const HORIZONS: { value: Draft["horizon"]; label: string }[] = [
  { value: 1, label: t("1 天") }, { value: 5, label: t("1 周") }, { value: 20, label: t("1 个月") }, { value: 60, label: t("1 个季度") },
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
        idea_id: draft.ideaId ?? null, idea_ids: draft.ideaIds ?? [], combo_id: draft.comboId ?? null,
        based_on: draft.basedOn ? `${draft.basedOn.id}.v${draft.basedOn.version}` : null });
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
        <div className="label">{t("02 · 研究立项")}</div>
        <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">{t("提出假设，选择研究范围")}</h1>
        <p className="text-muted mt-1 max-w-[70ch]">{t("研究员只需表达想法和范围；AI 会据此起草研究规格（研究计划、候选方向、可证伪测试），在下一步由你审核确认（Spec 第 16 章）。")}</p>
      </div>
      {!!draft.ideaIds?.length && (
        <div className="panel p-3 flex flex-col gap-1.5 text-sm">
          <div className="flex flex-wrap items-center gap-3">
            <span className="label">{draft.basedOn ? t("追加线索") : t("合并转立项")}</span>
            <span className="text-muted">{draft.ideaTitle}</span>
            <span className="num text-xs text-faint">{draft.comboId}</span>
            <button type="button" className="btn h-7 px-2 text-xs ml-auto" onClick={() => update({ ideaIds: [], comboId: null, basedOn: null, ideaTitle: null })}>{t("不关联线索")}</button>
          </div>
          <div className="text-xs text-muted">{t("来源线索：")}<span className="num">{joinList(draft.ideaIds)}</span></div>
          <span className="text-xs text-faint">{draft.basedOn
            ? tf("锁定后成为研究 {id} 的新版本（基于 v{v}，已检验的旧版本不变）；", { id: draft.basedOn.id, v: draft.basedOn.version })
            : t("锁定后成为一个新研究；")}{t('所有来源线索标记为"已转立项"并指向该版本，每条线索的防泄漏与文档许可确认项会出现在审核页')}</span>
        </div>
      )}
      {draft.ideaId && (
        <div className="panel p-3 flex flex-wrap items-center gap-3 text-sm">
          <span className="label">{t("来自研究线索")}</span>
          <span className="num text-xs">{draft.ideaId}</span>
          <span className="text-muted">{draft.ideaTitle}</span>
          <span className="text-xs text-faint">{t('锁定版本后，该线索标记为"已转立项"并指向研究版本；防泄漏与文档许可的确认项会出现在审核页')}</span>
          <button type="button" className="btn h-7 px-2 text-xs ml-auto" onClick={() => update({ ideaId: null, ideaTitle: null })}>{t("不关联线索")}</button>
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-5 min-w-0">
          <Panel eyebrow={t("第 1 步")} title={t("客户授权")}>
            {mandates.error ? <ErrorBox error={mandates.error} /> : !mandates.data ? <Loading what={t("授权")} /> : (
              <>
                <label htmlFor="mandate" className="sr-only">{t("模拟客户授权")}</label>
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
                  <p className="text-sm mt-3"><span className="text-faint">{t("需要 AI 解释的条款：")}</span>{mandate.free_text.join(IS_ZH ? "；" : "; ")}</p>
                )}
                <p className="text-xs text-faint mt-2">{t("模拟授权，不含真实客户信息。结构化条款由系统直接转换为约束；自由文本条款由 AI 解释，在审核页逐条确认（第 16.5 节）。")}</p>
              </>
            )}
          </Panel>

          <Panel eyebrow={t("第 2 步")} title={t("市场与预测周期")}>
            <div className="flex flex-wrap gap-3">
              {MARKETS.map((m: Market) => {
                const on = draft.markets.includes(m);
                const ok = allowed.includes(m);
                return (
                  <button key={m} type="button" aria-pressed={on} disabled={!ok} title={ok ? undefined : t("所选授权不包含该市场")}
                    onClick={() => update({ markets: toggle(draft.markets, m) })}
                    className="flex items-center gap-2 h-10 px-4 rounded border transition-colors disabled:opacity-35 disabled:cursor-not-allowed"
                    style={{ borderColor: on ? cssVar(`--${m}`) : cssVar("--line-2"), background: on ? `color-mix(in oklab, ${cssVar(`--${m}`)} 14%, transparent)` : "transparent" }}>
                    <span className="size-2.5 rounded-full" style={{ background: cssVar(`--${m}`) }} />
                    <span>{MARKET_LABEL[m]}</span>
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-muted mt-2">{t("同一份研究规格在所选市场分别独立运行，再比较迁移性。")}</p>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <span className="label">{t("预测周期")}</span>
              <Seg label={t("预测周期")} value={draft.horizon} onChange={(v) => update({ horizon: v })} options={HORIZONS} />
              {draft.horizon !== 20 && <Badge tone="warn">{t("目前只有 20 日周期的评估结果")}</Badge>}
            </div>
          </Panel>

          <Panel eyebrow={t("第 3 步")} title={t("投资假设")}>
            <label className="label" htmlFor="hyp">{t("假设（中文、英文或法文）")}</label>
            <textarea id="hyp" className="field mt-1 min-h-28 leading-relaxed" value={draft.hypothesis} onChange={(e) => update({ hypothesis: e.target.value })} />
            <label className="label mt-3 block" htmlFor="dir">{t("研究方向")}</label>
            <input id="dir" className="field mt-1" value={draft.direction} onChange={(e) => update({ direction: e.target.value })} />
            {mentions.length > 0 && (
              <div className="mt-3">
                <Notice title={tf("输入中出现了测试期内的年份：{l}", { l: joinList(mentions) })}>
                  {t("RD-Agent 回测测试期从 2022 年开始。描述测试期内发生的事等于提前透露考题（MINE-11）。如确需保留，请在审核页确认。")}
                </Notice>
              </div>
            )}
          </Panel>

          <Panel eyebrow={t("第 4 步")} title={t("证据筛选")}>
            <div className="label mb-2">{t("提供给 AI 的因子类型")}</div>
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
                {t("包含以往运行的假设与采纳结果")}
              </label>
              <span className="text-muted">{t("评估区间：")}<span className="num text-text">2015-01 {t("至")} 2021-12</span>{t("（测试期之前，固定）")}</span>
            </div>
          </Panel>
        </div>

        <aside className="xl:sticky xl:top-20 self-start flex flex-col gap-4">
          <Panel eyebrow={t("摘要")} title={t("将提供给 AI 的内容")}>
            <ul className="flex flex-col gap-2 text-sm">
              <li className="flex justify-between gap-3"><span className="text-muted">{t("市场")}</span><span>{joinList(draft.markets.map((m) => MARKET_LABEL[m])) || t("未选择")}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">{t("预测周期")}</span><span className="num">{tf("{n} 个交易日", { n: draft.horizon })}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">{t("授权约束")}</span><span className="text-right">{mandate ? tf("{a} 条 + {b} 条待解释", { a: mandate.constraints.length, b: mandate.free_text.length }) : "—"}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">{t("证据：显著因子")}</span><span className="num">{tf("{n} 个", { n: evidence.length })}</span></li>
              <li className="flex justify-between gap-3"><span className="text-muted">{t("证据：以往运行")}</span><span>{draft.includePastRuns ? t("包含") : t("不包含")}</span></li>
            </ul>
            {evidence.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {evidence.map((f) => <Badge key={f.name} tone="accent">{f.name}</Badge>)}
              </div>
            )}
            <button className="btn btn-primary w-full mt-4 h-10 justify-center" disabled={!ready} onClick={startDraft}>
              {busy ? t("AI 起草中…") : t("AI 起草研究规格 →")}
            </button>
            {busy && <p className="text-xs text-muted mt-2">{t("正在计算证据包并请大模型起草，通常需要 10–60 秒。")}</p>}
            {error && <div className="mt-3"><ErrorBox error={error} /></div>}
            {!busy && !error && <p className="text-xs text-faint mt-2">{t("起草结果在下一步由你逐条审核、修改后锁定。")}</p>}
          </Panel>
        </aside>
      </div>
    </div>
  );
}
