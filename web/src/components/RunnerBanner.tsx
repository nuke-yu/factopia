import { useEffect, useState } from "react";
import { api, type RunnerStatus } from "../api";
import { Notice } from "./ui";

/** 后台运行器暂停时（大模型余额不足、密钥无效等）在每个页面顶部提示，处理后可继续运行。 */
export default function RunnerBanner() {
  const [status, setStatus] = useState<RunnerStatus | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = () => api.runner().then((s) => alive && setStatus(s)).catch(() => undefined);
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!status?.paused) return null;
  return (
    <div className="mb-5">
      <Notice tone="bad" title="后台运行已暂停">
        <div className="flex flex-wrap items-center gap-3">
          <span>{status.paused.reason}{status.queued ? `（排队中 ${status.queued} 个任务）` : ""}</span>
          <button className="btn" disabled={busy} onClick={async () => {
            setBusy(true);
            try { setStatus(await api.resumeRunner()); } finally { setBusy(false); }
          }}>继续运行</button>
        </div>
      </Notice>
    </div>
  );
}
