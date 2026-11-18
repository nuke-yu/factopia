"""运行时模型设置（设置菜单）：读取 / 保存 AI 研发与平台 LLM 的模型选择。

- 角色与 .env 环境变量的对应关系只写在 configs/models.yaml，界面不显示变量名；
- 用户在设置菜单选择模型后写入 runtime/settings/models.json，立即影响平台调用；
  RD-Agent（AI 研发）在下一个运行任务启动时应用（runner 构造子进程环境时读取本文件）；
- 未设置的部分继续使用 .env 的值；费用为美元 / 百万 token，价格来自配置，
  默认模型回退到 FACTOPIA_LLM_PRICE_IN / OUT。
"""
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
if not RUNTIME.exists():                       # 本地直接运行时回退到仓库的 runtime/
    RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
if not CONFIGS.exists():                       # 本地直接运行时回退到仓库的 configs/
    CONFIGS = Path(__file__).resolve().parents[1] / "configs"
SETTINGS_DIR = RUNTIME / "settings"
SETTINGS_FILE = SETTINGS_DIR / "models.json"
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))

DEFAULT_ROLES = [
    {"key": "ai_rd", "env": "CHAT_MODEL", "prefix": "openai/"},
    {"key": "pro", "env": "FACTOPIA_LLM_MODEL"},
    {"key": "lite", "env": "FACTOPIA_LLM_LITE_MODEL"},
    {"key": "critic", "env": "FACTOPIA_LLM_CRITIC_MODEL"},
]
DEFAULT_OPTIONS = ["qwen3.8-max-0902", "deepseek-v4.1-flash", "qwen-plus"]


class SettingsError(ValueError):
    pass


def _config() -> dict:
    path = CONFIGS / "models.yaml"
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    roles = raw.get("roles")
    if not isinstance(roles, list) or not roles:
        roles = DEFAULT_ROLES
    options = raw.get("options")
    if not isinstance(options, list) or not options:
        options = DEFAULT_OPTIONS
    prices = raw.get("prices") if isinstance(raw.get("prices"), dict) else {}
    return {"roles": roles, "options": [str(x).strip() for x in options if str(x).strip()], "prices": prices}


def _read() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(f".json.tmp{secrets.token_hex(3)}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    try:                                          # runner 以 root 写；设置菜单由 API 用户写
        if os.geteuid() == 0:
            for p in (SETTINGS_FILE, SETTINGS_DIR):
                os.chown(p, API_UID, API_UID)
                os.chmod(p, 0o664 if p.is_file() else 0o775)
    except OSError:
        pass


def _strip_prefix(role: dict, value: str) -> str:
    prefix = str(role.get("prefix") or "")
    value = str(value or "").strip()
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


def current_models() -> dict[str, str]:
    """各角色当前生效的模型（界面显示名，AI 研发去掉 provider 前缀）。"""
    saved = _read().get("models") or {}
    out = {}
    for role in _config()["roles"]:
        key = str(role.get("key") or "")
        if not key:
            continue
        out[key] = str(saved.get(key) or _strip_prefix(role, os.environ.get(str(role.get("env")), "")))
    return out


def model_overrides() -> dict[str, str]:
    """用户在设置菜单里显式保存的模型（不含 .env 默认值），供 llm_config 覆盖使用。"""
    saved = _read().get("models") or {}
    keys = {str(r.get("key") or "") for r in _config()["roles"]}
    return {k: str(v).strip() for k, v in saved.items() if k in keys and str(v).strip()}


def _prices_for(model: str) -> dict | None:
    prices = _config()["prices"]
    p = prices.get(model)
    if isinstance(p, dict):
        try:
            return {"in": float(p.get("in")), "out": float(p.get("out"))}
        except (TypeError, ValueError):
            return None
    try:                                          # 默认模型：沿用 .env 的单价
        from factopia import llm_cost
        defaults = {_strip_prefix(r, os.environ.get(str(r.get("env")), "")) for r in _config()["roles"]}
        if model in defaults:
            pin, pout = llm_cost.prices()
            return {"in": pin, "out": pout}
    except Exception:
        pass
    return None


def state() -> dict:
    cfg = _config()
    current = current_models()
    roles = []
    for role in cfg["roles"]:
        key = str(role.get("key") or "")
        if not key:
            continue
        model = current.get(key) or ""
        default = _strip_prefix(role, os.environ.get(str(role.get("env")), ""))
        roles.append({"key": key, "model": model, "default": default, "price": _prices_for(model)})
    models = list(dict.fromkeys([*cfg["options"], *(r["model"] for r in roles if r["model"])]))
    return {"roles": roles, "options": cfg["options"],
            "prices": {m: _prices_for(m) for m in models},
            "updated_at": _read().get("updated_at")}


def save_models(models: dict) -> dict:
    """保存模型选择；值为空表示清除该角色的选择、回到 .env 默认值。"""
    cfg = _config()
    role_keys = {str(r.get("key") or "") for r in cfg["roles"]}
    defaults = {_strip_prefix(r, os.environ.get(str(r.get("env")), "")) for r in cfg["roles"]}
    allowed = set(cfg["options"]) | defaults
    if not isinstance(models, dict) or not models:
        raise SettingsError("没有需要保存的模型")
    clean, cleared = {}, []
    for key, value in models.items():
        if key not in role_keys:
            raise SettingsError(f"未知的模型角色：{key}")
        value = "" if value is None else str(value).strip()
        if not value:
            cleared.append(key)                 # 清除选择 → 使用 .env 默认
            continue
        if value not in allowed:
            raise SettingsError(f"不支持的模型：{value}")
        clean[key] = value
    merged = {**(_read().get("models") or {})}
    for key in cleared:
        merged.pop(key, None)
    merged.update(clean)
    _write({"models": merged, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return state()


def subprocess_env() -> dict:
    """runner 启动 RD-Agent 子进程时的覆盖环境（只覆盖用户在设置菜单里改过的 AI 研发模型）。"""
    overrides = model_overrides()
    ai_rd = overrides.get("ai_rd")
    if not ai_rd:
        return {}
    role = next((r for r in _config()["roles"] if r.get("key") == "ai_rd"), {})
    prefix = str(role.get("prefix") or "")
    value = ai_rd if not prefix or ai_rd.startswith(prefix) else f"{prefix}{ai_rd}"
    env = {"CHAT_MODEL": value}
    try:
        chat_map = json.loads(os.environ.get("CHAT_MODEL_MAP") or "{}")
    except json.JSONDecodeError:
        chat_map = {}
    if not isinstance(chat_map, dict):
        chat_map = {}
    feedback = chat_map.get("feedback")
    chat_map["feedback"] = {**(feedback if isinstance(feedback, dict) else {}), "model": value}
    env["CHAT_MODEL_MAP"] = json.dumps(chat_map, ensure_ascii=False)
    return env
