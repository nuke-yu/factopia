import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "./ui";
import type { Precheck, RuleHandling, RuleHit, Severity } from "../api";

// 预检结果（Spec 第 14.12 节）：命中规则按严重程度排列，每条给出原因、证据、影响、所需审批人；底部为建议审批路径

const TONE: Record<Severity, "bad" | "warn" | "muted"> = { block: "bad", warn: "warn", info: "muted" };
const STRIPE: Record<Severity, string> = { block: "border-l-bad", warn: "border-l-warn", info: "border-l-line" };

export const hitKey = (h: RuleHit) => `${h.rule_id}@${h.market}`;

// 处理状态（风控台）：警告命中在审批台由负责角色确认或例外；阻断须修改后重新预检；提示写入报告
function Handling({ h, handling }: { h: RuleHit; handling: RuleHandling | null }) {
  if (h.severity === "info") return <div className="text-xs text-faint">处理：写入报告，无需审批处理</div>;
  if (h.severity === "block") return <div className="text-xs text-bad">处理：阻断，须修改研究后重新预检，不能例外</div>;
  if (!handling) return <div className="text-xs text-faint">处理：尚未提交审批（提交后由{h.approvers.join("、")}在审批台确认或例外处理）</div>;
  const link = <Link className="text-accent" to={`/approvals?sid=${encodeURIComponent(handling.submission_id)}`}>在审批台查看 →</Link>;
  const rows = handling.items[hitKey(h)];
  if (!rows) return (
    <div className="text-xs text-warn">处理：最新提交（{handling.status_zh}）中没有这条命中，提交后材料或规则有变化，须重新提交 {link}</div>
  );
  return (
    <div className="text-xs flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="text-faint">处理（{handling.status_zh}）：</span>
      {rows.map((r) => (
        <span key={r.role} className="inline-flex items-center gap-1">
          {r.role}
          <Badge tone={r.action === "exception" ? "warn" : r.action ? "good" : "muted"}>
            {r.action === "exception" ? "例外" : r.action ? "已确认" : handling.status === "in_review" ? "待处理" : "未处理"}</Badge>
          {r.note && <span className="text-muted">理由：{r.note}</span>}
        </span>
      ))}
      {link}
    </div>
  );
}

function Hit({ h, handling, highlight }: { h: RuleHit; handling?: RuleHandling | null; highlight?: boolean }) {
  const [open, setOpen] = useState(!!highlight);
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => { if (highlight) ref.current?.scrollIntoView({ block: "center" }); }, [highlight]);
  return (
    <li ref={ref} id={`hit-${hitKey(h)}`} className={`border-l-2 ${STRIPE[h.severity]} pl-3 py-1 ${highlight ? "bg-accent/10 rounded-r" : ""}`}>
      <button type="button" className="text-left w-full" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={TONE[h.severity]}>{h.severity_zh}</Badge>
          <span className="num text-xs text-faint">{h.rule_id}</span>
          <span className="text-xs text-muted">{h.market_zh}</span>
          <span className="font-medium">{h.title}</span>
        </div>
        <div className="text-xs text-muted mt-0.5 leading-relaxed">{h.reason}</div>
      </button>
      {handling !== undefined && <div className="mt-1"><Handling h={h} handling={handling} /></div>}
      {open && (
        <dl className="text-xs grid grid-cols-[6em_minmax(0,1fr)] gap-x-2 gap-y-1 mt-1.5">
          <dt className="text-faint">影响</dt><dd>{h.impact}</dd>
          <dt className="text-faint">依据</dt><dd>{h.source}</dd>
          <dt className="text-faint">所需审批</dt><dd>{h.approvers.length ? h.approvers.join("、") : "无（写入报告）"}</dd>
          <dt className="text-faint">规则包</dt><dd className="num">{h.pack} {h.pack_version} · {h.pack_status} · {h.owner}</dd>
          {Object.keys(h.evidence).length > 0 && (<><dt className="text-faint">证据</dt>
            <dd className="num break-all">{JSON.stringify(h.evidence)}</dd></>)}
        </dl>
      )}
    </li>
  );
}

// handling：传入时（风控台）显示每条命中的处理状态；highlight：从审批台跳转来时展开并定位的命中（规则编号@市场）
export default function PrecheckView({ pre, compact = false, handling, highlight }: {
  pre: Precheck; compact?: boolean; handling?: RuleHandling | null; highlight?: string | null;
}) {
  const [showInfo, setShowInfo] = useState(() => !!highlight && pre.hits.some((h) => h.severity === "info" && hitKey(h) === highlight));
  const main = pre.hits.filter((h) => h.severity !== "info");
  const info = pre.hits.filter((h) => h.severity === "info");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {pre.blocked ? <Badge tone="bad">有阻断：不能继续</Badge> : main.length ? <Badge tone="warn">需审批确认</Badge> : <Badge tone="good">未命中阻断或警告</Badge>}
        <span className="text-muted">阻断 {pre.counts.block} · 警告 {pre.counts.warn} · 提示 {pre.counts.info}</span>
        <span className="text-xs text-faint num ml-auto">{pre.packs.map((p) => `${p.pack} ${p.version}`).join(" · ")} · {pre.sources_version}</span>
      </div>
      {main.length > 0 && <ul className="flex flex-col gap-2">{main.map((h, i) => <Hit key={`${h.rule_id}-${i}`} h={h} handling={handling} highlight={highlight === hitKey(h)} />)}</ul>}
      {info.length > 0 && (
        <div>
          <button type="button" className="text-xs text-accent" onClick={() => setShowInfo((v) => !v)}>
            {showInfo ? "收起" : "展开"}提示（{info.length}）
          </button>
          {showInfo && <ul className="flex flex-col gap-2 mt-2">{info.map((h, i) => <Hit key={`${h.rule_id}-${i}`} h={h} handling={handling} highlight={highlight === hitKey(h)} />)}</ul>}
        </div>
      )}
      {!compact && (
        <div>
          <div className="label mb-1.5">建议审批路径</div>
          <ol className="flex flex-col gap-1 text-sm">
            {pre.approval_path.steps.map((s, i) => (
              <li key={s.role} className="flex gap-2">
                <span className="num text-faint w-5 text-right">{i + 1}</span>
                <span className="font-medium whitespace-nowrap">{s.role}</span>
                <span className="text-xs text-muted pt-0.5">{s.reasons.join("、")}</span>
              </li>
            ))}
          </ol>
          <p className={`text-xs mt-1.5 ${pre.approval_path.blocked ? "text-bad" : "text-faint"}`}>{pre.approval_path.note}</p>
        </div>
      )}
      <p className="text-xs text-faint">{pre.disclaimer}</p>
    </div>
  );
}
