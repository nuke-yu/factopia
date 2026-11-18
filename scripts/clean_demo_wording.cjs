// 一次性脚本：清理客户可见的「演示」措辞（i18n 键+值、lang.py、api 默认名、configs、文档、前端调用点）
// 用法：node scripts/clean_demo_wording.mjs（用后即删）
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");

// 简体（键、调用点、文档、configs、py 文案）
const S = [
  ["研究员（演示，未登录）", "研究员（未登录）"],
  ["发起研究员（演示，未登录）", "发起研究员（未登录）"],
  ["审批人（演示，未登录）", "审批人（未登录）"],
  ["地区角色（演示用，不是登录鉴权）", "地区角色（本地视角，不是登录鉴权）"],
  ["（演示用，不是登录鉴权）", "（本地视角，不是登录鉴权）"],
  ["姓名（演示用，不校验身份）", "姓名（不校验身份）"],
  ["模拟数据，仅用于流程演示", "模拟数据，仅用于流程验证"],
  ["仅用于流程演示", "仅用于流程验证"],
  ["只用于流程演示", "只用于流程验证"],
  ["演示主线", "示例主线"],
  ["演示用地区视角", "本地地区视角"],
  ["演示一键接入", "模拟盘一键接入"],
  ["一键接入演示线", "一键接入模拟盘"],
  ["还不能接入演示线", "还不能接入模拟盘"],
  ["长桥演示通道", "长桥模拟盘通道"],
  ["切换演示身份后操作", "切换地区身份后操作"],
  ["审批人为演示角色", "审批人为平台内置角色"],
  ["合成违规演示", "合成违规样例"],
  ["用于演示上线后的监控", "用于展示上线后的监控"],
  ["仅演示", "仅流程验证"],
  ["演示身份", "地区身份"],
  ["演示用，不是登录鉴权", "本地视角，不是登录鉴权"],
  ["演示脚本", "介绍脚本"],
  ["演示数据", "预置数据"],
  ["主演示", "主线讲解"],
  ["演示路径", "讲解路径"],
  ["演示时", "介绍时"],
  ["演示边界", "常见问题边界"],
  ["演示成本", "单次运行成本"],
  ["演示效果", "实际效果"],
  ["偏演示型", "偏展示型"],
  ["演示线索", "预置线索"],
  ["演示 Agent", "预置 Agent"],
  ["演示期间", "介绍期间"],
  ["做现场演示", "做现场介绍"],
  ["演示用", "展示用"],
];

// 繁体（zh-hk 值）
const T = [
  ["模擬數據，僅用於流程演示", "模擬數據，僅用於流程驗證"],
  ["僅用於流程演示", "僅用於流程驗證"],
  ["只用于流程演示", "只用於流程驗證"],
  ["演示主線", "示例主線"],
  ["演示用地區視角", "本地地區視角"],
  ["演示一鍵接入", "模擬盤一鍵接入"],
  ["一鍵接入演示線", "一鍵接入模擬盤"],
  ["還不能接入演示線", "還不能接入模擬盤"],
  ["長橋演示通道", "長橋模擬盤通道"],
  ["切換演示身份後操作", "切換地區身份後操作"],
  ["審批人為演示角色", "審批人為平台內置角色"],
  ["合成違規演示", "合成違規樣例"],
  ["用於演示上線後的監控", "用於展示上線後的監控"],
  ["僅演示", "僅流程驗證"],
  ["演示身份", "地區身份"],
  ["地區角色（演示用，不是登入鑑權）", "地區角色（本地視角，不是登入鑑權）"],
  ["（演示用，不是登入鑑權）", "（本地視角，不是登入鑑權）"],
  ["姓名（演示用，不校驗身份）", "姓名（不校驗身份）"],
];

// 英文值
const E = [
  ["Region role (demo only, not authentication)", "Region role (local view, not authentication)"],
  [" (demo only, not authentication) · Shows only", " (local view, not authentication) · Shows only"],
  ["Name (for demo, identity not verified)", "Name (identity not verified)"],
  ["for process demonstration only", "for process verification only"],
  ["for workflow demonstration only", "for workflow verification only"],
  ["for workflow demo purposes only", "for workflow verification purposes only"],
  ["Demo storyline", "Sample storyline"],
  ["demo region view", "local region view"],
  ["demo one-click connect", "paper-trading one-click connect"],
  ["Demo role", "Region role"],
  ["Synthetic violation demo", "Synthetic violation sample"],
  ["switch the demo role to act", "switch the region role to act"],
  ["approvers are demo roles", "approvers are platform roles"],
  ["demo only", "process verification only"],
  ["to demonstrate post-launch monitoring", "to show post-launch monitoring"],
  ["Not ready for the demo line yet", "Not ready for paper trading yet"],
  ["one-click demo line enrolment", "one-click paper-trading enrolment"],
  ["Longbridge demo channel", "Longbridge paper-trading channel"],
  ["One-click demo line", "One-click paper-trading"],
];

// 法文值
const F = [
  ["Rôle régional (démo, sans authentification)", "Rôle régional (vue locale, sans authentification)"],
  [" (démo, sans authentification) · Affiche uniquement", " (vue locale, sans authentification) · Affiche uniquement"],
  ["Nom (pour la démo, identité non vérifiée)", "Nom (identité non vérifiée)"],
  ["uniquement pour la démonstration du processus", "uniquement pour la vérification du processus"],
  ["uniquement pour la démonstration du flux", "uniquement pour la vérification du flux"],
  ["Fil conducteur de la démo", "Fil conducteur de l'exemple"],
  ["vue régionale de démonstration", "vue régionale locale"],
  ["connexion démo en un clic", "connexion au trading simulé en un clic"],
  ["Rôle de démonstration", "Rôle régional"],
  ["changez de rôle de démonstration pour agir", "changez de rôle régional pour agir"],
  ["les approbateurs sont des rôles de démonstration, aucune personne réelle n'est impliquée",
   "les approbateurs sont des rôles intégrés à la plateforme, aucune personne réelle n'est impliquée"],
  ["démonstration uniquement", "vérification du processus uniquement"],
  ["pour démontrer le suivi après mise en production", "pour montrer le suivi après mise en production"],
  ["à des fins de démonstration du processus", "à des fins de vérification du processus"],
  ["Uniquement pour la démonstration du processus", "Uniquement pour la vérification du processus"],
  ["ligne de démonstration", "ligne de trading simulé"],
  ["Canal de démonstration Longbridge", "Canal de trading simulé Longbridge"],
  ["démo en un clic", "trading simulé en un clic"],
];

const FILES = [
  "web/src/i18n/zh-hk.ts", "web/src/i18n/en.ts", "web/src/i18n/fr.ts",
  "factopia/lang.py", "factopia/api/main.py",
  "configs/access/regions.yaml", "configs/rules/common.yaml", "configs/rules/data_sources.yaml",
  "docs/Agentic-workflow-结果-研究迭代.md", "docs/Agentic-workflow-线索-AI调研.md",
  "docs/Agentic-workflow-情报台.md", "docs/1.Agentic-workflow-rdAgent.md",
  "docs/2.Agentic-workflow-结果-研究迭代.md",
];
// 前端调用点（含 t() 与硬编码串；不碰后端注释）
const GLOBS = ["web/src/pages/**/*.tsx", "web/src/pages/**/*.ts", "web/src/components/**/*.tsx", "web/src/components/**/*.ts"];

// 后端调用点：t("旧键") 与默认值必须与 lang.py 新键一致；排除内部 demo 脚本与测试
function pyFiles() {
  const out = [];
  (function walk(d) {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const fp = path.join(d, e.name);
      if (e.isDirectory()) walk(fp);
      else if (fp.endsWith(".py") && !fp.endsWith("demo_e2e.py") && !fp.endsWith("seed_e2e.py")) out.push(fp);
    }
  })(path.join(ROOT, "factopia"));
  return out;
}

function glob(p) {
  const dir = path.dirname(path.dirname(p)), ext = path.extname(p);
  const out = [];
  (function walk(d) {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const fp = path.join(d, e.name);
      if (e.isDirectory()) walk(fp);
      else if (fp.endsWith(ext)) out.push(fp);
    }
  })(path.join(ROOT, dir));
  return out;
}

const all = [...S, ...T, ...E, ...F];
const ALL_REL = [...FILES, ...GLOBS.flatMap((g) => glob(g).map((p) => path.relative(ROOT, p).replace(/\\/g, "/"))),
  ...pyFiles().map((p) => path.relative(ROOT, p).replace(/\\/g, "/"))];
let total = 0;
for (const rel of ALL_REL) {
  const fp = path.join(ROOT, rel);
  if (!fs.existsSync(fp)) { console.log("SKIP (missing):", rel); continue; }
  let text = fs.readFileSync(fp, "utf8");
  let n = 0;
  for (const [a, b] of all) {
    if (text.includes(a)) {
      const k = text.split(a).length - 1;
      text = text.split(a).join(b);
      n += k;
    }
  }
  if (n) {
    fs.writeFileSync(fp, text);
    console.log(`${rel}: ${n} 处替换`);
    total += n;
  }
}
console.log(`共 ${total} 处`);
