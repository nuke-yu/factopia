import { useSearchParams } from "react-router-dom";
import { api, type Project } from "../api";
import { MARKET_LABEL, useAsync } from "../lib";

/** 选择研究项目与锁定版本（地址栏 ?id=&v=）；返回当前选择，供组合台、审批页等使用。 */
export function useVersionPick(): { list: Project[]; proj?: Project; id: string; version: number; loading: boolean; error?: string;
  set: (id: string, v?: number) => void } {
  const [params, setParams] = useSearchParams();
  const projects = useAsync(() => api.projects(), []);
  const list = (projects.data ?? []).filter((p) => !p.example && p.versions.length > 0);
  const id = params.get("id") ?? list[0]?.id ?? "";
  const proj = list.find((p) => p.id === id);
  const version = Number(params.get("v") ?? proj?.versions[proj.versions.length - 1] ?? 0);
  return { list, proj, id, version, loading: projects.loading, error: projects.error,
    set: (i, v) => setParams(v ? { id: i, v: String(v) } : { id: i }) };
}

export default function VersionPicker({ pick }: { pick: ReturnType<typeof useVersionPick> }) {
  const { list, proj, id, version, set } = pick;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <label className="text-sm flex items-center gap-2 whitespace-nowrap">研究项目
        <select id="pick-project" className="field w-auto" value={id} onChange={(e) => set(e.target.value)}>
          {list.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
      </label>
      <label className="text-sm flex items-center gap-2 whitespace-nowrap">版本
        <select id="pick-version" className="field w-auto" value={version} onChange={(e) => set(id, Number(e.target.value))}>
          {(proj?.versions ?? []).map((v) => <option key={v} value={v}>v{v}</option>)}
        </select>
      </label>
      <span className="text-xs text-muted">{proj?.markets.map((m) => MARKET_LABEL[m]).join("、")}</span>
    </div>
  );
}
