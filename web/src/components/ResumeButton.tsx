import { useState } from "react";
import { api } from "../api";
import { useAsync } from "../lib";
import { t, tf } from "../i18n";

/** 从断点继续：RD-Agent 从最近的检查点续跑剩下的轮数，已完成的轮次与费用保留（与"重新运行"从头开始不同）。 */
export default function ResumeButton({ jobId, onDone }: { jobId: string; onDone?: () => void }) {
  const info = useAsync(() => api.resumeInfo(jobId), [jobId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  if (!info.data) return null;
  if (!info.data.ok) return <span className="text-xs text-faint mr-2" title={info.data.reason}>{t("不能续跑")}</span>;
  return (
    <span className="inline-flex items-center gap-2 mr-2">
      <button className="btn btn-primary" disabled={busy || done} title={tf("检查点：{cp}；已完成 {n} 轮", { cp: info.data.checkpoint ?? "-", n: info.data.complete ?? 0 })}
        onClick={async () => {
          setBusy(true); setErr(null);
          try { await api.resumeJob(jobId); setDone(true); onDone?.(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
        }}>{done ? t("已排队续跑") : tf("从断点继续（还剩 {n} 轮）", { n: info.data.remaining ?? 0 })}</button>
      {err && <span className="text-xs text-bad">{err}</span>}
    </span>
  );
}
