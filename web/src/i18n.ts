// 界面多语言：语言随地区视角联动（Spec 第 14.20 节，演示用，不是登录鉴权）。
// 总部=简体中文 / 香港团队=繁體中文 / 伦敦团队=English / 巴黎团队=Français；
// 切换地区视角时整页重新加载（RegionSwitch），语言随 cookie 重新初始化。
// 字典以「简体中文原文」为键：未收录的条目原样回退显示简体中文，支持逐页渐进翻译。
import zhHK from "./i18n/zh-hk";
import en from "./i18n/en";
import fr from "./i18n/fr";

export type Lang = "zh-CN" | "zh-HK" | "en" | "fr";

/** 地区角色 → 界面语言 */
const REGION_LANG: Record<string, Lang> = { hq: "zh-CN", hk: "zh-HK", uk: "en", fr: "fr" };

function initialLang(): Lang {
  const m = document.cookie.match(/(?:^|;\s*)factopia_region=([A-Za-z_-]+)/);
  return REGION_LANG[m?.[1] ?? ""] ?? "zh-CN";
}

/** 当前语言。页面生命周期内固定（切换地区会整页重载）。 */
export const LANG: Lang = initialLang();

const DICTS: Record<Lang, Record<string, string>> = {
  "zh-CN": {},
  "zh-HK": zhHK,
  en,
  fr,
};

/** 翻译固定文案。键即简体中文原文；未收录时原样返回（简体中文回退）。 */
export function t(key: string): string {
  return DICTS[LANG][key] ?? key;
}

/** 翻译含变量的文案，占位符写作 {name}。 */
export function tf(key: string, vars: Record<string, string | number>): string {
  return t(key).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}

/** 当前是否中文（日期、单位等本地化分支用）。 */
export const IS_ZH = LANG.startsWith("zh");

/** 浏览器时间/日期格式化区域（与界面语言对应）。 */
export const LOCALE: Record<Lang, string> = { "zh-CN": "zh-CN", "zh-HK": "zh-HK", en: "en-GB", fr: "fr-FR" };

/** 顿号列表连接：中文「、」，西方语言「, 」。 */
export function joinList(items: string[]): string {
  return items.join(IS_ZH ? "、" : ", ");
}

/** 页面 <title> 前缀（main.tsx / App.tsx 设置） */
export const APP_TITLE = t("Factopia · 跨市场量化研究平台");

/** 地区角色的界面名称：后端 regions.yaml 的 name 是简体中文，按角色代码本地化显示。 */
const ROLE_LABELS: Record<string, string> = {
  hq: t("总部（全部地区）"),
  hk: t("香港团队"),
  uk: t("伦敦团队"),
  fr: t("巴黎团队"),
};
export function roleLabel(code: string, fallback: string): string {
  return ROLE_LABELS[code] ?? fallback;
}
