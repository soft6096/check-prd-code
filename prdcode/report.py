"""把结论渲染成两份文档。

为什么是两份：

  01-待修复  证据齐全，可以整份丢给 AI 直接照着改
  02-待确认  拿不准的，必须人先看一眼

这两份的边界，就是这套工具可信度的边界。判不准的东西混进 01，
下游 AI 就会照着错报告把本来正确的代码改坏。
所以宁可少写，不可写错。
"""

from __future__ import annotations

import random
from pathlib import Path

from .tasks import classify
from .utils import read_text, write_text

FIX_NAME = "01-待修复.md"
CONFIRM_NAME = "02-待确认.md"

# 抽查抽查几条
SAMPLE_SIZE = 6


# ---------------------------------------------------------------- 判定取舍

def is_fix(record: dict) -> bool:
    """能不能进「待修复」。口径统一在 tasks.classify，这里不另立一套。"""
    return classify(record) == "fix"


def is_confirm(record: dict) -> bool:
    """要不要进「待确认」：既不是"确定没问题"，也不是"确定要改"。"""
    if record.get("verdict") == "implemented":
        return False
    return not is_fix(record)


def is_ok(record: dict) -> bool:
    return record.get("verdict") == "implemented"


# ---------------------------------------------------------------- 主入口

def render_reports(
    records: list[dict],
    extras: list[dict],
    out_dir: Path,
    code_root: Path,
    sample_size: int = SAMPLE_SIZE,
) -> tuple[Path, Path]:
    """生成两份报告，返回它们的路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    missing = [r for r in records if r["verdict"] == "missing" and is_fix(r)]
    deviated = [r for r in records if r["verdict"] == "deviated" and is_fix(r)]
    confirms = [r for r in records if is_confirm(r)]
    ok = [r for r in records if is_ok(r)]

    fix_path = out_dir / FIX_NAME
    confirm_path = out_dir / CONFIRM_NAME

    write_text(
        fix_path,
        _render_fix(missing, deviated, ok, records, code_root, sample_size),
    )
    write_text(
        confirm_path,
        _render_confirm(confirms, extras, code_root),
    )
    return fix_path, confirm_path


# ---------------------------------------------------------------- 01 待修复

def _render_fix(
    missing: list[dict],
    deviated: list[dict],
    ok: list[dict],
    records: list[dict],
    code_root: Path,
    sample_size: int,
) -> str:
    out: list[str] = []
    out.append("# 待修复")
    out.append("")
    out.append("这份是核实过、可以直接改的。每条都给了需求出处和代码位置，照着改就行。")
    out.append("")
    out.append("改完重跑一次核对，这里的条目应该会消失。如果没消失，说明改的位置不对。")
    out.append("")

    total = len(missing) + len(deviated)
    if total:
        out.append(
            f"一共 {total} 条，"
            f"{len(missing)} 条是需求没做的，"
            f"{len(deviated)} 条是做的跟需求对不上的。"
        )
    else:
        out.append("这一轮没有需要直接修复的条目。")
    out.append("")

    if missing:
        out.append("---")
        out.append("")
        out.append("## 一、需求没做的")
        out.append("")
        for i, r in enumerate(missing, start=1):
            out.append(_render_missing(i, r, code_root))
            out.append("")

    if deviated:
        out.append("---")
        out.append("")
        out.append("## 二、做的跟需求对不上的")
        out.append("")
        for i, r in enumerate(deviated, start=1):
            out.append(_render_deviated(i, r))
            out.append("")

    sample = _render_sample(ok, code_root, sample_size)
    if sample:
        out.append("---")
        out.append("")
        out.append(sample)

    out.append("---")
    out.append("")
    out.append(_render_limits(records))

    return "\n".join(out).rstrip() + "\n"


def _render_missing(index: int, r: dict, code_root: Path) -> str:
    lines: list[str] = []
    lines.append(f"### {index}. {r['assertion']}")
    lines.append("")
    lines.append(f"需求出处：{_src_ref(r)}")
    lines.append("")

    if str(r.get("checked_by", "")) == "脚本":
        # 脚本判的：把搜查记录摆出来，人才有办法复核它搜得对不对
        log = r.get("search_log") or {}
        scope = "、".join(log.get("scope", []) or [])
        file_count = log.get("file_count", 0)
        keywords = "、".join(f"`{k}`" for k in (log.get("keywords") or [])[:8])

        sentence = "代码里没搜到相关实现。"
        if scope or file_count:
            sentence += f"搜查范围：{scope or '本项目'}（{file_count} 个文件）"
        if keywords:
            sentence += f"，搜过：{keywords}"
        lines.append(sentence + "。")
        lines.append("")

        nearest = _nearest_from_checks(r)
        if nearest:
            lines.append(f"最接近的实现是 {nearest}。")
            lines.append("")
    else:
        # 模型判的：附上它的依据和相关代码，人好核对
        if r.get("reason"):
            lines.append(r["reason"] + "。")
            lines.append("")
        snippet = _snippet_from_evidence(r, code_root)
        if snippet:
            lines.append(f"{_pos_ref(r)} 处的原文：")
            lines.append("")
            lines.append("```java")
            lines.append(snippet)
            lines.append("```")
            lines.append("")

    if r.get("missing"):
        lack = "；".join(str(m) for m in r["missing"])
        lines.append(f"缺的是：{lack}。")
        lines.append("")

    lines.append(f"**要改的地方**：{_pos_ref(r) or '（需要人工定位）'}")
    return "\n".join(lines)


def _render_deviated(index: int, r: dict) -> str:
    lines: list[str] = []
    lines.append(f"### {index}. {r['assertion']}")
    lines.append("")
    lines.append(f"需求出处：{_src_ref(r)}")
    lines.append("")

    if r.get("reason"):
        lines.append(f"代码里是这么做的：{r['reason']}")
        lines.append("")

    quote = _first_quote(r)
    if quote:
        pos = _pos_ref(r)
        lines.append(f"{pos} 处的原文：")
        lines.append("")
        lines.append("```java")
        lines.append(quote)
        lines.append("```")
        lines.append("")

    if r.get("missing"):
        lack = "；".join(str(m) for m in r["missing"])
        lines.append(f"缺的是：{lack}。")
        lines.append("")

    lines.append(f"**要改的地方**：{_pos_ref(r) or '（需要人工定位）'}")
    return "\n".join(lines)


def _render_sample(ok: list[dict], code_root: Path, size: int) -> str:
    """随机抽几条"判定为已实现"的，把代码原文摆出来让人自己验。"""
    candidates = [r for r in ok if r.get("evidence")]
    if not candidates:
        return ""
    sample = random.sample(candidates, min(size, len(candidates)))

    lines = [
        "## 抽查",
        "",
        "下面几行是随机抽出来的、判定为「已经实现」的条目，附上需求原话和代码实际内容。",
        "如果这里出现对不上的，说明这份报告不可信，那就先别照着改。",
        "",
    ]
    for r in sample:
        src = r.get("source") or {}
        lines.append("```text")
        lines.append(f"需求 {src.get('file', '')}:{src.get('line', '')}   {r['assertion']}")
        for ev in r["evidence"][:1]:
            lines.append(f"代码 {ev.get('file', '')}:{ev.get('line', '')}")
            snippet = _evidence_text(ev, code_root)
            for row in snippet.splitlines()[:4]:
                lines.append(f"     {row.strip()}")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _render_limits(records: list[dict]) -> str:
    unverified = [
        r for r in records
        if str(r.get("checked_by", "")).startswith("AI") and r.get("verify_problems")
    ]
    pending = [r for r in records if r.get("checked_by") == "待处理"]

    lines = [
        "## 这次没查什么",
        "",
        "- **业务逻辑类的 bug**（算法写错、并发问题、事务失效这类）——工具查不出来，"
        "得跑测试或者人看",
        "- 第三方依赖的源码不在这个工程里，没法核对",
        "- 需求文档里没写到的功能，不算「没做」，所以不会出现在这份清单里；"
        f"代码里多出来的东西另见 `{CONFIRM_NAME}`",
    ]
    if unverified:
        lines.append(
            f"- 有 {len(unverified)} 条 AI 判定没通过引用校验，已全部转入 `{CONFIRM_NAME}`"
        )
    if pending:
        lines.append(
            f"- 有 {len(pending)} 条还没被 AI 处理（任务包未回填），"
            f"已全部转入 `{CONFIRM_NAME}`"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 02 待确认

def _render_confirm(
    confirms: list[dict],
    extras: list[dict],
    code_root: Path,
) -> str:
    out: list[str] = []
    out.append("# 待确认")
    out.append("")
    out.append("这些地方我拿不准，得你定一下。分两种：一种代码里有、需求里没提；"
               "另一种是判不准或者有歧义的。")
    out.append("")
    out.append("**两种用法，挑一个**：")
    out.append("")
    out.append("- 让 AI 读这份文档，它会一条条弹给你选")
    out.append("- 或者你自己在下面勾，勾完把文档扔给 AI，它照着你的选择去改")
    out.append("")
    out.append(f"一共 {len(extras) + len(confirms)} 条。")
    out.append("")

    # 哪一节有内容才编号，避免出现只有"二"没有"一"
    section = 0
    if extras:
        section += 1
        out.append("---")
        out.append("")
        out.append(f"## {_cn(section)}、代码里有，需求里没提的")
        out.append("")
        out.append("> 只列对外能看到的东西：接口这类。")
        out.append("> 内部的工具类、日志、配置这些不算多余，没往里放。")
        out.append("")
        for i, e in enumerate(extras, start=1):
            out.append(_render_extra(i, e))
            out.append("")

    if confirms:
        section += 1
        out.append("---")
        out.append("")
        out.append(f"## {_cn(section)}、判不准的，要你定一下")
        out.append("")
        for i, r in enumerate(confirms, start=1):
            out.append(_render_confirm_item(i, r, code_root))
            out.append("")

    out.append("---")
    out.append("")
    out.append("## 勾完怎么办")
    out.append("")
    out.append("勾完直接把这整份文档给 AI 就行，它能看懂你勾了哪些，按你的选择去改。")
    out.append("")
    out.append("「需求漏写」和「保留」这两类不会产生代码改动，AI 会自动跳过。")
    out.append("标了「删掉」和「改代码」的，AI 会动手。")

    return "\n".join(out).rstrip() + "\n"


def _render_extra(index: int, e: dict) -> str:
    lines = [
        f"### {index}. {e['http']} {e['path']}",
        "",
        f"`{e['file']}:{e['line']}` 上有这个接口（处理方法 `{e['handler']}`），"
        "需求文档里没找到出处。",
        "",
        "- [ ] 需求本来就不需要 → 删掉",
        "- [ ] 需求漏写了 → 我去补需求文档，这条不用管",
        "- [ ] 是通用能力 → 保留，以后别再报出来",
        "",
        "处理人 / 日期：",
    ]
    return "\n".join(lines)


def _render_confirm_item(index: int, r: dict, code_root: Path) -> str:
    lines = [f"### {index}. {r['assertion']}", ""]
    lines.append(f"需求出处：{_src_ref(r)}")
    lines.append("")

    bounced = bool(r.get("verify_problems"))
    if r.get("checked_by") == "待处理":
        lines.append("这条还没被 AI 处理（任务包里的结果没回填），所以判不了。")
    elif bounced:
        lines.append("AI 给的判定没通过引用校验，所以不敢采信：")
        lines.append("")
        for p in r["verify_problems"][:3]:
            lines.append(f"- {p}")
    elif r.get("reason"):
        lines.append(r["reason"])
    lines.append("")

    # 引用没通过校验时，就别再展示那句对不上的引用了，
    # 直接把文件里真实的内容摆出来，人一眼能看出问题在哪
    snippet = _snippet_from_evidence(r, code_root, prefer_file=bounced)
    if snippet:
        label = "文件里的真实内容" if bounced else "处的原文"
        pos = _pos_ref(r)
        lines.append(f"{pos} {label}：")
        lines.append("")
        lines.append("```java")
        lines.append(snippet)
        lines.append("```")
        lines.append("")

    lines.extend(
        [
            "你定一下：",
            "",
            "- [ ] 其实已经做了 → 标为已实现，不用改",
            "- [ ] 确实没做 → 排期补上",
            "- [ ] 做得不对 → 按需求改",
            "- [ ] 需求本身要改 → 我去改需求文档",
            "- [ ] 先放着，暂不处理",
            "",
            "处理人 / 日期：",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 小工具

_CN_NUM = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def _cn(n: int) -> str:
    return _CN_NUM[n - 1] if 1 <= n <= len(_CN_NUM) else str(n)


def _snippet_from_evidence(
    r: dict, code_root: Path, prefer_file: bool = False
) -> str:
    """取一段代码原文。

    ``prefer_file`` 为真时强制从文件读——用在"引用没通过校验"的场合，
    这时候模型那句话本身就不作数了，得把文件里真实长什么样摆出来。
    """
    for ev in r.get("evidence") or []:
        if not ev.get("file"):
            continue
        if not prefer_file:
            quote = (ev.get("quote") or "").strip()
            if quote:
                return quote
        path = code_root / ev["file"]
        if not path.exists():
            continue
        lines = read_text(path).splitlines()
        try:
            line = int(ev.get("line") or 1)
        except (TypeError, ValueError):
            line = 1
        start = max(1, min(line, len(lines) or 1))
        end = min(len(lines), start + 3)
        return "\n".join(lines[start - 1 : end])
    return ""


def _src_ref(r: dict) -> str:
    src = r.get("source") or {}
    if src.get("file"):
        return f"`{src['file']}:{src.get('line', '')}`"
    return "（需求出处缺失）"


def _pos_ref(r: dict) -> str:
    for ev in r.get("evidence") or []:
        if ev.get("file"):
            return f"`{ev['file']}:{ev.get('line', '')}`"
    return ""


def _first_quote(r: dict) -> str:
    for ev in r.get("evidence") or []:
        q = (ev.get("quote") or "").strip()
        if q:
            return q
    return ""


def _evidence_text(ev: dict, code_root: Path) -> str:
    quote = (ev.get("quote") or "").strip()
    if quote:
        return quote
    path = code_root / (ev.get("file") or "")
    if not path.exists():
        return ""
    text = read_text(path)
    lines = text.splitlines()
    line = int(ev.get("line") or 1)
    start = max(1, line - 1)
    end = min(len(lines), start + 3)
    return "\n".join(lines[start - 1 : end])


def _nearest_from_checks(r: dict) -> str:
    """从搜查记录里挑一个"最接近的实现"，让报告更可读。"""
    for chk in r.get("checks") or []:
        for near in chk.get("nearest") or []:
            if near.get("file"):
                return f"`{near['file']}:{near.get('line', '')}`（{near.get('name', '')}）"
    return ""
