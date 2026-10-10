import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type DraftResult, type Market, type Question, type Rationale, type SignalSource, type Spec } from "../api";
import { testPeriodMentions, useDraft } from "../draft";
import { cssVar, MARKET_LABEL, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";
import PrecheckView from "../components/PrecheckView";

const SOURCES: { value: SignalSource; label: string }[] = [
  { value: "price_volume", label: "量价（RD-Agent）" }, { value: "text", label: "文本信号" },
];
const RATIONALES: { value: Rationale["category"]; label: string }[] = [
  { value: "behavioural", label: "心理偏差" }, { value: "risk_premium", label: "风险补偿" }, { value: "friction", label: "交易摩擦" },
  { value: "flows", label: "资金流" }, { value: "other", label: "其他" },
];
const PLATFORM_QUESTIONS = [
  "排除方向的依据只来自测试期之前（2015–2021 年）的数据或以往运行",
  "可证伪测试的门槛在运行前确定，运行后不再修改",
];
const lines = (t: string) => t.split("\n").map((x) => x.replace(/^\s*(?:[-*•]|\d+[.)、])\s*/, "").trim()).filter(Boolean);

type Editable = { id: string; title_zh: string; hypothesis: string; rationale: Rationale; directions: string; exclusions: string; falsification: string; signal_source: SignalSource };

function fromDraft(r: DraftResult): Editable {
  const d = r.draft;
  return { id: d.id ?? "", title_zh: d.title_zh, hypothesis: d.hypothesis, rationale: d.rationale,
    directions: d.directions.join("\n"), exclusions: d.exclusions.join("\n"), falsification: d.falsification,
    signal_source: d.signal_source ?? "price_volume" };
}

export default function AuditPage() {
  const { draft, result, locked, setLocked } = useDraft();
  if (!result) {
    return (
      <div className="flex flex-col gap-5">
        <Header locked={null} />
        <Notice tone="accent" title="还没有 AI 草稿">
          先在 <Link to="/research" className="text-accent underline">01 研究立项</Link> 填写假设并点击"AI 起草研究规格"，草稿会带到这里审核。
        </Notice>
      </div>
    );
  }
  return <Review key={result.meta.at} result={result} markets={draft.markets} horizon={draft.horizon} inputs={draft} locked={locked} setLocked={setLocked} />;
}

function Header({ locked, mandate }: { locked: Spec | null; mandate?: string }) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div>
        <div className="label">02 · 审核</div>
        <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">审核研究规格</h1>
        <p className="text-muted mt-1 max-w-[72ch]">逐条检查 AI 起草的内容，修改处与 AI 原稿对比留痕；处理完待确认事项后锁定版本，再在所选市场依次运行。</p>
      </div>
      <div className="ml-auto flex items-center gap-2">
        <Badge tone={locked ? "good" : "warn"}>{locked ? `已锁定 v${locked.version}` : "草稿 · 未锁定"}</Badge>
        {mandate && <Badge>{mandate}</Badge>}
      </div>
    </div>
  );
}

function Review({ result, markets, horizon, inputs, locked, setLocked }: {
  result: DraftResult; markets: Market[]; horizon: number; inputs: ReturnType<typeof useDraft>["draft"];
  locked: Spec | null; setLocked: (s: Spec | null) => void;
}) {
  const original = useMemo(() => fromDraft(result), [result]);
  const [f, setF] = useState<Editable>(original);
  const set = (patch: Partial<Editable>) => setF((x) => ({ ...x, ...patch }));
  const d = result.draft;
  const questions: Question[] = useMemo(() => [
    ...d.open_questions.map((text) => ({ text, source: "AI" as const, confirmed: false })),
    ...d.system_questions.map((text) => ({ text, source: "系统" as const, confirmed: false })),
    ...PLATFORM_QUESTIONS.map((text) => ({ text, source: "平台" as const, confirmed: false })),
  ], [d]);
  const [checked, setChecked] = useState<boolean[]>(questions.map(() => false));
  // 可证伪测试按规定写明测试期（2022 年起），不算泄漏；其他字段与研究员原文中出现测试期年份时要求确认
  const mentions = testPeriodMentions([f.hypothesis, f.directions, f.exclusions, inputs.hypothesis, inputs.direction].join(" "));
  const leakQ = mentions.length ? `保留测试期年份（${mentions.join("、")}），并已知悉其泄漏风险` : null;
  const [leakOk, setLeakOk] = useState(false);

  const spec: Spec = useMemo(() => ({
    id: f.id.trim(), title_zh: f.title_zh.trim(), mandate: inputs.mandate, markets, horizon,
    hypothesis_original: inputs.hypothesis, direction_original: inputs.direction,
    hypothesis: f.hypothesis.trim(), hypothesis_zh: d.hypothesis_zh, rationale: { ...f.rationale, note: f.rationale.note.trim() },
    directions: lines(f.directions), exclusions: lines(f.exclusions), falsification: f.falsification.trim(),
    data_needs: d.data_needs, constraints: d.constraints,
    evidence: { groups: result.evidence.groups, include_past_runs: inputs.includePastRuns, items: result.evidence.alpha158.map((e) => e.id) },
    questions: [...questions.map((q, i) => ({ ...q, confirmed: checked[i] })), ...(leakQ ? [{ text: leakQ, source: "系统" as const, confirmed: leakOk }] : [])],
    run: { loops: 5, budget_usd: 2 }, signal_source: f.signal_source,
    ai_draft: { ...result.meta, original: { hypothesis: d.hypothesis, rationale: d.rationale, directions: d.directions, exclusions: d.exclusions, falsification: d.falsification } },
  }), [f, d, inputs, markets, horizon, questions, checked, leakQ, leakOk, result]);

  const missing = [
    !/^[a-z][a-z0-9_]{2,39}$/.test(spec.id) && "编号（只能用小写字母、数字和下划线，3–40 个字符，以字母开头）",
    !spec.hypothesis && "规范化假设", !spec.rationale.note && "经济逻辑", !spec.falsification && "可证伪测试",
  ].filter(Boolean) as string[];
  const pending = spec.questions.filter((q) => !q.confirmed).length;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tried, setTried] = useState(false);   // 点过锁定但条件未满足：标出未勾选的事项
  const ro = !!locked;

  const pre = useAsync(() => api.precheck(spec), [spec.mandate, JSON.stringify(spec.markets), JSON.stringify(spec.data_needs)]);
  const blocked = !!pre.data?.blocked;

  async function lock() {
    setBusy(true); setError(null);
    try { setLocked(await api.lock(spec)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  const edited = (k: keyof Editable) => JSON.stringify(f[k]) !== JSON.stringify(original[k]);
  return (
    <div className="flex flex-col gap-5">
      <Header locked={locked} mandate={result.mandate.name} />
      <div className="text-xs text-faint num">
        AI 起草：{result.meta.model} · 提示词模板 {result.meta.prompt_version} · {result.meta.prompt_tokens + result.meta.completion_tokens} tokens · {result.meta.seconds} 秒
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Panel eyebrow="研究规格" title="逐条审阅">
          <div className="flex flex-col divide-y divide-line">
            <Row name="编号" source="AI 起草" edited={edited("id")} hint="锁定后作为研究项目编号，同一编号的后续锁定生成新版本">
              <input aria-label="编号" className="field num" value={f.id} disabled={ro} onChange={(e) => set({ id: e.target.value })} />
            </Row>
            <Row name="标题" source="AI 起草" edited={edited("title_zh")}>
              <input aria-label="标题" className="field" value={f.title_zh} disabled={ro} onChange={(e) => set({ title_zh: e.target.value })} />
            </Row>
            <Row name="原始假设" source="研究员">
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{inputs.hypothesis}</p>
            </Row>
            <Row name="规范化假设" source="AI 起草" required edited={edited("hypothesis")} original={original.hypothesis} hint="英文，发给 RD-Agent">
              <Area label="规范化假设" value={f.hypothesis} disabled={ro} onChange={(v) => set({ hypothesis: v })} />
              {d.hypothesis_zh && <p className="text-xs text-muted mt-1 leading-relaxed"><span className="text-faint">中文译文（AI 原稿，不随修改更新）：</span>{d.hypothesis_zh}</p>}
            </Row>
            <Row name="经济逻辑" source="AI 起草" required edited={edited("rationale")} original={`${original.rationale.category}: ${original.rationale.note}`}>
              <div className="mb-2"><Seg label="经济逻辑类别" value={f.rationale.category} options={RATIONALES}
                onChange={(v) => !ro && set({ rationale: { ...f.rationale, category: v } })} /></div>
              <Area label="经济逻辑说明" value={f.rationale.note} disabled={ro} onChange={(v) => set({ rationale: { ...f.rationale, note: v } })} />
            </Row>
            <Row name="候选特征方向" source="AI 起草" edited={edited("directions")} original={original.directions} hint="每行一个方向">
              <Area label="候选特征方向" value={f.directions} disabled={ro} onChange={(v) => set({ directions: v })} />
            </Row>
            <Row name="排除方向" source="AI 起草" edited={edited("exclusions")} original={original.exclusions} hint="每行一个，附依据">
              <Area label="排除方向" value={f.exclusions} disabled={ro} onChange={(v) => set({ exclusions: v })} />
            </Row>
            <Row name="可证伪测试" source="AI 起草" required edited={edited("falsification")} original={original.falsification} hint="在回测测试期上判断">
              <Area label="可证伪测试" value={f.falsification} disabled={ro} onChange={(v) => set({ falsification: v })} />
            </Row>
            <Row name="信号来源" source="系统" edited={edited("signal_source")} hint="量价：RD-Agent 自动挖掘；文本：检验情报台的风险表述变化信号（不启动 RD-Agent）">
              <Seg label="信号来源" value={f.signal_source} options={SOURCES} onChange={(v) => !ro && set({ signal_source: v })} />
              {f.signal_source === "text" && <p className="text-xs text-muted mt-1">香港、英国为模拟数据，仅用于流程演示；法国为 AMF 真实公告（2024 年 8 月起，测试期内数据不足）。可证伪测试须写明 rank IC 方向（positive / negative）。</p>}
            </Row>
            <Row name="数据需求与缺口" source="系统">
              <DataNeeds result={result} markets={markets} />
            </Row>
            <Row name="约束" source="系统" hint="授权结构化条款 + 已解释的自由文本条款 + 平台规则">
              <ul className="text-sm flex flex-col gap-1">{d.constraints.map((c) => <li key={c} className="num text-xs">· {c}</li>)}</ul>
            </Row>
            <Row name="证据包" source="系统" hint="只用测试期之前的数据（MINE-11）">
              <EvidenceList result={result} />
            </Row>
          </div>
        </Panel>

        <aside className="flex flex-col gap-5 self-start">
          <Panel eyebrow="锁定前" title={`待确认事项（${spec.questions.length - pending} / ${spec.questions.length}）`}>
            <ul className="flex flex-col gap-2.5">
              {questions.map((q, i) => (
                <li key={q.text}>
                  <label className={`flex gap-2 text-sm leading-snug ${!ro && !checked[i] && tried ? "text-warn" : ""}`}>
                    <input type="checkbox" disabled={ro} checked={checked[i]} className="mt-0.5 shrink-0"
                      onChange={(e) => setChecked((c) => c.map((x, j) => (j === i ? e.target.checked : x)))} />
                    <span><Badge tone={q.source === "AI" ? "accent" : "muted"}>{q.source}</Badge> {q.text}</span>
                  </label>
                </li>
              ))}
              {leakQ && (
                <li>
                  <label className="flex gap-2 text-sm leading-snug text-warn">
                    <input type="checkbox" disabled={ro} checked={leakOk} onChange={(e) => setLeakOk(e.target.checked)} className="mt-0.5 shrink-0" />
                    <span>{leakQ}</span>
                  </label>
                </li>
              )}
            </ul>
            <p className="text-xs text-faint mt-2">勾选表示已阅读并接受；不同意的内容请直接修改左侧字段。确认记录随版本保存。</p>
          </Panel>
          <Panel eyebrow="MINE-11" title="防泄漏检查">
            <ul className="flex flex-col gap-2 text-sm">
              <Check ok text={`证据包计算区间截至 ${result.evidence.alpha158[0]?.period.end ?? "—"}（测试期 ${result.evidence.alpha158[0]?.test_start ?? "—"} 起）`} />
              <Check ok text="以往运行只提供假设方向与是否采纳；受泄漏影响的运行已排除" />
              <Check ok={mentions.length === 0} text={mentions.length ? `规格中出现测试期年份：${mentions.join("、")}` : "规格中没有测试期内的年份"} />
              <Check ok={false} warnOnly text="大模型自身可能记得测试期的行情，无法消除（第 8 章 #20）" />
            </ul>
          </Panel>
          <Panel eyebrow="BR-21 · 锁定前" title="立项预检">
            {pre.loading && <div className="label">检查中…</div>}
            {pre.error && <div className="text-xs text-bad">{pre.error}</div>}
            {(locked?.precheck_setup ?? pre.data) && <PrecheckView pre={(locked?.precheck_setup ?? pre.data)!} compact />}
            <p className="text-xs text-faint mt-2">授权、市场、数据来源与许可按通用规则包和所选市场的规则包检查；建议审批路径与组合预检见 <Link className="text-accent" to="/rules">风控台</Link>。</p>
          </Panel>
          {!locked ? (
            <div className="flex flex-col gap-2">
              {!tried && (missing.length > 0 || pending > 0) && (
                <div className="text-xs text-muted">锁定前需要：必填字段完整，"待确认事项"全部勾选（{spec.questions.length - pending} / {spec.questions.length}）</div>
              )}
              {tried && (missing.length > 0 || pending > 0 || blocked) && (
                <Notice title="还不能锁定">
                  <ul className="flex flex-col gap-1">
                    {missing.length > 0 && <li>请填写或修正：{missing.join("、")}</li>}
                    {pending > 0 && <li>"待确认事项"中还有 {pending} 项未勾选（已标为黄色）</li>}
                    {blocked && <li>立项预检命中阻断规则，须先修改研究范围或数据来源</li>}
                  </ul>
                </Notice>
              )}
              <button className="btn btn-primary h-10 justify-center" disabled={busy} onClick={() => {
                if (missing.length > 0 || pending > 0 || blocked) return setTried(true);
                lock();
              }}>
                {busy ? "锁定中…" : "锁定版本"}
              </button>
              {error && <ErrorBox error={error} />}
            </div>
          ) : <RunLauncher spec={locked} />}
        </aside>
      </div>

      <Panel eyebrow="RS-07 · 所见即所发" title="提示词预览">
        <PromptPreview spec={locked ?? spec} markets={markets} />
      </Panel>
    </div>
  );
}

function Row({ name, source, required, edited, original, hint, children }: {
  name: string; source: "研究员" | "AI 起草" | "系统"; required?: boolean; edited?: boolean; original?: string | string[]; hint?: string; children: ReactNode;
}) {
  return (
    <div className="py-3 grid gap-2 md:grid-cols-[150px_1fr]">
      <div className="flex md:flex-col gap-2 md:gap-1">
        <div className="font-medium">{name}{required && <span className="text-bad"> *</span>}</div>
        <div className="flex gap-1.5 flex-wrap">
          <Badge tone={source === "AI 起草" ? "accent" : "muted"}>{source}</Badge>
          {edited && <Badge tone="warn">已修改</Badge>}
        </div>
        {hint && <div className="text-[11px] text-faint leading-snug">{hint}</div>}
      </div>
      <div className="min-w-0">
        {children}
        {edited && original != null && (
          <details className="mt-1 text-xs">
            <summary className="text-muted cursor-pointer">查看 AI 原稿</summary>
            <p className="text-faint mt-1 whitespace-pre-wrap line-through decoration-bad/60">{Array.isArray(original) ? original.join("\n") : original}</p>
          </details>
        )}
      </div>
    </div>
  );
}

function Area({ label, value, disabled, onChange }: { label: string; value: string; disabled?: boolean; onChange: (v: string) => void }) {
  return (
    <textarea aria-label={label} className="field leading-relaxed text-sm" disabled={disabled} value={value} onChange={(e) => onChange(e.target.value)}
      rows={Math.max(2, value.split("\n").length + Math.ceil(value.length / 130))} />
  );
}

function DataNeeds({ result, markets }: { result: DraftResult; markets: Market[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs text-muted"><tr>
          <th className="text-left font-normal py-1">数据</th>
          {markets.map((m) => <th key={m} className="text-left font-normal">{MARKET_LABEL[m]}</th>)}
          <th className="text-left font-normal">挖掘可用</th>
        </tr></thead>
        <tbody>
          {result.draft.data_needs.map((n) => (
            <tr key={n.category + n.detail} className="border-t border-line align-top">
              <td className="py-1.5 pr-3"><div>{n.name}</div><div className="text-xs text-faint">{n.detail}</div></td>
              {markets.map((m) => <td key={m} className="pr-3 text-xs">{n.status[m]}</td>)}
              <td>{n.usable ? <Badge tone="good">可用</Badge> : <Badge tone="warn">缺口</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceList({ result }: { result: DraftResult }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      {result.evidence.alpha158.map((e) => (
        <div key={e.id}>
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-medium">{MARKET_LABEL[e.market]}</span>
            <span className="num text-xs text-muted">{e.period.start} 至 {e.period.end} · 显著因子 {e.significant} 个</span>
          </div>
          <div className="flex flex-wrap gap-1.5 mt-1">
            {e.factors.length ? e.factors.map((x) => (
              <span key={x.name} title={`${x.desc} · rank IC ${x.ric} · ${x.label}`}><Badge tone="accent">{x.name} t={x.t}</Badge></span>
            )) : <span className="text-xs text-faint">该区间没有 |t| &gt; 3 的因子</span>}
          </div>
        </div>
      ))}
      <div className="text-xs text-muted">以往运行：{result.evidence.past_runs.length ? `${result.evidence.past_runs.length} 轮假设（只含方向与是否采纳）` : "无可用记录（受泄漏影响的运行已排除）"}</div>
    </div>
  );
}

function Check({ ok, warnOnly, text }: { ok: boolean; warnOnly?: boolean; text: string }) {
  const tone = ok ? "--good" : warnOnly ? "--warn" : "--bad";
  return (
    <li className="flex gap-2 leading-snug">
      <span className="num shrink-0" style={{ color: cssVar(tone) }}>{ok ? "通过" : warnOnly ? "提示" : "注意"}</span>
      <span className={ok ? "text-muted" : ""}>{text}</span>
    </li>
  );
}

function RunLauncher({ spec }: { spec: Spec }) {
  const navigate = useNavigate();
  const [markets, setMarkets] = useState<Market[]>(spec.markets);
  const [loops, setLoops] = useState(spec.run.loops);
  const [budget, setBudget] = useState(spec.run.budget_usd);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function start() {
    setBusy(true); setError(null);
    try {
      const r = await api.startRuns(spec.id, spec.version!, { markets, loops, budget_usd: budget });
      navigate(`/run?job=${r.jobs[0].job_id}`);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }
  return (
    <Panel eyebrow={`${spec.id} · v${spec.version}`} title="运行">
      <div className="flex flex-col gap-3 text-sm">
        <div className="flex flex-wrap gap-3">
          {spec.markets.map((m) => (
            <label key={m} className="flex items-center gap-1.5">
              <input type="checkbox" checked={markets.includes(m)} onChange={(e) => setMarkets((xs) => e.target.checked ? spec.markets.filter((x) => x === m || xs.includes(x)) : xs.filter((x) => x !== m))} />
              <span className="size-2 rounded-full" style={{ background: cssVar(`--${m}`) }} />{MARKET_LABEL[m]}
            </label>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1"><span className="label">每个市场轮数</span>
            <input id="loops" type="number" min={1} max={20} className="field num" value={loops} onChange={(e) => setLoops(Number(e.target.value))} /></label>
          <label className="flex flex-col gap-1"><span className="label">每个市场费用上限（美元）</span>
            <input id="budget" type="number" min={0.1} max={20} step={0.1} className="field num" value={budget} onChange={(e) => setBudget(Number(e.target.value))} /></label>
        </div>
        <p className="text-xs text-muted">按 {markets.map((m) => MARKET_LABEL[m]).join(" → ")} 的顺序依次运行；每个市场 5 轮约 20–40 分钟、0.1–0.3 美元。累计费用超过上限时自动停止。</p>
        <button className="btn btn-primary h-10 justify-center" disabled={busy || markets.length === 0 || loops < 1 || budget <= 0} onClick={start}>
          {busy ? "提交中…" : `开始运行（${markets.length} 个市场）`}
        </button>
        {error && <ErrorBox error={error} />}
      </div>
    </Panel>
  );
}

function PromptPreview({ spec, markets }: { spec: Spec; markets: Market[] }) {
  const [market, setMarket] = useState<Market>(markets[0] ?? "hk");
  const [debounced, setDebounced] = useState(spec);
  useEffect(() => { const id = setTimeout(() => setDebounced(spec), 700); return () => clearTimeout(id); }, [spec]);
  const p = useAsync(() => api.preview(debounced, market), [debounced, market]);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-3 items-center">
        <Seg label="市场" value={market} onChange={setMarket} options={markets.map((m) => ({ value: m, label: MARKET_LABEL[m] }))} />
        {p.data && <span className="num text-xs text-muted">{p.data.chars.toLocaleString()} 字符 · 测试期 {p.data.dates.TEST_START} 至 {p.data.dates.TEST_END}</span>}
        {p.loading && <span className="text-xs text-faint">更新中…</span>}
      </div>
      {p.error ? <ErrorBox error={p.error} /> : !p.data ? <Loading what="提示词" /> : (
        <pre className="num text-xs whitespace-pre-wrap leading-relaxed text-muted max-h-[520px] overflow-y-auto bg-bg rounded p-3 border-l-2"
          style={{ borderColor: cssVar(`--${market}`) }}>{p.data.text}</pre>
      )}
      <p className="text-xs text-faint">由 RD-Agent 场景子类运行时使用的同一个函数生成（factopia.research.context.build_context），追加在 RD-Agent 自带的通用背景之后发给 AI。修改左侧内容后自动更新。</p>
    </div>
  );
}
