# 前端 i18n 转换规范（子代理共用）

Factopia 前端界面语言随地区视角联动（cookie `factopia_region`）：
**hq=简体中文 / hk=繁體中文 / uk=English / fr=Français**。
基础设施 `web/src/i18n.ts` 与 App 外壳、公共组件已转换完毕（可参考 `src/App.tsx`、`src/components/LiveStatus.tsx`、`src/components/ui.tsx` 的现有写法）。
执行分两个阶段（降低单次派发的输出量，避免结果丢失）：
- **阶段一（页面转换代理）**：只转换分给你的文件，执行本规范第一、二、五节；**禁止打开或编辑任何字典文件**——缺词条没关系，t() 会回退显示简体中文，词条由阶段二统一补齐。
- **阶段二（语言代理）**：按语言各一个代理（zh-hk / en / fr），从源码统一提取全部 t()/tf() 键并填写自己的字典，执行第三、四节。

## 一、t() / tf() 用法

```tsx
import { t, tf } from "../i18n";          // 只 import 实际用到的名字
<h2>{t("信号目录")}</h2>
<span>{tf("第 {i} 轮", { i })}</span>
```

- **键 = 简体中文原文**，逐字照抄（含 ：、（）、……、· 等标点）。缺词条时回退显示简体中文，不会报错。
- 文案含变量（`第 ${i} 轮`、`${n} 个任务`）→ 改写为 `tf("第 {i} 轮", { i })`，占位符写 `{name}`，变量入第二参。
- **模块顶层的常量数组/对象可以直接调用 t()**（本文件 import 了 i18n，i18n 先求值），参考 LiveStatus 的 `STATE_LABEL`、App.tsx 的 `STEPS`。
- 需要转换的中文位置：JSX 文本、字符串字面量赋值、`aria-label`、`title`、`placeholder`、`<option>`、`<button>` 文本、ECharts 的 `axisLabel`/`tooltip`/`legend`/`formatter`/series name。
- 日期时间格式化：`new Date(x).toLocaleTimeString(LOCALE[LANG], { hour12: false })`（`import { LANG, LOCALE } from "…/i18n"`）。
- 列表连接用 `joinList()`（i18n.ts 导出）：中文「、」西方「,  」。
- **若文件已有同名局部变量 `LANG` / `LOCALE`（如 IntelPage 的 `const LANG = { zh: "中文", … }`），不要 import i18n 同名导出**——只 import t/tf，或用别名 `import { LOCALE as LOC }`。
- import 路径：`src/pages/*` 与 `src/components/*` 用 `"../i18n"`；`src/components/charts/*` 用 `"../../i18n"`；`src/*` 根文件用 `"./i18n"`。tsconfig 开了 noUnusedLocals：import 里不许有没用到的名字。

## 二、不翻译清单（硬性规则）

1. **代码注释**保持简体中文不动。
2. `console.*` 的参数。
3. **协议值**：URL、查询参数、发给后端的请求体字段与值（如 `"watch"`、`"pause"`、`"must"`、状态代码 `"running"`）、后端枚举代码。
4. localStorage 键、cookie 名、CSS 类名、对象键（key）、路由路径。
5. 与后端字段比较的字面量（`job.state === "running"` 右侧不译）。若**中文**字面量被同时用作 Map key 与比较（如 LiveStatus BacktestBars 的 `byKey`），两侧必须**同时**包 t()，保证比较一致。
6. **后端内容字段**（`state_zh`、`market_zh`、`reason`、公告标题/摘要等）：优先改用前端按代码映射（`STATE_LABEL[job.state] ?? job.state_zh`；市场名用 `MARKET_LABEL[market]`），映射不了就原样显示，不要塞进字典。
7. **演示/fixture 数据**里代表后端生成内容的字符串（公告正文、摘要、公司名等数据样本）：原样保留（内容语言由后端流水线按市场另行生成）。判断标准：它是"界面控件文案"还是"模拟的数据内容"。拿不准时保留并在报告中列出。
8. LaTeX 公式、代码片段、数字、纯英文。

## 三、字典填写（仅阶段二语言代理）

三个字典：`web/src/i18n/zh-hk.ts`、`web/src/i18n/en.ts`、`web/src/i18n/fr.ts`，结构：

```ts
export default {
  "简体中文键": "译文",
} satisfies Record<string, string>;
```

- 文件末尾（`} satisfies …` 之前）预置了 G1–G12 分组锚点注释行，如 `  // ── G3 StudyPanel+PrecheckView+小图表 ──`。
- 语言代理负责整个字典：把文件末尾 G1–G12 锚点注释块整体替换为本次新增词条（可按组保留注释行分隔），其余内容一字不改。每个字典同时只有一个代理在改。
- 词条格式：`  "键": "译文",` 每行一条；键必须与代码里 t()/tf() 的参数**逐字一致**。
- 追加前先把三个字典**通读一遍**：已存在的键（含基础词条与其他组已加的）不要重复添加，直接用即可。自己的词条内部也不得重复（TS1117 重复键报错）。
- **zh-hk**：先转成繁体，与简体完全相同的词条**可省略**（回退即正确）。用词习惯：信号→訊號、数据→數據、信息→資訊、刷新页面→重新整理頁面、登录→登入。
- **en**：en-GB 用词（organise/analyse 可，程序 UI 用名词短语、首字母大写）。
- **fr**：法文排版（冒号后空格）；术语参考术语表。

## 四、术语表（跨组一致，优先级最高）

| 简体 | zh-HK | en | fr |
|---|---|---|---|
| 模拟盘 | 模擬盤 | Paper trading | Trading papier |
| 影子撮合 | 影子撮合 | Shadow fill | Exécution fictive |
| 长桥 | 長橋 | Longbridge (live broker) | Longbridge (courtier réel) |
| 信号 | 訊號 | Signal | Signal |
| 情报台 | 情報台 | Intel Desk | Veille |
| 数据台 | 數據台 | Data Desk | Données |
| 因子库 | 因子庫 | Factor Library | Bibliothèque de facteurs |
| 风控台 | 風控台 | Risk Desk | Risques |
| 组合台 | 組合台 | Portfolio Desk | Portefeuille |
| 审批台 | 審批台 | Approvals | Approbations |
| 信号目录 | 訊號目錄 | Signal Catalog | Catalogue de signaux |
| 净值 | 淨值 | NAV | NAV |
| 回测 | 回測 | Backtest | Backtest |
| 超额年化 | 超額年化 | Annualized excess | Surperformance annualisée |
| 扣成本年化 | 扣成本年化 | Annualized net of costs | Annualisé net de coûts |
| 最大回撤 | 最大回撤 | Max drawdown | Drawdown max. |
| 信息比率 | 信息比率 | Information ratio | Ratio d'information |
| 起点 | 起點 | Baseline | Référence |
| 排队中 | 排隊中 | Queued | En file |
| 已完成 | 已完成 | Done | Terminé |
| 失败 | 失敗 | Failed | Échec |
| 运行中 | 運行中 | Running | En cours |
| 香港/英国/法国 | 香港/英國/法國 | Hong Kong / UK / France | Hong Kong / Royaume-Uni / France |

（基础词条 108 条已含导航/状态/市场名等，转换前先读字典，勿重复。）

## 五、流程与自查

1. 通读本规范 → 逐个读取并转换分给你的文件。（通读字典了解已有词条是阶段二语言代理的事。）
2. 【防「输出过长」中断 · 最高优先级】工作期间回复中**不写任何叙述/分析/计划文字**，只发工具调用；文字只出现在最终报告。编辑小步多次：每次 Edit 的 old_string 与 new_string **各 ≤10 行**；每条回复最多 5 个编辑；**禁止 Write 重写整个文件**；重复出现的相同文案用 `replace_all`。只读必要文件（规范 + 你负责的文件 + 至多一个参考文件）。
3. 阶段一代理不碰字典；阶段二语言代理统一从源码提取全部键后一次性填写。
4. **不要运行 tsc / vite build**（主控在整波结束后统一验证，避免并发期间互相看到对方的半成品报错）。
5. 自查：① 每个 t()/tf() 键都是逐字照抄代码原文的简体中文（词条由阶段二补齐）；② import 无未用名字；③ 协议值、注释、后端内容未动；④ （仅阶段二）字典除锚点块外一字未改、无重复键。
6. 报告限 5 行内：转换的文件数与 t()/tf() 总数；拿不准而保留的中文（含原因）。不要粘贴代码或大段文件内容。
