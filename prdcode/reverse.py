"""反向覆盖：代码里有、需求里没提的。

这是抓"AI 自己加戏"的唯一手段。正向只查"需求写了没做"，
永远发现不了"需求没写、代码却做了"。

但这类判断有个大坑：**"需求没提"不等于"多余"。**
需求文档从来不写工具类、日志、配置、DTO 转换这些东西。
要是不加限制地反查，报出来的一大半都是正常代码，报告当场作废。

所以这里的口径卡得很死：

  只看**对外暴露的能力** —— HTTP 接口（要能对上 Controller 的注解）
  内部实现细节一律不报

即便这样，结论也只能进「待确认」，由人来定：
是需求外实现（删）、需求漏写（补文档），还是通用能力（加白名单）。
"""

from __future__ import annotations

import re

from .codeindex import CodeIndex, _normalize_url
from .matcher import extract_identifiers

# 这些路径段太常见，单独命中不算"需求里提过"
_TOO_COMMON = {
    "api", "app", "admin", "inner", "internal", "open", "public", "v1", "v2",
    "list", "detail", "page", "info", "query", "get", "post", "update",
}

# 常规动作段：需求里已经点到过该模块的接口时，这些动作接口大概率是正常实现。
# 中文需求不会写英文动作名（"提交申请" 不会写成 apply），逐段比对必然误报，
# 所以同模块下这类常规动作一律不报；不常见的动作（如 retry、batchReconcile）仍会报出。
_GENERIC_ACTIONS = {
    "get", "list", "detail", "query", "page", "search", "save", "add",
    "create", "update", "edit", "delete", "remove", "info", "view",
    "apply", "submit", "cancel", "close", "confirm", "status", "count",
}


def find_extra_capabilities(
    index: CodeIndex,
    items: list[dict],
    min_path_segments: int = 2,
) -> list[dict]:
    """找出代码里对外暴露、但需求条目里没提过的接口。"""
    mentioned_paths, mentioned_words = _collect_mentions(items)
    mentioned_modules = _mentioned_modules(mentioned_paths)
    all_paths = index.all_url_paths()
    seg_freq = _segment_frequency(all_paths)

    extras: list[dict] = []
    for path, hits in sorted(all_paths.items()):
        segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
        if len(segments) < min_path_segments:
            continue

        if _normalize_url(path) in mentioned_paths:
            continue
        if _segments_mentioned(segments, mentioned_words, seg_freq, mentioned_modules):
            continue
        if _handler_mentioned(hits, mentioned_words):
            continue

        first = hits[0]
        extras.append(
            {
                "path": path,
                "http": first.get("http", "ANY"),
                "file": first.get("file", ""),
                "line": first.get("line", 0),
                "handler": first.get("handler", ""),
                "owner": first.get("owner", ""),
                "group": _guess_group(path, segments),
            }
        )

    return extras


def _collect_mentions(items: list[dict]) -> tuple[set[str], set[str]]:
    """把需求条目里提过的路径和词收起来。"""
    paths: set[str] = set()
    words: set[str] = set()

    for item in items:
        text = " ".join(
            [
                item.get("assertion", "") or "",
                item.get("text", "") or "",
                item.get("source", {}).get("file", "") or "",
            ]
        )
        ids = extract_identifiers(text)
        for u in ids["urls"]:
            paths.add(_normalize_url(u["path"]))
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text):
            words.add(token.lower())
        for token in ids["types"] + ids["methods"] + ids["fields"]:
            words.add(token.lower())

    return paths, words


def _segment_frequency(paths: dict[str, list[dict]]) -> dict[str, int]:
    """统计每个路径段出现在多少个接口里。

    出现得特别多的段（比如模块名 ``/refund``）没有区分度，
    拿它去比"需求里提没提"只会一路绿灯，等于白比。
    """
    freq: dict[str, int] = {}
    for path in paths:
        for seg in {
            s for s in path.strip("/").split("/") if s and not s.startswith("{")
        }:
            key = seg.lower()
            freq[key] = freq.get(key, 0) + 1
    return freq


def _mentioned_modules(paths: set[str]) -> set[str]:
    """需求里点到过的接口路径的模块段（第一段）。"""
    modules: set[str] = set()
    for path in paths:
        segs = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
        if segs:
            modules.add(segs[0].lower())
    return modules


def _segments_mentioned(
    segments: list[str],
    words: set[str],
    seg_freq: dict[str, int],
    mentioned_modules: set[str],
) -> bool:
    """判断这条路径算不算"需求里提过"。

    有区分度的段都能在需求里找到出处 → 提过；否则，若它属于需求里点到过的
    模块、且动作段是常规动作，也当作提过（中文需求不会写英文动作名，避免误报）；
    两者都不满足才报出来让人确认。
    """
    meaningful = [
        s
        for s in segments
        if s.lower() not in _TOO_COMMON and seg_freq.get(s.lower(), 0) <= 2
    ]
    if not meaningful:
        # 整条路径都是通用前缀，判断不了，先不报
        return True
    if all(s.lower() in words for s in meaningful):
        return True

    module = next((s for s in segments if s.lower() not in _TOO_COMMON), None)
    action = segments[-1] if segments else ""
    return bool(
        module
        and module.lower() in mentioned_modules
        and action.lower() in _GENERIC_ACTIONS
    )


def _handler_mentioned(hits: list[dict], words: set[str]) -> bool:
    return any((h.get("handler") or "").lower() in words for h in hits)


def _guess_group(path: str, segments: list[str]) -> str:
    meaningful = [s for s in segments if s.lower() not in _TOO_COMMON]
    return meaningful[0] if meaningful else path
