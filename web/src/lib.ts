import { useEffect, useState } from "react";
import type { Market } from "./api";

export const MARKET_LABEL: Record<Market, string> = { hk: "香港", uk: "英国", fr: "法国" };
export const MARKET_CITY: Record<Market, string> = { hk: "HONG KONG", uk: "LONDON", fr: "PARIS" };

/** 读取当前主题下的 CSS 变量（图表库需要具体颜色值）。 */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
export const marketColor = (m: Market) => cssVar(`--${m}`);

/** 主题变化时返回新的版本号，用于让图表重新读取颜色。 */
export function useThemeVersion(): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setV((x) => x + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return v;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data?: T; error?: string; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true }));
    fn().then(
      (data) => alive && setState({ data, loading: false }),
      (e: Error) => alive && setState({ error: e.message, loading: false }),
    );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}

export const pct = (x: number | null | undefined, nd = 1) => (x == null ? "—" : `${(x * 100).toFixed(nd)}%`);
export const fix = (x: number | null | undefined, nd = 3) => (x == null ? "—" : x.toFixed(nd));
export const signed = (x: number | null | undefined, nd = 1) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(nd)}%`);

/** 系统设置了"减少动画"时，关闭自动播放与相机漫游。 */
export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** 迁移性分类标签 → 显示颜色（语义色，不使用市场色以外的强调色）。 */
export function labelColor(label: string): string {
  if (label === "三市场通用") return cssVar("--accent");
  if (label === "需调参") return cssVar("--warn");
  if (label.startsWith("地区特定")) {
    const ms = label.slice(5, -1).toLowerCase().split("/") as Market[];
    return ms.length === 1 ? marketColor(ms[0]) : cssVar("--text");
  }
  return cssVar("--faint");
}
