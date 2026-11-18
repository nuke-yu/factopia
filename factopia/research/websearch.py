"""联网搜索（可选）：研究立项页的外部文献 / 网页线索。

安全边界：
- 只采用测试期开始前、发表日期可靠的结果；日期未知默认排除，避免 2022 年后的信息泄漏。
- 网页内容是不可信输入：只取标题、摘要、URL，不执行网页指令，也不把网页内容当作平台事实。
- 支持博查（Bocha，国内）、Tavily（海外）与百炼内置搜索（bailian，仅用现有百炼 key）。
- 百炼内置搜索的 URL/日期由模型整理，不是搜索引擎的原始结构化结果，页面会提示人工核验。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_BEFORE = "2021-12-31"          # 测试期从 2022 年开始：默认只取此前发表的资料
CACHE_TTL_S = 3600
_CACHE: dict[tuple, tuple[float, dict]] = {}


class SearchError(RuntimeError):
    pass


def config() -> dict:
    provider = (os.environ.get("FACTOPIA_SEARCH_PROVIDER") or "bailian").strip().lower()
    before = (os.environ.get("FACTOPIA_SEARCH_BEFORE") or DEFAULT_BEFORE).strip()
    allow_unknown = (os.environ.get("FACTOPIA_SEARCH_ALLOW_UNKNOWN") or "false").strip().lower() in ("1", "true", "yes")
    try:
        count = max(1, min(20, int(os.environ.get("FACTOPIA_SEARCH_COUNT", "8"))))
    except ValueError:
        count = 8
    if provider == "bailian":
        # 当前环境只配置了百炼 key：用模型内置搜索，不额外申请博查 / Tavily
        key = (os.environ.get("FACTOPIA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
               or os.environ.get("FACTOPIA_SEARCH_API_KEY") or "").strip()
        base = (os.environ.get("FACTOPIA_LLM_BASE_URL") or os.environ.get("LITELLM_PROXY_API_BASE")
                or os.environ.get("OPENAI_API_BASE") or "").rstrip("/")
        model = (os.environ.get("FACTOPIA_SEARCH_MODEL") or os.environ.get("FACTOPIA_LLM_CRITIC_MODEL")
                 or "qwen-plus").strip()
    else:
        key = (os.environ.get("FACTOPIA_SEARCH_API_KEY") or "").strip()
        default_base = "https://api.bochaai.com/v1" if provider == "bocha" else "https://api.tavily.com"
        base = (os.environ.get("FACTOPIA_SEARCH_BASE_URL") or default_base).rstrip("/")
        model = None
    base = (os.environ.get("FACTOPIA_SEARCH_BASE_URL") or base).rstrip("/")
    return {"provider": provider, "api_key": key, "base_url": base, "model": model, "before": before,
            "count": count, "allow_unknown": allow_unknown}


def configured() -> bool:
    return bool(config()["api_key"])


def search(query: str, before: str | None = None, count: int | None = None) -> dict:
    cfg = config()
    q = (query or "").strip()[:300]
    cutoff = (before if before is not None else cfg["before"]).strip()
    n = max(1, min(20, int(count or cfg["count"])))
    if not q:
        return _response(cfg, q, cutoff, [], "请输入检索关键词", configured=bool(cfg["api_key"]))
    if not cfg["api_key"]:
        hint = ("未配置百炼内置搜索：检查 FACTOPIA_LLM_API_KEY" if cfg["provider"] == "bailian"
                else "未配置联网搜索：在 .env 中设置 FACTOPIA_SEARCH_API_KEY（博查或 Tavily）")
        return _response(cfg, q, cutoff, [], hint, configured=False)
    cache_key = (cfg["provider"], q, cutoff, n, cfg["allow_unknown"])
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        if cfg["provider"] == "bailian":
            rows = _bailian(cfg, q, cutoff, n)
        elif cfg["provider"] == "tavily":
            rows = _tavily(cfg, q, cutoff, n)
        elif cfg["provider"] == "bocha":
            rows = _bocha(cfg, q, cutoff, n)
        else:
            raise SearchError(f"未知搜索 provider：{cfg['provider']}（可用 bailian / bocha / tavily）")
        kept, excluded = _filter_by_date(rows, cutoff, cfg["allow_unknown"])
        out = _response(cfg, q, cutoff, kept[:n], None, configured=True, raw_count=len(rows), excluded=excluded)
    except Exception as e:                                   # 配置错误 / 网络 / 余额：返回给页面提示
        out = _response(cfg, q, cutoff, [], str(e)[:500], configured=True)
    _CACHE[cache_key] = (time.time(), out)
    return out


def _response(cfg: dict, query: str, before: str, results: list[dict], error: str | None,
              configured: bool, raw_count: int = 0, excluded: int = 0) -> dict:
    warning = ("百炼内置搜索由模型整理，URL/日期可能不准确，请人工核验"
               if cfg["provider"] == "bailian" else None)
    return {"configured": configured, "provider": cfg["provider"], "model": cfg.get("model"),
            "query": query, "before": before, "results": results, "error": error,
            "warning": warning, "raw_count": raw_count, "excluded": excluded}


def _filter_by_date(results: list[dict], before: str, allow_unknown: bool = False) -> tuple[list[dict], int]:
    if not before:
        return results, 0
    kept, excluded = [], 0
    for r in results:
        published = r.get("published")
        if published and published <= before:
            kept.append(r)
        elif not published and allow_unknown:
            kept.append(r)
        else:
            excluded += 1
    return kept, excluded


def _date(value) -> str | None:
    s = str(value or "").strip()
    return s[:10] if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-" else None


def _bailian(cfg: dict, query: str, before: str, count: int) -> list[dict]:
    """百炼内置搜索：用聊天模型 + enable_search，要求它返回结构化来源。

    接口不返回搜索引擎原始结构化结果，URL/日期由模型整理；页面会提示人工核验。
    """
    date_rule = (f"Only include sources published on or before {before}. " if before
                 else "Include recent sources; there is no date restriction. ")
    date_rule2 = f"Do not include sources published after {before}; " if before else ""
    prompt = (
        "Use web search to find public sources about: " + query + "\n"
        + date_rule
        + "Return JSON only: {\"results\":[{\"title\":\"source title\",\"url\":\"https://... or empty\","
          "\"published\":\"YYYY-MM-DD or empty\",\"snippet\":\"one-sentence finding\"}]}. "
        f"At most {count} results. " + date_rule2
        + "if the date is unknown use an empty string. Only use URLs returned by search; never invent a URL."
    )
    body = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
            "enable_search": True, "response_format": {"type": "json_object"},
            "temperature": 0.2, "max_tokens": max(800, count * 180)}
    data = _post_json(cfg, "/chat/completions", body, timeout=150)
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as e:
        raise SearchError(f"百炼搜索输出不是合法 JSON：{content[:300]}") from e
    return _normalize_bailian(payload.get("results") or [])


def _normalize_bailian(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        title = str(r.get("title") or "").strip()
        url = str(r.get("url") or "").strip()
        if not title:
            continue
        out.append({"title": title[:300], "url": url[:1000],
                    "snippet": str(r.get("snippet") or r.get("summary") or "")[:1200],
                    "published": _date(r.get("published") or r.get("date")),
                    "source": (urlparse(url).netloc[:200] if url else "百炼内置搜索"),
                    "score": None})
    return out


def _bocha(cfg: dict, query: str, before: str, count: int) -> list[dict]:
    body = {"query": query, "count": count, "summary": False,
            "freshness": f"1900-01-01..{before}" if before else "noLimit"}
    data = _post_json(cfg, "/web-search", body)
    if data.get("code") not in (None, 200):
        raise SearchError(f"博查返回 {data.get('code')}：{data.get('msg') or ''}")
    pages = (((data.get("data") or {}).get("webPages")) or {}).get("value") or []
    return _normalize_bocha(pages)


def _normalize_bocha(pages: list[dict]) -> list[dict]:
    out = []
    for p in pages:
        title, url = str(p.get("name") or "").strip(), str(p.get("url") or "").strip()
        if not title or not url:
            continue
        out.append({"title": title[:300], "url": url[:1000],
                    "snippet": str(p.get("snippet") or p.get("summary") or "")[:1200],
                    "published": _date(p.get("datePublished")),
                    "source": str(p.get("siteName") or "").strip()[:200] or None,
                    "score": None})
    return out


def _tavily(cfg: dict, query: str, before: str, count: int) -> list[dict]:
    body = {"query": query, "max_results": count, "search_depth": "basic",
            "include_answer": False, "include_raw_content": False}
    if before:
        body["end_date"] = before
    data = _post_json(cfg, "/search", body)
    return _normalize_tavily(data.get("results") or [])


def _normalize_tavily(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        title, url = str(r.get("title") or "").strip(), str(r.get("url") or "").strip()
        if not title or not url:
            continue
        out.append({"title": title[:300], "url": url[:1000],
                    "snippet": str(r.get("content") or "")[:1200],
                    "published": _date(r.get("published_date")),
                    "source": urlparse(url).netloc[:200] or None,
                    "score": r.get("score")})
    return out


def _post_json(cfg: dict, path: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(cfg["base_url"] + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SearchError(f"搜索接口返回 {e.code}：{e.read()[:300].decode(errors='replace')}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SearchError(f"无法连接搜索接口：{e}") from e
