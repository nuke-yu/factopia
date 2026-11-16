"""研究迭代 Agent 的记录存储（Spec 第 18.3 节）。

    runtime/agents/<agent_id>/agent.json              状态、根研究版本、策略、费用
    runtime/agents/<agent_id>/rounds/<n>/facts.json   事实包（决策安全：不含测试期 / holdout 数值）
    runtime/agents/<agent_id>/rounds/<n>/diagnosis.json
    runtime/agents/<agent_id>/rounds/<n>/plan.json
    runtime/agents/<agent_id>/rounds/<n>/action.json
    runtime/agents/<agent_id>/rounds/<n>/evaluation.json
    runtime/agents/<agent_id>/memory.json             尝试记录与排除方向
    runtime/agents/<agent_id>/trace.jsonl             每次 LLM / 工具调用
    runtime/agents/<agent_id>/report.md               迭代报告

P0 用文件存储；阶段 2 可改数据库。所有写入原子替换，trace 追加写。
"""
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from factopia import db
from factopia.repo import agents as _repo_agents

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
if not RUNTIME.exists():                       # 本地直接运行（不在容器内）时回退到仓库的 runtime/
    RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
AGENTS_DIR = RUNTIME / "agents"
AGENT_ID = re.compile(r"^agt_\d{14}_[0-9a-f]{4}$")
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))   # runner 以 root 运行；新文件交给 API 用户读写


class AgentError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(obj):
    """递归把 NaN/Inf 换成 None（与 research/store.py 相同，避免写出非法 JSON）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _dumps(obj, indent: int | None = None) -> str:
    return json.dumps(_json_safe(obj), ensure_ascii=False, indent=indent, allow_nan=False)


def _own(path: Path) -> None:
    """runner 以 root 写文件时，把文件与目录交给 API 用户（10001），否则 API 无法写 approval/trace。"""
    # Windows 没有 geteuid/chown，本机开发时直接跳过（容器里仍是原来的行为）
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        for p in (path, path.parent):
            os.chown(p, API_UID, API_UID)
            if p.is_dir():
                os.chmod(p, 0o775)
            else:
                os.chmod(p, 0o664)
        if path.parent.parent != AGENTS_DIR and AGENTS_DIR in path.parent.parent.parents:
            os.chown(path.parent.parent, API_UID, API_UID)
            os.chmod(path.parent.parent, 0o775)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _own(path)


def new_id() -> str:
    return f"agt_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"


def dir_path(agent_id: str) -> Path:
    if not AGENT_ID.match(agent_id or ""):
        raise AgentError(f"Agent 编号无效：{agent_id}")
    return AGENTS_DIR / agent_id


def create(research_id: str, version: int, policy: dict, markets: list[str], horizon: int,
           dry_run: bool = False, web_search: bool = False, deep_think: bool = False) -> dict:
    """新建一个 Agent 记录；根研究版本、策略与预算在启动时固定。"""
    agent = {
        "agent_id": new_id(), "kind": "iteration", "state": "queued",
        "created_at": now(), "updated_at": now(),
        "root": {"research_id": research_id, "version": int(version),
                 "markets": list(markets), "horizon": int(horizon)},
        "policy": policy,
        "current_round": 0,
        "current_version": int(version),
        "parent_version": int(version),
        "child_version": None,
        "round_limit": int(policy.get("max_rounds") or 5),
        "rounds": [],
        "dry_run": bool(dry_run),
        "web_search": bool(web_search),
        "deep_think": bool(deep_think),
        "auto_approve": False,
        "approved": None,
        "approved_run": True,
        "cost_usd": 0.0,
        "costed_jobs": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "child_jobs": [],
        "last_action": None,
        "stop_reason": None,
        "last_error": None,
    }
    _persist(agent)
    return agent


def _file_save(agent: dict) -> None:
    agent["updated_at"] = now()
    _atomic_write(dir_path(agent["agent_id"]) / "agent.json", _dumps(agent, indent=1))


def _file_load(agent_id: str) -> dict:
    path = dir_path(agent_id) / "agent.json"
    if not path.exists():
        raise AgentError(f"Agent 不存在：{agent_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _file_list_agents() -> list[dict]:
    if not AGENTS_DIR.exists():
        return []
    out = []
    for path in sorted(AGENTS_DIR.glob("agt_*/agent.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda a: a.get("created_at") or "", reverse=True)


def round_dir(agent_id: str, n: int) -> Path:
    return dir_path(agent_id) / "rounds" / f"{int(n):03d}"


def _file_write_round(agent_id: str, n: int, name: str, data) -> Path:
    path = round_dir(agent_id, n) / f"{name}.json"
    _atomic_write(path, _dumps(data, indent=1))
    return path


def _file_read_round(agent_id: str, n: int, name: str) -> dict | None:
    path = round_dir(agent_id, n) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _file_append_trace(agent_id: str, entry: dict) -> None:
    """追加一条 trace：LLM 调用、工具调用、状态迁移都写在这里（WB-01 / AGT-09）。"""
    record = {"at": now(), **entry}
    path = dir_path(agent_id) / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_dumps(record) + "\n")
    _own(path)


def add_usage(agent: dict, usage: dict | None) -> float:
    """累计 token 与费用。draft._chat 返回的 usage 含 usd；make_draft 的 meta 只有 token，按单价换算。"""
    usage = usage or {}
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    usd = float(usage.get("usd") or 0.0)
    if not usd and (pt or ct):
        from factopia import llm_cost
        usd = llm_cost.usd(pt, ct)
    agent["usage"]["prompt_tokens"] += pt
    agent["usage"]["completion_tokens"] += ct
    agent["cost_usd"] = round(float(agent.get("cost_usd") or 0.0) + usd, 6)
    _persist(agent)
    return usd


def _file_memory(agent_id: str) -> dict:
    path = dir_path(agent_id) / "memory.json"
    if not path.exists():
        return {"attempts": [], "exclusions": [], "updated_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def _file_save_memory(agent_id: str, data: dict) -> None:
    data["updated_at"] = now()
    _atomic_write(dir_path(agent_id) / "memory.json", _dumps(data, indent=1))


# ══════════════════════════════════════════════════════════════════════════════
# 存储分发：files / dual / db
#
# 签名与返回形状保持不变（load 仍给嵌套的 root，rounds 仍是空列表——轮次在独立表里）。
# report.md 例外：它是整篇 markdown，db 模式存进 agent.report 一列，
# 调用方改用 report() / save_report() 而不是自己拼路径。
# ══════════════════════════════════════════════════════════════════════════════

DOMAIN = _repo_agents.DOMAIN


def _persist(agent: dict) -> None:
    mode = db.mode(DOMAIN)
    if mode == "files":
        _file_save(agent)
        return
    if mode == "dual":
        _file_save(agent)
        db.mirror(lambda: _repo_agents.save(agent), "agents.save")
        return
    _repo_agents.save(agent)


def save(agent: dict) -> None:
    _persist(agent)


def load(agent_id: str) -> dict:
    _dir_ok = dir_path(agent_id)          # 保持"Agent 编号无效"的校验
    if db.is_primary(DOMAIN):
        try:
            return _repo_agents.load(agent_id)
        except FileNotFoundError as exc:
            raise AgentError(f"Agent 不存在：{agent_id}") from exc
    return _file_load(agent_id)


def list_agents() -> list[dict]:
    if db.is_primary(DOMAIN):
        return _repo_agents.list_agents()
    return _file_list_agents()


def write_round(agent_id: str, n: int, name: str, data) -> Path | None:
    mode = db.mode(DOMAIN)
    if mode == "files":
        return _file_write_round(agent_id, n, name, data)
    if mode == "dual":
        path = _file_write_round(agent_id, n, name, data)
        db.mirror(lambda: _repo_agents.write_round(agent_id, n, name, data), "agents.write_round")
        return path
    _repo_agents.write_round(agent_id, n, name, data)
    return None


def read_round(agent_id: str, n: int, name: str) -> dict | None:
    if db.is_primary(DOMAIN):
        return _repo_agents.read_round(agent_id, n, name)
    return _file_read_round(agent_id, n, name)


def rounds(agent_id: str) -> list[dict]:
    """全部轮次（一次查询）；文件模式按目录拼。"""
    if db.is_primary(DOMAIN):
        return _repo_agents.rounds(agent_id)
    out = []
    base = dir_path(agent_id) / "rounds"
    if not base.exists():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        rec = {"n": int(d.name)}
        for p in sorted(d.glob("*.json")):
            try:
                rec[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        out.append(rec)
    return out


def append_trace(agent_id: str, entry: dict) -> None:
    mode = db.mode(DOMAIN)
    if mode == "files":
        _file_append_trace(agent_id, entry)
        return
    if mode == "dual":
        _file_append_trace(agent_id, entry)
        db.mirror(lambda: _repo_agents.append_trace(agent_id, entry), "agents.append_trace")
        return
    _repo_agents.append_trace(agent_id, entry)


def trace(agent_id: str, limit: int | None = None) -> list[dict]:
    """调用留痕。文件模式读 trace.jsonl（api 原来的做法），数据库模式走索引。"""
    if db.is_primary(DOMAIN):
        return _repo_agents.trace(agent_id, limit)
    path = dir_path(agent_id) / "trace.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[-limit:]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def memory(agent_id: str) -> dict:
    if db.is_primary(DOMAIN):
        return _repo_agents.memory(agent_id)
    return _file_memory(agent_id)


def save_memory(agent_id: str, data: dict) -> None:
    mode = db.mode(DOMAIN)
    if mode == "files":
        _file_save_memory(agent_id, data)
        return
    if mode == "dual":
        _file_save_memory(agent_id, data)
        db.mirror(lambda: _repo_agents.save_memory(agent_id, data), "agents.save_memory")
        return
    _repo_agents.save_memory(agent_id, data)


def report(agent_id: str) -> str | None:
    """迭代报告 markdown。"""
    if db.is_primary(DOMAIN):
        try:
            return _repo_agents.report(agent_id)
        except FileNotFoundError as exc:
            raise AgentError(f"Agent 不存在：{agent_id}") from exc
    path = dir_path(agent_id) / "report.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def save_report(agent_id: str, text: str) -> None:
    mode = db.mode(DOMAIN)
    path = dir_path(agent_id) / "report.md"
    if mode in ("files", "dual"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _own(path)
    if mode == "db":
        _repo_agents.save_report(agent_id, text)
    elif mode == "dual":
        db.mirror(lambda: _repo_agents.save_report(agent_id, text), "agents.save_report")
