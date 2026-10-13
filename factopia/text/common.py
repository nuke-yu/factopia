"""文本数据集的公共定义：路径、数据集属性、术语表、文件读写。"""
import hashlib
import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
TEXT = RUNTIME / "text"
REVIEW = TEXT / "review"
PRICES = RUNTIME / "data" / "standardized"

SIMULATED_LABEL = "模拟数据，仅用于流程演示"

# series_by_subtype：与同类公告（年报比年报）的上一期比较；否则与同一公司同一语言的上一篇比较
# processing_delay_min：从正式发布到研究人员实际可获取（下载、抽取、翻译、理解）的保守延迟（Spec 第 11.5 节 ④）
DATASETS = {
    "fr": {"market": "fr", "calendar": "XPAR", "tz": "Europe/Paris", "simulated": False,
           "source": "AMF info-financière 开放数据", "licence": "Licence Ouverte 2.0（Etalab）", "processing_delay_min": 60,
           "series_by_subtype": True},
    "demo_hk": {"market": "hk", "calendar": "XHKG", "tz": "Asia/Hong_Kong", "simulated": True,
                "source": SIMULATED_LABEL, "licence": "内部生成", "processing_delay_min": 60,
                "series_by_subtype": False},
    "demo_uk": {"market": "uk", "calendar": "XLON", "tz": "Europe/London", "simulated": True,
                "source": SIMULATED_LABEL, "licence": "内部生成", "processing_delay_min": 60,
                "series_by_subtype": False},
}
LANG_ZH = {"fr": "法文", "en": "英文", "zh": "中文"}


def ds_dir(dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"未知数据集：{dataset}（可选 {', '.join(DATASETS)}）")
    return TEXT / dataset


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


ADDITIONS = REVIEW / "glossary_additions.json"   # 人工批准的新增术语（第 14.9 节 v1.30）；不改 configs 里的基础术语表


def write_shared(path: Path, data) -> None:
    """写入 review 目录下 api（非 root）与命令行（root）都要改写的文件。"""
    write_json(path, data)
    try:
        os.chmod(path, 0o666)
    except PermissionError:
        pass


@lru_cache(maxsize=1)
def glossary() -> dict:
    """基础术语表 + 人工批准的新增术语；有新增时版本号为 <基础版本>+a<条数>。"""
    g = yaml.safe_load((CONFIGS / "text" / "glossary.yaml").read_text(encoding="utf-8"))
    adds = read_json(ADDITIONS, []) if ADDITIONS.exists() else []
    for a in adds:
        terms = g["concepts"].get(a["concept"], {}).get("terms", {}).setdefault(a["lang"], [])
        if a["term"] not in terms:
            terms.append(a["term"])
    if adds:
        g["version"] = f"{g['version']}+a{len(adds)}"
    g["additions"] = adds
    return g


def pipeline() -> dict:
    """文本流程开关（configs/text/pipeline.yaml）；环境变量 FACTOPIA_TEXT_SCREENING 可临时覆盖预筛方式。
    按文件修改时间缓存：逐段调用（is_table 等）不会反复读文件，改了配置立即生效。"""
    p = CONFIGS / "text" / "pipeline.yaml"
    return _pipeline(p.stat().st_mtime_ns if p.exists() else 0, os.environ.get("FACTOPIA_TEXT_SCREENING", ""))


@lru_cache(maxsize=4)
def _pipeline(_mtime: int, _env: str) -> dict:
    p = CONFIGS / "text" / "pipeline.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg.setdefault("screening", "glossary")
    cfg["screening"] = os.environ.get("FACTOPIA_TEXT_SCREENING", cfg["screening"])
    if cfg["screening"] not in ("glossary", "full"):
        raise ValueError(f"screening 只能是 glossary 或 full：{cfg['screening']}")
    cfg.setdefault("full_min_chars", 40)
    r = cfg.setdefault("review", {})
    for k, v in (("sample_size", 100), ("sample_unscreened", 30), ("sample_seed", 20261013), ("high_impact_top", 50),
                 ("must_flags", ["low_translation_confidence"])):
        r.setdefault(k, v)
    cfg.setdefault("glossary_suggest", {}).setdefault("sample", 300)
    sf = cfg.setdefault("series_filter", {})
    for k, v in (("datasets", []), ("min_paragraphs", 10), ("notice_max_paragraphs", 20), ("notice_title_pattern", "")):
        sf.setdefault(k, v)
    tr = cfg.setdefault("table_rows", {})
    for k, v in (("enabled", False), ("min_tokens", 4), ("numeric_share", 0.4)):
        tr.setdefault(k, v)
    return cfg


_NUM = re.compile(r"^[(\-–−+]?[\d.,  %€$£]+[)%]?$|^[–—-]+$")


def is_table(text: str) -> bool:
    """以数字为主的段落（财务报表行）：数字类词占比 ≥ table_rows.numeric_share。"""
    tr = pipeline()["table_rows"]
    if not tr["enabled"]:
        return False
    toks = text.split()
    if len(toks) < tr["min_tokens"]:
        return False
    return sum(bool(_NUM.match(t)) for t in toks) / len(toks) >= tr["numeric_share"]


def series_exclusion(dataset: str, n_paragraphs: int, title: str) -> str | None:
    """通知类公告：返回排除原因；不排除时返回 None（configs/text/pipeline.yaml series_filter）。"""
    sf = pipeline()["series_filter"]
    if dataset not in sf["datasets"]:
        return None
    if n_paragraphs < sf["min_paragraphs"]:
        return f"段落过少（{n_paragraphs} 段 < {sf['min_paragraphs']}）：通知或 PDF 抽不出文字"
    if sf["notice_title_pattern"] and n_paragraphs < sf["notice_max_paragraphs"] and re.search(sf["notice_title_pattern"], title or "", re.I):
        return f"通知类公告（标题为报告发布 / 提供查阅通知，{n_paragraphs} 段）"
    return None


def fold(text: str) -> str:
    """小写并去掉重音、统一撇号，便于法文 / 英文词典匹配。"""
    text = unicodedata.normalize("NFKD", text.replace("’", "'")).encode("ascii", "ignore").decode() \
        if not re.search(r"[㐀-鿿]", text) else text.replace("’", "'")
    return text.casefold()


def _hits(text: str, terms: list[str]) -> list[str]:
    t = fold(text)
    out = []
    for term in terms:
        f = fold(term)
        if re.search(r"[㐀-鿿]", f):
            if f in t:
                out.append(term)
        elif f.endswith("*"):   # 词干匹配，如 deteriorat*
            if re.search(rf"(?<![a-z]){re.escape(f[:-1])}", t):
                out.append(term)
        elif re.search(rf"(?<![a-z]){re.escape(f)}(?![a-z])", t):
            out.append(term)
    return out


def concept_hits(text: str, lang: str) -> dict[str, list[str]]:
    """段落中各概念命中的术语（只查该语言的术语表）。"""
    g = glossary()["concepts"]
    return {c: h for c, spec in g.items() if (h := _hits(text, spec["terms"].get(lang, [])))}


def flag_hits(text: str, lang: str) -> dict[str, list[str]]:
    g = glossary()
    return {k: h for k in ("negation", "regulatory_wording", "key_financial_terms")
            if (h := _hits(text, g[k].get(lang, [])))}


def query_concepts(query: str) -> dict[str, list[str]]:
    """任意语言的提问 → 概念（三种语言的术语表都查，BR-12）。"""
    out: dict[str, list[str]] = {}
    for lang in ("zh", "en", "fr"):
        for c, h in concept_hits(query, lang).items():
            out.setdefault(c, []).extend(h)
    g = glossary()["concepts"]
    q = fold(query)
    for c, spec in g.items():   # 直接输入概念名也算
        if any(fold(spec[k]) in q for k in ("zh_name", "en_name", "fr_name")):
            out.setdefault(c, []).append(spec["zh_name"])
    return {c: list(dict.fromkeys(t)) for c, t in out.items()}


def concept_names() -> dict[str, str]:
    return {c: s["zh_name"] for c, s in glossary()["concepts"].items()}
