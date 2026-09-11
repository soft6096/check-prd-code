"""Java 源码的轻量结构化解析（纯标准库，不引入任何依赖）。

不再"拿正则往原文上套"，而是分两步：

  1. **词法分析** —— 把源码切成 token，正确处理注释、字符串、字符、文本块。
     注释和字符串在词法阶段就被吃掉，不会再来扰乱结构。
  2. **结构化扫描** —— 沿着 token 走一遍，识别类型声明、成员（方法/字段/
     枚举常量）和注解。得到的东西等价于一棵"只保留我们关心的节点"的语法树。

这一层只回答"代码里有什么符号"，不做任何判断。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Tuple

# token 形如 (kind, text, pos)：
#   "id"  —— 标识符/关键字
#   "num" —— 数字字面量
#   "str" —— 字符串字面量的内容（不含引号）
#   "op"  —— 运算符/标点（单字符）
Token = Tuple[str, str, int]

_ID_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_ID_PART = _ID_START | set("0123456789")
_WS = set(" \t\r\n\f\u000b")
_OP_CHARS = set("(){}[];,.<>=@+-*/%&|^!~?:")
_TYPE_KEYWORDS = {"class", "interface", "enum", "record"}

# HTTP 映射注解 → 方法（None 表示不限方法）。放在这里，索引端从这里取。
MAPPING_ANNOTATIONS = {
    "RequestMapping": None,
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


# ---------------------------------------------------------------- 词法分析

def tokenize(src: str) -> list[Token]:
    """把 Java 源码切成 token；注释、字符串、字符都按各自规则吃掉。"""
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in _WS:
            i += 1
            continue

        # 注释：// 到行尾，/* */ 到闭合
        if c == "/" and i + 1 < n:
            nxt = src[i + 1]
            if nxt == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j + 1
                continue
            if nxt == "*":
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue

        # 字符串 / 文本块：只保留内容，供注解参数使用
        if c == '"':
            if src.startswith('"""', i):
                j = src.find('"""', i + 3)
                if j < 0:
                    tokens.append(("str", src[i + 3 :], i))
                    i = n
                else:
                    tokens.append(("str", src[i + 3 : j], i))
                    i = j + 3
                continue
            start = i
            i += 1
            buf: list[str] = []
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(src[i : i + 2])
                    i += 2
                    continue
                if ch == '"' or ch == "\n":
                    break
                buf.append(ch)
                i += 1
            if i < n and src[i] == '"':
                i += 1
            tokens.append(("str", "".join(buf), start))
            continue

        # 字符字面量：整段丢掉
        if c == "'":
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if src[i] == "'" or src[i] == "\n":
                    i += 1
                    break
                i += 1
            continue

        if c in _ID_START:
            j = i + 1
            while j < n and src[j] in _ID_PART:
                j += 1
            tokens.append(("id", src[i:j], i))
            i = j
            continue

        if c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            tokens.append(("num", src[i:j], i))
            i = j
            continue

        if c in _OP_CHARS:
            tokens.append(("op", c, i))
        i += 1
    return tokens


class _Lines:
    """offset → 行号（1 起算）。预存所有换行位置，查询走二分。"""

    def __init__(self, src: str) -> None:
        self.starts = [0]
        for k, ch in enumerate(src):
            if ch == "\n":
                self.starts.append(k + 1)

    def line(self, pos: int) -> int:
        return bisect_right(self.starts, pos)


# ---------------------------------------------------------------- 基础工具

def _match(toks: list[Token], open_idx: int, open_ch: str, close_ch: str) -> int:
    """返回与 ``open_idx`` 处括号配对的收尾下标；找不到就返回末尾。"""
    depth = 0
    j = open_idx
    while j < len(toks):
        k, t, _ = toks[j]
        if k == "op":
            if t == open_ch:
                depth += 1
            elif t == close_ch:
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return len(toks) - 1


def _first_top_level(seg: list[Token], target: str) -> int | None:
    """在一段 token 里找第一个"顶层"（不在括号/泛型/数组内）的目标运算符。"""
    paren = angle = brack = brace = 0
    for i, (k, t, _) in enumerate(seg):
        if k != "op":
            continue
        if t == "(":
            paren += 1
        elif t == ")":
            paren = max(0, paren - 1)
        elif t == "<":
            angle += 1
        elif t == ">":
            angle = max(0, angle - 1)
        elif t == "[":
            brack += 1
        elif t == "]":
            brack = max(0, brack - 1)
        elif t == "{":
            brace += 1
        elif t == "}":
            brace = max(0, brace - 1)
        elif t == target and not (paren or angle or brack or brace):
            return i
    return None


def _split_top_level(seg: list[Token], sep: str) -> list[list[Token]]:
    """按顶层分隔符切分 token（逗号在泛型/括号里不算）。"""
    parts: list[list[Token]] = []
    cur: list[Token] = []
    depth = 0
    for tok in seg:
        k, t, _ = tok
        if k == "op":
            if t in "([{<":
                depth += 1
            elif t in ")]}>":
                depth = max(0, depth - 1)
            elif t == sep and depth == 0:
                parts.append(cur)
                cur = []
                continue
        cur.append(tok)
    parts.append(cur)
    return parts


def _has_op(seg: list[Token], op: str) -> bool:
    return any(k == "op" and t == op for k, t, _ in seg)


def _has_type_keyword(seg: list[Token]) -> bool:
    for i, (k, t, _) in enumerate(seg):
        if k == "id" and t in _TYPE_KEYWORDS and i + 1 < len(seg) and seg[i + 1][0] == "id":
            return True
    return False


# ---------------------------------------------------------------- 声明识别

def _is_type_decl(toks: list[Token], i: int) -> bool:
    """``class`` 这类关键字后面紧跟标识符，才算类型声明（排除 ``X.class``）。"""
    if i > 0 and toks[i - 1][0] == "op" and toks[i - 1][1] == ".":
        return False
    return i + 1 < len(toks) and toks[i + 1][0] == "id"


def _find_body_open(toks: list[Token], i: int) -> int | None:
    """从名字后面往后找类型体的 ``{``，跳过泛型/extends/参数表。"""
    paren = angle = 0
    j = i
    while j < len(toks):
        k, t, _ = toks[j]
        if k == "op":
            if t == "(":
                paren += 1
            elif t == ")":
                paren = max(0, paren - 1)
            elif t == "<":
                angle += 1
            elif t == ">":
                angle = max(0, angle - 1)
            elif t == "{":
                if not paren and not angle:
                    return j
            elif t == ";":
                return None
        j += 1
    return None


def _declaration_start(toks: list[Token], i: int) -> int:
    """往回找到这个声明的起点（上一个顶层 ``; { }`` 之后），把注解一起框进来。

    注解参数里的 ``{...}``（如 ``@RequestMapping({"/a","/b"})``）不是边界，
    往回走时用括号深度把它们跳过去。
    """
    depth = 0
    j = i - 1
    while j >= 0:
        k, t, _ = toks[j]
        if k == "op":
            if t == ")":
                depth += 1
            elif t == "(":
                depth -= 1
            elif depth == 0 and t in "{};":
                return j + 1
        j -= 1
    return 0


def _extract_annotations(
    toks: list[Token], start: int, end: int
) -> list[tuple[str, list[str]]]:
    """抽一段 token 里所有注解及其字符串参数。"""
    out: list[tuple[str, list[str]]] = []
    j = start
    while j < end:
        k, t, _ = toks[j]
        if k == "op" and t == "@" and j + 1 < end and toks[j + 1][0] == "id":
            ann = toks[j + 1][1]
            args: list[str] = []
            k2 = j + 2
            if k2 < end and toks[k2][0] == "op" and toks[k2][1] == "(":
                close = _match(toks, k2, "(", ")")
                for m in range(k2 + 1, min(close, end)):
                    if toks[m][0] == "str":
                        args.append(toks[m][1])
                j = close + 1
            else:
                j += 2
            out.append((ann, args))
            continue
        j += 1
    return out


def _mapping_paths(toks: list[Token], start: int, end: int) -> list[str]:
    """从一段声明头里抽所有映射路径（只认以 ``/`` 开头的字符串）。"""
    paths: list[str] = []
    for ann, args in _extract_annotations(toks, start, end):
        if ann in MAPPING_ANNOTATIONS:
            paths.extend(a for a in args if a.startswith("/"))
    return paths


def _method_signature(seg: list[Token]) -> tuple[str, int] | None:
    """判断一段成员 token 是不是方法声明，是就返回 (名字, 位置)。

    条件：``(`` 前面紧挨标识符，且该 ``(`` 之前没有顶层 ``=``（有 ``=`` 说明是
    字段初始化）。``(`` 前是 ``@`` 的属于注解参数，跳过。
    """
    eq = _first_top_level(seg, "=")
    limit = eq if eq is not None else len(seg)
    i = 0
    while i < limit:
        k, t, _ = seg[i]
        if k == "op" and t == "(":
            if i >= 1 and seg[i - 1][0] == "id":
                before = seg[i - 2] if i >= 2 else None
                if not (before and before[0] == "op" and before[1] == "@"):
                    return (seg[i - 1][1], seg[i - 1][2])
            i = _match(seg, i, "(", ")") + 1
            continue
        i += 1
    return None


def _field_names(seg: list[Token]) -> list[tuple[str, int]]:
    """从字段声明里取名字，支持 ``int a, b, c = 3;`` 这种多声明符。"""
    out: list[tuple[str, int]] = []
    for part in _split_top_level(seg, ","):
        eq = _first_top_level(part, "=")
        limit = part[:eq] if eq is not None else part
        last: tuple[str, int] | None = None
        for k, t, pos in limit:
            if k == "id":
                last = (t, pos)
        if last:
            out.append(last)
    return out


def _enum_constant_names(seg: list[Token]) -> list[tuple[str, int]]:
    """从枚举体首个 ``;`` 之前的一段里取常量名（跳过注解名和构造参数）。"""
    out: list[tuple[str, int]] = []
    for part in _split_top_level(seg, ","):
        prev_at = False
        for k, t, pos in part:
            if k == "op" and t == "@":
                prev_at = True
                continue
            if k == "id":
                if prev_at:
                    prev_at = False
                    continue
                out.append((t, pos))
                break
            prev_at = False
    return out


# ---------------------------------------------------------------- 结果容器

class JavaSymbols:
    """一个 java 文件里解析出的符号。字段都带行号。"""

    def __init__(self) -> None:
        self.types: list[dict] = []
        self.methods: list[dict] = []
        self.fields: list[dict] = []
        self.enum_constants: list[dict] = []


# ---------------------------------------------------------------- 成员扫描

def _scan_members(
    toks: list[Token],
    lines: _Lines,
    syms: JavaSymbols,
    owner: str,
    start: int,
    end: int,
    is_enum: bool,
) -> None:
    """扫一个类型体 [start, end)：登记方法、字段、枚举常量。"""
    pending = start          # 当前成员声明的起点
    enum_done = False        # 枚举体是否已过首个 ;
    j = start
    while j < end:
        k, t, _ = toks[j]
        if k != "op":
            j += 1
            continue

        if t == "(":
            j = _match(toks, j, "(", ")") + 1
            continue
        if t == "[":
            j = _match(toks, j, "[", "]") + 1
            continue

        if t == "{":
            seg = toks[pending:j]
            close = _match(toks, j, "{", "}")
            if is_enum and not enum_done:
                # 枚举常量的类体：保持 pending，等它后面的 ,
                pass
            elif _has_type_keyword(seg):
                pending = close + 1          # 嵌套类型，交给类型扫描
            else:
                sig = _method_signature(seg)
                if sig:
                    _add_method(syms, lines, owner, sig, close, seg, toks)
                    pending = close + 1
                elif _has_op(seg, "="):
                    pass                     # 字段初始化块，等它的 ;
                else:
                    pending = close + 1      # 静态/实例初始化块
            j = close + 1
            continue

        if t == "}":
            return

        if t == ";":
            seg = toks[pending:j]
            if is_enum and not enum_done:
                for name, pos in _enum_constant_names(seg):
                    syms.enum_constants.append(
                        {"name": name, "owner": owner, "line": lines.line(pos)}
                    )
                enum_done = True
            elif not _has_type_keyword(seg):
                sig = _method_signature(seg)
                if sig:
                    _add_method(syms, lines, owner, sig, j, seg, toks)
                else:
                    for name, pos in _field_names(seg):
                        syms.fields.append(
                            {"name": name, "owner": owner, "line": lines.line(pos)}
                        )
            pending = j + 1
            j += 1
            continue

        j += 1


def _add_method(
    syms: JavaSymbols,
    lines: _Lines,
    owner: str,
    sig: tuple[str, int],
    close: int,
    seg: list[Token],
    toks: list[Token],
) -> None:
    name, pos = sig
    syms.methods.append(
        {
            "name": name,
            "owner": owner,
            "line": lines.line(pos),
            "end_line": lines.line(toks[close][2]),
            "annotations": _extract_annotations(seg, 0, len(seg)),
        }
    )


# ---------------------------------------------------------------- 入口

def parse_java(src: str) -> JavaSymbols:
    """解析一个 java 源文件，返回其中的类型/方法/字段/枚举常量。"""
    toks = tokenize(src)
    lines = _Lines(src)
    syms = JavaSymbols()

    n = len(toks)
    i = 0
    while i < n:
        k, t, _ = toks[i]
        if k == "id" and t in _TYPE_KEYWORDS and _is_type_decl(toks, i):
            name_tok = toks[i + 1]
            body_open = _find_body_open(toks, i + 1)
            if body_open is not None:
                body_close = _match(toks, body_open, "{", "}")
                base_paths = _mapping_paths(toks, _declaration_start(toks, i), i)
                syms.types.append(
                    {
                        "name": name_tok[1],
                        "kind": t,
                        "line": lines.line(name_tok[2]),
                        "base_paths": base_paths or [""],
                    }
                )
                _scan_members(
                    toks, lines, syms, name_tok[1], body_open + 1, body_close, t == "enum"
                )
                i = body_open + 1
                continue
        i += 1

    return syms
