import type { ReactNode } from "react";

export function Panel({ title, eyebrow, actions, children, className = "" }: {
  title: string; eyebrow?: string; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`panel p-4 ${className}`}>
      <header className="flex flex-wrap items-start gap-3 mb-3">
        <div className="min-w-0">
          {eyebrow && <div className="label mb-1">{eyebrow}</div>}
          <h2 className="font-display text-base font-semibold tracking-wide">{title}</h2>
        </div>
        {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export function Seg<T extends string | number>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** 3D 视图与精确视图（2D 表格 / 平面图）切换：审核与汇报时使用精确视图。 */
export function ViewToggle({ precise, onChange }: { precise: boolean; onChange: (v: boolean) => void }) {
  return (
    <Seg label="视图" value={precise ? "2d" : "3d"} onChange={(v) => onChange(v === "2d")}
      options={[{ value: "3d", label: "3D 视图" }, { value: "2d", label: "精确视图" }]} />
  );
}

export function Badge({ tone = "muted", children }: { tone?: "good" | "bad" | "warn" | "muted" | "accent"; children: ReactNode }) {
  const map = {
    good: "text-good border-good/40 bg-good/10",
    bad: "text-bad border-bad/40 bg-bad/10",
    warn: "text-warn border-warn/40 bg-warn/10",
    accent: "text-accent border-accent/40 bg-accent/10",
    muted: "text-muted border-line-2 bg-bg-3",
  } as const;
  return <span className={`inline-flex items-center h-5 px-1.5 rounded border text-[11px] whitespace-nowrap ${map[tone]}`}>{children}</span>;
}

export function Notice({ tone = "warn", title, children }: { tone?: "warn" | "bad" | "accent"; title: string; children: ReactNode }) {
  const c = { warn: "border-warn/50 bg-warn/8", bad: "border-bad/50 bg-bad/8", accent: "border-accent/50 bg-accent/8" }[tone];
  const t = { warn: "text-warn", bad: "text-bad", accent: "text-accent" }[tone];
  return (
    <div className={`border-l-2 ${c} px-3 py-2 rounded-r`} role="note">
      <div className={`text-sm font-semibold ${t}`}>{title}</div>
      <div className="text-sm text-muted mt-0.5">{children}</div>
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="label">{label}</div>
      <div className="num text-xl mt-1" style={tone ? { color: tone } : undefined}>{value}</div>
      {sub && <div className="text-xs text-faint mt-0.5">{sub}</div>}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <div className="h-full min-h-40 grid place-items-center label">正在载入{what}…</div>;
}

export function ErrorBox({ error }: { error: string }) {
  return <Notice tone="bad" title="数据载入失败">{error}</Notice>;
}
