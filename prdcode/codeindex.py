"""扫描代码库，建立符号索引。

索引是整个工具的底座：**"在代码里搜不到"这个结论准不准，全看这里。**

所以这里只干一件事——把"代码里有什么"如实记下来：
哪个文件、第几行、什么类型、什么方法、什么接口、什么字段、什么枚举值。
不做任何判断，判断在后面的模块里做。

解析策略（不依赖任何第三方库）：
  1. 先把注释和字符串抹成空格（行号不变），避免注释里的花括号扰乱配对
  2. 预计算花括号配对表，一次 O(n) 扫描，后续 O(1) 查询
  3. 沿着花括号深度走，深度 1 处遇到的就是类的成员
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .utils import (
    ERROR_CODE_RE,
    pos_to_line,
    read_text,
    strip_comments_keep_lines,
)

CODE_EXTS = (".java",)
XML_EXTS = (".xml",)

# 类 / 接口 / 枚举 / record 声明
_TYPE_RE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_]\w*)")
# 注解（带括号那种）
_ANN_CALL_RE = re.compile(r"@([A-Za-z_]\w*)\s*\(")
# 注解（不带括号，如 @Override）
_ANN_BARE_RE = re.compile(r"@([A-Za-z_]\w*)")
# 注解里引号包着的字符串（在原始文本上匹配）
_ANN_STR_RE = re.compile(r'"([^"]*)"')
# 标识符
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# 枚举常量
_ENUM_CONST_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*(?:\(|,|;|$)")
# 错误码正则与判定端共用同一条（见 utils.ERROR_CODE_RE），
# 两边不一致会让需求里的错误码永远搜不到，被误判成"没做"。
# MyBatis XML
_XML_NS_RE = re.compile(r"<mapper[^>]*\bnamespace\s*=\s*\"([^\"]+)\"")
_XML_STMT_RE = re.compile(
    r"<(select|insert|update|delete)\b[^>]*\bid\s*=\s*\"([^\"]+)\"", re.I
)

# HTTP 注解 → 方法
MAPPING_ANNOTATIONS = {
    "RequestMapping": None,
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

# 这些不是方法名，扫成员时要排掉
_NOT_METHOD = {
    "if", "for", "while", "switch", "catch", "synchronized", "try", "do",
    "return", "new", "super", "this", "assert",
}


# ---------------------------------------------------------------- 花括号配对

def build_brace_map(sane: str) -> dict[int, int]:
    """一次扫描，算出所有 ``{`` 到 ``}`` 的配对关系。"""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, ch in enumerate(sane):
        if ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                pairs[stack.pop()] = i
    return pairs


def match_paren(sane: str, open_pos: int) -> int:
    """给一个 ``(`` 的位置，返回配对 ``)`` 的位置。"""
    depth = 0
    for i in range(open_pos, len(sane)):
        ch = sane[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


# ---------------------------------------------------------------- 索引容器

class CodeIndex:
    """符号索引。所有列表元素都是 ``(位置信息)`` 字典。

    这里存的是"代码里有什么"，不含任何判断结论。
    """

    def __init__(self, roots: Iterable[str | Path] = ()) -> None:
        self.roots: list[str] = [str(r) for r in roots]
        self.files: list[str] = []
        self.types: dict[str, list[dict]] = {}
        self.methods: dict[str, list[dict]] = {}
        self.urls: dict[str, list[dict]] = {}
        self.fields: dict[str, list[dict]] = {}
        self.enum_constants: dict[str, list[dict]] = {}
        self.error_codes: dict[str, list[dict]] = {}
        self.xml_statements: dict[str, list[dict]] = {}

    # ---- 写入 ----

    def add_type(self, name: str, item: dict) -> None:
        self.types.setdefault(name, []).append(item)

    def add_method(self, name: str, item: dict) -> None:
        self.methods.setdefault(name, []).append(item)

    def add_url(self, key: str, item: dict) -> None:
        self.urls.setdefault(key, []).append(item)

    def add_field(self, name: str, item: dict) -> None:
        self.fields.setdefault(name, []).append(item)

    def add_enum_constant(self, name: str, item: dict) -> None:
        self.enum_constants.setdefault(name, []).append(item)

    def add_error_code(self, code: str, item: dict) -> None:
        self.error_codes.setdefault(code, []).append(item)

    def add_xml_statement(self, stmt_id: str, item: dict) -> None:
        self.xml_statements.setdefault(stmt_id, []).append(item)

    # ---- 查询 ----

    def find_type(self, name: str) -> list[dict]:
        return self.types.get(name, [])

    def find_method(self, name: str) -> list[dict]:
        return self.methods.get(name, [])

    def find_field(self, name: str) -> list[dict]:
        return self.fields.get(name, [])

    def find_enum_constant(self, name: str) -> list[dict]:
        return self.enum_constants.get(name, [])

    def find_error_code(self, code: str) -> list[dict]:
        return self.error_codes.get(code, [])

    def find_url(self, path: str, http_method: str | None = None) -> list[dict]:
        """按路径找接口。路径做归一化（去掉结尾斜杠、参数占位差异）。

        ``http_method`` 为空或 ``"ANY"`` 时不限定方法：需求里常只写路径不写
        动词，不能因此把带显式动词（如 ``POST``）的接口排除掉。
        """
        wanted = _normalize_url(path)
        method = (http_method or "").strip().upper()
        hits: list[dict] = []
        for key, items in self.urls.items():
            key_method, _, key_path = _split_url_key(key)
            if _normalize_url(key_path) != wanted:
                continue
            if method and method != "ANY" and key_method not in ("ANY", method):
                continue
            hits.extend(items)
        return hits

    def all_url_paths(self) -> dict[str, list[dict]]:
        """返回 ``{路径: [位置]}``，用于反向覆盖。"""
        out: dict[str, list[dict]] = {}
        for key, items in self.urls.items():
            _, _, path = _split_url_key(key)
            out.setdefault(path, []).extend(items)
        return out

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "roots": self.roots,
            "file_count": len(self.files),
            "files": self.files,
            "types": self.types,
            "methods": self.methods,
            "urls": self.urls,
            "fields": self.fields,
            "enum_constants": self.enum_constants,
            "error_codes": self.error_codes,
            "xml_statements": self.xml_statements,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CodeIndex":
        idx = cls(data.get("roots", []))
        idx.files = data.get("files", [])
        idx.types = data.get("types", {})
        idx.methods = data.get("methods", {})
        idx.urls = data.get("urls", {})
        idx.fields = data.get("fields", {})
        idx.enum_constants = data.get("enum_constants", {})
        idx.error_codes = data.get("error_codes", {})
        idx.xml_statements = data.get("xml_statements", {})
        return idx


def _split_url_key(key: str) -> tuple[str, str, str]:
    """把 ``POST /a/b`` 拆成 (方法, ' ', 路径) 三元组。"""
    parts = key.split(" ", 1)
    if len(parts) == 2:
        return parts[0].upper(), " ", parts[1]
    return "ANY", " ", key


def _normalize_url(path: str) -> str:
    """归一化路径：去掉首尾斜杠、去掉重复斜杠、统一占位符写法。"""
    p = path.strip()
    p = p.split("?")[0]
    p = re.sub(r"/{2,}", "/", p)
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    # {id} 与 {orderId} 视为同一个位置的占位符
    p = re.sub(r"\{[^}]*\}", "{}", p)
    return p


# ---------------------------------------------------------------- 文件收集

def collect_code_files(
    roots: Iterable[str | Path],
    exts: tuple[str, ...] = CODE_EXTS,
    skip_dirs: Iterable[str] = ("target", "build", "out", "node_modules", ".git", "test"),
) -> list[Path]:
    """收集源码文件。默认跳掉构建产物和测试目录。"""
    skip = set(skip_dirs)
    found: list[Path] = []
    for raw in roots:
        p = Path(raw)
        if p.is_file():
            found.append(p)
            continue
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            if any(part in skip for part in f.parts):
                continue
            found.append(f)
    return found


# ---------------------------------------------------------------- 单个文件解析

def parse_java_file(path: Path, rel: str, index: CodeIndex) -> None:
    """解析一个 java 文件，把发现的符号写进索引。"""
    raw = read_text(path)
    sane = strip_comments_keep_lines(raw)
    pairs = build_brace_map(sane)

    for m in _TYPE_RE.finditer(sane):
        kind = m.group(1)
        name = m.group(2)
        brace = sane.find("{", m.end())
        if brace < 0:
            continue
        line = pos_to_line(raw, m.start())
        index.add_type(
            name,
            {"file": rel, "line": line, "kind": kind, "name": name},
        )
        # 类级注解（在 class 关键字之前的一段）
        head_start = _declaration_start(sane, m.start())
        class_head = raw[head_start : m.start()]
        base_paths = _extract_paths(class_head) or [""]
        _scan_members(
            raw, sane, pairs, brace, name, rel, index, base_paths,
            is_enum=(kind == "enum"), is_interface=(kind == "interface"),
        )

    _scan_error_codes(raw, rel, index)


def _declaration_start(sane: str, pos: int, lookback: int = 600) -> int:
    """往左找到这个声明的起点（上一个 ; { } 之后），用于取注解。"""
    start = max(0, pos - lookback)
    seg = sane[start:pos]
    cut = max(seg.rfind(";"), seg.rfind("}"), seg.rfind("{"))
    return start + cut + 1 if cut >= 0 else start


def _extract_paths(segment: str) -> list[str]:
    """从一段声明头里抽出**所有**映射路径。

    一个注解可以一次写多个路径（``@RequestMapping({"/a", "/b"})``），
    只取第一个会漏掉其余路径。只认以 ``/`` 开头的字符串，避开
    ``produces = "application/json"`` 这类非路径参数。
    """
    paths: list[str] = []
    for m in _ANN_CALL_RE.finditer(segment):
        ann = m.group(1)
        if ann not in MAPPING_ANNOTATIONS:
            continue
        open_paren = m.end() - 1
        close = match_paren(segment, open_paren)
        if close < 0:
            continue
        args = segment[open_paren + 1 : close]
        paths.extend(s for s in _ANN_STR_RE.findall(args) if s.startswith("/"))
    return paths


def _extract_annotations(segment: str) -> list[tuple[str, list[str]]]:
    """抽一段文本里所有注解及其字符串参数。"""
    out: list[tuple[str, list[str]]] = []
    for m in _ANN_CALL_RE.finditer(segment):
        ann = m.group(1)
        open_paren = m.end() - 1
        close = match_paren(segment, open_paren)
        if close < 0:
            continue
        args = segment[open_paren + 1 : close]
        out.append((ann, _ANN_STR_RE.findall(args)))
    for m in _ANN_BARE_RE.finditer(segment):
        if m.end() < len(segment) and segment[m.end() : m.end() + 1] == "(":
            continue
        out.append((m.group(1), []))
    return out


def _scan_members(
    raw: str,
    sane: str,
    pairs: dict[int, int],
    cls_open: int,
    cls_name: str,
    rel: str,
    index: CodeIndex,
    class_base_paths: list[str],
    is_enum: bool = False,
    is_interface: bool = False,
) -> None:
    """扫类体：把方法、字段、枚举常量登记进索引。"""
    cls_close = pairs.get(cls_open, len(sane))
    i = cls_open + 1
    last = cls_open + 1          # 上一个成员结束后的位置
    paren_depth = 0

    while i < cls_close:
        ch = sane[i]

        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        if paren_depth != 0:
            i += 1
            continue

        # ---- 方法体 / 初始化块 ----
        if ch == "{":
            head = sane[last:i]
            head_raw = raw[last:i]
            body_end = pairs.get(i, i)
            name = _method_name(head)
            if name and name not in _NOT_METHOD:
                line = pos_to_line(raw, last + _first_code_offset(head))
                _register_method(
                    index, name, cls_name, rel, line,
                    pos_to_line(raw, body_end), head_raw, class_base_paths,
                )
            i = body_end + 1
            last = i
            continue

        # ---- 成员声明结束（字段、抽象方法） ----
        if ch == ";":
            decl = sane[last:i]
            decl_raw = raw[last:i]
            line = pos_to_line(raw, last + _first_code_offset(decl))
            name = _method_name(decl)
            if name and name not in _NOT_METHOD and "(" in decl:
                # 接口里的抽象方法：没有方法体，以 ; 结尾
                _register_method(
                    index, name, cls_name, rel, line, line,
                    decl_raw, class_base_paths,
                )
            else:
                field = _field_name(decl)
                if field:
                    index.add_field(
                        field, {"file": rel, "line": line, "owner": cls_name}
                    )
            last = i + 1
            i += 1
            continue

        # ---- 枚举常量 ----
        if is_enum and ch == ",":
            seg = sane[last:i]
            m = _ENUM_CONST_RE.match(seg)
            if m:
                index.add_enum_constant(
                    m.group(1),
                    {"file": rel, "line": pos_to_line(raw, last), "owner": cls_name},
                )
                last = i + 1
            i += 1
            continue

        i += 1


def _register_method(
    index: CodeIndex,
    name: str,
    cls_name: str,
    rel: str,
    line: int,
    end_line: int,
    head_raw: str,
    class_base_paths: list[str],
) -> None:
    """登记一个方法；如果是 HTTP 入口，顺手登记 URL。

    类上可能一次写了多个基础路径，方法上也可能写多个子路径，
    两者做笛卡尔积，保证每个组合都被登记。
    """
    index.add_method(
        name,
        {
            "file": rel,
            "line": line,
            "end_line": end_line,
            "owner": cls_name,
        },
    )

    for ann, args in _extract_annotations(head_raw):
        if ann not in MAPPING_ANNOTATIONS:
            continue
        http = MAPPING_ANNOTATIONS[ann]
        sub_paths = [a for a in args if a.startswith("/")] or [""]
        for base in class_base_paths or [""]:
            for sub in sub_paths:
                full = _join_path(base, sub)
                key = f"{http or 'ANY'} {full}"
                index.add_url(
                    key,
                    {
                        "file": rel,
                        "line": line,
                        "handler": name,
                        "owner": cls_name,
                        "http": http or "ANY",
                    },
                )


def _join_path(base: str, sub: str) -> str:
    b = (base or "").strip()
    s = (sub or "").strip()
    if not b and not s:
        return "/"
    if not b:
        return s if s.startswith("/") else "/" + s
    if not s:
        return b if b.startswith("/") else "/" + b
    return (b.rstrip("/") + "/" + s.lstrip("/"))


def _first_code_offset(head: str) -> int:
    """跳过声明头里的空白与注解，返回第一段实际代码的下标。"""
    i = 0
    n = len(head)
    while i < n:
        ch = head[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "@":
            m = _ANN_CALL_RE.match(head, i)
            if m:
                close = match_paren(head, m.end() - 1)
                if close > 0:
                    i = close + 1
                    continue
            m = _ANN_BARE_RE.match(head, i)
            if m:
                i = m.end()
                continue
        break
    return i


def _method_name(head: str) -> str:
    """从声明头里取方法名。

    注意要把前面的注解跳掉 —— ``@PostMapping("/apply")`` 里那个括号
    比方法签名先出现，直接找第一个 ``(`` 会取成 ``PostMapping``。
    """
    offset = _first_code_offset(head)
    body = head[offset:]
    open_paren = body.find("(")
    if open_paren <= 0:
        return ""
    prefix = body[:open_paren]
    if "=" in prefix.split("\n")[-1]:
        # 形如 ``private int x = compute(``，是字段初始化，不是方法
        return ""
    names = _IDENT_RE.findall(prefix)
    if not names:
        return ""
    candidate = names[-1]
    if candidate in _NOT_METHOD:
        return ""
    return candidate


def _field_name(decl: str) -> str:
    """从字段声明里取字段名。同样要先跳过注解。"""
    offset = _first_code_offset(decl)
    body = decl[offset:]
    if "(" in body:
        return ""
    before_eq = body.split("=")[0]
    names = _IDENT_RE.findall(before_eq)
    if len(names) < 2:
        return ""
    return names[-1]


def _scan_error_codes(raw: str, rel: str, index: CodeIndex) -> None:
    """登记文件里出现的错误码（与判定端共用 utils.ERROR_CODE_RE）。"""
    for m in ERROR_CODE_RE.finditer(raw):
        code = m.group(1)
        index.add_error_code(code, {"file": rel, "line": pos_to_line(raw, m.start())})


# ---------------------------------------------------------------- XML 解析

def parse_mapper_xml(path: Path, rel: str, index: CodeIndex) -> None:
    """解析 MyBatis 映射文件，登记语句 id。"""
    raw = read_text(path)
    ns_match = _XML_NS_RE.search(raw)
    namespace = ns_match.group(1) if ns_match else ""
    for m in _XML_STMT_RE.finditer(raw):
        kind = m.group(1).lower()
        stmt_id = m.group(2)
        index.add_xml_statement(
            stmt_id,
            {
                "file": rel,
                "line": pos_to_line(raw, m.start()),
                "kind": kind,
                "namespace": namespace,
            },
        )


# ---------------------------------------------------------------- 入口

def build_index(
    roots: Iterable[str | Path],
    base: Path | None = None,
    progress=None,
    skip_dirs: Iterable[str] = ("target", "build", "out", "node_modules", ".git", "test"),
) -> CodeIndex:
    """扫描代码库，建索引。

    ``base`` 用于把绝对路径换成相对路径，让报告在任何机器上都一样。
    """
    root_list = list(roots)
    index = CodeIndex(root_list)

    java_files = collect_code_files(root_list, CODE_EXTS, skip_dirs)
    xml_files = collect_code_files(root_list, XML_EXTS, skip_dirs)

    total = len(java_files) + len(xml_files)

    for n, path in enumerate(java_files, start=1):
        rel = _rel(path, base)
        index.files.append(rel)
        try:
            parse_java_file(path, rel, index)
        except (OSError, UnicodeDecodeError):
            # 单个文件解析失败不该拖垮整轮扫描，跳过并继续
            continue
        if progress and n % 200 == 0:
            progress.tick(f"{n}/{total} {path.name}")

    for n, path in enumerate(xml_files, start=1):
        rel = _rel(path, base)
        index.files.append(rel)
        try:
            parse_mapper_xml(path, rel, index)
        except (OSError, UnicodeDecodeError):
            continue

    return index


def _rel(path: Path, base: Path | None) -> str:
    if base is None:
        return path.name
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def index_summary(index: CodeIndex) -> str:
    """给进度显示用的一句话摘要。"""
    return (
        f"{len(index.files)} 个文件"
        f"，{len(index.types)} 个类型"
        f"，{len(index.methods)} 个方法"
        f"，{len(index.urls)} 个接口"
    )
