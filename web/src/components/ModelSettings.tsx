import { useEffect, useRef, useState } from "react";
import { api, type ModelRoleKey, type ModelSettingsState } from "../api";
import { t, tf } from "../i18n";

const ROLES: { key: ModelRoleKey; label: string }[] = [
  { key: "ai_rd", label: t("AI研发") },
  { key: "pro", label: t("LLM Pro") },
  { key: "lite", label: t("LLM Lite") },
  { key: "critic", label: t("Critic LLM") },
];

type RoleState = ModelSettingsState["roles"][number];
type Price = { in: number; out: number } | null;

function priceText(p: Price | undefined) {
  return p ? `$${p.in.toFixed(2)} / $${p.out.toFixed(2)}` : "—";
}

/** 自定义下拉：模型名靠左，费用靠右（原生 select 无法控制 option 对齐）。 */
function ModelSelect({ role, options, prices, value, onChange, ariaLabel }: {
  role: RoleState | undefined; options: string[]; prices: Record<string, Price>;
  value: string; onChange: (v: string) => void; ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  const defaultModel = role?.default ?? "";
  const choices = [
    { value: "", label: tf("默认（{model}）", { model: defaultModel || "—" }), model: defaultModel },
    ...options.map((m) => ({ value: m, label: m, model: m })),
  ];
  if (value && !options.includes(value)) choices.splice(1, 0, { value, label: value, model: value });
  const selected = choices.find((c) => c.value === value) ?? choices[0];
  return (
    <div ref={ref} className="relative">
      <button type="button" className="field w-full flex items-center justify-between gap-3 text-left cursor-pointer"
        aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="truncate">{selected.label}</span>
        <span className="num text-xs text-muted whitespace-nowrap">{priceText(prices[selected.model])}</span>
      </button>
      {open && (
        <div className="absolute left-0 right-0 z-50 mt-1 max-h-64 overflow-y-auto rounded border border-line bg-bg shadow-lg p-1"
          role="listbox" aria-label={ariaLabel}>
          {choices.map((c) => (
            <button key={c.value || "__default"} type="button" role="option" aria-selected={c.value === value}
              className={`w-full flex items-center justify-between gap-3 rounded px-2 py-1.5 text-left text-sm cursor-pointer hover:bg-accent/10 ${
                c.value === value ? "bg-accent/10" : ""}`}
              onClick={() => { onChange(c.value); setOpen(false); }}>
              <span className="truncate">{c.label}</span>
              <span className="num text-xs text-muted whitespace-nowrap">{priceText(prices[c.model])}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 顶部齿轮按钮：模型选择设置（角色 → 模型、费用）。 */
export default function ModelSettings() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ModelSettingsState | null>(null);
  const [draft, setDraft] = useState<Partial<Record<ModelRoleKey, string>>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setErr(null); setDraft({}); setData(null);
    api.settingsModels().then(setData).catch((e: Error) => setErr(e.message));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const next = await api.saveSettingsModels(draft);
      setData(next); setDraft({});
      setOpen(false);                             // 保存成功后自动关闭
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const changed = Object.keys(draft).length > 0;
  return (
    <>
      <button className="btn h-9 w-9 grid place-items-center p-0 shrink-0" aria-label={t("模型设置")} title={t("模型设置")}
        onClick={() => setOpen(true)}>
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8"
          strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>

      {open && (
        <div className="fixed inset-0 z-40 bg-black/60 grid place-items-center p-4" role="dialog" aria-modal="true"
          aria-label={t("模型设置")} onClick={() => setOpen(false)}>
          <div className="panel w-full max-w-[660px] p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-3">
              <h2 className="font-display font-semibold">{t("模型设置")}</h2>
              <button className="btn h-9 w-9 grid place-items-center p-0 ml-auto" aria-label={t("关闭")} title={t("关闭")}
                onClick={() => setOpen(false)}>
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2"
                  strokeLinecap="round" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
            {err && <p className="text-xs text-bad mb-2">{err}</p>}
            {!data ? (
              <p className="text-sm text-muted">{err ? "" : t("加载中…")}</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="label">
                    <th className="text-left pb-2 pr-3 font-normal"></th>
                    <th className="pb-2 font-normal">
                      <div className="flex items-center justify-between gap-3">
                        <span>{t("模型选择")}</span>
                        <span className="text-right">{t("费用（美元 / 百万 token）")}</span>
                      </div>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {ROLES.map(({ key, label }) => {
                    const role = data.roles.find((r) => r.key === key);
                    const current = role?.model ?? "";
                    const selected = draft[key] !== undefined ? draft[key] : current;
                    return (
                      <tr key={key} className="border-t border-line align-middle">
                        <td className="py-2 pr-3 whitespace-nowrap">{label}</td>
                        <td className="py-2">
                          <ModelSelect role={role} options={data.options} prices={data.prices} value={selected}
                            ariaLabel={label} onChange={(v) => setDraft((d) => ({ ...d, [key]: v }))} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
            <p className="text-xs text-faint mt-3">{t("保存后立即用于平台调用；AI研发的新任务在下次启动时生效。")}</p>
            <div className="flex items-center justify-end gap-2 mt-3 pt-3 border-t border-line">
              <button className="btn" onClick={() => setOpen(false)}>{t("取消")}</button>
              <button className="btn btn-primary" disabled={busy || !changed} onClick={save}>
                {busy ? t("保存中…") : t("保存")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
