import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { LANG, t } from "./i18n";
import "./styles.css";

try {
  const saved = localStorage.getItem("factopia-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch { /* 浏览器禁用存储时使用默认深色 */ }

document.documentElement.lang = LANG;
document.title = t("Factopia 研究工作台");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
