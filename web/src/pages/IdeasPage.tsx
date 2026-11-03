import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";
import { api, visibleMarkets, type Idea, type IdeaChannel, type IdeaCombo, type IdeaDetail, type IdeaInput, type IdeaList, type IdeaSuggestion, type Market } from "../api";
import { useDraft } from "../draft";
import { MARKET_LABEL, useAsync } from "../lib";
import SuggestionBasis from "../components/SuggestionBasis";
import { t, tf, joinList } from "../i18n";

// 研究线索池（Spec 第 17 章，EXT-01）：首页汇集多种信息输入渠道，统一为线索卡片，挑选后转为研究立项

const MANUAL: { value: IdeaChannel; label: string }[] = [
  { value: "hypothesis", label: t("投资假设") }, { value: "paper", label: t("论文 / 研报") },
  { value: "experience", label: t("研究员经验") }, { value: "feedback", label: t("人工反馈") },
];
const BODY_LABEL: Record<string, [string, string]> = {
  hypothesis: [t("假设（中文 / English / Français）"), t("例：管理层在公告中提到的风险增加，预示未来一个月股价相对落后")],
  paper: [t("摘要或说明（上传文件时可简写）"), t("例：这篇研报讨论了波动率调整后的动量，请抽取可以检验的因子")],
  experience: [t("经验描述"), t("例：港股在业绩发布后的三个交易日常出现与公告当日方向相反的走势")],
  feedback: [t("反馈内容"), t("例：客户团队反映，跨境客户更关心信号在市场急跌时的表现")],
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
    r.onerror = () => reject(new Error(t("文件读取失败")));
    r.readAsDataURL(f);
  });
}

function Submit({ meta, actor, setActor, onCreated, onSuggestion }: { meta: IdeaList; actor: string; setActor: (v: string) => void; onCreated: (i: Idea) => void;
  onSuggestion: (s: IdeaSuggestion | null) => void }) {
  const [ch, setCh] = useState<IdeaChannel>("hypothesis");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [markets, setMarkets] = useState<Market[]>(visibleMarkets());
  const [extra, setExtra] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // AI 默认假设（Spec 第 17.11 节）：论文以外的渠道打开即预填，每次不同；"换一个"重新生成
  const [sug, setSug] = useState<IdeaSuggestion | null>(null);
  const [sugBusy, setSugBusy] = useState(false);
  const [sugErr, setSugErr] = useState<string | null>(null);
  const [seen, setSeen] = useState<string[]>([]);
  const [seed, setSeed] = useState("");             // 研究员的想法种子：填了就以它为主线，不填由平台按证据选方向
  async function suggest(channel: IdeaChannel, another = false) {
    if (channel === "paper") { setSug(null); return; }
    setSugBusy(true); setSugErr(null);
    try {
      const r = await api.ideaSuggest(channel, markets.length ? markets : visibleMarkets(), seen.slice(-20), seed.trim(),
        another && !seed.trim() && sug ? sug.angle : null);
      setSug(r); setSeen((xs) => [...xs, r.title]);
      setTitle(r.title); setBody(r.body);
      setExtra((e) => ({ ...e, direction: r.direction, period: r.period, feedback_role: r.feedback_role }));
      if (r.markets.length) setMarkets(r.markets.filter((m) => visibleMarkets().includes(m)));
    } catch (e) { setSugErr((e as Error).message); } finally { setSugBusy(false); }
  }
  useEffect(() => { suggest(ch); }, [ch]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { onSuggestion(ch === "paper" ? null : sug); }, [sug, ch]); // eslint-disable-line react-hooks/exhaustive-deps
  const edited = !!sug && (sug.title !== title.trim() || sug.body !== body.trim());
  const x = (k: string) => extra[k] ?? "";
  const setX = (k: string, v: string) => setExtra((e) => ({ ...e, [k]: v }));
  async function submit() {
    setBusy(true); setError(null);
    try {
      const input: IdeaInput = { channel: ch, title: title.trim(), body: body.trim(), markets, submitted_by: actor.trim(), confirm_no_client_data: confirm,
        ai_suggestion: ch !== "paper" ? sug : null };
      if (ch === "hypothesis") input.direction = x("direction");
      if (ch === "experience") input.period = x("period");
      if (ch === "feedback") input.feedback_role = x("feedback_role") || meta.feedback_roles[0];
      if (ch === "paper") {
        if (file && file.size > 20 * 1024 * 1024) throw new Error(t("文件超过 20 MB"));
        input.document = { source: x("source"), licence: x("licence"), licence_note: x("licence_note"), published: x("published"),
          sample_start: x("sample_start"), sample_end: x("sample_end"),
          ...(file ? { name: file.name, content_base64: await fileToBase64(file) } : {}) };
      }
      saveActor(actor.trim());
      const idea = await api.createIdea(input);
      setTitle(""); setBody(""); setExtra({}); setFile(null); setConfirm(false); setSug(null);
      onCreated(idea);
      suggest(ch);                                   // 提交后给出下一条不同的建议
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const [bodyLabel, placeholder] = BODY_LABEL[ch];
  return (
    <Panel eyebrow={t("人工渠道：提交后进入线索列表，由研究员挑选转为研究立项")} title={t("提交线索")}>
      <div className="flex flex-col gap-3">
        <Seg label={t("渠道")} value={ch} options={MANUAL} onChange={(v) => { setCh(v); setError(null); setTitle(""); setBody(""); setExtra({}); }} />
        {ch !== "paper" && (
          <div className="border border-accent/40 bg-accent/5 rounded p-3 text-sm flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="accent">{t("AI 默认假设")}</Badge>
              {sug && <span className="text-xs text-faint">{tf("方向：{angle}", { angle: t(sug.angle) })}{sug.focus_market ? tf(" · 侧重{market}", { market: MARKET_LABEL[sug.focus_market] }) : ""} · {sug.model} · {tf("提示词 {v}", { v: sug.prompt_version })}{edited ? ` · ${t("已修改")}` : ""}</span>}
              <button type="button" className="btn h-7 px-2 text-xs ml-auto" disabled={sugBusy} onClick={() => suggest(ch, true)}>{sugBusy ? t("AI 生成中…") : seed.trim() ? t("按想法生成") : t("换一个")}</button>
            </div>
            <label className="text-xs text-muted">{t("你的想法（可选）：填一句话或几个关键词，AI 围绕它展开；不填则由平台按证据选方向")}
              <input id="idea-seed" className="field mt-1 text-sm" value={seed} maxLength={500} onChange={(e) => setSeed(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !sugBusy) { e.preventDefault(); suggest(ch, true); } }}
                placeholder={t("例如：港股大跌之后放量的股票反弹更快")} />
            </label>
            {sugBusy && !sug && <p className="text-muted">{t("AI 正在根据平台证据生成建议（约 10–20 秒）…")}</p>}
            {sugErr && <p className="text-xs text-bad">{sugErr}</p>}
            {sug && <>
              {sug.selection && sug.selection.length > 0 && <div className="text-xs">
                <span className="text-faint">{sug.seed ? t("依据：") : t("为什么选这个方向（加分 / 减分项）：")}</span>
                <ul className="list-disc pl-4 mt-0.5 text-muted">{sug.selection.map((w) => <li key={w}>{w}</li>)}</ul>
              </div>}
              <p className="text-muted">{sug.why_zh}</p>
              {sug.table && <a href="#suggestion-basis" className="text-xs text-accent hover:underline">
                {tf("完整生成依据：{n} 个数据维度 · {d} 个方向 × {s} 项打分 · {e} 条证据（引用 {c}）· 完整提示词 ↓",
                  { n: sug.inputs?.used.length ?? 0, d: sug.table.length, s: sug.dimensions?.length ?? 0, e: sug.evidence?.length ?? 0, c: sug.evidence?.filter((e) => e.cited).length ?? 0 })}</a>}
              <p className="text-xs text-faint">{t("已预填到下方，可直接修改；")}{ch === "experience" ? t("经验请改成你自己的观察。") : ch === "feedback" ? t("反馈请改成真实收到的内容。") : t("提交后研究员仍须挑选、立项并通过检验。")}</p>
            </>}
          </div>
        )}
        <label className="text-sm">{t("标题")}
          <input id="idea-title" className="field mt-1" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder={t("一句话概括")} />
        </label>
        <label className="text-sm">{bodyLabel}
          <textarea id="idea-body" className="field mt-1 min-h-24 leading-relaxed" value={body} maxLength={20000} onChange={(e) => setBody(e.target.value)} placeholder={placeholder} />
        </label>
        {ch === "hypothesis" && <label className="text-sm">{t("方向（可选）")}
          <input id="idea-direction" className="field mt-1" value={x("direction")} onChange={(e) => setX("direction", e.target.value)} placeholder={t("例：只看中小盘、持有一个月")} /></label>}
        {ch === "experience" && <label className="text-sm">{t("经验来自的时间段（可选）")}
          <input id="idea-period" className="field mt-1" value={x("period")} onChange={(e) => setX("period", e.target.value)} placeholder={t("例：2018–2021 年观察")} /></label>}
        {ch === "feedback" && <label className="text-sm">{t("反馈来源")}
          <select id="idea-fbrole" className="field mt-1" value={x("feedback_role") || meta.feedback_roles[0]} onChange={(e) => setX("feedback_role", e.target.value)}>
            {meta.feedback_roles.map((r) => <option key={r}>{r}</option>)}</select></label>}
        {ch === "paper" && (
          <div className="grid gap-3 sm:grid-cols-2 border border-line rounded p-3">
            <label className="text-sm sm:col-span-2">{t("文件（PDF / TXT / Markdown，≤ 20 MB；也可只粘贴摘要）")}
              <input id="idea-file" type="file" accept=".pdf,.txt,.md" className="field mt-1" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
            <label className="text-sm">{t("来源（必填）")}
              <input id="idea-source" className="field mt-1" value={x("source")} onChange={(e) => setX("source", e.target.value)} placeholder={t("例：SSRN / 券商研报 / 团队内部")} /></label>
            <label className="text-sm">{t("许可类型（必填）")}
              <select id="idea-licence" className="field mt-1" value={x("licence")} onChange={(e) => setX("licence", e.target.value)}>
                <option value="">{t("请选择")}</option>
                {Object.entries(meta.licences).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
            <label className="text-sm sm:col-span-2">{t("许可说明")}{x("licence") && x("licence") !== "internal" ? t("（必填）") : t("（可选）")}
              <input id="idea-licnote" className="field mt-1" value={x("licence_note")} onChange={(e) => setX("licence_note", e.target.value)} placeholder={t("公开许可写许可名称；已购买授权写合同或订阅")} /></label>
            <label className="text-sm">{t("发表日期")}
              <input id="idea-pub" className="field mt-1" value={x("published")} onChange={(e) => setX("published", e.target.value)} placeholder={t("YYYY-MM-DD，未知可空")} /></label>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-sm">{t("样本期起")}
                <input id="idea-s0" className="field mt-1" value={x("sample_start")} onChange={(e) => setX("sample_start", e.target.value)} placeholder="YYYY-MM" /></label>
              <label className="text-sm">{t("样本期止")}
                <input id="idea-s1" className="field mt-1" value={x("sample_end")} onChange={(e) => setX("sample_end", e.target.value)} placeholder="YYYY-MM" /></label>
            </div>
            <p className="text-xs text-faint sm:col-span-2">{t("论文的因子通常是作者在历史数据上验证有效后才发表的：样本期或发表日期晚于测试期开始（2022 年）或未知时，平台会提示结果可能被高估。")}</p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-muted">{t("涉及市场")}</span>
          {visibleMarkets().map((m) => (
            <label key={m} className="flex items-center gap-1.5"><input id={`idea-m-${m}`} type="checkbox" checked={markets.includes(m)}
              onChange={() => setMarkets((xs) => xs.includes(m) ? xs.filter((y) => y !== m) : [...xs, m])} />{MARKET_LABEL[m]}</label>
          ))}
        </div>
        <label className="text-sm">{t("提交人")}
          <input id="idea-actor" className="field mt-1" value={actor} maxLength={60} onChange={(e) => setActor(e.target.value)} placeholder={t("姓名（演示用，不校验身份）")} /></label>
        <label className="flex items-start gap-2 text-sm">
          <input id="idea-confirm" type="checkbox" className="mt-1" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
          <span>{t("确认内容不含真实客户身份信息（不要输入客户姓名；平台会拦截电子邮件、电话、身份证号、IBAN 等格式明确的信息）")}</span>
        </label>
        {error && <ErrorBox error={error} />}
        <button className="btn btn-primary self-start" disabled={busy || !title.trim() || !actor.trim() || !confirm} onClick={submit}>
          {busy ? (ch === "paper" ? t("提交并抽取候选因子（约 30–60 秒）…") : t("提交中…")) : t("提交线索")}</button>
      </div>
    </Panel>
  );
}

function Card({ i, active, onClick, checked, onCheck }: { i: Idea; active: boolean; onClick: () => void; checked: boolean; onCheck?: () => void }) {
  const dup = (i.similar?.ideas.length ?? 0) > 0;
  return (
    <div className={`flex gap-2 p-3 rounded border transition-colors ${checked ? "border-accent bg-accent/5" : active ? "border-accent/60 bg-accent/10" : "border-line hover:border-accent/40"}`}>
    {onCheck ? <input id={`idea-pick-${i.id}`} type="checkbox" className="mt-1 shrink-0" checked={checked} onChange={onCheck} aria-label={tf("选择线索：{title}", { title: i.title })} />
      : <span className="w-[13px] shrink-0" title={t("重复 / 不跟进的线索不能合并")} />}
    <button type="button" onClick={onClick} className="min-w-0 flex-1 text-left">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={AUTO.has(i.channel) ? "warn" : "accent"}>{t(i.channel_zh)}</Badge>
        <Badge tone={STATUS_TONE[i.status]}>{({ new: t("待处理"), converted: t("已转立项"), duplicate: t("重复"), dismissed: t("不跟进") })[i.status]}</Badge>
        {dup && <Badge tone="warn">{t("可能重复")}</Badge>}
        {i.leakage?.overlap && <Badge tone="bad">{t("防泄漏提示")}</Badge>}
        <span className="ml-auto text-xs text-faint num">{i.created_at.slice(0, 16).replace("T", " ")}</span>
      </div>
      <div className="mt-1.5 font-medium text-sm">{i.title}</div>
      <div className="text-xs text-muted mt-0.5 line-clamp-2">{i.excerpt}</div>
      <div className="text-xs text-faint mt-1">{i.submitted_by} · {i.markets.length ? i.markets.map((m) => MARKET_LABEL[m]).join(" / ") : t("不限市场")}</div>
    </button>
    </div>
  );
}

function SourceLink({ ref_ }: { ref_: Record<string, string> }) {
  if (ref_.signal_id) return <Link className="text-accent" to={`/signals?sid=${ref_.signal_id}`}>{t("信号目录")} · {ref_.signal_id}</Link>;
  if (ref_.submission) return <Link className="text-accent" to="/approvals">{t("审批台")} · {ref_.submission}</Link>;
  if (ref_.research) return <Link className="text-accent" to="/portfolio">{t("组合台")} · {ref_.research}</Link>;
  return null;
}

// 合并转立项（Spec 第 17.12 节）：勾选多条线索由 AI 合成一个研究假设；或把新线索追加到已有研究，锁定为新版本
function CombineBar({ picked, items, actor, onClear, onDone }: { picked: string[]; items: Idea[]; actor: string; onClear: () => void; onDone: (c: IdeaCombo) => void }) {
  const projects = useAsync(() => api.projects(), []);
  const [base, setBase] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const who = actor.trim();
  const studies = (projects.data ?? []).filter((p) => !p.example && p.versions.length > 0);
  const enough = base ? picked.length >= 1 : picked.length >= 2;
  const titles = picked.map((id) => items.find((i) => i.id === id)?.title ?? id);
  const go = async () => {
    setBusy(true); setErr(null);
    try { onDone(await api.ideaCombine(picked, who, base || null)); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="panel p-3 flex flex-col gap-2 text-sm border-accent/50">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">{tf("已选 {n} 条", { n: picked.length })}</span>
        <span className="text-muted min-w-0 truncate max-w-[60ch]">{titles.join("；")}</span>
        <button type="button" className="btn h-7 px-2 text-xs ml-auto" onClick={onClear}>{t("清空")}</button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="combine-base" className="text-xs text-faint">{t("方式")}</label>
        <select id="combine-base" className="field h-8 w-auto max-w-full" value={base} onChange={(e) => setBase(e.target.value)}>
          <option value="">{t("合并为一个新研究（至少 2 条）")}</option>
          {studies.map((p) => <option key={p.id} value={p.id}>{tf("追加到已有研究：{title}（{id} v{v}）", { title: p.title, id: p.id, v: p.versions[p.versions.length - 1] })}</option>)}
        </select>
        <button type="button" className="btn btn-primary h-8" disabled={busy || !enough || !who} onClick={go}>
          {busy ? t("AI 正在合成研究假设（约 10–30 秒）…") : base ? t("AI 合成追加假设") : t("AI 合并为一个研究假设")}</button>
      </div>
      {!who && <p className="text-xs text-warn">{t("请先在左侧填写提交人 / 处理人。")}</p>}
      {err && <ErrorBox error={err} />}
    </div>
  );
}

function CombineResult({ c, onClose }: { c: IdeaCombo; onClose: () => void }) {
  const { update } = useDraft();
  const navigate = useNavigate();
  const go = () => {
    update({ hypothesis: c.hypothesis, direction: c.direction, markets: c.markets, ideaId: null, ideaIds: c.idea_ids, comboId: c.id,
      basedOn: c.base ? { id: c.base.id, version: c.base.version } : null, ideaTitle: c.title });
    navigate("/research");
  };
  const tagTitle = (t: string) => t === "B" ? tf("已有研究 {id} v{v}", { id: c.base?.id ?? "", v: c.base?.version ?? "" }) : c.sources.find((s) => s.tag === t)?.title ?? t;
  const parts = c.hypothesis_cited.split(/(\[(?:I\d+|B)\])/g);
  return (
    <Panel eyebrow={tf("{id} · {model} · 提示词 {v} · {by}", { id: c.id, model: c.model, v: c.prompt_version, by: c.by })}
      title={c.base ? tf("追加到 {title}：{t}", { title: c.base.title, t: c.title }) : tf("合并假设：{t}", { t: c.title })}
      actions={<button type="button" className="btn h-7 px-2 text-xs" onClick={onClose}>{t("关闭")}</button>}>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="flex flex-col gap-3 min-w-0">
          <div>
            <div className="label mb-1">{t("AI 合成的研究假设（每句后标明来源）")}</div>
            <p className="text-sm leading-relaxed">{parts.map((x, k) => /^\[(I\d+|B)\]$/.test(x)
              ? <span key={k} className="num text-[11px] text-accent align-super cursor-help" title={tagTitle(x.slice(1, -1))}>{x}</span> : <span key={k}>{x}</span>)}</p>
          </div>
          {c.direction && <p className="text-sm text-muted">{tf("方向：{d}", { d: c.direction })}</p>}
          <p className="text-xs text-muted">{c.why_zh}</p>
          {c.conflicts.length > 0 && <Notice tone="warn" title={t("线索之间的矛盾")}>{c.conflicts.map((x, k) => <div key={k}>· {x}</div>)}</Notice>}
          {c.not_testable.length > 0 && <Notice tone="accent" title={t("平台数据做不到、已排除的部分")}>{c.not_testable.map((x, k) => <div key={k}>· {x}</div>)}</Notice>}
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-primary" onClick={go}>{t("用这条假设转立项")}</button>
            <span className="text-xs text-faint">{t('在研究立项页可以修改；锁定研究版本时，所有来源线索才标记为"已转立项"。')}</span>
          </div>
        </div>
        <div className="flex flex-col gap-2 min-w-0">
          <div className="label">{t("来源线索")}</div>
          <ul className="text-sm flex flex-col gap-1.5">
            {c.base && <li><span className="num text-xs text-accent mr-1.5">[B]</span>{tf("已有研究 {title}（{id} v{v}）", { title: c.base.title, id: c.base.id, v: c.base.version })}
              {c.base.inherited.length > 0 && <span className="text-xs text-faint">{tf("，原来源线索 {list}", { list: joinList(c.base.inherited) })}</span>}</li>}
            {c.sources.map((s) => {
              const use = c.uses.find((u) => u.tag === s.tag);
              return (
                <li key={s.tag}>
                  <span className="num text-xs text-accent mr-1.5">[{s.tag}]</span><Badge tone="muted">{t(s.channel_zh)}</Badge> {s.title}
                  {c.unused.includes(s.tag) && <Badge tone="warn">{t("假设中未引用")}</Badge>}
                  {use?.how_zh && <div className="text-xs text-muted ml-7">{use.how_zh}</div>}
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </Panel>
  );
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
  const [aiBusy, setAiBusy] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);
  const genAi = async () => {                        // 自动回流线索：AI 改写为研究假设（只生成一次，存入线索）
    setAiBusy(true); setAiErr(null);
    try { await api.ideaAiHypothesis(id); reload(); } catch (e) { setAiErr((e as Error).message); } finally { setAiBusy(false); }
  };
  useEffect(() => {
    if (data && AUTO.has(data.channel) && !data.ai_hypothesis && !aiBusy && !aiErr) genAi();
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setPicked((data?.extraction?.factors ?? []).filter((f) => f.availability.ok).map((f) => f.key));
    setTarget(data?.similar?.ideas[0]?.id ?? data?.similar?.research[0]?.ref ?? "");
  }, [data]);
  if (loading && !data) return <Loading what={t("线索")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  const d: IdeaDetail = data;
  const run = async (f: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await f(); setNote(""); reload(); onChanged(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const convert = () => run(async () => {
    const p = await api.ideaPrefill(d.id, picked);
    update({ hypothesis: p.hypothesis, direction: p.direction, markets: p.markets, ideaId: p.idea_id, ideaTitle: `${t(p.channel_zh)} · ${p.title}`,
      ideaIds: [], comboId: null, basedOn: null });
    navigate("/research");
  });
  const who = actor.trim();
  const open = d.status === "new" || d.status === "converted";
  return (
    <Panel eyebrow={tf("{id} · {ch} · {by}{role} · {at} UTC", { id: d.id, ch: t(d.channel_zh), by: d.submitted_by, role: d.role ? tf("（{role}）", { role: t(d.role) }) : "", at: d.created_at.slice(0, 16).replace("T", " ") })} title={d.title}
      actions={<Badge tone={STATUS_TONE[d.status]}>{({ new: t("待处理"), converted: t("已转立项"), duplicate: t("重复"), dismissed: t("不跟进") } as Record<string, string>)[d.status] ?? d.status_zh[d.status]}</Badge>}>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="flex flex-col gap-4 min-w-0">
          {d.body && <div><div className="label mb-1">{tf("原文（{lang}，不翻译、不改写）", { lang: ({ zh: t("中文"), en: t("英文"), fr: t("法文") })[d.language] })}</div>
            <p className="text-sm whitespace-pre-wrap leading-relaxed">{d.body}</p></div>}
          {Object.keys(d.fields).length > 0 && <div className="text-sm text-muted">{Object.entries(d.fields).map(([k, v]) =>
            tf("{k}：{v}", { k: ({ direction: t("方向"), period: t("经验时间段"), feedback_role: t("反馈来源") } as Record<string, string>)[k] ?? k, v })).join("；")}</div>}
          {d.source_ref && <div className="text-sm">{t("来源：")}<SourceLink ref_={d.source_ref} /></div>}
          {AUTO.has(d.channel) && (
            <div className="border border-accent/40 bg-accent/5 rounded p-3 text-sm flex flex-col gap-1">
              <div className="flex items-center gap-2"><Badge tone="accent">{t("AI 研究假设")}</Badge>
                {d.ai_hypothesis && <span className="text-xs text-faint">{d.ai_hypothesis.model} · {tf("提示词 {v}", { v: d.ai_hypothesis.prompt_version })} · {d.ai_hypothesis.at.slice(0, 16).replace("T", " ")} UTC</span>}</div>
              {aiBusy && <p className="text-muted">{t("AI 正在把这条平台记录改写为可检验的研究假设…")}</p>}
              {aiErr && <p className="text-xs text-bad">{aiErr} <button type="button" className="btn h-6 px-2 text-xs" onClick={genAi}>{t("重试")}</button></p>}
              {d.ai_hypothesis && <>
                <div className="font-medium">{d.ai_hypothesis.title}</div>
                <p className="leading-relaxed">{d.ai_hypothesis.hypothesis}</p>
                {d.ai_hypothesis.direction && <p className="text-xs text-muted">{tf("方向：{d}", { d: d.ai_hypothesis.direction })}</p>}
                <p className="text-xs text-muted">{d.ai_hypothesis.why_zh}</p>
                <p className="text-xs text-faint">{t("转为研究立项时使用这条假设，可在研究立项页修改。")}</p>
              </>}
            </div>
          )}
          {d.ai_suggested && <div className="text-xs text-faint">{tf("采用 AI 默认假设（{model} · {v} · 方向：{angle}）", { model: d.ai_suggested.model, v: d.ai_suggested.prompt_version, angle: t(d.ai_suggested.angle) })}{d.ai_suggested.edited ? t("，提交人已修改") : t("，未修改")}
            {d.ai_suggested.seed && <div>{tf("研究员的想法：{seed}", { seed: d.ai_suggested.seed })}</div>}
            {d.ai_suggested.selection && d.ai_suggested.selection.length > 0 && <div>{tf("选择依据：{list}", { list: d.ai_suggested.selection.join("；") })}</div>}
            {"table" in d.ai_suggested && <details className="mt-1"><summary className="cursor-pointer text-accent">{t("查看提交时的完整生成依据")}</summary>
              <div className="mt-2"><SuggestionBasis s={d.ai_suggested as unknown as IdeaSuggestion} /></div></details>}</div>}
          {d.document && (
            <div className="text-sm border border-line rounded p-3 flex flex-col gap-1">
              <div className="label">{t("文档")}</div>
              <div>{d.document.name ?? t("粘贴的文字")}{d.document.pages != null && tf(" · {n} 页", { n: d.document.pages })} · {tf("{n} 段", { n: d.document.paragraphs })}
                {d.document.sha256 && <span className="num text-xs text-faint"> · sha256 {d.document.sha256.slice(0, 16)}…</span>}</div>
              <div>{tf("来源：{s}", { s: d.document.source })} · {tf("许可：{l}", { l: d.document.licence_zh })}{d.document.licence_note && tf("（{n}）", { n: d.document.licence_note })}</div>
              <div className="text-muted">{tf("发表 {p}", { p: d.document.published ?? t("未知") })} · {tf("样本期 {s} 至 {e}", { s: d.document.sample_start ?? "?", e: d.document.sample_end ?? "?" })}
                {d.document.ai_filled?.length ? <span className="text-faint">{tf("（{n} 项由 AI 从原文抽取）", { n: d.document.ai_filled.length })}</span> : null}</div>
            </div>
          )}
          {d.leakage && <Notice tone={d.leakage.overlap ? "bad" : "accent"} title={d.leakage.overlap ? t("防泄漏提示") : t("样本期早于测试期")}>{d.leakage.note}</Notice>}
          {d.channel === "paper" && (
            <div>
              <div className="flex items-center gap-2 mb-1">
                <div className="label">{t("候选因子")}</div>
                {d.extraction && <span className="text-xs text-faint">{d.extraction.model} · {tf("提示词 {v}", { v: d.extraction.prompt_version })} · {tf("用了 {a}/{b} 段", { a: d.extraction.paragraphs_used, b: d.extraction.paragraphs_total })}
                  {d.extraction.dropped_no_cite > 0 && tf(" · 丢弃无引用 {n} 个", { n: d.extraction.dropped_no_cite })}</span>}
                <button type="button" className="btn h-7 px-2 text-xs ml-auto" disabled={busy || !who} onClick={() => run(() => api.extractIdea(d.id, who))}>
                  {busy ? t("抽取中…") : d.extraction ? t("重新抽取") : t("抽取候选因子")}</button>
              </div>
              {d.extract_error && <p className="text-xs text-bad">{d.extract_error}</p>}
              {d.extraction?.summary_zh && <p className="text-sm text-muted mb-2">{d.extraction.summary_zh}</p>}
              {!d.extraction ? <p className="text-sm text-muted">{t("尚未抽取。")}</p> : d.extraction.factors.length === 0 ? <p className="text-sm text-muted">{t("没有抽取到带原文引用的因子。")}</p> : (
                <ul className="flex flex-col divide-y divide-line">
                  {d.extraction.factors.map((f) => (
                    <li key={f.key} className="py-2 flex gap-2">
                      <input id={`idea-f-${f.key}`} type="checkbox" className="mt-1" disabled={!f.availability.ok} checked={picked.includes(f.key)}
                        onChange={() => setPicked((xs) => xs.includes(f.key) ? xs.filter((y) => y !== f.key) : [...xs, f.key])} aria-label={tf("选择 {name}", { name: f.name })} />
                      <div className="min-w-0 text-sm flex flex-col gap-0.5">
                        <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{f.name}</span>
                          <Badge tone={f.availability.ok ? "good" : "muted"}>{f.availability.ok ? t("可做") : t("数据不可得")}</Badge>
                          <span className="text-xs text-faint">{joinList(f.availability.needs_zh)}</span></div>
                        <div>{f.description_zh}</div>
                        {f.formula && <div className="num text-xs text-muted">{tf("计算：{f}", { f: f.formula })}</div>}
                        {f.logic_zh && <div className="text-xs text-muted">{tf("经济逻辑：{l}", { l: f.logic_zh })}</div>}
                        {!f.availability.ok && <div className="text-xs text-warn">{tf("缺：{list}", { list: joinList(f.availability.missing) })}</div>}
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
            <div className="label mb-1">{t("相似研究与线索（只作提示）")}</div>
            {!d.similar || (d.similar.research.length === 0 && d.similar.ideas.length === 0) ? <p className="text-sm text-muted">{t("没有发现相似内容。")}</p> : (
              <ul className="text-sm flex flex-col gap-1">
                {d.similar.research.map((r) => <li key={r.ref}><Badge tone="muted">{r.kind_zh}</Badge> <span>{r.title}</span> <span className="num text-xs text-faint">{tf("得分 {s}", { s: r.score })}</span></li>)}
                {d.similar.ideas.map((r) => <li key={r.id}><Badge tone="warn">{t("线索")}</Badge> <span>{r.title}</span> <span className="text-xs text-faint">{t(r.channel_zh)} · {tf("重合 {p}%", { p: Math.round(r.overlap * 100) })}</span></li>)}
              </ul>
            )}
          </div>
          {d.links.research?.length ? <div className="text-sm">{t("已转立项：")}{d.links.research.map((r) => <span key={r} className="num text-xs mr-2">{r}</span>)}<Link className="text-accent" to="/projects">{t("研究项目")}</Link></div> : null}
          {d.links.duplicate_of && <div className="text-sm text-muted">{t("重复，指向：")}<span className="num text-xs">{d.links.duplicate_of}</span></div>}
          <div className="flex flex-col gap-2 border border-line rounded p-3">
            <div className="label">{tf("处理（处理人：{who}）", { who: who || t("请先在左侧填写提交人 / 处理人") })}</div>
            {open && <button className="btn btn-primary self-start" disabled={busy || (d.channel === "paper" && picked.length === 0) || (AUTO.has(d.channel) && !d.ai_hypothesis)} onClick={convert}>
              {t("转为研究立项")}{d.channel === "paper" ? tf("（{n} 个因子）", { n: picked.length }) : ""}</button>}
            <input id="idea-note" className="field" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("原因或意见")} />
            <div className="flex flex-wrap gap-2">
              {d.status === "new" && <button className="btn" disabled={busy || !who || !note.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "dismiss", by: who, note }))}>{t("不跟进")}</button>}
              <button className="btn" disabled={busy || !who || !note.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "comment", by: who, note }))}>{t("写意见")}</button>
              {(d.status === "duplicate" || d.status === "dismissed") && <button className="btn" disabled={busy || !who} onClick={() => run(() => api.ideaAct(d.id, { action: "reopen", by: who, note }))}>{t("重新打开")}</button>}
            </div>
            {d.status === "new" && (
              <div className="flex flex-wrap gap-2 items-center">
                <input id="idea-target" className="field flex-1 min-w-[12rem]" value={target} onChange={(e) => setTarget(e.target.value)} placeholder={t("重复指向：线索编号或研究编号")} />
                <button className="btn" disabled={busy || !who || !target.trim()} onClick={() => run(() => api.ideaAct(d.id, { action: "duplicate", by: who, note, target }))}>{t("标为重复")}</button>
              </div>
            )}
            {err && <ErrorBox error={err} />}
          </div>
          <div>
            <div className="label mb-1">{tf("事件记录（只追加，哈希链{ok}）", { ok: d.chain.ok ? t("校验通过") : t("校验失败") })}</div>
            <ul className="text-xs flex flex-col gap-1">
              {d.events.map((e) => <li key={e.hash}><span className="num text-faint mr-2">{e.at.slice(0, 16).replace("T", " ")}</span>{e.by} · {t(d.action_zh[e.action] ?? e.action)}
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
  const [picked, setPicked] = useState<string[]>([]);
  const [combo, setCombo] = useState<IdeaCombo | null>(null);
  const [sugShown, setSugShown] = useState<IdeaSuggestion | null>(null);
  const pick = (id: string) => setPicked((xs) => xs.includes(id) ? xs.filter((x) => x !== id) : xs.length >= 8 ? xs : [...xs, id]);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("00 · 线索池")}</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">{t("所有研究从一条线索开始")}</h1>
          <p className="text-muted mt-1 max-w-[80ch]">{t("投资假设、论文研报、研究员经验与人工反馈从这里提交；监控预警、组合归因建议与审批退回自动回流。研究员挑选线索转为研究立项，之后走同一条有治理的流程（Spec 第 17 章）。")}</p>
        </div>
      </div>
      {list.error && <ErrorBox error={list.error} />}
      {meta && (
        <div className="panel p-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div><span className="label mr-2">{t("待处理")}</span><span className="font-display text-xl text-accent num">{meta.counts.status.new ?? 0}</span></div>
          {Object.entries(meta.channels).map(([k, v]) => (
            <button key={k} type="button" onClick={() => set("channel", ch === k ? "" : k)}
              className={`flex items-center gap-1.5 ${ch === k ? "text-text" : "text-muted hover:text-text"}`}>
              <Badge tone={AUTO.has(k as IdeaChannel) ? "warn" : "accent"}>{v}</Badge><span className="num">{meta.counts.channel[k] ?? 0}</span>
            </button>
          ))}
          {meta.synced > 0 && <span className="text-xs text-faint">{tf("本次自动回流新增 {n} 条", { n: meta.synced })}</span>}
        </div>
      )}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        {meta ? <Submit meta={meta} actor={actor} setActor={setActor} onCreated={(i) => { list.reload(); set("id", i.id); }} onSuggestion={setSugShown} /> : <Loading what={t("线索池")} />}
        <Panel eyebrow={t("人工提交（蓝）与自动回流（橙）")} title={tf("线索（{n}）", { n: meta?.items.length ?? 0 })}>
          <p className="text-xs text-faint mb-2">{t("点卡片查看并单独转立项；勾选多条可以合并为一个研究，或追加到已有研究（最多 8 条）。")}</p>
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <Seg label={t("状态")} value={st} onChange={(v) => set("status", v === "new" ? "" : v)}
              options={[{ value: "new", label: t("待处理") }, { value: "converted", label: t("已转立项") }, { value: "duplicate", label: t("重复") }, { value: "dismissed", label: t("不跟进") }, { value: "all", label: t("全部") }]} />
            <Seg label={t("市场")} value={mk} onChange={(v) => set("market", v)}
              options={[{ value: "", label: t("全部市场") }, ...visibleMarkets().map((m) => ({ value: m as string, label: MARKET_LABEL[m] }))]} />
          </div>
          {list.loading && !meta ? <Loading what={t("线索")} /> : meta && meta.items.length === 0 ? <p className="text-sm text-muted">{t("没有符合条件的线索。")}</p> : (
            <div className="flex flex-col gap-2 max-h-[40rem] overflow-y-auto pr-1">
              {meta?.items.map((i) => <Card key={i.id} i={i} active={i.id === sel} onClick={() => set("id", i.id)} checked={picked.includes(i.id)}
                onCheck={i.status === "new" || i.status === "converted" ? () => pick(i.id) : undefined} />)}
            </div>
          )}
        </Panel>
      </div>
      {sugShown && <SuggestionBasis key={sugShown.id} s={sugShown} />}
      {picked.length > 0 && <CombineBar picked={picked} items={meta?.items ?? []} actor={actor} onClear={() => setPicked([])} onDone={setCombo} />}
      {combo && <CombineResult key={combo.id} c={combo} onClose={() => setCombo(null)} />}
      {sel && <Detail key={sel} id={sel} actor={actor} onChanged={list.reload} />}
    </div>
  );
}
