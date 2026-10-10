import { Component, lazy, Suspense, useState, type ComponentType, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ResearchDraftProvider } from "./draft";
import ResearchPage from "./pages/ResearchPage";
import AuditPage from "./pages/AuditPage";
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
        <div className="font-display font-semibold">页面加载失败</div>
        <p className="text-sm text-muted">可能是平台刚更新过版本。刷新页面即可加载最新版本；已发起的运行不受影响。</p>
        <pre className="num text-xs text-faint whitespace-pre-wrap">{this.state.error.message}</pre>
        <button className="btn btn-primary self-start" onClick={() => window.location.reload()}>刷新页面</button>
      </div>
    );
  }
}

function Pages() {
  const location = useLocation();
  return (
    <PageBoundary resetKey={location.pathname}>
      <Suspense fallback={<div className="label">加载中…</div>}>
        <Routes>
          <Route path="/" element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/run" element={<RunPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/results" element={<ResultsPage />} />
          <Route path="/intel" element={<IntelPage />} />
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Routes>
      </Suspense>
    </PageBoundary>
  );
}

const STEPS = [
  { to: "/projects", no: "00", name: "研究项目", hint: "选择或新建" },
  { to: "/research", no: "01", name: "研究立项", hint: "范围与假设" },
  { to: "/audit", no: "02", name: "审核", hint: "确认研究规格" },
  { to: "/run", no: "03", name: "运行中", hint: "AI 研究过程" },
  { to: "/results", no: "04", name: "结果", hint: "挖掘与验证" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? "dark");
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      className="btn"
      aria-label={`切换为${next === "dark" ? "深色" : "浅色"}主题`}
      onClick={() => {
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem("factopia-theme", next); } catch { /* 忽略 */ }
        setTheme(next);
      }}
    >
      {theme === "dark" ? "浅色" : "深色"}
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
              <span className="hidden lg:inline text-xs text-muted">跨境量化研究平台 · 最小 POC</span>
            </div>
            <nav className="flex items-center gap-1 overflow-x-auto" aria-label="研究流程">
              {STEPS.map((s, i) => (
                <div key={s.to} className="flex items-center">
                  {i > 0 && <span className="mx-1 text-faint" aria-hidden>→</span>}
                  <NavLink
                    to={s.to}
                    className={({ isActive }) =>
                      `flex items-center gap-2 h-9 px-3 rounded border transition-colors whitespace-nowrap ${
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
              <NavLink
                to="/intel"
                className={({ isActive }) =>
                  `flex items-center gap-2 h-9 px-3 rounded border transition-colors whitespace-nowrap ${
                    isActive ? "border-accent/60 bg-accent/10 text-text" : "border-transparent text-muted hover:text-text"
                  }`}
              >
                <span className="font-medium">情报台</span>
                <span className="hidden 2xl:inline text-xs text-faint">多语言公告</span>
              </NavLink>
            </nav>
            <div className="ml-auto"><ThemeToggle /></div>
          </div>
        </header>
        <main className="flex-1 mx-auto w-full max-w-[1680px] px-4 md:px-6 py-6">
          <RunnerBanner />
          <Pages />
        </main>
      </div>
    </ResearchDraftProvider>
  );
}
