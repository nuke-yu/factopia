"""研究线索池（首页的信息输入入口；Spec 第 17 章，EXT-01）。

所有渠道进来的信息统一为"线索卡片"，研究员挑选后转为研究立项，之后完全走现有的治理流程。

- 人工渠道：投资假设、论文 / 研报（上传文件或粘贴摘要）、研究员经验、人工反馈；
- 自动回流：监控预警（加强观察 / 建议暂停）、组合归因建议（进一步研究 / 重新校准 / 限定地区 / 停用）、
  审批退回 / 拒绝；打开线索池时同步，按来源去重。
- 提交检查：敏感信息（格式明确的个人信息，命中即拒绝）、论文来源与许可必填、相似研究与线索提示、地区可见范围。
- 论文抽取：pypdf 抽取段落 → 大模型抽取候选因子（每个须引用段落，标注数据可得性）→ 防泄漏提示（样本期 / 发表日期
  与测试期重叠或未知）。
- 留痕：每条线索 events.jsonl 只追加、哈希链。

存储：runtime/ideas/<id>/（idea.json、events.jsonl、files/），自动回流去重索引 runtime/ideas/sources.json。
"""
import base64
import hashlib
import json
import os
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
DIR = RUNTIME / "ideas"
PROMPT_VERSION = "idea-paper-v1"
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
MARKETS = ("hk", "uk", "fr")
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}
TEST_START = "2022-01-01"            # 测试期开始（与 RD-Agent 数据切分一致）
MAX_BYTES = 20 * 1024 * 1024
MAX_PROMPT_CHARS = 24000

CHANNELS = {
    "hypothesis": {"zh": "投资假设", "manual": True},
    "paper": {"zh": "论文 / 研报", "manual": True},
    "experience": {"zh": "研究员经验", "manual": True},
    "feedback": {"zh": "人工反馈", "manual": True},
    "monitor": {"zh": "监控预警", "manual": False},
    "portfolio": {"zh": "组合归因建议", "manual": False},
    "approval": {"zh": "审批退回 / 拒绝", "manual": False},
}
STATUS_ZH = {"new": "待处理", "converted": "已转立项", "duplicate": "重复", "dismissed": "不跟进"}
ACTION_ZH = {"create": "创建", "extract": "抽取候选因子", "convert": "转为研究立项", "duplicate": "标为重复",
             "dismiss": "不跟进", "comment": "意见", "reopen": "重新打开"}
LICENCES = {"internal": "团队内部资料", "open": "公开许可", "purchased": "已购买授权"}
FEEDBACK_ROLES = ("投资组合经理", "客户团队", "本地研究员（香港）", "本地研究员（英国）", "本地研究员（法国）", "合规", "模型风险", "其他")
# 研究规格的数据类别（factopia/research/draft.py DATA）中平台已有的：可做；其余需数据商
DATA_OK = {"price_volume": "日线行情与成交量", "benchmark": "基准指数", "fx": "汇率", "text": "公告文本（情报台）"}
DATA_NO = {"fundamentals": "财务与基本面数据（未接入，需数据商）", "alternative": "另类数据（未接入）", "other": "其他数据（待确认）"}
SYSTEM_BY = "平台（自动）"

# 敏感信息：只检查格式明确的个人信息（业务 spec：全程不处理真实客户身份信息）
PII = {
    "电子邮件": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # 电话：带国家代码，或香港 8 位手机（连写）、英国 07 手机、法国 0X XX XX XX XX；不匹配表格数字与年份
    "电话号码": re.compile(r"\+(?:852|44|33|86)[\s-]?\(?\d[\d\s-]{6,13}\d|(?<![\d.,])[5-9]\d{7}(?![\d.,%])"
                       r"|(?<![\d.,])07\d{3}\s?\d{6}(?![\d.,])|(?<![\d.,])0[1-9](?:[\s.]\d{2}){4}(?![\d.,])"),
    "香港身份证号": re.compile(r"\b[A-Z]{1,2}\d{6}\([0-9A]\)"),          # 须带括号校验位，避免误判 ISIN 等代码
    "英国国民保险号": re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
    "法国社会保障号": re.compile(r"\b[12]\s?\d{2}\s?(?:0[1-9]|1[0-2])\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?:\s[A-Z0-9]{4}){3,7}(?:\s[A-Z0-9]{1,3})?)\b"),   # 不匹配 12 位 ISIN
    "银行卡号": re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b"),
}


class IdeaError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _dump(x) -> bytes:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _dir(iid: str) -> Path:
    if not re.fullmatch(r"idea_\d{14}_[0-9a-f]{4}", iid or ""):
        raise IdeaError("线索编号无效")
    return DIR / iid


def language(text: str) -> str:
    t = text or ""
    if len(re.findall(r"[一-鿿]", t)) >= 5:
        return "zh"
    fr = len(re.findall(r"[éèêàçùâîôû]|\b(?:le|la|les|des|une|est|pour|dans|avec|sur)\b", t.lower()))
    return "fr" if fr >= 3 else "en"


def pii_hits(*texts: str) -> list[dict]:
    """返回命中的敏感信息类型与位置（前后各 12 个字符，命中部分打码）。"""
    out = []
    for field, text in texts:
        for kind, rx in PII.items():
            for m in rx.finditer(text or ""):
                s = m.group(0)
                ctx = (text[max(0, m.start() - 12):m.start()] + "【" + s[:2] + "…】" + text[m.end():m.end() + 12]).replace("\n", " ")
                out.append({"type": kind, "field": field, "context": ctx})
                if len(out) >= 20:
                    return out
    return out


# ---------------------------------------------------------------- 存储与留痕

def load(iid: str) -> dict:
    p = _dir(iid) / "idea.json"
    if not p.exists():
        raise IdeaError("线索不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(idea: dict) -> None:
    d = _dir(idea["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "idea.json.tmp"
    tmp.write_text(json.dumps(idea, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "idea.json")


def events(iid: str) -> list[dict]:
    p = _dir(iid) / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _append(iid: str, ev: dict) -> dict:
    prev = (events(iid) or [{"hash": "0" * 64}])[-1]["hash"]
    ev = {**ev, "at": now(), "prev": prev}
    ev["hash"] = _sha(_dump({k: v for k, v in ev.items() if k != "hash"}))
    with (_dir(iid) / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def verify_chain(iid: str) -> dict:
    prev = "0" * 64
    evs = events(iid)
    for i, ev in enumerate(evs):
        if ev["prev"] != prev or _sha(_dump({k: v for k, v in ev.items() if k != "hash"})) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(evs)}


def all_ideas() -> list[dict]:
    if not DIR.exists():
        return []
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in DIR.glob("idea_*/idea.json")),
                  key=lambda x: x["created_at"], reverse=True)


def visible(idea: dict) -> bool:
    from factopia import access
    return access.cross_region() or not idea.get("markets") or any(access.can(m) for m in idea["markets"])


# ---------------------------------------------------------------- 相似研究与线索

def similar(idea: dict, others: list[dict] | None = None) -> dict:
    from factopia.research import semantic
    q = f"{idea['title']} {idea['body'][:600]}"
    try:
        res = semantic.search(q, ["research", "factor"], limit=12, use_llm=False)["results"]
    except Exception:                                   # 检索失败不影响提交
        res = []
    known = [{"kind": r["kind"], "kind_zh": r["kind_zh"], "title": r["title"], "ref": r["ref"], "score": r["score"]}
             for r in res if r["score"] > 0][:3]
    mine = Counter(semantic.tokens(q))
    scored = []
    for o in others if others is not None else all_ideas():
        if o["id"] == idea.get("id"):
            continue
        theirs = Counter(semantic.tokens(f"{o['title']} {o['body'][:600]}"))
        inter = sum((mine & theirs).values())
        union = sum((mine | theirs).values()) or 1
        if inter / union >= 0.2:
            scored.append({"id": o["id"], "title": o["title"], "channel_zh": o["channel_zh"], "overlap": round(inter / union, 2)})
    return {"research": known, "ideas": sorted(scored, key=lambda x: -x["overlap"])[:3],
            "note": "只作提示，不阻止提交；相似度为检索得分 / 词语重合度"}


# ---------------------------------------------------------------- 提交

def _new_id() -> str:
    return f"idea_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(2)}"


def _clean_markets(ms) -> list[str]:
    return [m for m in MARKETS if m in (ms or [])]


def _paragraphs(text: str) -> list[str]:
    parts = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "")]
    out = []
    for p in parts:
        while len(p) > 1500:                            # 超长段落按句号切开，便于引用
            cut = max(p.rfind(". ", 0, 1500), p.rfind("。", 0, 1500))
            cut = cut + 1 if cut > 200 else 1500
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(p) >= 20:
            out.append(p)
    return out


def _extract_text(name: str, data: bytes) -> tuple[str, int | None]:
    low = name.lower()
    if low.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(pg.extract_text() or "") for pg in reader.pages]
        except Exception as e:
            raise IdeaError(f"PDF 无法读取：{e}") from e
        return "\n\n".join(pages), len(pages)
    if low.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace"), None
    raise IdeaError("只支持 PDF、TXT、Markdown 文件")


def create(body: dict) -> dict:
    """body：channel、title、body、markets、submitted_by、role、confirm_no_client_data，以及渠道特有字段；
    论文渠道可带 document={name, content_base64, source, licence, licence_note, published, sample_start, sample_end}。"""
    ch = body.get("channel")
    if ch not in CHANNELS or not CHANNELS[ch]["manual"]:
        raise IdeaError(f"渠道无效：{ch}（可提交：{', '.join(k for k, v in CHANNELS.items() if v['manual'])}）")
    title = str(body.get("title") or "").strip()[:200]
    text = str(body.get("body") or "").strip()[:20000]
    by = str(body.get("submitted_by") or "").strip()[:60]
    if not title:
        raise IdeaError("请填写标题")
    if not by:
        raise IdeaError("请填写提交人")
    if not body.get("confirm_no_client_data"):
        raise IdeaError("请确认内容不含真实客户身份信息")
    doc_in = body.get("document") or {}
    if ch == "paper":
        if not doc_in.get("source") or doc_in.get("licence") not in LICENCES:
            raise IdeaError("论文 / 研报须填写来源与许可类型（团队内部资料 / 公开许可 / 已购买授权）")
        if doc_in.get("licence") in ("open", "purchased") and not str(doc_in.get("licence_note") or "").strip():
            raise IdeaError("公开许可须注明许可名称；已购买授权须注明合同或订阅")
        if not doc_in.get("content_base64") and len(text) < 50:
            raise IdeaError("请上传文件，或粘贴不少于 50 字的摘要 / 正文")
    elif len(text) < 10:
        raise IdeaError("内容太短（至少 10 个字）")
    raw, doc_text, pages = None, "", None
    if doc_in.get("content_base64"):
        try:
            raw = base64.b64decode(doc_in["content_base64"], validate=True)
        except Exception as e:
            raise IdeaError("文件内容无效") from e
        if len(raw) > MAX_BYTES:
            raise IdeaError("文件超过 20 MB")
        doc_text, pages = _extract_text(str(doc_in.get("name") or "document"), raw)
    hits = pii_hits(("标题", title), ("内容", text), ("文档", doc_text))
    if hits:
        raise IdeaError("内容中疑似包含个人身份信息，请删除后重新提交：" + "；".join(f"{h['type']}（{h['field']}：{h['context']}）" for h in hits[:5]))
    iid = _new_id()
    idea = {"id": iid, "channel": ch, "channel_zh": CHANNELS[ch]["zh"], "title": title, "body": text, "language": language(f"{title} {text} {doc_text[:2000]}"),
            "markets": _clean_markets(body.get("markets")), "submitted_by": by, "role": str(body.get("role") or "").strip()[:60] or None,
            "source_ref": None, "created_at": now(), "status": "new", "links": {}, "pii_checked": True,
            "fields": {k: str(body.get(k) or "").strip()[:500] for k in ("direction", "period", "feedback_role") if body.get(k)}}
    if ch == "feedback" and idea["fields"].get("feedback_role") not in FEEDBACK_ROLES + (None,):
        idea["fields"]["feedback_role"] = "其他"
    if ch == "paper":
        name = re.sub(r"[^\w.\-]+", "_", str(doc_in.get("name") or "pasted.txt"))[:120]
        paras = _paragraphs(doc_text) if raw is not None else _paragraphs(text)
        idea["document"] = {"name": name if raw is not None else None, "sha256": _sha(raw) if raw is not None else None,
                            "bytes": len(raw) if raw is not None else None, "pages": pages, "paragraphs": len(paras),
                            "source": str(doc_in["source"])[:200], "licence": doc_in["licence"], "licence_zh": LICENCES[doc_in["licence"]],
                            "licence_note": str(doc_in.get("licence_note") or "")[:300],
                            "published": str(doc_in.get("published") or "")[:10] or None,
                            "sample_start": str(doc_in.get("sample_start") or "")[:10] or None,
                            "sample_end": str(doc_in.get("sample_end") or "")[:10] or None}
        d = _dir(iid) / "files"
        d.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (d / name).write_bytes(raw)
        (d / "paragraphs.json").write_text(json.dumps(paras, ensure_ascii=False, indent=0), encoding="utf-8")
        idea["leakage"] = leakage(idea["document"])
    idea["similar"] = similar(idea)
    _save(idea)
    _append(iid, {"action": "create", "by": by, "channel": ch, "sha256": idea.get("document", {}).get("sha256") if ch == "paper" else None})
    return idea


def paragraphs(iid: str) -> list[str]:
    p = _dir(iid) / "files" / "paragraphs.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


# ---------------------------------------------------------------- 论文抽取

def leakage(doc: dict) -> dict:
    """论文的因子通常是作者已在历史数据上验证有效才发表：样本期或发表时间晚于测试期开始（或未知）即提示。"""
    end, pub = doc.get("sample_end"), doc.get("published")
    if not end and not pub:
        return {"overlap": True, "known": False,
                "note": "样本期与发表日期均未知，按重叠处理：测试期（2022 年起）的结果可能被高估"}
    over = bool((end and end >= TEST_START) or (pub and pub >= TEST_START))
    return {"overlap": over, "known": True,
            "note": (f"样本期结束 {end or '未知'}、发表 {pub or '未知'}，晚于测试期开始（{TEST_START}）："
                     "作者可能已在与测试期重叠的数据上验证过，测试期结果可能被高估" if over
                     else f"样本期结束 {end or '未知'}、发表 {pub or '未知'}，均早于测试期开始（{TEST_START}）")}


def _availability(needs: list[str]) -> dict:
    missing = [n for n in needs if n not in DATA_OK]
    return {"ok": not missing and bool(needs), "missing": [DATA_NO.get(n, n) for n in missing],
            "needs_zh": [DATA_OK.get(n) or DATA_NO.get(n, n) for n in needs]}


def extract(iid: str, by: str = SYSTEM_BY) -> dict:
    idea = load(iid)
    if idea["channel"] != "paper":
        raise IdeaError("只有论文 / 研报渠道需要抽取")
    paras = paragraphs(iid)
    if not paras:
        raise IdeaError("没有可抽取的段落（PDF 可能是扫描件）")
    from factopia.research import draft as drafting
    numbered, used = [], 0
    for i, p in enumerate(paras):
        line = f"[P{i + 1}] {p}"
        if used + len(line) > MAX_PROMPT_CHARS:
            break
        numbered.append(line)
        used += len(line)
    prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
        title=idea["title"], note=idea["body"][:1500] or "(none)", paragraphs="\n".join(numbered),
        truncated="yes" if len(numbered) < len(paras) else "no")
    cfg = drafting.llm_config()
    try:
        text, usage = drafting._chat(prompt, cfg)
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise IdeaError(f"大模型输出不是合法 JSON：{e}") from e
    except drafting.DraftError as e:
        raise IdeaError(str(e)) from e
    n = len(numbered)
    cands, dropped = [], 0
    for c in raw.get("factors") or []:
        cites = sorted({int(x) for x in re.findall(r"\d+", " ".join(str(v) for v in c.get("cites") or [])) if 1 <= int(x) <= n})
        if not cites or not str(c.get("name") or "").strip():
            dropped += 1
            continue
        needs = [x for x in (c.get("data_needs") or []) if x in DATA_OK or x in DATA_NO] or ["other"]
        cands.append({"key": f"f{len(cands) + 1}", "name": str(c["name"])[:120], "description_zh": str(c.get("description_zh") or "")[:600],
                      "formula": str(c.get("formula") or "")[:600], "logic_zh": str(c.get("logic_zh") or "")[:600],
                      "data_needs": needs, "availability": _availability(needs), "cites": cites,
                      "quotes": [{"p": k, "text": paras[k - 1][:400]} for k in cites[:3]]})
    doc = idea["document"]
    for k in ("published", "sample_start", "sample_end"):           # 人工填写优先
        v = str(raw.get(k) or "")[:10]
        if not doc.get(k) and re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v):
            doc[k] = v
            doc.setdefault("ai_filled", []).append(k)
    idea["document"] = doc
    idea["leakage"] = leakage(doc)
    idea["extraction"] = {"factors": cands, "dropped_no_cite": dropped, "paragraphs_used": n, "paragraphs_total": len(paras),
                          "summary_zh": str(raw.get("summary_zh") or "")[:800], "model": cfg["model"], "prompt_version": PROMPT_VERSION,
                          "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")}, "at": now()}
    _save(idea)
    _append(iid, {"action": "extract", "by": by, "factors": len(cands), "dropped": dropped, "model": cfg["model"], "prompt_version": PROMPT_VERSION})
    return idea


# ---------------------------------------------------------------- 处理与转立项

def act(iid: str, action: str, by: str, note: str = "", target: str | None = None) -> dict:
    idea = load(iid)
    by = (by or "").strip()[:60]
    if not by:
        raise IdeaError("请填写处理人")
    if action == "comment":
        if not note.strip():
            raise IdeaError("意见不能为空")
    elif action == "dismiss":
        if not note.strip():
            raise IdeaError("不跟进须写明原因")
        idea["status"] = "dismissed"
    elif action == "duplicate":
        if not target:
            raise IdeaError("标为重复须选择指向的线索或研究")
        idea["status"], idea["links"]["duplicate_of"] = "duplicate", str(target)[:120]
    elif action == "reopen":
        if idea["status"] == "converted":
            raise IdeaError("已转立项的线索不能重新打开")
        idea["status"] = "new"
    else:
        raise IdeaError(f"操作无效：{action}")
    _save(idea)
    _append(iid, {"action": action, "by": by, "note": note[:1000], "target": target})
    return idea


def prefill(iid: str, factor_keys: list[str] | None = None) -> dict:
    """生成预填到研究立项页的假设文字；锁定研究版本时才正式标记为已转立项（mark_converted）。"""
    idea = load(iid)
    if idea["status"] in ("duplicate", "dismissed"):
        raise IdeaError(f"该线索状态为「{STATUS_ZH[idea['status']]}」，请先重新打开")
    direction = idea.get("fields", {}).get("direction", "")
    if idea["channel"] == "paper":
        fs = [f for f in (idea.get("extraction") or {}).get("factors") or [] if not factor_keys or f["key"] in factor_keys]
        if not fs:
            raise IdeaError("请先抽取并勾选候选因子")
        bad = [f["name"] for f in fs if not f["availability"]["ok"]]
        if bad:
            raise IdeaError("所选因子需要平台没有的数据：" + "、".join(bad))
        doc = idea["document"]
        hyp = (f"依据《{idea['title']}》（来源：{doc['source']}；{doc['licence_zh']}）中的候选因子，检验其在所选市场是否预测未来收益：\n"
               + "\n".join(f"- {f['name']}：{f['description_zh']}（计算：{f['formula']}；经济逻辑：{f['logic_zh']}；原文段落 P{'、P'.join(map(str, f['cites']))}）" for f in fs))
        direction = direction or "；".join(f["name"] for f in fs)
    else:
        hyp = idea["body"]
    return {"idea_id": iid, "hypothesis": hyp[:3900], "direction": direction[:900], "markets": idea["markets"] or list(MARKETS),
            "questions": confirm_questions(iid, idea), "channel_zh": idea["channel_zh"], "title": idea["title"]}


def confirm_questions(iid: str, idea: dict | None = None) -> list[str]:
    """转立项时研究规格须确认的事项（第 17.5、17.6 节）：防泄漏提示与文档来源许可；与勾选哪些因子无关。"""
    try:
        idea = idea or load(iid)
    except IdeaError:
        return []
    out = []
    if (idea.get("leakage") or {}).get("overlap"):
        out.append(f"线索 {iid} 来自论文：{idea['leakage']['note']}。确认已知悉该风险")
    if idea["channel"] == "paper":
        doc = idea["document"]
        out.append(f"线索 {iid} 的文档来源为 {doc['source']}（{doc['licence_zh']}{' ' + doc['licence_note'] if doc.get('licence_note') else ''}），确认可用于研究")
    return out


def mark_converted(iid: str, research_id: str, version: int, by: str) -> None:
    try:
        idea = load(iid)
    except IdeaError:
        return
    idea["status"] = "converted"
    idea["links"].setdefault("research", [])
    ref = f"{research_id}.v{version}"
    if ref not in idea["links"]["research"]:
        idea["links"]["research"].append(ref)
    _save(idea)
    _append(iid, {"action": "convert", "by": by, "research": ref})


def lineage(iid: str) -> dict | None:
    try:
        i = load(iid)
    except IdeaError:
        return None
    return {"id": i["id"], "channel_zh": i["channel_zh"], "title": i["title"], "submitted_by": i["submitted_by"], "created_at": i["created_at"],
            "document": {k: (i.get("document") or {}).get(k) for k in ("name", "sha256", "source", "licence_zh", "licence_note")} if i.get("document") else None,
            "leakage": i.get("leakage")}


# ---------------------------------------------------------------- 自动回流

def _sources() -> dict:
    p = DIR / "sources.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _auto(key: str, idx: dict, channel: str, title: str, body: str, markets: list[str], ref: dict) -> None:
    if key in idx:
        return
    iid = _new_id()
    while (DIR / iid).exists():
        iid = _new_id()
    idea = {"id": iid, "channel": channel, "channel_zh": CHANNELS[channel]["zh"], "title": title[:200], "body": body[:4000],
            "language": "zh", "markets": _clean_markets(markets), "submitted_by": SYSTEM_BY, "role": None, "source_ref": ref,
            "created_at": now(), "status": "new", "links": {}, "pii_checked": True, "fields": {}}
    idea["similar"] = {"research": [], "ideas": [], "note": "自动线索来自平台内部记录"}
    _save(idea)
    _append(iid, {"action": "create", "by": SYSTEM_BY, "channel": channel, "source": key})
    idx[key] = iid


def sync() -> int:
    """把平台内部的监控预警、组合归因建议、审批退回 / 拒绝同步为线索（按来源去重）。返回新增数量。"""
    DIR.mkdir(parents=True, exist_ok=True)
    idx = _sources()
    before = len(idx)
    try:
        from factopia import approval, signals
        for e in signals.catalog():
            mon = signals.monitor_result(e["signal_id"])
            if mon and mon.get("suggestion") in ("watch", "pause"):
                al = "；".join(a["title"] for a in mon.get("alerts") or [])
                _auto(f"monitor:{e['signal_id']}:{mon['computed_at']}", idx, "monitor",
                      f"{e['market_zh']}信号{ {'watch': '加强观察', 'pause': '建议暂停'}[mon['suggestion']] }：{e['title']}",
                      f"信号 {e['signal_id']}（{e['state_zh']}）上线后监控建议：{mon['suggestion_zh']}。预警：{al or '—'}。\n诊断："
                      + "；".join(mon.get("diagnosis") or []), [e["market"]], {"signal_id": e["signal_id"], "computed_at": mon["computed_at"]})
        for s in approval.list_all():
            for ev in approval.events(s["id"]):
                if ev["action"] in ("return", "reject"):
                    _auto(f"approval:{ev['hash']}", idx, "approval",
                          f"审批{approval.ACTION_ZH[ev['action']]}（{ev['role']}）：{s['title']}",
                          f"提交 {s['id']} 被「{ev['role']}」{approval.ACTION_ZH[ev['action']]}。原因：{ev.get('note') or '—'}",
                          s["markets"], {"submission": s["id"], "event": ev["hash"]})
    except Exception:                                   # 审批 / 信号模块不可用时跳过
        pass
    try:
        from factopia import portfolio as P
        from factopia.research import store
        for rid in store.research_ids():
            for v in store.versions(rid):
                summ = P.load(rid, v)
                for fb in (summ or {}).get("feedback") or []:
                    if fb["action"] in ("research", "recalibrate", "restrict", "retire"):
                        _auto(f"portfolio:{rid}.v{v}:{fb['market']}:{fb['action']}", idx, "portfolio",
                              f"{MARKET_ZH[fb['market']]}组合建议{fb['action_zh']}：{rid} v{v}",
                              f"研究 {rid} v{v} 的授权组合归因建议「{fb['action_zh']}」（{MARKET_ZH[fb['market']]}）。原因："
                              + "；".join(fb.get("reasons") or []), [fb["market"]], {"research": f"{rid}.v{v}", "market": fb["market"], "action": fb["action"]})
    except Exception:
        pass
    if len(idx) != before:
        tmp = DIR / "sources.json.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(DIR / "sources.json")
    return len(idx) - before


def listing(channel: str | None = None, status: str | None = None, market: str | None = None) -> dict:
    added = sync()
    items = [i for i in all_ideas() if visible(i)]
    counts = {"channel": Counter(i["channel"] for i in items), "status": Counter(i["status"] for i in items)}
    out = [i for i in items if (not channel or i["channel"] == channel) and (not status or i["status"] == status)
           and (not market or market in (i["markets"] or [market]))]
    return {"items": [{k: v for k, v in i.items() if k not in ("body",)} | {"excerpt": i["body"][:200]} for i in out],
            "counts": {"channel": dict(counts["channel"]), "status": dict(counts["status"])}, "synced": added,
            "channels": {k: v["zh"] for k, v in CHANNELS.items()}, "manual": [k for k, v in CHANNELS.items() if v["manual"]],
            "status_zh": STATUS_ZH, "licences": LICENCES, "feedback_roles": list(FEEDBACK_ROLES)}


def detail(iid: str) -> dict:
    idea = load(iid)
    if not visible(idea):
        from factopia import access
        access.deny(f"研究线索 {iid}", (idea.get("markets") or [None])[0])
    return {**idea, "events": events(iid), "chain": verify_chain(iid), "action_zh": ACTION_ZH, "status_zh": STATUS_ZH,
            "paragraph_count": len(paragraphs(iid))}
