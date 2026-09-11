"""AI 任务包：生成 → 回收 → 验真。

这是整个流程里唯一需要大模型出手的一环。规矩定得死一点：

  1. **任务包自包含** —— 条目、出处、候选位置、相关代码，全放在一个文件里，
     模型不用回头翻库，也就没机会瞎猜
  2. **必须逐字引用代码** —— 脚本回头逐字校验，引用对不上，判定直接丢弃
  3. **宁可少报，不许猜** —— 判不了就写 undecidable，进"待确认"

第 2 条是关键：引用必须逐字，模型就编不出不存在的代码。
"""

from __future__ import annotations

from pathlib import Path

from . import recall
from .codeindex import CodeIndex
from .utils import norm_ws, read_json, read_text, resolve_within, write_json, write_text

TASK_DIR_NAME = "tasks"
RESULT_DIR_NAME = "results"

# 每个批次最多放几条需求
DEFAULT_BATCH_SIZE = 8
# 每个批次最多放多少行代码（防止任务包太长，模型注意力被稀释）
DEFAULT_MAX_CODE_LINES = 600
# 召回时每条需求取多少行上下文
DEFAULT_CONTEXT_LINES = 45


# ---------------------------------------------------------------- 召回

def recall_for_group(
    items: list[dict],
    static_by_id: dict[str, dict],
    index: CodeIndex,
    code_root: Path,
    base: Path | None,
    context: int = DEFAULT_CONTEXT_LINES,
    max_files: int = 5,
    corpus: "recall.RecallIndex | None" = None,
) -> list[dict]:
    """给一组需求召回相关代码。

    按"组"召回而不是按"条"召回：同一个功能组里的需求，代码位置基本挨着，
    而且有些需求是纯中文的行为描述、抽不出任何标识符，只能蹭同组的上下文。
    """
    anchors: list[tuple[str, int]] = []

    for item in items:
        st = static_by_id.get(item.get("id", ""), {})
        for ev in st.get("evidence", []) or []:
            if ev.get("file"):
                anchors.append((ev["file"], int(ev.get("line") or 1)))
        for chk in st.get("checks", []) or []:
            for h in chk.get("hits", []) or []:
                if h.get("file"):
                    anchors.append((h["file"], int(h.get("line") or 1)))
                # 接口命中后，跟着处理方法名把实现也捞出来。
                # Controller 只是入口，真正的逻辑在 Service 里，
                # 不跟这一下，行为类需求就只能看到一层壳。
                handler = h.get("handler")
                if handler:
                    for impl in index.find_method(handler):
                        if impl.get("file"):
                            anchors.append((impl["file"], int(impl.get("line") or 1)))

    # 按文件聚合，每个文件取锚点最集中的一小段
    by_file: dict[str, list[int]] = {}
    for file, line in anchors:
        by_file.setdefault(file, []).append(line)

    snippets: list[dict] = []
    for file, lines in list(by_file.items())[:max_files]:
        line = min(lines)
        snippets.append(_read_snippet(code_root, file, line, context))

    # 锚点不够时，用 BM25 按"需求文字 ↔ 代码注释/标识符"补召回。
    # 纯中文需求抽不出标识符，这一步是关键兜底。
    if corpus is not None and len(snippets) < max_files:
        seen = {s["file"] for s in snippets}
        query = " ".join(
            f"{it.get('assertion', '')} {it.get('text', '')}" for it in items
        )
        for hit in corpus.search(query, top_k=max_files, min_ratio=0.3):
            if hit["file"] in seen:
                continue
            snippets.append(_read_snippet(code_root, hit["file"], hit["line"], context))
            seen.add(hit["file"])
            if len(snippets) >= max_files:
                break

    return [s for s in snippets if s["code"]]


def _read_snippet(code_root: Path, rel: str, center: int, context: int) -> dict:
    path = resolve_within(code_root, rel)
    if path is None or not path.exists():
        return {"file": rel, "start": 0, "end": 0, "code": ""}
    text = read_text(path)
    lines = text.splitlines()
    start = max(1, center - context // 2)
    end = min(len(lines), start + context)
    start = max(1, end - context)
    body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
    return {"file": rel, "start": start, "end": end, "code": body}


# ---------------------------------------------------------------- 任务包

def build_task_packages(
    work_dir: Path,
    items: list[dict],
    static_results: list[dict],
    index: CodeIndex,
    code_root: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    work_label: str = ".checkprd/compare",
) -> list[Path]:
    """把需要 AI 判定的需求切成若干任务包。"""
    static_by_id = {r["item_id"]: r for r in static_results}
    todo = [it for it in items if static_by_id.get(it.get("id", ""), {}).get("route") == "ai"]
    if not todo:
        return []

    # 按功能组切，保证同一组的条目在同一批里（共享上下文）
    groups: dict[str, list[dict]] = {}
    for item in todo:
        key = item.get("group") or "未分组"
        groups.setdefault(key, []).append(item)

    batches: list[list[dict]] = []
    for _, group_items in groups.items():
        for i in range(0, len(group_items), batch_size):
            batches.append(group_items[i : i + batch_size])

    task_dir = work_dir / TASK_DIR_NAME
    task_dir.mkdir(parents=True, exist_ok=True)
    corpus = recall.RecallIndex.from_code_index(index)

    written: list[Path] = []
    for n, batch in enumerate(batches, start=1):
        snippets = recall_for_group(
            batch, static_by_id, index, code_root, None, corpus=corpus
        )
        used = 0
        kept: list[dict] = []
        for s in snippets:
            cost = s["code"].count("\n") + 1
            if used + cost > max_code_lines and kept:
                break
            kept.append(s)
            used += cost

        group_evidence: list[dict] = []
        for it in batch:
            group_evidence.extend(
                static_by_id.get(it.get("id", ""), {}).get("evidence") or []
            )

        name = f"batch-{n:03d}"
        content = _render_task(
            name, batch, static_by_id, kept, work_label, group_evidence
        )
        path = task_dir / f"{name}.md"
        write_text(path, content)

        # 同步写一份模板 JSON，方便模型直接填。
        # 已经填过的不覆盖 —— 重跑 compare 时不该把人家写好的判定冲掉。
        json_path = task_dir / f"{name}.json"
        if not json_path.exists():
            write_json(
                json_path,
                [
                    {
                        "item_id": it["id"],
                        "verdict": "",
                        "evidence": [],
                        "reason": "",
                        "missing": [],
                    }
                    for it in batch
                ],
            )
        written.append(path)

    return written


def _render_task(
    name: str,
    batch: list[dict],
    static_by_id: dict[str, dict],
    snippets: list[dict],
    work_label: str = ".checkprd/compare",
    group_evidence: list[dict] | None = None,
) -> str:
    out: list[str] = []
    out.append(f"# 任务包 {name}")
    out.append("")
    out.append(f"请判断下面 {len(batch)} 条需求，在代码里到底实现了没有。")
    out.append("")
    out.append("## 输出要求")
    out.append("")
    out.append("把结果写成一个 JSON 数组，保存到：")
    out.append("")
    out.append(f"    {work_label}/tasks/{name}.json")
    out.append("")
    out.append("每条的样子：")
    out.append("")
    out.append("```json")
    out.append("{")
    out.append('  "item_id": "R-007",')
    out.append('  "verdict": "implemented | partial | missing | deviated | undecidable",')
    out.append('  "evidence": [')
    out.append('    {"file": "src/main/java/.../RefundServiceImpl.java", "line": 1415,')
    out.append('     "quote": "从那一行开始逐字复制的代码原文"}')
    out.append("  ],")
    out.append('  "reason": "一句话说清依据",')
    out.append('  "missing": ["具体缺哪一段"]')
    out.append("}")
    out.append("```")
    out.append("")
    out.append("**四条硬规矩：**")
    out.append("")
    out.append("1. `quote` 必须是从代码里**逐字复制**的原文，不要改写、不要概括。")
    out.append("   脚本会逐字校验，对不上的判定会被直接丢掉。")
    out.append("2. `quote` 里不要带行号前缀，只写代码本身。")
    out.append("3. 找不到就是找不到，老老实实写 `missing`，不要猜。")
    out.append("4. 拿不准就写 `undecidable`，它会进「待确认」，不丢人。")
    out.append("")
    out.append("## 待判定条目")
    out.append("")
    for item in batch:
        st = static_by_id.get(item.get("id", ""), {})
        out.append(f"### {item['id']}")
        out.append("")
        out.append(f"需求：{item.get('assertion', '')}")
        src = item.get("source") or {}
        if src:
            out.append(f"出处：`{src.get('file', '')}:{src.get('line', '')}`")
        if item.get("note"):
            out.append(f"补充：{item['note']}")
        cands = _dedupe_positions(st.get("evidence") or [])
        if not cands:
            # 本条抽不出标识符时，借同组的线索用一用，总比什么都不给强
            cands = _dedupe_positions(group_evidence or [])
        if cands:
            out.append("脚本找到的相关位置：" + "、".join(f"`{p}`" for p in cands[:5]))
        else:
            out.append("脚本没找到明显相关的代码位置，需要你在下面给的代码里找。")
        out.append("")

    out.append("## 相关代码")
    out.append("")
    if not snippets:
        out.append("> 没有召回到相关代码。这种情况请把该条判为 `undecidable`，")
        out.append("> 并在 reason 里说明「没找到相关代码」，不要凭印象下结论。")
        out.append("")
    for s in snippets:
        out.append(f"### {s['file']}:{s['start']}-{s['end']}")
        out.append("")
        out.append("```java")
        out.append(s["code"])
        out.append("```")
        out.append("")

    return "\n".join(out)


def _dedupe_positions(evidence: list[dict], limit: int = 5) -> list[str]:
    """把位置去重，保持出现顺序。"""
    seen: list[str] = []
    for ev in evidence:
        file = ev.get("file")
        if not file:
            continue
        pos = f"{file}:{ev.get('line', '')}"
        if pos not in seen:
            seen.append(pos)
        if len(seen) >= limit:
            break
    return seen


# ---------------------------------------------------------------- 回收

def load_results(work_dir: Path) -> dict[str, dict]:
    """读回模型写的判定结果，按 item_id 汇总。"""
    result_dir = work_dir / TASK_DIR_NAME
    merged: dict[str, dict] = {}
    if not result_dir.exists():
        return merged

    for path in sorted(result_dir.glob("batch-*.json")):
        data = read_json(path)
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            item_id = row.get("item_id")
            verdict = (row.get("verdict") or "").strip()
            if not item_id or not verdict:
                # 模板里留空的条目说明模型没处理，跳过
                continue
            row["_batch"] = path.name
            merged[item_id] = row
    return merged


# ---------------------------------------------------------------- 验真

def verify_evidence(evidence: list[dict], code_root: Path) -> list[str]:
    """逐字校验模型引用的代码。

    返回问题清单。空清单表示全部通过。
    """
    problems: list[str] = []
    if not evidence:
        return ["没有给出任何代码引用"]

    for ev in evidence:
        file = (ev.get("file") or "").strip()
        line = ev.get("line") or 0
        quote = (ev.get("quote") or "").strip()

        if not file:
            problems.append("引用缺少文件名")
            continue
        if not quote:
            problems.append(f"{file}:{line} 的引用是空的")
            continue

        target = resolve_within(code_root, file)
        if target is None:
            problems.append(f"引用的路径越界：{file}")
            continue
        if not target.exists():
            problems.append(f"引用的文件不存在：{file}")
            continue

        text = read_text(target)
        lines = text.splitlines()
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = 1
        if line < 1 or line > len(lines):
            problems.append(f"引用的行号越界：{file}:{line}（文件共 {len(lines)} 行）")
            continue

        window = "\n".join(lines[max(0, line - 1) : line - 1 + 25])
        if not _quote_matches(quote, window):
            problems.append(
                f"引用对不上原文：{file}:{line} —— 代码里没有找到「{_clip(quote)}」"
            )

    return problems


def _quote_matches(quote: str, window: str) -> bool:
    """比对引用是否真实存在。

    三级比对，由严到宽；一旦命中即通过：

    1. 原样包含；
    2. 空白归一化后包含（容忍缩进/换行差异）；
    3. 多行引用逐行比对 —— 要求**每一行**都能在窗口里找到。

    第 3 条不能放宽成"命中任意一行即可"：那等于允许模型拿一行真代码夹带
    若干编造的行来充数，验真会形同虚设。此处由回归测试锁死，勿改回。
    """
    if quote in window:
        return True
    if norm_ws(quote) in norm_ws(window):
        return True
    lines = [ln.strip() for ln in quote.splitlines() if ln.strip()]
    return len(lines) >= 2 and all(ln in window for ln in lines)


def _clip(text: str, limit: int = 60) -> str:
    flat = norm_ws(text)
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------- 合并

def merge_results(
    items: list[dict],
    static_results: list[dict],
    ai_results: dict[str, dict],
    code_root: Path,
) -> list[dict]:
    """把静态判定和 AI 判定合成一份最终结论。"""
    static_by_id = {r["item_id"]: r for r in static_results}
    merged: list[dict] = []

    for item in items:
        item_id = item.get("id", "")
        st = static_by_id.get(item_id, {})
        route = st.get("route", "ai")

        record = {
            "item_id": item_id,
            "group": item.get("group", ""),
            "assertion": item.get("assertion", ""),
            "source": item.get("source", {}),
            "kind": item.get("kind", ""),
            "route": route,
            "search_log": st.get("search_log", {}),
        }

        if route == "static":
            record.update(
                {
                    "verdict": st.get("verdict", "undecidable"),
                    "evidence": st.get("evidence", []),
                    # 带上 checks：报告里"最接近的实现是…"要从中取候选位置，
                    # 不带的话这段提示永远是空的。
                    "checks": st.get("checks", []),
                    "reason": st.get("reason", ""),
                    "missing": [],
                    "checked_by": "脚本",
                }
            )
            merged.append(record)
            continue

        ai = ai_results.get(item_id)
        if not ai:
            record.update(
                {
                    "verdict": "undecidable",
                    "evidence": [],
                    "reason": "AI 还没处理到这一条（任务包未回填）",
                    "missing": [],
                    "checked_by": "待处理",
                }
            )
            merged.append(record)
            continue

        evidence = ai.get("evidence") or []
        verdict = (ai.get("verdict") or "undecidable").strip()
        # 只有"声称代码里有什么"的判定才需要拿引用对质。
        # 判「没做」和「判不了」本来就没有代码可引，不该逼模型编一段出来交差。
        need_evidence = verdict in ("implemented", "partial", "deviated")
        problems = verify_evidence(evidence, code_root) if need_evidence else []

        if problems:
            record.update(
                {
                    "verdict": "undecidable",
                    "evidence": evidence,
                    "reason": "引用没通过校验：" + "；".join(problems[:2]),
                    "missing": ai.get("missing", []),
                    "checked_by": "AI（已打回）",
                    "verify_problems": problems,
                }
            )
        else:
            record.update(
                {
                    "verdict": verdict,
                    "evidence": evidence,
                    "reason": ai.get("reason", ""),
                    "missing": ai.get("missing", []),
                    "checked_by": "AI（已验真）",
                }
            )
        merged.append(record)

    return merged


# ---------------------------------------------------------------- 分类

def classify(record: dict) -> str:
    """决定一条记录去哪个报告：``fix`` / ``confirm`` / ``ok``。

    进「待修复」意味着可以整份丢给 AI 直接改，所以门槛要高一点：
    脚本判的算硬结论；模型判的得看它有没有给出可核验的东西。

    对抗复核（recheck）会给记录写入 ``forced_placement``，那是复核后的最终归属，
    优先级高于这里的一切推断，必须最先认。
    """
    forced = record.get("forced_placement")
    if forced in ("ok", "fix", "confirm"):
        return forced

    verdict = record.get("verdict", "")
    by = str(record.get("checked_by", ""))

    if verdict == "implemented":
        return "ok"

    if verdict == "missing":
        # 脚本说「搜不到」是硬结论；模型说「没做」得看它有没有指到相关位置
        if by == "脚本" or record.get("evidence"):
            return "fix"
        return "confirm"

    if verdict == "deviated":
        if by == "脚本" or "已验真" in by:
            return "fix"
        return "confirm"

    return "confirm"
