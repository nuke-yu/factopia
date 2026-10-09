import { lazy, Suspense, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { ResearchDraftProvider } from "./draft";
import ResearchPage from "./pages/ResearchPage";
import AuditPage from "./pages/AuditPage";

// 结果页包含 3D 库，按需加载
const ResultsPage = lazy(() => import("./pages/ResultsPage"));

const STEPS = [
  { to: "/research", no: "01", name: "研究立项", hint: "选择与筛选" },
  { to: "/audit", no: "02", name: "审核", hint: "确认研究规格" },
  { to: "/results", no: "03", name: "结果", hint: "挖掘与验证" },
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
                    <span className="hidden xl:inline text-xs text-faint">{s.hint}</span>
                  </NavLink>
                </div>
              ))}
            </nav>
            <div className="ml-auto"><ThemeToggle /></div>
          </div>
        </header>
        <main className="flex-1 mx-auto w-full max-w-[1680px] px-4 md:px-6 py-6">
          <Suspense fallback={<div className="label">加载中…</div>}>
            <Routes>
              <Route path="/" element={<Navigate to="/results" replace />} />
              <Route path="/research" element={<ResearchPage />} />
              <Route path="/audit" element={<AuditPage />} />
              <Route path="/results" element={<ResultsPage />} />
              <Route path="*" element={<Navigate to="/results" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </ResearchDraftProvider>
  );
}
