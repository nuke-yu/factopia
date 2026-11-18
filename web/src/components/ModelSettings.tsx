import { useEffect, useState } from "react";
import { api, type ModelRoleKey, type ModelSettingsState } from "../api";
import { t, tf } from "../i18n";

const ROLES: { key: ModelRoleKey; label: string }[] = [
  { key: "ai_rd", label: t("AI研发") },
  { key: "pro", label: t("LLM Pro") },
  { key: "lite", label: t("LLM Lite") },
  { key: "critic", label: t("Critic LLM") },
];

/** 顶部齿轮按钮：模型选择设置（角色 → 模型、费用）。 */
export default function ModelSettings() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ModelSettingsState | null>(null);
  const [draft, setDraft] = useState<Partial<Record<ModelRoleKey, string>>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!open) return;
    setErr(null); setSaved(false); setDraft({}); setData(null);
    api.settingsModels().then(setData).catch((e: Error) => setErr(e.message));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const save = async () => {
    setBusy(true); setErr(null); setSaved(false);
    try {
      const next = await api.saveSettingsModels(draft);
      setData(next); setDraft({}); setSaved(true);
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
            {saved && <p className="text-xs text-accent mb-2">{t("已保存")}</p>}
            {!data ? (
              <p className="text-sm text-muted">{err ? "" : t("加载中…")}</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="label">
                    <th className="text-left pb-2 pr-3 font-normal"></th>
                    <th className="text-left pb-2 pr-3 font-normal">{t("模型选择")}</th>
                    <th className="text-left pb-2 font-normal">{t("费用（美元 / 百万 token）")}</th>
                  </tr>
                </thead>
                <tbody>
                  {ROLES.map(({ key, label }) => {
                    const role = data.roles.find((r) => r.key === key);
                    const current = role?.model ?? "";
                    const selected = draft[key] !== undefined ? draft[key] : current;
                    const effective = selected === "" ? (role?.default ?? "") : selected;
                    const price = data.prices[effective];
                    const options = data.options.includes(selected) || !selected ? data.options : [selected, ...data.options];
                    return (
                      <tr key={key} className="border-t border-line align-top">
                        <td className="py-2 pr-3 whitespace-nowrap">{label}</td>
                        <td className="py-2 pr-3">
                          <select className="field" value={selected} aria-label={label}
                            onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}>
                            <option value="">{tf("默认（{model}）", { model: role?.default || "—" })}</option>
                            {options.map((m) => <option key={m} value={m}>{m}</option>)}
                          </select>
                        </td>
                        <td className="py-2 num whitespace-nowrap">
                          {price ? <>
                            <div>${price.in.toFixed(2)} / ${price.out.toFixed(2)}</div>
                            <div className="text-xs text-faint">{t("输入 / 输出")}</div>
                          </> : "—"}
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
