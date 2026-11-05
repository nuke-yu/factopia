import { api, setRegion } from "../api";
import { roleLabel, t, tf } from "../i18n";
import { useAsync } from "../lib";

// 地区视角（Spec 第 14.20 节；BR-45）：演示用，不是登录鉴权。切换后整页重新加载，
// 后端按 configs/access/regions.yaml 过滤；界面语言随之切换（总部=简中 / 香港=繁中 / 伦敦=English / 巴黎=Français）

export function RegionSwitch() {
  const { data } = useAsync(() => api.access(), []);
  if (!data) return null;
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted whitespace-nowrap" title={`${data.status} · ${data.version}`}>
      <span className="hidden 2xl:inline">{t("地区视角")}</span>
      <select id="region-role" aria-label={t("地区角色（演示用，不是登录鉴权）")} className="field h-8 py-0 pl-2 pr-6 text-xs w-[7.5rem]"
        value={data.current.code}
        onChange={(e) => { const r = data.roles.find((x) => x.code === e.target.value); if (r) { setRegion(r); window.location.reload(); } }}>
        {data.roles.map((r) => <option key={r.code} value={r.code}>{roleLabel(r.code, r.name)}</option>)}
      </select>
    </label>
  );
}

export function RegionBanner() {
  const { data } = useAsync(() => api.access(), []);
  if (!data || data.current.cross_region) return null;
  return (
    <div className="mb-4 rounded border border-warn/50 bg-warn/10 px-3 py-2 text-sm">
      <span className="font-medium">{tf("地区视角：{name}", { name: roleLabel(data.current.code, data.current.name) })}</span>
      <span className="text-muted">{t("（演示用，不是登录鉴权）· 只显示本地区市场的数据与信号；多市场组合、研究报告、审计证据包与跨市场比较只对总部开放。")}</span>
    </div>
  );
}
