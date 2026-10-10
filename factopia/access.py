"""地区视角（业务需求 BR-45；Spec 第 14.20 节）——演示用，不是登录鉴权。

页面顶部选择地区角色（总部 / 香港团队 / 伦敦团队 / 巴黎团队），每个请求带 Cookie factopia_region（或请求头 X-Factopia-Region）；
后端中间件把角色放入上下文，各接口按 configs/access/regions.yaml 过滤：只返回本地区市场的数据与信号，
访问其他地区的内容返回 403，并写入访问留痕（runtime/access/events.jsonl，哈希链，与审批留痕相同的做法）。
请求不带角色时按总部处理（命令行与旧客户端不受影响）。正式部署须接入身份认证后按用户所属地区授权。
"""
import hashlib
import json
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
LOG = RUNTIME / "access" / "events.jsonl"
HEADER = "x-factopia-region"
COOKIE = "factopia_region"         # 页面切换角色时写入；普通链接（证据包、报告）也带上同一角色
_current: ContextVar[str | None] = ContextVar("factopia_region", default=None)


class Denied(Exception):
    pass


@lru_cache(maxsize=1)
def policy() -> dict:
    return yaml.safe_load((CONFIGS / "access" / "regions.yaml").read_text(encoding="utf-8"))


def set_role(code: str | None):
    p = policy()
    return _current.set(code if code in p["roles"] else None)


def reset(token) -> None:
    _current.reset(token)


def role() -> dict:
    p = policy()
    code = _current.get() or p["default"]
    return {"code": code, **p["roles"][code]}


def markets() -> list[str]:
    return list(role()["markets"])


def can(market: str | None) -> bool:
    return market is None or market in role()["markets"]


def cross_region() -> bool:
    return bool(role().get("cross_region"))


def events() -> list[dict]:
    return [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()] if LOG.exists() else []


def _log(ev: dict) -> None:
    try:
        prev = (events() or [{"hash": "0" * 64}])[-1]["hash"]
        ev = {**ev, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "prev": prev}
        ev["hash"] = hashlib.sha256(json.dumps({k: v for k, v in ev.items() if k != "hash"}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass                                        # 留痕失败不影响拒绝本身


def deny(what: str, market: str | None = None) -> None:
    r = role()
    _log({"role": r["code"], "role_name": r["name"], "denied": what, "market": market})
    raise Denied(f"当前地区角色「{r['name']}」无权查看：{what}（演示用地区视角，见 configs/access/regions.yaml）")


def require(market: str | None, what: str) -> None:
    if not can(market):
        deny(what, market)


def require_cross_region(what: str) -> None:
    if not cross_region():
        deny(f"{what}（跨地区内容只对总部开放）")


def info() -> dict:
    p = policy()
    return {"current": role(), "roles": [{"code": k, **v} for k, v in p["roles"].items()], "version": p["version"],
            "status": p["status"], "scope": p.get("scope") or [], "denied_recent": [e for e in events()[-20:]][::-1]}
