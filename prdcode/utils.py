"""公共小工具。

这里只放"跟业务无关、谁都要用"的东西：读文件、算行号、净化源码。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 读写

def read_text(path: str | Path) -> str:
    """读文本文件，按 UTF-8 解码，遇到坏字节不炸。"""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path: str | Path, content: str) -> None:
    """写文本文件，自动建父目录。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def read_json(path: str | Path, default: Any = None) -> Any:
    """读 JSON，文件不存在或格式坏掉时返回 default。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str | Path, data: Any) -> None:
    """写 JSON，缩进 2 格，保留中文。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 行号

def pos_to_line(text: str, pos: int) -> int:
    """把字符下标换算成 1 起算的行号。"""
    if pos <= 0:
        return 1
    return text.count("\n", 0, pos) + 1


def lines_of(text: str) -> list[str]:
    """切成行数组，行号 = 下标 + 1。"""
    return text.splitlines()


def get_line(text: str, line_no: int) -> str:
    """取第 line_no 行（1 起算），越界返回空串。"""
    if line_no < 1:
        return ""
    parts = text.splitlines()
    if line_no > len(parts):
        return ""
    return parts[line_no - 1]


def snippet_around(text: str, line_no: int, before: int = 2, after: int = 2) -> str:
    """取某一行附近的原文，用于给人看或给 AI 当上下文。"""
    parts = text.splitlines()
    start = max(1, line_no - before)
    end = min(len(parts), line_no + after)
    out = []
    for i in range(start, end + 1):
        out.append(f"{i}\t{parts[i - 1]}")
    return "\n".join(out)


# ---------------------------------------------------------------- 源码净化

# 一次匹配掉所有"注释 / 字符串 / 字符"。比逐字符扫快得多，大工程上差很多。
_STRIP_RE = re.compile(
    r'"""(?s:.*?)"""'          # 文本块
    r'|"(?:\\.|[^"\\\n])*"'    # 普通字符串
    r"|'(?:\\.|[^'\\\n])*'"    # 字符字面量
    r"|//[^\n]*"               # 行注释
    r"|/\*.*?\*/",             # 块注释
    re.S,
)


def strip_comments_keep_lines(src: str) -> str:
    """把注释和字符串字面量替换成等长空格，换行原样保留。

    为什么要做这一步：源码里的注释经常写着 ``{`` ``(`` ``"`` 这类字符，
    直接拿正则去匹配"代码结构"会被它们带偏（括号配对错位、方法签名误判）。
    替换成同长度的空格后，字符位置和行号都不变，但干扰没了。

    注意：字符串的内容也会一起被抹掉。所以需要读字符串内容的地方
    （比如注解里写的接口路径），要回到原始文本、用相同的位置区间去取。
    """

    def _blank(match: re.Match) -> str:
        seg = match.group(0)
        if "\n" not in seg:
            return " " * len(seg)
        return "".join("\n" if ch == "\n" else " " for ch in seg)

    return _STRIP_RE.sub(_blank, src)


# ---------------------------------------------------------------- 共用规则

# 错误码：5 位、首位非 0。索引端（codeindex）和判定端（matcher）必须用同一条规则，
# 否则需求里写的错误码在索引里永远搜不到，会被误判成"没做"。
# 前后不允许紧挨字母/数字/点，避开小数、订单号这类数字。
ERROR_CODE_RE = re.compile(r"(?<![\w.])([1-9]\d{4})(?![\w.])")


# ---------------------------------------------------------------- 文本判断

def norm_ws(s: str) -> str:
    """把连续空白压成一个空格，便于比对。"""
    return re.sub(r"\s+", " ", s).strip()
