import { Component, lazy, Suspense, useState, type ComponentType, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ResearchDraftProvider } from "./draft";
import { t, tf } from "./i18n";
import ResearchPage from "./pages/ResearchPage";
import AuditPage from "./pages/AuditPage";
import { RegionBanner, RegionSwitch } from "./components/RegionSwitch";
import RunnerBanner from "./components/RunnerBanner";

/** 按需加载页面。重新部署后旧页面引用的文件名已不存在：加载失败时自动刷新一次，取得新版本。 */
function lazyPage(load: () => Promise<{ default: ComponentType }>) {
  return lazy(() => load().then((m) => {
    try { sessionStorage.removeItem("factopia-reloaded"); } catch { /* 忽略 */ }
    return m;
  }, (e) => {
    let reloaded = false;
    try { reloaded = sessionStorage.getItem("factopia-reloaded") === "1"; sessionStorage.setItem("factopia-reloaded", "1"); } catch { /* 忽略 */ }
    if (!reloaded) {
      window.location.reload();
      return new Promise<never>(() => undefined);  // 等待刷新
    }
    throw e;
  }));
}

// 含 3D 库的页面按需加载
const ProjectsPage = lazyPage(() => import("./pages/ProjectsPage"));
const RunPage = lazyPage(() => import("./pages/RunPage"));
const ResultsPage = lazyPage(() => import("./pages/ResultsPage"));
const IntelPage = lazyPage(() => import("./pages/IntelPage"));
const DataPage = lazyPage(() => import("./pages/DataPage"));
const RulesPage = lazyPage(() => import("./pages/RulesPage"));
const PortfolioPage = lazyPage(() => import("./pages/PortfolioPage"));
const ApprovalsPage = lazyPage(() => import("./pages/ApprovalsPage"));
const SignalsPage = lazyPage(() => import("./pages/SignalsPage"));
const OverviewPage = lazyPage(() => import("./pages/OverviewPage"));
const AcceptancePage = lazyPage(() => import("./pages/AcceptancePage"));
const LibraryPage = lazyPage(() => import("./pages/LibraryPage"));
const IdeasPage = lazyPage(() => import("./pages/IdeasPage"));

/** 页面出错时显示原因与刷新按钮，而不是空白页。切换页面后自动恢复。 */
class PageBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="panel p-5 flex flex-col gap-3 max-w-[70ch]">
        <div className="font-display font-semibold">{t("页面加载失败")}</div>
        <p className="text-sm text-muted">{t("可能是平台刚更新过版本。刷新页面即可加载最新版本；已发起的运行不受影响。")}</p>
        <pre className="num text-xs text-faint whitespace-pre-wrap">{this.state.error.message}</pre>
        <button className="btn btn-primary self-start" onClick={() => window.location.reload()}>{t("刷新页面")}</button>
      </div>
    );
  }
}

function Pages() {
  const location = useLocation();
  return (
    <PageBoundary resetKey={location.pathname}>
      <Suspense fallback={<div className="label">{t("加载中…")}</div>}>
        <Routes>
          <Route path="/" element={<Navigate to="/ideas" replace />} />
          <Route path="/ideas" element={<IdeasPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/run" element={<RunPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/results" element={<ResultsPage />} />
          <Route path="/intel" element={<IntelPage />} />
          <Route path="/data" element={<DataPage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/rules" element={<RulesPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/overview" element={<OverviewPage />} />
          <Route path="/acceptance" element={<AcceptancePage />} />
          <Route path="*" element={<Navigate to="/ideas" replace />} />
        </Routes>
      </Suspense>
    </PageBoundary>
  );
}

const STEPS = [
  { to: "/ideas", no: "00", name: t("线索池"), hint: t("信息输入") },
  { to: "/projects", no: "01", name: t("研究项目"), hint: t("选择或新建") },
  { to: "/research", no: "02", name: t("研究立项"), hint: t("范围与假设") },
  { to: "/audit", no: "03", name: t("审核"), hint: t("确认研究规格") },
  { to: "/run", no: "04", name: t("运行中"), hint: t("AI 研究过程") },
  { to: "/results", no: "05", name: t("结果"), hint: t("挖掘与验证") },
];

function ThemeToggle() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? "dark");
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      className="btn whitespace-nowrap shrink-0"
      aria-label={tf("切换为{mode}主题", { mode: next === "dark" ? t("深色") : t("浅色") })}
      onClick={() => {
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem("factopia-theme", next); } catch { /* 忽略 */ }
        setTheme(next);
      }}
    >
      {theme === "dark" ? t("浅色") : t("深色")}
    </button>
  );
}

export default function App() {
  return (
    <ResearchDraftProvider>
      <div className="min-h-full flex flex-col">
        <header className="sticky top-0 z-20 border-b border-line bg-bg/85 backdrop-blur">
          <div className="mx-auto max-w-[1680px] px-4 md:px-6 h-14 flex items-center gap-6">
            <div className="flex items-baseline gap-3 shrink-0">
              <span className="font-display text-lg font-semibold tracking-[0.18em] text-accent">FACTOPIA</span>
              <span className="hidden 2xl:inline text-xs text-muted">{t("跨境量化研究平台 · 最小 POC")}</span>
            </div>
            <nav className="flex items-center gap-1 overflow-x-auto" aria-label={t("研究流程")}>
              {STEPS.map((s, i) => (
                <div key={s.to} className="flex items-center">
                  {i > 0 && <span className="hidden 2xl:inline mx-0.5 text-faint" aria-hidden>→</span>}
                  <NavLink
                    to={s.to}
                    className={({ isActive }) =>
                      `flex items-center gap-1.5 h-9 px-2 rounded border transition-colors whitespace-nowrap ${
                        isActive ? "border-accent/60 bg-accent/10 text-text" : "border-transparent text-muted hover:text-text"
                      }`}
                  >
                    <span className="num text-xs text-accent">{s.no}</span>
                    <span className="font-medium">{s.name}</span>
                    <span className="hidden 2xl:inline text-xs text-faint">{s.hint}</span>
                  </NavLink>
                </div>
              ))}
              <span className="mx-2 h-5 border-l border-line" aria-hidden />
              {[{ to: "/intel", name: t("情报台"), hint: t("多语言公告") }, { to: "/data", name: t("数据台"), hint: t("主数据与时间点") }, { to: "/library", name: t("因子库"), hint: t("因子库与知识检索") }, { to: "/rules", name: t("风控台"), hint: t("规则包与预检") }, { to: "/portfolio", name: t("组合台"), hint: t("授权组合与报告") }, { to: "/approvals", name: t("审批台"), hint: t("跨地区审批与审计") },
                { to: "/signals", name: t("信号目录"), hint: t("分市场版本与监控") }, { to: "/overview", name: t("总览"), hint: t("管理总览") }, { to: "/acceptance", name: t("验收"), hint: t("成功标准与红队") }].map((d) => (
                <NavLink
                  key={d.to}
                  to={d.to}
                  className={({ isActive }) =>
                    `flex items-center gap-2 h-9 px-1.5 rounded border transition-colors whitespace-nowrap ${
                      isActive ? "border-accent/60 bg-accent/10 text-text" : "border-transparent text-muted hover:text-text"
                    }`}
                >
                  <span className="font-medium">{d.name}</span>
                  <span className="hidden 2xl:inline text-xs text-faint">{d.hint}</span>
                </NavLink>
              ))}
            </nav>
            <div className="ml-auto flex items-center gap-2"><RegionSwitch /><ThemeToggle /></div>
          </div>
        </header>
        <main className="flex-1 mx-auto w-full max-w-[1680px] px-4 md:px-6 py-6">
          <RegionBanner />
          <RunnerBanner />
          <Pages />
        </main>
      </div>
    </ResearchDraftProvider>
  );
}
