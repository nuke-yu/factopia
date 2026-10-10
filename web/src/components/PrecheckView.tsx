import { useState } from "react";
import { Badge } from "./ui";
import type { Precheck, RuleHit, Severity } from "../api";

// 预检结果（Spec 第 14.12 节）：命中规则按严重程度排列，每条给出原因、证据、影响、所需审批人；底部为建议审批路径

const TONE: Record<Severity, "bad" | "warn" | "muted"> = { block: "bad", warn: "warn", info: "muted" };
const STRIPE: Record<Severity, string> = { block: "border-l-bad", warn: "border-l-warn", info: "border-l-line" };

function Hit({ h }: { h: RuleHit }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`border-l-2 ${STRIPE[h.severity]} pl-3 py-1`}>
      <button type="button" className="text-left w-full" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={TONE[h.severity]}>{h.severity_zh}</Badge>
          <span className="num text-xs text-faint">{h.rule_id}</span>
          <span className="text-xs text-muted">{h.market_zh}</span>
          <span className="font-medium">{h.title}</span>
        </div>
        <div className="text-xs text-muted mt-0.5 leading-relaxed">{h.reason}</div>
      </button>
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

export default function PrecheckView({ pre, compact = false }: { pre: Precheck; compact?: boolean }) {
  const [showInfo, setShowInfo] = useState(false);
  const main = pre.hits.filter((h) => h.severity !== "info");
  const info = pre.hits.filter((h) => h.severity === "info");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {pre.blocked ? <Badge tone="bad">有阻断：不能继续</Badge> : main.length ? <Badge tone="warn">需审批确认</Badge> : <Badge tone="good">未命中阻断或警告</Badge>}
        <span className="text-muted">阻断 {pre.counts.block} · 警告 {pre.counts.warn} · 提示 {pre.counts.info}</span>
        <span className="text-xs text-faint num ml-auto">{pre.packs.map((p) => `${p.pack} ${p.version}`).join(" · ")} · {pre.sources_version}</span>
      </div>
      {main.length > 0 && <ul className="flex flex-col gap-2">{main.map((h, i) => <Hit key={`${h.rule_id}-${i}`} h={h} />)}</ul>}
      {info.length > 0 && (
        <div>
          <button type="button" className="text-xs text-accent" onClick={() => setShowInfo((v) => !v)}>
            {showInfo ? "收起" : "展开"}提示（{info.length}）
          </button>
          {showInfo && <ul className="flex flex-col gap-2 mt-2">{info.map((h, i) => <Hit key={`${h.rule_id}-${i}`} h={h} />)}</ul>}
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
