"""公共小工具。

这里只放"跟业务无关、谁都要用"的东西：读文件、算行号、比对文本。
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


# ---------------------------------------------------------------- 共用规则

# 错误码：5 位、首位非 0。索引端（codeindex）和判定端（matcher）必须用同一条规则，
# 否则需求里写的错误码在索引里永远搜不到，会被误判成"没做"。
# 前后不允许紧挨字母/数字/点，避开小数、订单号这类数字。
ERROR_CODE_RE = re.compile(r"(?<![\w.])([1-9]\d{4})(?![\w.])")


# ---------------------------------------------------------------- 路径安全

def resolve_within(base: str | Path, rel: str | Path) -> Path | None:
    """把 rel 拼到 base 下，并保证结果仍落在 base 内；越界返回 None。

    rel 来自 AI 的输出（引用里的文件名），可能带 ``..`` 或写成绝对路径。
    直接 ``base / rel`` 会被读到工程外面去，所以这里收敛一次。
    """
    base_p = Path(base).resolve()
    target = (base_p / str(rel)).resolve()
    try:
        target.relative_to(base_p)
    except ValueError:
        return None
    return target


# ---------------------------------------------------------------- 文本判断

def norm_ws(s: str) -> str:
    """把连续空白压成一个空格，便于比对。"""
    return re.sub(r"\s+", " ", s).strip()
