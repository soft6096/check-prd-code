"""把 PRD 切成块。

这一步不做判断，只做切分——因为切分是纯结构问题，脚本比模型可靠。
切完的块交给大模型改写成"一句能判对错的话"。

切块的粒度原则：**宁可块小一点，也不要让一个块里塞十条需求。**
块太大，模型会漏；块太小，又丢掉上下文。所以按内容类型切，超长再拆。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

from .utils import read_text

# 标题：# ## ### ...
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# 列表项：- * + 或 1. 1)
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+\S")
# 表格分隔行：| --- | :---: |
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# 围栏代码块
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# 图片引用：![alt](path)
_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")

# 这些标题下面的内容，基本不是"要实现的规则"，先标出来给模型参考
_NON_REQ_HINTS = (
    "背景", "概述", "目标", "价值", "术语", "名词", "变更记录", "修订",
    "历史", "参考", "附录", "文档说明", "版本记录", "评审", "待办",
    "目录", "引言", "目的", "范围说明", "名词解释",
)

MAX_BLOCK_LINES = 40


# ---------------------------------------------------------------- 入口

def collect_md_files(inputs: Iterable[str | Path]) -> list[Path]:
    """把输入展开成一批 .md 文件（输入可以是文件，也可以是目录）。"""
    found: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in (".md", ".markdown"):
                found.append(p)
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".md", ".markdown"):
                    found.append(f)
    # 去重，保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for f in found:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def rel_name(path: Path, base: Path | None) -> str:
    """给出用于报告展示的文件名。"""
    if base is None:
        return path.name
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return path.name


def split_prd(
    inputs: Iterable[str | Path],
    base: Path | None = None,
    max_block_lines: int = MAX_BLOCK_LINES,
) -> list[dict]:
    """把 PRD 切成块。

    返回的每一块形如::

        {
          "id": "B0001",
          "file": "需求文档.md",
          "start_line": 12,
          "end_line": 18,
          "heading": ["退款", "撤销"],
          "kind": "list",
          "text": "- 撤销必须按批次整批关闭\\n- ...",
          "likely_requirement": True
        }
    """
    blocks: list[dict] = []
    seq = 0

    for path in collect_md_files(inputs):
        text = read_text(path)
        lines = text.splitlines()
        name = rel_name(path, base)
        heading: list[str] = []
        i = 0
        while i < len(lines):
            kind, end, matched = _unit_at(lines, i)
            if kind == "blank":
                i = end
                continue

            if kind == "heading" and matched is not None:
                level = len(matched.group(1))
                title = matched.group(2).strip()
                heading = heading[: level - 1] + [title]

            chunk = lines[i:end]
            # 超长单元再按空行切成小段
            for sub_start, sub_end in _split_long(chunk, max_block_lines):
                seq += 1
                body = lines[i + sub_start : i + sub_end]
                text = "\n".join(body).strip("\n")
                blocks.append(
                    {
                        "id": f"B{seq:04d}",
                        "file": name,
                        "start_line": i + sub_start + 1,
                        "end_line": i + sub_end,
                        "heading": list(heading),
                        "kind": kind,
                        "text": text,
                        "images": extract_images(text),
                        "likely_requirement": _likely_requirement(kind, heading),
                    }
                )
            i = end

    return blocks


# ---------------------------------------------------------------- 单元识别

def _unit_at(lines: list[str], i: int) -> tuple[str, int, re.Match | None]:
    """判断从第 i 行开始的是一个什么单元，以及它到哪里结束。

    返回 ``(类型, 结束行下标(不含), 标题正则匹配)``。
    """
    line = lines[i]
    if not line.strip():
        return "blank", i + 1, None

    m = _HEADING_RE.match(line)
    if m:
        return "heading", i + 1, m

    if _FENCE_RE.match(line):
        return "code", _code_end(lines, i), None

    if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
        return "table", _table_end(lines, i), None

    if line.lstrip().startswith(">"):
        return "quote", _quote_end(lines, i), None

    if _LIST_RE.match(line):
        return "list", _list_end(lines, i), None

    return "para", _para_end(lines, i), None


def _code_end(lines: list[str], i: int) -> int:
    fence = _FENCE_RE.match(lines[i])
    marker = fence.group(1) if fence else "```"
    j = i + 1
    while j < len(lines):
        if lines[j].strip().startswith(marker):
            return j + 1
        j += 1
    return len(lines)


def _table_end(lines: list[str], i: int) -> int:
    j = i + 1
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        j += 1
    return j


def _quote_end(lines: list[str], i: int) -> int:
    j = i
    while j < len(lines):
        s = lines[j].strip()
        if s.startswith(">"):
            j += 1
            continue
        if not s and j + 1 < len(lines) and lines[j + 1].strip().startswith(">"):
            j += 1
            continue
        break
    return j


def _list_end(lines: list[str], i: int) -> int:
    j = i
    while j < len(lines):
        cur = lines[j]
        if _LIST_RE.match(cur):
            j += 1
            continue
        # 缩进的续行（列表项的第二行）
        if cur.strip() and j > i and (cur.startswith("  ") or cur.startswith("\t")):
            j += 1
            continue
        # 空行后如果还是列表或缩进续行，算同一块
        if not cur.strip():
            nxt = lines[j + 1] if j + 1 < len(lines) else ""
            if _LIST_RE.match(nxt) or (nxt.strip() and (nxt.startswith("  ") or nxt.startswith("\t"))):
                j += 1
                continue
            break
        break
    return j


def _para_end(lines: list[str], i: int) -> int:
    j = i
    while j < len(lines):
        cur = lines[j]
        if not cur.strip():
            break
        if _HEADING_RE.match(cur) or _LIST_RE.match(cur) or _FENCE_RE.match(cur):
            break
        if cur.lstrip().startswith((">", "|")):
            break
        j += 1
    return max(j, i + 1)


def _split_long(chunk: list[str], max_lines: int) -> list[tuple[int, int]]:
    """把过长的块按空行拆开，返回若干 (起始, 结束) 下标对。"""
    if len(chunk) <= max_lines:
        return [(0, len(chunk))]

    spans: list[tuple[int, int]] = []
    start = 0
    for idx, line in enumerate(chunk):
        if idx > start and not line.strip():
            if idx - start >= max_lines // 2:
                spans.append((start, idx))
                start = idx + 1
    if start < len(chunk):
        spans.append((start, len(chunk)))

    # 兜底：按固定长度硬切
    result: list[tuple[int, int]] = []
    for s, e in spans:
        while e - s > max_lines:
            result.append((s, s + max_lines))
            s += max_lines
        if e > s:
            result.append((s, e))
    return result or [(0, len(chunk))]


def extract_images(text: str) -> list[str]:
    """把块里的图片引用抽出来，路径做 URL 解码。

    Markdown 里的图片路径常常是 URL 编码的（``image%2020.png``），
    直接拿去当文件名会找不到文件。
    """
    out: list[str] = []
    for raw in _IMG_REF_RE.findall(text or ""):
        path = unquote(raw.strip())
        if path and path not in out:
            out.append(path)
    return out


def _likely_requirement(kind: str, heading: list[str]) -> bool:
    """粗判这块内容像不像"要实现的规则"。只是给模型的提示，不丢内容。

    只看**最近一级标题**。有些文档把全部内容都挂在「文档概述」底下，
    这时候按整条标题路径去匹配，会把整篇文档判成背景。
    """
    if kind == "heading":
        return False
    leaf = heading[-1] if heading else ""
    if not leaf:
        return True
    return not any(hint in leaf for hint in _NON_REQ_HINTS)


# ---------------------------------------------------------------- 渲染

def render_blocks_markdown(blocks: list[dict], limit: int = 0) -> str:
    """把切块结果渲染成给人看的清单（也方便人工抽检切分质量）。"""
    rows = blocks[:limit] if limit else blocks
    lines = [
        "| 块号 | 文件:行 | 位置（标题路径） | 类型 | 像需求 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for b in rows:
        path = " / ".join(b["heading"]) or "-"
        lines.append(
            f"| {b['id']} | `{b['file']}:{b['start_line']}-{b['end_line']}` "
            f"| {path} | {b['kind']} | {'是' if b['likely_requirement'] else '否'} |"
        )
    return "\n".join(lines)
