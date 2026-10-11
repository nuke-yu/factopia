import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";
import { api, visibleMarkets, type Idea, type IdeaChannel, type IdeaDetail, type IdeaInput, type IdeaList, type Market } from "../api";
import { useDraft } from "../draft";
import { MARKET_LABEL, useAsync } from "../lib";

// 研究线索池（Spec 第 17 章，EXT-01）：首页汇集多种信息输入渠道，统一为线索卡片，挑选后转为研究立项

const MANUAL: { value: IdeaChannel; label: string }[] = [
  { value: "hypothesis", label: "投资假设" }, { value: "paper", label: "论文 / 研报" },
  { value: "experience", label: "研究员经验" }, { value: "feedback", label: "人工反馈" },
];
const BODY_LABEL: Record<string, [string, string]> = {
  hypothesis: ["假设（中文 / English / Français）", "例：管理层在公告中提到的风险增加，预示未来一个月股价相对落后"],
  paper: ["摘要或说明（上传文件时可简写）", "例：这篇研报讨论了波动率调整后的动量，请抽取可以检验的因子"],
  experience: ["经验描述", "例：港股在业绩发布后的三个交易日常出现与公告当日方向相反的走势"],
  feedback: ["反馈内容", "例：客户团队反映，跨境客户更关心信号在市场急跌时的表现"],
};
const STATUS_TONE = { new: "accent", converted: "good", duplicate: "muted", dismissed: "muted" } as const;
const AUTO = new Set<IdeaChannel>(["monitor", "portfolio", "approval"]);
const ACTOR_KEY = "factopia-idea-actor";

function readActor() { try { return localStorage.getItem(ACTOR_KEY) ?? ""; } catch { return ""; } }
function saveActor(v: string) { try { localStorage.setItem(ACTOR_KEY, v); } catch { /* 忽略 */ } }

function fileToBase64(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(new Error("文件读取失败"));
    r.readAsDataURL(f);
  });
}

function Submit({ meta, actor, setActor, onCreated }: { meta: IdeaList; actor: string; setActor: (v: string) => void; onCreated: (i: Idea) => void }) {
  const [ch, setCh] = useState<IdeaChannel>("hypothesis");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [markets, setMarkets] = useState<Market[]>(visibleMarkets());
  const [extra, setExtra] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const x = (k: string) => extra[k] ?? "";
  const setX = (k: string, v: string) => setExtra((e) => ({ ...e, [k]: v }));
  async function submit() {
    setBusy(true); setError(null);
    try {
      const input: IdeaInput = { channel: ch, title: title.trim(), body: body.trim(), markets, submitted_by: actor.trim(), confirm_no_client_data: confirm };
      if (ch === "hypothesis") input.direction = x("direction");
      if (ch === "experience") input.period = x("period");
      if (ch === "feedback") input.feedback_role = x("feedback_role") || meta.feedback_roles[0];
      if (ch === "paper") {
        if (file && file.size > 20 * 1024 * 1024) throw new Error("文件超过 20 MB");
        input.document = { source: x("source"), licence: x("licence"), licence_note: x("licence_note"), published: x("published"),
          sample_start: x("sample_start"), sample_end: x("sample_end"),
          ...(file ? { name: file.name, content_base64: await fileToBase64(file) } : {}) };
      }
      saveActor(actor.trim());
      const idea = await api.createIdea(input);
      setTitle(""); setBody(""); setExtra({}); setFile(null); setConfirm(false);
      onCreated(idea);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const [bodyLabel, placeholder] = BODY_LABEL[ch];
  return (
    <Panel eyebrow="人工渠道：提交后进入线索列表，由研究员挑选转为研究立项" title="提交线索">
      <div className="flex flex-col gap-3">
        <Seg label="渠道" value={ch} options={MANUAL} onChange={(v) => { setCh(v); setError(null); }} />
        <label className="text-sm">标题
          <input id="idea-title" className="field mt-1" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="一句话概括" />
        </label>
        <label className="text-sm">{bodyLabel}
          <textarea id="idea-body" className="field mt-1 min-h-24 leading-relaxed" value={body} maxLength={20000} onChange={(e) => setBody(e.target.value)} placeholder={placeholder} />
        </label>
        {ch === "hypothesis" && <label className="text-sm">方向（可选）
          <input id="idea-direction" className="field mt-1" value={x("direction")} onChange={(e) => setX("direction", e.target.value)} placeholder="例：只看中小盘、持有一个月" /></label>}
        {ch === "experience" && <label className="text-sm">经验来自的时间段（可选）
          <input id="idea-period" className="field mt-1" value={x("period")} onChange={(e) => setX("period", e.target.value)} placeholder="例：2018–2021 年观察" /></label>}
        {ch === "feedback" && <label className="text-sm">反馈来源
          <select id="idea-fbrole" className="field mt-1" value={x("feedback_role") || meta.feedback_roles[0]} onChange={(e) => setX("feedback_role", e.target.value)}>
            {meta.feedback_roles.map((r) => <option key={r}>{r}</option>)}</select></label>}
        {ch === "paper" && (
          <div className="grid gap-3 sm:grid-cols-2 border border-line rounded p-3">
            <label className="text-sm sm:col-span-2">文件（PDF / TXT / Markdown，≤ 20 MB；也可只粘贴摘要）
              <input id="idea-file" type="file" accept=".pdf,.txt,.md" className="field mt-1" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
            <label className="text-sm">来源（必填）
              <input id="idea-source" className="field mt-1" value={x("source")} onChange={(e) => setX("source", e.target.value)} placeholder="例：SSRN / 券商研报 / 团队内部" /></label>
            <label className="text-sm">许可类型（必填）
              <select id="idea-licence" className="field mt-1" value={x("licence")} onChange={(e) => setX("licence", e.target.value)}>
                <option value="">请选择</option>
                {Object.entries(meta.licences).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
            <label className="text-sm sm:col-span-2">许可说明{x("licence") && x("licence") !== "internal" ? "（必填）" : "（可选）"}
              <input id="idea-licnote" className="field mt-1" value={x("licence_note")} onChange={(e) => setX("licence_note", e.target.value)} placeholder="公开许可写许可名称；已购买授权写合同或订阅" /></label>
            <label className="text-sm">发表日期
              <input id="idea-pub" className="field mt-1" value={x("published")} onChange={(e) => setX("published", e.target.value)} placeholder="YYYY-MM-DD，未知可空" /></label>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-sm">样本期起
                <input id="idea-s0" className="field mt-1" value={x("sample_start")} onChange={(e) => setX("sample_start", e.target.value)} placeholder="YYYY-MM" /></label>
              <label className="text-sm">样本期止
                <input id="idea-s1" className="field mt-1" value={x("sample_end")} onChange={(e) => setX("sample_end", e.target.value)} placeholder="YYYY-MM" /></label>
            </div>
            <p className="text-xs text-faint sm:col-span-2">论文的因子通常是作者在历史数据上验证有效后才发表的：样本期或发表日期晚于测试期开始（2022 年）或未知时，平台会提示结果可能被高估。</p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-muted">涉及市场</span>
          {visibleMarkets().map((m) => (
            <label key={m} className="flex items-center gap-1.5"><input id={`idea-m-${m}`} type="checkbox" checked={markets.includes(m)}
              onChange={() => setMarkets((xs) => xs.includes(m) ? xs.filter((y) => y !== m) : [...xs, m])} />{MARKET_LABEL[m]}</label>
          ))}
        </div>
        <label className="text-sm">提交人
          <input id="idea-actor" className="field mt-1" value={actor} maxLength={60} onChange={(e) => setActor(e.target.value)} placeholder="姓名（演示用，不校验身份）" /></label>
        <label className="flex items-start gap-2 text-sm">
          <input id="idea-confirm" type="checkbox" className="mt-1" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
          <span>确认内容不含真实客户身份信息（不要输入客户姓名；平台会拦截电子邮件、电话、身份证号、IBAN 等格式明确的信息）</span>
        </label>
        {error && <ErrorBox error={error} />}
        <button className="btn btn-primary self-start" disabled={busy || !title.trim() || !actor.trim() || !confirm} onClick={submit}>
          {busy ? (ch === "paper" ? "提交并抽取候选因子（约 30–60 秒）…" : "提交中…") : "提交线索"}</button>
      </div>
    </Panel>
  );
}

function Card({ i, active, onClick }: { i: Idea; active: boolean; onClick: () => void }) {
  const dup = (i.similar?.ideas.length ?? 0) > 0;
  return (
    <button type="button" onClick={onClick} className={`w-full text-left p-3 rounded border transition-colors ${active ? "border-accent/60 bg-accent/10" : "border-line hover:border-accent/40"}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={AUTO.has(i.channel) ? "warn" : "accent"}>{i.channel_zh}</Badge>
        <Badge tone={STATUS_TONE[i.status]}>{({ new: "待处理", converted: "已转立项", duplicate: "重复", dismissed: "不跟进" })[i.status]}</Badge>
        {dup && <Badge tone="warn">可能重复</Badge>}
        {i.leakage?.overlap && <Badge tone="bad">防泄漏提示</Badge>}
        <span className="ml-auto text-xs text-faint num">{i.created_at.slice(0, 16).replace("T", " ")}</span>
      </div>
      <div className="mt-1.5 font-medium text-sm">{i.title}</div>
      <div className="text-xs text-muted mt-0.5 line-clamp-2">{i.excerpt}</div>
      <div className="text-xs text-faint mt-1">{i.submitted_by} · {i.markets.length ? i.markets.map((m) => MARKET_LABEL[m]).join(" / ") : "不限市场"}</div>
    </button>
  );
}

function SourceLink({ ref_ }: { ref_: Record<string, string> }) {
  if (ref_.signal_id) return <Link className="text-accent" to={`/signals?sid=${ref_.signal_id}`}>信号目录 · {ref_.signal_id}</Link>;
  if (ref_.submission) return <Link className="text-accent" to="/approvals">审批台 · {ref_.submission}</Link>;
  if (ref_.research) return <Link className="text-accent" to="/portfolio">组合台 · {ref_.research}</Link>;
  return null;
}

function Detail({ id, actor, onChanged }: { id: string; actor: string; onChanged: () => void }) {
  const { data, error, loading, reload } = useAsync(() => api.idea(id), [id]);
  const [picked, setPicked] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const { update } = useDraft();
  const navigate = useNavigate();
  useEffect(() => {
    setPicked((data?.extraction?.factors ?? []).filter((f) => f.availability.ok).map((f) => f.key));
    setTarget(data?.similar?.ideas[0]?.id ?? data?.similar?.research[0]?.ref ?? "");
  }, [data]);
  if (loading && !data) return <Loading what="线索" />;
  if (error || !data) return <ErrorBox error={error ?? "无数据"} />;
  const d: IdeaDetail = data;
  const run = async (f: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await f(); setNote(""); reload(); onChanged(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const convert = () => run(async () => {
    const p = await api.ideaPrefill(d.id, picked);
    update({ hypothesis: p.hypothesis, direction: p.direction, markets: p.markets, ideaId: p.idea_id, ideaTitle: `${p.channel_zh} · ${p.title}` });
    navigate("/research");
  });
  const who = actor.trim();
  const open = d.status === "new" || d.status === "converted";
  return (
    <Panel eyebrow={`${d.id} · ${d.channel_zh} · ${d.submitted_by}${d.role ? `（${d.role}）` : ""} · ${d.created_at.slice(0, 16).replace("T", " ")} UTC`} title={d.title}
      actions={<Badge tone={STATUS_TONE[d.status]}>{d.status_zh[d.status]}</Badge>}>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="flex flex-col gap-4 min-w-0">
          {d.body && <div><div className="label mb-1">原文（{({ zh: "中文", en: "英文", fr: "法文" })[d.language]}，不翻译、不改写）</div>
            <p className="text-sm whitespace-pre-wrap leading-relaxed">{d.body}</p></div>}
          {Object.keys(d.fields).length > 0 && <div className="text-sm text-muted">{Object.entries(d.fields).map(([k, v]) =>
            `${({ direction: "方向", period: "经验时间段", feedback_role: "反馈来源" } as Record<string, string>)[k] ?? k}：${v}`).join("；")}</div>}
          {d.source_ref && <div className="text-sm">来源：<SourceLink ref_={d.source_ref} /></div>}
          {d.document && (
            <div className="text-sm border border-line rounded p-3 flex flex-col gap-1">
              <div className="label">文档</div>
              <div>{d.document.name ?? "粘贴的文字"}{d.document.pages != null && ` · ${d.document.pages} 页`} · {d.document.paragraphs} 段
                {d.document.sha256 && <span className="num text-xs text-faint"> · sha256 {d.document.sha256.slice(0, 16)}…</span>}</div>
              <div>来源：{d.document.source} · 许可：{d.document.licence_zh}{d.document.licence_note && `（${d.document.licence_note}）`}</div>
              <div className="text-muted">发表 {d.document.published ?? "未知"} · 样本期 {d.document.sample_start ?? "?"} 至 {d.document.sample_end ?? "?"}
                {d.document.ai_filled?.length ? <span className="text-faint">（{d.document.ai_filled.length} 项由 AI 从原文抽取）</span> : null}</div>
            </div>
          )}
          {d.leakage && <Notice tone={d.leakage.overlap ? "bad" : "accent"} title={d.leakage.overlap ? "防泄漏提示" : "样本期早于测试期"}>{d.leakage.note}</Notice>}
          {d.channel === "paper" && (
            <div>
              <div className="flex items-center gap-2 mb-1">
                <div className="label">候选因子</div>
                {d.extraction && <span className="text-xs text-faint">{d.extraction.model} · 提示词 {d.extraction.prompt_version} · 用了 {d.extraction.paragraphs_used}/{d.extraction.paragraphs_total} 段
                  {d.extraction.dropped_no_cite > 0 && ` · 丢弃无引用 ${d.extraction.dropped_no_cite} 个`}</span>}
                <button type="button" className="btn h-7 px-2 text-xs ml-auto" disabled={busy || !who} onClick={() => run(() => api.extractIdea(d.id, who))}>
                  {busy ? "抽取中…" : d.extraction ? "重新抽取" : "抽取候选因子"}</button>
              </div>
              {d.extract_error && <p className="text-xs text-bad">{d.extract_error}</p>}
              {d.extraction?.summary_zh && <p className="text-sm text-muted mb-2">{d.extraction.summary_zh}</p>}
              {!d.extraction ? <p className="text-sm text-muted">尚未抽取。</p> : d.extraction.factors.length === 0 ? <p className="text-sm text-muted">没有抽取到带原文引用的因子。</p> : (
                <ul className="flex flex-col divide-y divide-line">
                  {d.extraction.factors.map((f) => (
                    <li key={f.key} className="py-2 flex gap-2">
                      <input id={`idea-f-${f.key}`} type="checkbox" className="mt-1" disabled={!f.availability.ok} checked={picked.includes(f.key)}
                        onChange={() => setPicked((xs) => xs.includes(f.key) ? xs.filter((y) => y !== f.key) : [...xs, f.key])} aria-label={`选择 ${f.name}`} />
                      <div className="min-w-0 text-sm flex flex-col gap-0.5">
                        <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{f.name}</span>
                          <Badge tone={f.availability.ok ? "good" : "muted"}>{f.availability.ok ? "可做" : "数据不可得"}</Badge>
                          <span className="text-xs text-faint">{f.availability.needs_zh.join("、")}</span></div>
                        <div>{f.description_zh}</div>
                        {f.formula && <div className="num text-xs text-muted">计算：{f.formula}</div>}
                        {f.logic_zh && <div className="text-xs text-muted">经济逻辑：{f.logic_zh}</div>}
                        {!f.availability.ok && <div className="text-xs text-warn">缺：{f.availability.missing.join("、")}</div>}
                        {f.quotes.map((q) => <blockquote key={q.p} className="text-xs text-faint border-l-2 border-line pl-2">P{q.p} · {q.text}</blockquote>)}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-4 min-w-0">
          <div>
            <div className="label mb-1">相似研究与线索（只作提示）</div>
            {!d.similar || (d.similar.research.length === 0 && d.similar.ideas.length === 0) ? <p className="text-sm text-muted">没有发现相似内容。</p> : (
              <ul className="text-sm flex flex-col gap-1">
                {d.similar.research.map((r) => <li key={r.ref}><Badge tone="muted">{r.kind_zh}</Badge> <span>{r.title}</span> <span className="num text-xs text-faint">得分 {r.score}</span></li>)}
                {d.similar.ideas.map((r) => <li key={r.id}><Badge tone="warn">线索</Badge> <span>{r.title}</span> <span className="text-xs text-faint">{r.channel_zh} · 重合 {Math.round(r.overlap * 100)}%</span></li>)}
              </ul>
            )}
          </div>
          {d.links.research?.length ? <div className="text-sm">已转立项：{d.links.research.map((r) => <span key={r} className="num text-xs mr-2">{r}</span>)}<Link className="text-accent" to="/projects">研究项目</Link></div> : null}
          {d.links.duplicate_of && <div className="text-sm text-muted">重复，指向：<span className="num text-xs">{d.links.duplicate_of}</span></div>}
          <div className="flex flex-col gap-2 border border-line rounded p-3">
            <div className="label">处理（处理人：{who || "请先在左侧填写提交人 / 处理人"}）</div>
            {open && <button className="btn btn-primary self-start" disabled={busy || (d.channel === "paper" && picked.length === 0)} onClick={convert}>
              转为研究立项{d.channel === "paper" ? `（${picked.length} 个因子）` : ""}</button>}
            <input id="idea-note" className="field" value={note} onChange={(e) => setNote(e.target.value)} placeholder="原因或意见" />
            <div className="flex flex-wrap gap-2">
              {d.status === "new" && <button className="btn" disabled={busy || !who || !note.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "dismiss", by: who, note }))}>不跟进</button>}
              <button className="btn" disabled={busy || !who || !note.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "comment", by: who, note }))}>写意见</button>
              {(d.status === "duplicate" || d.status === "dismissed") && <button className="btn" disabled={busy || !who} onClick={() => run(() => api.ideaAct(d.id, { action: "reopen", by: who, note }))}>重新打开</button>}
            </div>
            {d.status === "new" && (
              <div className="flex flex-wrap gap-2 items-center">
                <input id="idea-target" className="field flex-1 min-w-[12rem]" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="重复指向：线索编号或研究编号" />
                <button className="btn" disabled={busy || !who || !target.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "duplicate", by: who, note, target }))}>标为重复</button>
              </div>
            )}
            {err && <ErrorBox error={err} />}
          </div>
          <div>
            <div className="label mb-1">事件记录（只追加，哈希链{d.chain.ok ? "校验通过" : "校验失败"}）</div>
            <ul className="text-xs flex flex-col gap-1">
              {d.events.map((e) => <li key={e.hash}><span className="num text-faint mr-2">{e.at.slice(0, 16).replace("T", " ")}</span>{e.by} · {d.action_zh[e.action] ?? e.action}
                {e.research && ` → ${e.research}`}{e.target && ` → ${e.target}`}{e.note && <span className="text-muted"> · {e.note}</span>}</li>)}
            </ul>
          </div>
        </div>
      </div>
    </Panel>
  );
}

export default function IdeasPage() {
  const [params, setParams] = useSearchParams();
  const ch = params.get("channel") ?? "", st = params.get("status") ?? "new", mk = params.get("market") ?? "";
  const list = useAsync(() => api.ideas({ channel: ch, status: st === "all" ? "" : st, market: mk }), [ch, st, mk]);
  const [actor, setActor] = useState(readActor);
  const set = (k: string, v: string) => { const p = new URLSearchParams(params); if (v) p.set(k, v); else p.delete(k); setParams(p); };
  const meta = list.data;
  const sel = params.get("id") ?? meta?.items[0]?.id ?? "";
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">00 · 线索池</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">所有研究从一条线索开始</h1>
          <p className="text-muted mt-1 max-w-[80ch]">投资假设、论文研报、研究员经验与人工反馈从这里提交；监控预警、组合归因建议与审批退回自动回流。研究员挑选线索转为研究立项，之后走同一条有治理的流程（Spec 第 17 章）。</p>
        </div>
      </div>
      {list.error && <ErrorBox error={list.error} />}
      {meta && (
        <div className="panel p-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div><span className="label mr-2">待处理</span><span className="font-display text-xl text-accent num">{meta.counts.status.new ?? 0}</span></div>
          {Object.entries(meta.channels).map(([k, v]) => (
            <button key={k} type="button" onClick={() => set("channel", ch === k ? "" : k)}
              className={`flex items-center gap-1.5 ${ch === k ? "text-text" : "text-muted hover:text-text"}`}>
              <Badge tone={AUTO.has(k as IdeaChannel) ? "warn" : "accent"}>{v}</Badge><span className="num">{meta.counts.channel[k] ?? 0}</span>
            </button>
          ))}
          {meta.synced > 0 && <span className="text-xs text-faint">本次自动回流新增 {meta.synced} 条</span>}
        </div>
      )}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        {meta ? <Submit meta={meta} actor={actor} setActor={setActor} onCreated={(i) => { list.reload(); set("id", i.id); }} /> : <Loading what="线索池" />}
        <Panel eyebrow="人工提交（蓝）与自动回流（橙）" title={`线索（${meta?.items.length ?? 0}）`}>
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <Seg label="状态" value={st} onChange={(v) => set("status", v === "new" ? "" : v)}
              options={[{ value: "new", label: "待处理" }, { value: "converted", label: "已转立项" }, { value: "duplicate", label: "重复" }, { value: "dismissed", label: "不跟进" }, { value: "all", label: "全部" }]} />
            <Seg label="市场" value={mk} onChange={(v) => set("market", v)}
              options={[{ value: "", label: "全部市场" }, ...visibleMarkets().map((m) => ({ value: m as string, label: MARKET_LABEL[m] }))]} />
          </div>
          {list.loading && !meta ? <Loading what="线索" /> : meta && meta.items.length === 0 ? <p className="text-sm text-muted">没有符合条件的线索。</p> : (
            <div className="flex flex-col gap-2 max-h-[40rem] overflow-y-auto pr-1">
              {meta?.items.map((i) => <Card key={i.id} i={i} active={i.id === sel} onClick={() => set("id", i.id)} />)}
            </div>
          )}
        </Panel>
      </div>
      {sel && <Detail key={sel} id={sel} actor={actor} onChanged={list.reload} />}
    </div>
  );
}
