"""扫描代码库，建立符号索引。

索引是整个工具的底座：**"在代码里搜不到"这个结论准不准，全看这里。**

所以这里只干一件事——把"代码里有什么"如实记下来：
哪个文件、第几行、什么类型、什么方法、什么接口、什么字段、什么枚举值。
不做任何判断，判断在后面的模块里做。

Java 结构解析交给 javasrc（自带词法分析器，不再用正则套原文）；
本模块负责把解析结果装进索引，外加 MyBatis XML 的语句 id。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from .javasrc import MAPPING_ANNOTATIONS, parse_java
from .utils import error_code_regex, pos_to_line, read_json, read_text, write_json

CODE_EXTS = (".java",)
XML_EXTS = (".xml",)

# MyBatis XML
_XML_NS_RE = re.compile(r"<mapper[^>]*\bnamespace\s*=\s*\"([^\"]+)\"")
_XML_STMT_RE = re.compile(
    r"<(select|insert|update|delete)\b[^>]*\bid\s*=\s*\"([^\"]+)\"", re.I
)


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
    syms = parse_java(raw)
    owner_bases = {t["name"]: (t["base_paths"] or [""]) for t in syms.types}

    for t in syms.types:
        index.add_type(
            t["name"],
            {"file": rel, "line": t["line"], "kind": t["kind"], "name": t["name"]},
        )
    for m in syms.methods:
        index.add_method(
            m["name"],
            {
                "file": rel,
                "line": m["line"],
                "end_line": m["end_line"],
                "owner": m["owner"],
            },
        )
        _register_urls(index, m, owner_bases.get(m["owner"]) or [""], rel)
    for f in syms.fields:
        index.add_field(
            f["name"], {"file": rel, "line": f["line"], "owner": f["owner"]}
        )
    for e in syms.enum_constants:
        index.add_enum_constant(
            e["name"], {"file": rel, "line": e["line"], "owner": e["owner"]}
        )

    _scan_error_codes(raw, rel, index)


def _register_urls(
    index: CodeIndex, method: dict, bases: list[str], rel: str
) -> None:
    """把方法的映射注解登记成接口：类上多个基础路径 × 方法上多个子路径。"""
    for ann, args in method.get("annotations", []):
        if ann not in MAPPING_ANNOTATIONS:
            continue
        http = MAPPING_ANNOTATIONS[ann]
        sub_paths = [a for a in args if a.startswith("/")] or [""]
        for base in bases or [""]:
            for sub in sub_paths:
                full = _join_path(base, sub)
                key = f"{http or 'ANY'} {full}"
                index.add_url(
                    key,
                    {
                        "file": rel,
                        "line": method["line"],
                        "handler": method["name"],
                        "owner": method["owner"],
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


def _scan_error_codes(raw: str, rel: str, index: CodeIndex) -> None:
    """登记文件里出现的错误码（与判定端共用 utils 的错误码规则）。"""
    for m in error_code_regex().finditer(raw):
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


def source_fingerprint(
    roots: Iterable[str | Path],
    base: Path | None = None,
    skip_dirs: Iterable[str] = ("target", "build", "out", "node_modules", ".git", "test"),
) -> str:
    """把参与索引的源码文件的 (相对路径, mtime, size) 拼成一个指纹。

    指纹没变，说明源码一个字都没动，可以直接复用上次的索引。
    """
    files = collect_code_files(roots, CODE_EXTS, skip_dirs) + collect_code_files(
        roots, XML_EXTS, skip_dirs
    )
    parts: list[str] = []
    for path in sorted(files, key=lambda p: str(p)):
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{_rel(path, base)}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def build_index_cached(
    roots: Iterable[str | Path],
    base: Path | None = None,
    cache_path: str | Path | None = None,
    progress=None,
    skip_dirs: Iterable[str] = ("target", "build", "out", "node_modules", ".git", "test"),
) -> tuple[CodeIndex, bool]:
    """带缓存的建索引：源码指纹没变就复用 ``cache_path`` 里的旧索引。

    返回 ``(索引, 是否命中缓存)``。指纹对不上就重扫并刷新缓存。
    """
    fingerprint = source_fingerprint(roots, base, skip_dirs)
    if cache_path is not None and Path(cache_path).exists():
        data = read_json(cache_path)
        if (
            isinstance(data, dict)
            and data.get("fingerprint") == fingerprint
            and isinstance(data.get("index"), dict)
        ):
            return CodeIndex.from_dict(data["index"]), True

    index = build_index(roots, base=base, progress=progress, skip_dirs=skip_dirs)
    if cache_path is not None:
        write_json(cache_path, {"fingerprint": fingerprint, "index": index.to_dict()})
    return index, False


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
