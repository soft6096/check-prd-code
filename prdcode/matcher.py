"""抽标识符 → 静态判定 → 留搜查记录。

分工的分界线在这里定死：

  能用「搜得到 / 搜不到」回答的   → 这里直接判，不花 AI
  必须读懂意思才能判的            → 交给 AI
  谁都不能替你决定的              → 交给人（需求矛盾、业务取舍）

静态判定有一条铁律：**每个「搜不到」的结论，必须留下搜查记录。**
记录里写清搜了哪些范围、用了哪些关键词、最接近的是什么。
这样人才能发现"是它搜漏了"，而不是只能在"信"和"不信"之间选一个。
这比让 AI 再判一遍更靠谱，而且一分钱不花。
"""

from __future__ import annotations

import re

from .codeindex import CodeIndex
from .utils import find_error_codes

# ---------------------------------------------------------------- 抽取规则

# 反引号里的东西最可信，优先从这里抽
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# 接口路径：至少两段，避免把普通斜杠算进来。
# 段首允许 `{`，因为路径占位符长这样：/refund/detail/{refundId}
_PATH_RE = re.compile(
    r"(?<![\w/])/(?:[A-Za-z_{][\w\-{}.]*)(?:/[A-Za-z_{][\w\-{}.]*)+"
)
# 类名：大写开头的驼峰，且带常见后缀
_TYPE_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*"
    r"(?:Service|ServiceImpl|Controller|API|Mapper|Repository|Client|"
    r"DTO|VO|Entity|Model|Enum|Config|Util|Utils|Tools|Handler|Listener|Job|Task))"
    r"\b"
)
# 方法调用：foo() 或 foo(dto)。带参调用也要能抽出来，否则"方法 X 存在"
# 这类判定会大量漏召回；控制关键字（if/for/while...）用 _METHOD_CALL_STOP 排掉。
_METHOD_CALL_RE = re.compile(r"\b([a-z][A-Za-z0-9_]*)\s*\(")
_METHOD_CALL_STOP = {
    "if", "for", "while", "switch", "catch", "return", "new", "super",
    "this", "assert", "do", "try", "synchronized", "else", "throw",
}
_METHOD_DOT_RE = re.compile(r"\b([A-Z]\w*)\.([a-z]\w*)")
# 错误码正则与索引端共用同一条（见 utils.error_code_regex），
# 两边不一致会让需求里的错误码永远搜不到，被误判成"没做"。
# 枚举值：含下划线的大写常量（避免把普通大写词当枚举）
_ENUM_RE = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")
# 驼峰字段名
_CAMEL_RE = re.compile(r"\b([a-z][a-z0-9]*[A-Z][A-Za-z0-9]*)\b")
# HTTP 方法词
_HTTP_WORDS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# 抽字段名时要排掉的常见词（它们长得像驼峰但不是字段）
_CAMEL_STOPWORDS = {
    "jsonObject", "getUserId", "setUserId", "toString", "equals", "hashCode",
    "rollbackFor", "required", "description", "summary", "timezone",
}


def extract_identifiers(text: str) -> dict:
    """从一段文字里抽出所有"能拿去代码里搜"的标识符。"""
    if not text:
        return _empty_ids()

    ticked = _BACKTICK_RE.findall(text)
    ticked_text = " ".join(ticked)
    # 反引号里的内容也参与全文搜索
    haystack = text

    urls: list[dict] = []
    for raw in _PATH_RE.findall(haystack):
        http = _guess_http(haystack, raw)
        urls.append({"path": raw, "http": http, "display": f"{http} {raw}".strip()})

    types = _dedupe(
        _TYPE_NAME_RE.findall(haystack) + _TYPE_NAME_RE.findall(ticked_text)
    )

    methods: list[str] = []
    for name in _METHOD_CALL_RE.findall(ticked_text or haystack):
        if name not in _METHOD_CALL_STOP:
            methods.append(name)
    for owner, name in _METHOD_DOT_RE.findall(haystack):
        methods.append(name)
    methods = _dedupe(methods)

    codes = _dedupe(find_error_codes(ticked_text or haystack))
    enums = _dedupe(_ENUM_RE.findall(haystack))

    fields: list[str] = []
    for token in ticked:
        token = token.strip()
        m = re.fullmatch(r"[a-z][A-Za-z0-9_]*", token)
        if m and _CAMEL_RE.fullmatch(token) and token not in _CAMEL_STOPWORDS:
            fields.append(token)
    if not fields:
        for cand in _CAMEL_RE.findall(haystack):
            if cand not in _CAMEL_STOPWORDS and len(cand) >= 5:
                fields.append(cand)
    fields = _dedupe(fields)

    return {
        "urls": urls,
        "types": types,
        "methods": methods,
        "fields": fields,
        "error_codes": codes,
        "enum_values": enums,
    }


def _empty_ids() -> dict:
    return {
        "urls": [], "types": [], "methods": [],
        "fields": [], "error_codes": [], "enum_values": [],
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _guess_http(text: str, path: str) -> str:
    """在路径附近找 HTTP 方法词，找不到算 ANY。"""
    pos = text.find(path)
    window = text[max(0, pos - 40) : pos + len(path) + 10].upper()
    for word in _HTTP_WORDS:
        if word in window:
            return word
    return "ANY"


# 存在性条目的标识符优先级：越靠前越"主"
_PRIMARY_ORDER = ("urls", "types", "methods", "error_codes", "enum_values", "fields")


def _primary_ids(ids: dict) -> dict:
    """存在性条目只留优先级最高的那一类标识符，其余置空。"""
    for key in _PRIMARY_ORDER:
        if ids.get(key):
            return {k: (ids[key] if k == key else []) for k in ids}
    return ids


# ---------------------------------------------------------------- 静态判定

def judge_static(item: dict, index: CodeIndex) -> dict:
    """对一条需求做静态判定。

    只有"存在性"条目才会给出结论；"行为类"条目只提供候选位置，
    结论留给 AI —— 因为"这段代码有没有实现那条规则"不是搜得出来的。
    """
    assertion = item.get("assertion", "") or ""
    source_text = item.get("text", "") or ""
    ids = extract_identifiers(f"{assertion} {source_text}")
    kind = (item.get("kind") or "").strip().lower()

    # 存在性条目只认一个"主标识符"：一条断言本来就只该判一件事。
    # 顺带提到的旁支（比如入参里的字段名）不参与存在性判定，
    # 否则"接口存在"会因为一个旁支字段没搜到，被判成"模棱两可"。
    if kind == "existence":
        ids = _primary_ids(ids)

    checks = _run_checks(ids, index)

    base = {
        "item_id": item.get("id", ""),
        "kind": kind or "behavior",
        "identifiers": ids,
        "checks": checks,
        "search_log": _search_log(index, ids),
    }

    # ---- 行为类：能搜的只是线索，不下结论 ----
    if kind != "existence":
        candidates = [c for c in checks if c["hits"]]
        return {
            **base,
            "route": "ai",
            "verdict": "need_ai",
            "evidence": _flatten(candidates, limit=8),
            "reason": "行为类条目，必须读懂代码逻辑才能判",
        }

    # ---- 存在性：没有可搜的标识符，说明条目化时写得不够具体 ----
    if not checks:
        return {
            **base,
            "route": "ai",
            "verdict": "need_ai",
            "evidence": [],
            "reason": "标为存在性条目，但没抽出可检索的标识符，交给 AI 兜底",
        }

    hit = [c for c in checks if c["hits"]]
    miss = [c for c in checks if not c["hits"]]

    # 全部找不到
    if miss and not hit:
        return {
            **base,
            "route": "static",
            "verdict": "missing",
            "evidence": [],
            "reason": _missing_reason(miss),
        }

    # 全部找到
    if hit and not miss:
        if any(len(c["hits"]) > 1 for c in hit):
            return {
                **base,
                "route": "ai",
                "verdict": "ambiguous",
                "evidence": _flatten(hit, limit=10),
                "reason": "命中了多处，无法确定是哪一处，交给 AI 判定",
            }
        return {
            **base,
            "route": "static",
            "verdict": "implemented",
            "evidence": _flatten(hit, limit=10),
            "reason": "标识符在代码中唯一定位到",
        }

    # 一半找得到一半找不到
    return {
        **base,
        "route": "ai",
        "verdict": "partial",
        "evidence": _flatten(hit, limit=10),
        "reason": (
            "部分标识符找到了，部分没找到："
            + "；".join(f"缺 {c['target']}" for c in miss)
        ),
    }


def _run_checks(ids: dict, index: CodeIndex) -> list[dict]:
    """把标识符逐个拿去索引里搜。"""
    checks: list[dict] = []

    for u in ids["urls"]:
        hits = index.find_url(u["path"], u["http"])
        checks.append(
            {
                "kind": "url",
                "target": u["display"],
                "hits": hits,
                "nearest": _nearest_url(index, u["path"]) if not hits else [],
            }
        )

    for name in ids["types"]:
        hits = index.find_type(name)
        checks.append(
            {
                "kind": "type",
                "target": name,
                "hits": hits,
                "nearest": _nearest_name(index.types, name) if not hits else [],
            }
        )

    for name in ids["methods"]:
        hits = index.find_method(name)
        checks.append(
            {
                "kind": "method",
                "target": name,
                "hits": hits,
                "nearest": _nearest_name(index.methods, name) if not hits else [],
            }
        )

    for name in ids["fields"]:
        hits = index.find_field(name)
        checks.append(
            {
                "kind": "field",
                "target": name,
                "hits": hits,
                "nearest": _nearest_name(index.fields, name) if not hits else [],
            }
        )

    for code in ids["error_codes"]:
        hits = index.find_error_code(code)
        checks.append(
            {"kind": "error_code", "target": code, "hits": hits, "nearest": []}
        )

    for name in ids["enum_values"]:
        hits = index.find_enum_constant(name)
        checks.append(
            {
                "kind": "enum",
                "target": name,
                "hits": hits,
                "nearest": _nearest_name(index.enum_constants, name) if not hits else [],
            }
        )

    return checks


def _flatten(checks: list[dict], limit: int = 10) -> list[dict]:
    """把命中结果摊平成一串位置，供报告引用。同一个位置只留一次。"""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for c in checks:
        for h in c["hits"]:
            key = (h.get("file", ""), int(h.get("line") or 0))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "file": h.get("file", ""),
                    "line": h.get("line", 0),
                    "target": c["target"],
                    "kind": c["kind"],
                    "owner": h.get("owner", ""),
                }
            )
            if len(out) >= limit:
                return out
    return out


def _missing_reason(miss: list[dict]) -> str:
    targets = "、".join(c["target"] for c in miss)
    return f"这些标识符在整个代码库里都没搜到：{targets}"


# ---------------------------------------------------------------- 搜查记录

def _search_log(index: CodeIndex, ids: dict) -> dict:
    """记下"为了这条结论，都搜过什么"。"""
    keywords: list[str] = []
    keywords += [u["display"] for u in ids["urls"]]
    keywords += ids["types"]
    keywords += ids["methods"]
    keywords += ids["fields"]
    keywords += ids["error_codes"]
    keywords += ids["enum_values"]

    return {
        "scope": list(index.roots),
        "file_count": len(index.files),
        "indexes_searched": ["接口", "类型", "方法", "字段", "枚举值", "错误码"],
        "keywords": _dedupe(keywords),
    }


def _nearest_name(table: dict, name: str, limit: int = 3) -> list[dict]:
    """找不到时，给几个"长得像"的候选，方便人判断是不是搜漏了。"""
    low = name.lower()
    out: list[dict] = []
    for key, items in table.items():
        k = key.lower()
        if k == low:
            continue
        if low in k or k in low:
            for it in items[:1]:
                out.append(
                    {
                        "name": key,
                        "file": it.get("file", ""),
                        "line": it.get("line", 0),
                    }
                )
        if len(out) >= limit:
            break
    return out


def _nearest_url(index: CodeIndex, path: str, limit: int = 3) -> list[dict]:
    """找不到接口时，给路径前缀相近的候选。"""
    seg = [s for s in path.strip("/").split("/") if s]
    prefix = "/" + (seg[0] if seg else "")
    out: list[dict] = []
    for key, items in index.urls.items():
        _, _, key_path = key.partition(" ")
        if key_path.startswith(prefix):
            for it in items[:1]:
                out.append(
                    {
                        "name": key,
                        "file": it.get("file", ""),
                        "line": it.get("line", 0),
                    }
                )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 汇总

def summarize(results: list[dict]) -> dict:
    """统计各路结果，给进度和报告头用。"""
    counter = {
        "static": 0, "ai": 0, "human": 0,
        "implemented": 0, "missing": 0,
        "ambiguous": 0, "partial": 0, "need_ai": 0,
    }
    for r in results:
        counter[r.get("route", "ai")] = counter.get(r.get("route", "ai"), 0) + 1
        v = r.get("verdict", "")
        if v in counter:
            counter[v] += 1
    return counter
