import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel } from "../components/ui";
import VersionPicker, { useVersionPick } from "../components/VersionPicker";
import { api, type ApprovalAct, type ApprovalDetail, type ApprovalStep, type Market, type RuleHit, type Submission } from "../api";
import { MARKET_LABEL, useAsync } from "../lib";

// 审批台（Spec 第 14.14 节；BR-22、23、29、47）：提交审批、按角色逐级审批、例外处理、获批锁定、审计证据包

const STATUS_TONE: Record<string, "good" | "warn" | "bad" | "accent" | "muted"> = {
  in_review: "accent", returned: "warn", rejected: "bad", approved: "good", approved_restricted: "good",
};
const STEP_TONE: Record<string, "good" | "warn" | "bad" | "muted"> = { approved: "good", return: "warn", reject: "bad", pending: "muted" };
const STEP_ZH: Record<string, string> = { approved: "已通过", return: "已退回", reject: "已拒绝", pending: "待处理" };

function hitOf(sub: Submission, key: string): RuleHit | undefined {
  const [rule, market] = key.split("@");
  return [...sub.materials.prechecks.setup.hits, ...sub.materials.prechecks.portfolio.hits].find((h) => h.rule_id === rule && String(h.market) === market);
}

function SubmitPanel({ onDone }: { onDone: (sid: string) => void }) {
  const pick = useVersionPick();
  const check = useAsync(() => (pick.id && pick.version ? api.submissionCheck(pick.id, pick.version) : Promise.resolve(null)), [pick.id, pick.version]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (pick.loading) return <Loading what="研究项目" />;
  const c = check.data;
  return (
    <Panel eyebrow="WB-05：立项预检 → 运行 → 组合与组合预检 → 报告 → 提交审批" title="提交审批">
      <div className="flex flex-col gap-3">
        <VersionPicker pick={pick} />
        {check.loading && <div className="label">检查中…</div>}
        {c && (c.ready ? (
          <div className="text-sm text-muted">前置条件已满足；将冻结提交材料（报告 {c.report}）。</div>
        ) : (
          <Notice title="还不能提交"><ul className="flex flex-col gap-0.5">{c.missing.map((m) => <li key={m}>· {m}</li>)}</ul></Notice>
        ))}
        {c && c.open.length > 0 && <div className="text-xs text-warn">该版本已有审批中的提交：{c.open[0].id}</div>}
        <input id="submit-note" className="field" placeholder="提交说明（可选）" value={note} onChange={(e) => setNote(e.target.value)} />
        {error && <ErrorBox error={error} />}
        <button className="btn btn-primary self-start" disabled={busy || !c?.ready || (c?.open.length ?? 0) > 0} onClick={async () => {
          setBusy(true); setError(null);
          try { const s = await api.submitApproval(pick.id, pick.version, note); onDone(s.id); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
        }}>{busy ? "提交中…" : "提交审批"}</button>
      </div>
    </Panel>
  );
}

function StepCard({ s, i, sub, actor, onAct }: { s: ApprovalStep; i: number; sub: Submission; actor: string;
  onAct: (body: { action: string; note?: string; rule?: string; markets?: Market[]; thresholds?: Record<string, number> }) => Promise<void> }) {
  const current = sub.status === "in_review" && i === sub.current;
  const mine = current && actor === s.role;
  const [note, setNote] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [markets, setMarkets] = useState<Market[]>(sub.approved_markets ?? sub.markets);
  const [ic, setIc] = useState("0");
  const [dd, setDd] = useState("-0.10");
  const [busy, setBusy] = useState(false);
  const todo = [...s.hits, ...s.model_exceptions.map((m) => `MODEL@${m}`)];
  const run = async (body: Parameters<typeof onAct>[0]) => { setBusy(true); try { await onAct(body); } finally { setBusy(false); } };
  return (
    <li className={`border-l-2 pl-4 py-2 ${current ? "border-l-accent" : s.state === "approved" ? "border-l-good" : "border-l-line"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="num text-xs text-faint w-5">{i + 1}</span>
        <span className="font-medium">{s.role}</span>
        <Badge tone={current ? "accent" : STEP_TONE[s.state] ?? "muted"}>{current ? "当前" : STEP_ZH[s.state] ?? s.state}</Badge>
        <span className="text-xs text-muted">{s.duty}</span>
      </div>
      {todo.length > 0 && (
        <ul className="mt-1.5 flex flex-col gap-1.5 text-sm">
          {todo.map((k) => {
            const d = sub.rule_decisions[`${s.role}|${k}`];
            const h = k.startsWith("MODEL@") ? null : hitOf(sub, k);
            const label = h ? `${h.rule_id} ${h.title}` : `模型例外：${MARKET_LABEL[k.split("@")[1] as Market]}可信度检查${sub.materials.results[k.split("@")[1]]?.credibility ?? "未完成"}`;
            return (
              <li key={k} className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  {d ? <Badge tone={d.action === "exception" ? "warn" : "good"}>{d.action === "exception" ? "例外" : "已确认"}</Badge> : <Badge tone="muted">待处理</Badge>}
                  <span className="text-xs">{label}</span>
                  {h && <span className="text-xs text-faint">{h.market_zh} · {h.reason}</span>}
                </div>
                {d && d.note && <div className="text-xs text-muted pl-1">理由：{d.note}</div>}
                {mine && !d && (
                  <div className="flex flex-wrap gap-2">
                    <input id={`reason-${i}-${k}`} className="field flex-1 min-w-[12rem]" placeholder="例外理由（例外处理必填）" value={reasons[k] ?? ""}
                      onChange={(e) => setReasons((r) => ({ ...r, [k]: e.target.value }))} />
                    <button className="btn" disabled={busy} onClick={() => run({ action: "acknowledge", rule: k, note: reasons[k] ?? "" })}>确认</button>
                    <button className="btn" disabled={busy} onClick={() => run({ action: "exception", rule: k, note: reasons[k] ?? "" })}>例外处理</button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {mine && (
        <div className="mt-2 flex flex-col gap-2 max-w-[44rem]">
          {s.role === "运营 / 技术负责人" ? (
            <div className="grid gap-2 sm:grid-cols-2 text-sm">
              <label className="flex flex-col gap-1">滚动 60 日 rank IC 下限<input id="th-ic" className="field num" value={ic} onChange={(e) => setIc(e.target.value)} /></label>
              <label className="flex flex-col gap-1">相对基准回撤下限<input id="th-dd" className="field num" value={dd} onChange={(e) => setDd(e.target.value)} /></label>
            </div>
          ) : (
            <input id={`note-${i}`} className="field" placeholder={s.role === "投资组合经理" ? "决定说明（退回 / 拒绝必填）" : "意见或批准条件（退回 / 拒绝必填）"}
              value={note} onChange={(e) => setNote(e.target.value)} />
          )}
          {(s.role === "投资组合经理" || s.role === "运营 / 技术负责人") && (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-muted">{s.role === "投资组合经理" ? "批准的市场" : "部署范围"}</span>
              {sub.markets.map((m) => (
                <label key={m} className="flex items-center gap-1">
                  <input type="checkbox" checked={markets.includes(m)} onChange={(e) => setMarkets((x) => e.target.checked ? [...x, m] : x.filter((y) => y !== m))} />
                  {MARKET_LABEL[m]}
                </label>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {s.role === "运营 / 技术负责人" ? (
              <button className="btn btn-primary" disabled={busy} onClick={() => run({ action: "complete", markets,
                thresholds: { rolling_rank_ic_60d_min: Number(ic), drawdown_vs_bench_max: Number(dd) } })}>完成部署核对并锁定</button>
            ) : s.role === "投资组合经理" && markets.length < sub.markets.length ? (
              <button className="btn btn-primary" disabled={busy} onClick={() => run({ action: "restrict", markets, note })}>限定地区批准</button>
            ) : (
              <button className="btn btn-primary" disabled={busy} onClick={() => run({ action: "approve", note })}>批准</button>
            )}
            {s.role !== "运营 / 技术负责人" && <button className="btn" disabled={busy} onClick={() => run({ action: "return", note })}>退回</button>}
            {(s.role === "投资组合经理" || s.role === "模型风险" || s.role.endsWith("合规")) &&
              <button className="btn" disabled={busy} onClick={() => run({ action: "reject", note })}>拒绝</button>}
          </div>
        </div>
      )}
    </li>
  );
}

function Detail({ sid, roles }: { sid: string; roles: string[] }) {
  const [d, setD] = useState<ApprovalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actor, setActor] = useState<string>("");
  const [comment, setComment] = useState("");
  useEffect(() => {
    let alive = true;
    setD(null); setError(null);
    api.approval(sid).then((x) => { if (alive) { setD(x); setActor(x.summary.current_role ?? "发起研究员"); } }, (e) => alive && setError((e as Error).message));
    return () => { alive = false; };
  }, [sid]);
  if (error && !d) return <ErrorBox error={error} />;
  if (!d) return <Loading what="提交" />;
  const sub = d.submission;
  const act = async (body: Omit<ApprovalAct, "role"> & { role?: string }) => {
    setError(null);
    try { const x = await api.approvalAct(sid, { role: actor, ...body }); setD(x); if (x.summary.current_role) setActor(x.summary.current_role); }
    catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="flex flex-col gap-5">
      <Panel eyebrow={`${sub.research_id} v${sub.version} · 提交于 ${sub.created_at.slice(0, 16).replace("T", " ")} UTC`} title={sub.materials.title}
        actions={<Badge tone={STATUS_TONE[sub.status]}>{d.summary.status_zh}</Badge>}>
        <div className="flex flex-col gap-2 text-sm">
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
            <span>提交指纹 <span className="num">{sub.fingerprint.slice(0, 16)}…</span></span>
            <span>代码指纹 <span className="num">{sub.materials.code_fingerprint.slice(0, 12)}…</span></span>
            <span>报告 <a className="text-accent" href={`/api/reports/${sub.materials.report.name}`} target="_blank" rel="noopener">{sub.materials.report.name}</a></span>
            <span>事件 {d.events.length} 条 · 哈希链{d.chain.ok ? "校验通过" : `第 ${d.chain.broken_at} 条起被改动`}</span>
          </div>
          {!d.materials_current && <Notice tone="warn" title="提交材料已变化">{d.materials_note}：运营无法完成部署核对，须重新提交。</Notice>}
          <div className="overflow-x-auto">
            <table className="w-full text-sm num">
              <thead className="label text-left"><tr><th className="py-1 pr-3">市场</th><th className="pr-3">运行 · 轮次</th><th className="pr-3">rank IC</th><th className="pr-3">t</th><th className="pr-3">可信度</th><th>数据快照</th></tr></thead>
              <tbody>
                {sub.markets.map((m) => { const r = sub.materials.results[m]; return (
                  <tr key={m} className="border-t border-line"><td className="py-1.5 pr-3 font-sans">{MARKET_LABEL[m]}</td>
                    <td className="pr-3 text-xs">{r?.run_id ?? "—"} · {r?.loop ?? "—"}</td><td className="pr-3">{r?.rank_ic?.toFixed(4) ?? "—"}</td>
                    <td className="pr-3">{r?.rank_ic_t?.toFixed(2) ?? "—"}</td><td className="pr-3 font-sans">{r?.credibility ?? "—"}</td>
                    <td className="text-xs text-faint">{(sub.materials.data_snapshot[m] ?? "—").slice(0, 12)}…</td></tr>); })}
              </tbody>
            </table>
          </div>
          {sub.lock && (
            <Notice tone="accent" title={`获批锁定 · ${sub.lock.locked_at.slice(0, 16).replace("T", " ")} UTC`}>
              地区适用性：{Object.entries(sub.lock.applicability).map(([m, v]) => `${MARKET_LABEL[m as Market]} ${v}`).join("、")}；
              监控阈值：{Object.entries(sub.lock.thresholds).map(([k, v]) => `${k} = ${v}`).join("、")}
              {sub.lock.conditions.length > 0 && `；批准条件：${sub.lock.conditions.map((c) => `${c.role}：${c.note}`).join("；")}`}
            </Notice>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <a className="btn" href={`/api/approvals/${sid}/evidence`}>导出审计证据包</a>
            <label className="text-sm flex items-center gap-2 ml-auto whitespace-nowrap">演示身份
              <select id="actor" className="field w-auto" value={actor} onChange={(e) => setActor(e.target.value)}>
                {roles.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
          </div>
        </div>
      </Panel>
      {error && <ErrorBox error={error} />}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <Panel eyebrow="第 12.2.1 节：本地研究员（香港 → 英国 → 法国）→ 数据负责人 → 地区合规 → 模型风险 → 投资组合经理 → 运营" title="审批路线">
          <ol className="flex flex-col gap-1">
            {sub.steps.map((s, i) => <StepCard key={`${s.role}-${i}-${sub.current}`} s={s} i={i} sub={sub} actor={actor} onAct={act} />)}
          </ol>
          {sub.status === "in_review" && actor !== sub.steps[sub.current]?.role && (
            <p className="text-xs text-faint mt-2">当前应由「{sub.steps[sub.current]?.role}」处理；切换演示身份后操作。其他身份只能发表意见。</p>
          )}
        </Panel>
        <Panel eyebrow="BR-23：只追加，每条记录上一条的哈希" title="留痕">
          <ol className="flex flex-col gap-2 text-sm">
            {d.events.map((e) => (
              <li key={e.hash} className="border-b border-line pb-1.5">
                <div className="flex flex-wrap gap-2 items-baseline">
                  <span className="text-xs text-faint num">{e.at.slice(5, 16).replace("T", " ")}</span>
                  <span className="font-medium">{e.role}</span><span>{d.action_zh[e.action]}</span>
                  {e.rule && <span className="num text-xs text-muted">{e.rule}</span>}
                  {e.markets && <span className="text-xs">{e.markets.map((m) => MARKET_LABEL[m]).join("、")}</span>}
                </div>
                {e.note && <div className="text-xs text-muted">{e.note}</div>}
                <div className="num text-[10px] text-faint">#{e.hash.slice(0, 10)} ← {e.prev.slice(0, 10)}</div>
              </li>
            ))}
          </ol>
          <div className="flex gap-2 mt-3">
            <input id="comment" className="field flex-1" placeholder={`以「${actor}」身份发表意见`} value={comment} onChange={(e) => setComment(e.target.value)} />
            <button className="btn" disabled={!comment.trim()} onClick={async () => { await act({ role: actor, action: "comment", note: comment }); setComment(""); }}>发表</button>
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default function ApprovalsPage() {
  const [params, setParams] = useSearchParams();
  const list = useAsync(() => api.approvals(), []);
  const sid = params.get("sid") ?? list.data?.items[0]?.id ?? "";
  if (list.loading) return <Loading what="审批台" />;
  if (list.error || !list.data) return <ErrorBox error={list.error ?? "无数据"} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">审批台</div>
          <h1 className="font-display text-xl font-semibold">跨地区审批与审计</h1>
        </div>
        <span className="text-sm text-muted">香港 → 伦敦 → 巴黎 · 例外处理 · 获批锁定 · 审计证据包</span>
        <span className="ml-auto text-xs text-faint">模拟审批：审批人为演示角色，不涉及真实人员</span>
      </div>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,4fr)_minmax(0,8fr)]">
        <div className="flex flex-col gap-5">
          <SubmitPanel onDone={(id) => { list.reload(); setParams({ sid: id }); }} />
          <Panel eyebrow="WB-02：当前环节、负责人、等待时长" title={`提交（${list.data.items.length}）`}>
            {list.data.items.length === 0 ? <p className="text-sm text-muted">还没有提交。</p> : (
              <ul className="flex flex-col divide-y divide-line">
                {list.data.items.map((s) => (
                  <li key={s.id}>
                    <button type="button" className={`w-full text-left py-2 ${s.id === sid ? "text-accent" : ""}`} onClick={() => setParams({ sid: s.id })}>
                      <div className="flex items-center gap-2"><Badge tone={STATUS_TONE[s.status]}>{s.status_zh}</Badge>
                        <span className="text-sm font-medium truncate">{s.research_id} v{s.version}</span></div>
                      <div className="text-xs text-muted mt-0.5">
                        {s.current_role ? `等待「${s.current_role}」· 第 ${s.step + 1} / ${s.steps} 步 · ${s.waiting_hours} 小时` :
                          s.approved_markets ? `获批：${s.approved_markets.map((m) => MARKET_LABEL[m]).join("、")}` : "已结束"}
                      </div>
                      <div className="num text-[10px] text-faint">{s.id}</div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
        {sid ? <Detail key={sid} sid={sid} roles={list.data.roles} /> : <Notice tone="accent" title="选择或提交一个研究版本">提交后按角色逐级审批。</Notice>}
      </div>
    </div>
  );
}
