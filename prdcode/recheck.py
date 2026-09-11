"""对抗复核：对第一遍判「已实现 / 已偏离」的结论做一次独立的推翻式复核。

第一遍判定只证明了"引用的代码真实存在"，没证明"这段代码正好是需求说的那件事"。
于是留下一个漏洞：模型判 `implemented` 时引用一句放到哪都成立的代码
（如 ``return Result.ok();``），照样能通过逐字验真。

本模块补上这一环：

  1. 把第一遍判「已实现 / 已偏离」、引用已验真、且是走行为路线的条目挑出来；
  2. 连同脚本能给的代码窗口，打包成任务包，交给**另一个强推理模型**逐条尝试推翻；
  3. 复核模型只能引用脚本给过的代码窗口，引用一旦越界就算无效；
  4. 复核不通（refuted / partial / unverifiable）的条目降级到「待确认」。

这一步是**可选**的：不跑 verify，后面的流程与以前完全一致。
"""

from __future__ import annotations

from pathlib import Path

from . import recall, tasks
from .codeindex import CodeIndex
from .utils import read_json, read_text, resolve_within

# 对抗复核产物的阶段目录名（对应 ``.checkprd/verify``）
RECHECK_DIR_NAME = "verify"
# 每个复核批次最多放几条（默认比第一遍更小：复核要逐条深挖，批次越小越好）
DEFAULT_RECHECK_BATCH = 5
# 每批最多给多少行代码（防止上下文过长，模型注意力被稀释）
DEFAULT_RECHECK_MAX_LINES = 900
# 方法窗口：以引用行为中心，向上游/下游各留出的上下文行数
METHOD_HEAD = 100
METHOD_TAIL = 120
# 找不到所在方法时，退化为引用行附近固定长度的窗口
_FALLBACK_WINDOW = 25

# 复核模型可能给出的四种结论
_SECOND_VERDICTS = ("confirmed", "refuted", "partial", "unverifiable")
# 需要给出代码引用才作数的结论
_NEED_EVIDENCE = ("confirmed", "refuted", "partial")


# ---------------------------------------------------------------- 挑选条目

def select_claims(merged: list[dict]) -> list[dict]:
    """挑出需要对抗复核的记录。

    只挑第一遍判「已实现 / 已偏离」且引用已验真的行为类条目：

    - ``route == "ai"``：脚本直接判的存在性条目不在范围（它的证据是索引，不是引用）；
    - ``verdict in ("implemented", "deviated")``：只有"声称代码里有什么"才需要推翻；
    - ``checked_by == "AI（已验真）"``：被打回的（``AI（已打回）``）已经进待确认，不必再复核；
    - ``evidence`` 非空：没有引用就没法复核。

    @param merged 合并后的判定记录列表
    @return 需要复核的记录列表（保持原顺序）
    """
    out: list[dict] = []
    for record in merged:
        if record.get("route") != "ai":
            continue
        if record.get("verdict") not in ("implemented", "deviated"):
            continue
        if str(record.get("checked_by", "")) != "AI（已验真）":
            continue
        if not record.get("evidence"):
            continue
        out.append(record)
    return out


# ---------------------------------------------------------------- 代码窗口

def enclosing_method(index: CodeIndex, file: str, line: int) -> dict | None:
    """找出某一行落在哪个方法体里。

    方法可能有嵌套（匿名内部类方法已被解析器过滤），命中多个时取范围最小的那个。

    @param index 代码索引
    @param file  代码文件（相对路径）
    @param line  行号（1 起算）
    @return ``{name, owner, file, line, end_line}``；找不到返回 ``None``
    """
    try:
        target = int(line)
    except (TypeError, ValueError):
        return None

    best: dict | None = None
    for name, items in index.methods.items():
        for method in items:
            if method.get("file") != file:
                continue
            try:
                start = int(method.get("line") or 0)
                end = int(method.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            if not (start <= target <= end):
                continue
            # 命中多个嵌套方法时取起点最靠后的（即范围最小的那个）
            if best is None or start > best["line"]:
                best = {
                    "name": name,
                    "owner": method.get("owner", ""),
                    "file": file,
                    "line": start,
                    "end_line": end,
                }
    return best


def method_window(index: CodeIndex, code_root: Path, claim: dict) -> dict | None:
    """给一条记录算一段带行号的代码窗口（围绕它的第一条引用）。

    优先取"引用所在方法的完整（裁剪后的）方法体"：
    起点 = ``max(方法起点, 引用行 - METHOD_HEAD)``，
    终点 = ``min(方法终点, 引用行 + METHOD_TAIL)``。
    找不到所在方法时，退化为引用行附近固定 25 行的窗口。

    @param index     代码索引
    @param code_root 代码根目录
    @param claim     一条判定记录（取其第一条 evidence）
    @return ``{file, start, end, code}``；文件不可读或没有引用时返回 ``None``
    """
    # 1. 取第一条引用（没有引用就没法算窗口）
    evidence = claim.get("evidence") or []
    if not evidence:
        return None
    ev = evidence[0]
    file = (ev.get("file") or "").strip()
    if not file:
        return None

    # 2. 读原文件，拿不到就放弃
    path = resolve_within(code_root, file)
    if path is None or not path.exists():
        return None
    lines = read_text(path).splitlines()
    if not lines:
        return None

    try:
        line = int(ev.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    line = max(1, min(line, len(lines)))

    # 3. 优先按所在方法裁窗口，其次退化为固定长度窗口
    method = enclosing_method(index, file, line)
    if method:
        start = max(method["line"], line - METHOD_HEAD)
        end = min(method["end_line"], line + METHOD_TAIL)
    else:
        half = _FALLBACK_WINDOW // 2
        start = max(1, line - half)
        end = min(len(lines), start + _FALLBACK_WINDOW - 1)
        start = max(1, end - _FALLBACK_WINDOW + 1)

    # 4. 收进文件边界，拼成带行号的文本
    start = max(1, min(start, len(lines)))
    end = max(start, min(end, len(lines)))
    body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
    return {"file": file, "start": start, "end": end, "code": body}


def shared_snippets(
    index: CodeIndex,
    code_root: Path,
    group_claims: list[dict],
    static_by_id: dict[str, dict],
    corpus: "recall.RecallIndex | None",
) -> list[dict]:
    """给一组待复核条目召回"共用"的相关代码。

    复用第一遍的 ``tasks.recall_for_group``：同一功能组里的条目代码位置基本挨着，
    而且纯中文需求抽不出标识符，只能蹭同组上下文。

    会按 ``(file, start, end)`` 去重，并排除已经作为某条"方法窗口"给过的片段，
    避免同一段代码在任务包里出现两遍。

    @param index         代码索引
    @param code_root     代码根目录
    @param group_claims  同一批的待复核条目（需带 ``id``/``assertion`` 等字段）
    @param static_by_id  按 item_id 索引的静态/第一遍结果（提供锚点）
    @param corpus        BM25 召回索引
    @return 相关代码片段列表，元素为 ``{file, start, end, code}``
    """
    # 1. 复用第一遍的召回逻辑
    raw = tasks.recall_for_group(
        group_claims, static_by_id, index, code_root, None, corpus=corpus
    )

    # 2. 排除"已经是某条方法窗口"的片段
    shown: set[tuple] = set()
    for claim in group_claims:
        window = method_window(index, code_root, claim)
        if window:
            shown.add((window["file"], window["start"], window["end"]))

    # 3. 去重后返回
    out: list[dict] = []
    seen: set[tuple] = set()
    for snippet in raw:
        key = (snippet.get("file"), snippet.get("start"), snippet.get("end"))
        if key in seen or key in shown:
            continue
        seen.add(key)
        out.append(snippet)
    return out


# ---------------------------------------------------------------- 任务包

def build_recheck_packages(
    work_dir: Path,
    merged: list[dict],
    items: list[dict],
    index: CodeIndex,
    code_root: Path,
    batch_size: int = DEFAULT_RECHECK_BATCH,
    max_code_lines: int = DEFAULT_RECHECK_MAX_LINES,
    work_label: str = ".checkprd/verify",
) -> list[Path]:
    """把需要对抗复核的条目切成若干任务包，并落盘。

    产出：

    - ``{work_dir}/tasks/batch-NNN.md``  任务包
    - ``{work_dir}/tasks/batch-NNN.json`` 回填模板（已存在则不覆盖）
    - ``{work_dir}/windows.json``         每条记录"被允许引用"的行区间集合

    ``windows.json`` 是关键：复核模型只能引用脚本给过的代码；一段引用若不在
    这些窗口里，验真阶段会直接判为无效。窗口既包含每条自己的方法窗口，
    也包含本批共用的相关代码。

    @param work_dir       对抗复核阶段目录（如 ``.checkprd/verify``）
    @param merged         合并后的判定记录
    @param items          需求条目（提供 group / text / note）
    @param index          代码索引
    @param code_root      代码根目录
    @param batch_size     每个批次最多几条
    @param max_code_lines 每批最多给多少行共用代码
    @param work_label     中间产物目录在文档里的显示路径
    @return 写出的任务包 ``.md`` 路径列表
    """
    # 1. 挑条目，并按功能组归拢（同组同批，共享上下文）
    claims = select_claims(merged)
    if not claims:
        return []

    items_by_id = {it.get("id", ""): it for it in items}
    enriched = [_enrich_claim(claim, items_by_id) for claim in claims]

    groups: dict[str, list[dict]] = {}
    for claim in enriched:
        key = claim.get("group") or "未分组"
        groups.setdefault(key, []).append(claim)

    # 2. 组内按 batch_size 切批
    batches: list[list[dict]] = []
    for _, group_claims in groups.items():
        for i in range(0, len(group_claims), batch_size):
            batches.append(group_claims[i : i + batch_size])

    task_dir = work_dir / tasks.TASK_DIR_NAME
    task_dir.mkdir(parents=True, exist_ok=True)
    corpus = recall.RecallIndex.from_code_index(index)

    written: list[Path] = []
    all_windows: dict[str, list[list]] = {}

    # 3. 逐批渲染任务包
    for n, batch in enumerate(batches, start=1):
        # 3.1 每条先算自己的方法窗口
        windows_by_id: dict[str, list[dict]] = {}
        used_lines = 0
        for claim in batch:
            iid = claim.get("item_id", "")
            window = method_window(index, code_root, claim)
            if window:
                entry = dict(window)
                entry["role"] = "method"
                windows_by_id.setdefault(iid, []).append(entry)
                used_lines += entry["code"].count("\n") + 1

        # 3.2 再算本批共用的相关代码，并受行数预算约束
        static_by_id = {
            r.get("item_id", ""): {
                "evidence": r.get("evidence") or [],
                "checks": r.get("checks") or [],
            }
            for r in merged
        }
        shared_raw = shared_snippets(index, code_root, batch, static_by_id, corpus)
        shared: list[dict] = []
        for snippet in shared_raw:
            cost = snippet["code"].count("\n") + 1
            if used_lines + cost > max_code_lines and shared:
                break
            shared.append(snippet)
            used_lines += cost

        # 3.3 共用代码对每一条都算"被允许引用"，并计入窗口
        for claim in batch:
            iid = claim.get("item_id", "")
            entries = windows_by_id.setdefault(iid, [])
            for snippet in shared:
                entry = dict(snippet)
                entry["role"] = "shared"
                entries.append(entry)

        # 3.4 渲染并落盘
        name = f"batch-{n:03d}"
        content = render_recheck_task(name, batch, windows_by_id, shared, work_label)
        path = task_dir / f"{name}.md"
        tasks.write_text(path, content)

        # 模板 JSON 已存在就不覆盖：重跑 verify 不该冲掉已经填好的复核结果
        json_path = task_dir / f"{name}.json"
        if not json_path.exists():
            tasks.write_json(
                json_path,
                [
                    {
                        "item_id": claim.get("item_id", ""),
                        "second_verdict": "",
                        "clauses": [],
                        "evidence": [],
                        "reason": "",
                    }
                    for claim in batch
                ],
            )
        written.append(path)

        # 3.5 汇总全局窗口（持久化用）
        for iid, entries in windows_by_id.items():
            all_windows[iid] = [
                [e.get("file", ""), e.get("start", 0), e.get("end", 0)] for e in entries
            ]

    # 4. 持久化窗口清单
    tasks.write_json(work_dir / "windows.json", all_windows)
    return written


def _enrich_claim(claim: dict, items_by_id: dict[str, dict]) -> dict:
    """给一条判定记录补上任务包渲染需要的条目字段（不改原记录）。"""
    iid = claim.get("item_id", "")
    item = items_by_id.get(iid, {})
    enriched = dict(claim)
    enriched["id"] = iid
    enriched["text"] = item.get("text", "")
    enriched["note"] = item.get("note", "")
    if not enriched.get("group"):
        enriched["group"] = item.get("group", "")
    return enriched


def render_recheck_task(
    name: str,
    claims: list[dict],
    windows_by_id: dict[str, list[dict]],
    shared: list[dict],
    work_label: str,
) -> str:
    """渲染一份对抗复核任务包。

    @param name          批次名，如 ``batch-001``
    @param claims        本批待复核的条目
    @param windows_by_id ``{item_id: [{file,start,end,code,role}, ...]}``
    @param shared        本批共用的相关代码片段
    @param work_label    中间产物目录在文档里的显示路径
    @return 任务包 Markdown 全文
    """
    out: list[str] = []

    # 1. 抬头：口径与输出要求
    out.extend(
        [
            f"# 对抗复核任务包 {name}",
            "",
            '你的任务不是"再判一遍"，而是**专门推翻**下面这些"已实现 / 已偏离"的结论。',
            '默认它们站不住；只有当代码里给出了**直接、具体、能逐字引用**的证据，才允许判"成立"。',
            "",
            "**口径先说死：**",
            "- 判 `confirmed`（原判成立）→ 必须逐字引用代码，证明这段逻辑**正是**需求说的那件事；",
            '  不是"相关"、不是"沾边"，更不是 `return Result.ok();` 这种放到哪都成立的话。',
            "- 判 `refuted`（原判不成立）→ 说明第一条引用的代码为什么**答非所问**：它实际做了什么，和需求差在哪。",
            "- 判 `partial` → 只满足了一部分。",
            '- 看不到足够的代码 → 写 `unverifiable`，**不要**因为"没看到"就判 `refuted`。',
            "- 拿不准就写 `unverifiable`，它会进「待确认」。",
            "",
            "## 输出要求",
            "",
            "写成一个 JSON 数组，保存到：",
            "",
            f"    {work_label}/tasks/{name}.json",
            "",
            "每条的样子：",
            "",
            "```json",
            "{",
            '  "item_id": "R-007",',
            '  "second_verdict": "confirmed | refuted | partial | unverifiable",',
            '  "clauses": [',
            '    {"text": "需求拆出的一个最小子条件", "evidence": [',
            '      {"file": "src/.../X.java", "line": 1415, "quote": "逐字复制的代码"}',
            "    ]}",
            "  ],",
            '  "evidence": [',
            '    {"file": "src/.../X.java", "line": 1415, "quote": "逐字复制的代码"}',
            "  ],",
            '  "reason": "一句话说清：这段代码到底做没做这件事"',
            "}",
            "```",
            "",
            "**五条硬规矩：**",
            "",
            "1. `quote` 必须从**下面给出的代码**里逐字复制，不改写、不概括，也不引用没给过的代码",
            "   （脚本逐字校验，还会核对你引的是不是它给过的那段）。",
            "2. **判 `confirmed` 必须把需求拆成子条件（clauses），每个子条件都要给出代码位置。**",
            "   只要有一个子条件找不到对应代码，就不能判 confirmed —— 改写 `partial` 或 `unverifiable`。",
            "   （例：「按批次整批原子关闭」要分别证明\"按批次\"和\"原子\"。）",
            "3. 判 `confirmed` / `refuted` / `partial` 都必须带至少一条 `evidence`；一条都引不出，只能写 `unverifiable`。",
            '4. 需求是纯中文、没有类名/方法名/路径的，**照样按需求语义核对**：去代码里找"做了这件事"的逻辑，而不是找某个名字。',
            "5. `quote` 不带行号前缀，只写代码本身。",
            "",
            "## 待复核条目",
        ]
    )

    # 2. 逐条列出第一遍判定与引用
    for claim in claims:
        out.extend(_render_claim_block(claim))
    out.append("")

    # 3. 代码上下文：先每条的方法体，再本批共用代码
    out.extend(
        [
            "## 代码上下文",
            "",
            "> 下面是脚本给出的全部材料。**判断只能基于这些代码**；缺口自己标出来，不要脑补。",
            "> 下面没有代码上下文的条目，请直接判 `unverifiable`。",
            "",
        ]
    )
    for claim in claims:
        for entry in windows_by_id.get(claim.get("item_id", ""), []):
            if entry.get("role") == "method":
                out.extend(_render_snippet_block(entry, "引用位置的完整方法体"))
    for snippet in shared:
        out.extend(_render_snippet_block(snippet, "本批共用的相关代码"))

    return "\n".join(out).rstrip() + "\n"


def _render_claim_block(claim: dict) -> list[str]:
    """渲染"待复核条目"里的一条：需求 + 第一遍判定 + 它的引用。"""
    ev = (claim.get("evidence") or [{}])[0]
    first_quote = (ev.get("quote") or "").strip()
    src = claim.get("source") or {}
    return [
        "",
        f"### {claim.get('item_id', '')}",
        f"需求：{claim.get('assertion', '')}",
        f"出处：`{src.get('file', '')}:{src.get('line', '')}`",
        f"需求原文：{claim.get('text', '')}",
        f"（补充：{claim.get('note', '')}）",
        "",
        f"**第一条判定（待你推翻）**：{claim.get('verdict', '')}",
        f"它的理由：{claim.get('reason', '')}",
        f"它引用的位置：`{ev.get('file', '')}:{ev.get('line', '')}`",
        "它引用的代码：",
        "```java",
        first_quote,
        "```",
    ]


def _render_snippet_block(snippet: dict, role: str) -> list[str]:
    """渲染一段代码上下文（带角色标注）。"""
    return [
        f"### {snippet.get('file', '')}:{snippet.get('start', '')}-{snippet.get('end', '')}（{role}）",
        "",
        "```java",
        snippet.get("code", ""),
        "```",
        "",
    ]


# ---------------------------------------------------------------- 结果回收

def load_recheck_results(work_dir: Path) -> dict[str, dict]:
    """读回复核模型写的结果，按 item_id 汇总。

    与 ``tasks.load_results`` 同构；差别只在字段名是 ``second_verdict``，
    且模板里留空的行直接跳过（说明模型没处理到这条）。

    @param work_dir 对抗复核阶段目录
    @return ``{item_id: 复核结果}``
    """
    result_dir = Path(work_dir) / tasks.TASK_DIR_NAME
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
            second = (row.get("second_verdict") or "").strip()
            if not item_id or not second:
                continue
            row["_batch"] = path.name
            merged[item_id] = row
    return merged


# ---------------------------------------------------------------- 复核验真

def verify_recheck_evidence(
    second: dict, code_root: Path, allowed_windows: list
) -> list[str]:
    """校验复核模型给出的引用。

    比第一遍多一条"防编造"规则：复核只能引用脚本给过的那几段代码。
    返回问题清单，空清单表示全部通过。

    @param second         复核结果（含 ``second_verdict`` 与 ``evidence``）
    @param code_root      代码根目录
    @param allowed_windows 允许引用的窗口列表，元素形如 ``[file, start, end]``
    @return 问题清单
    """
    verdict = (second.get("second_verdict") or "").strip()
    evidence = second.get("evidence") or []
    problems: list[str] = []

    # 1. 声称"有/被推翻/部分"的，必须给出引用
    if verdict in _NEED_EVIDENCE and not evidence:
        problems.append(f"判 {verdict} 必须给出至少一条代码引用")

    # 2. 引用要逐字对得上原文
    if evidence:
        problems.extend(tasks.verify_evidence(evidence, code_root))

    # 3. 引用必须落在脚本给过的窗口内（防编造）
    for ev in evidence:
        file = (ev.get("file") or "").strip()
        if not file:
            continue
        try:
            line = int(ev.get("line") or 0)
        except (TypeError, ValueError):
            continue
        if not any(_in_window(file, line, win) for win in allowed_windows or []):
            problems.append(f"引用不在提供的代码范围内：{file}:{line}")

    return problems


def _in_window(file: str, line: int, window) -> bool:
    """判断 ``(file, line)`` 是否落在某个 ``[file, start, end]`` 窗口内。"""
    if not isinstance(window, (list, tuple)) or len(window) < 3:
        return False
    if window[0] != file:
        return False
    try:
        start = int(window[1])
        end = int(window[2])
    except (TypeError, ValueError):
        return False
    return start <= line <= end


# ---------------------------------------------------------------- 取舍与应用

def decide(first_verdict: str, second_verdict: str) -> str:
    """根据第一遍判定与复核结论，决定这条记录去哪个报告。

    - 第一遍「已实现」：复核成立 → ``ok``；否则 → ``confirm``（降级待人看）
    - 第一遍「已偏离」：复核成立 → ``fix``；否则 → ``confirm``

    @param first_verdict  第一遍判定
    @param second_verdict 复核结论（已归一化，必为四种之一）
    @return ``"ok" | "fix" | "confirm"``
    """
    if first_verdict == "implemented":
        return "ok" if second_verdict == "confirmed" else "confirm"
    if first_verdict == "deviated":
        return "fix" if second_verdict == "confirmed" else "confirm"
    return "confirm"


def apply_recheck(
    merged: list[dict],
    rechecks: dict,
    windows_by_id: dict,
    code_root: Path,
) -> list[dict]:
    """把复核结果应用到判定记录上（原地修改并返回同一份列表）。

    只处理 ``select_claims`` 挑出的条目；其余记录一个字段都不动。
    复核结果缺失、结论非法、引用验不过、或（判 confirmed 时）子条件不齐，
    一律按 ``unverifiable`` 处理 —— 宁可降级到待确认，也不放它进「待修复」。

    @param merged        合并后的判定记录
    @param rechecks      复核结果 ``{item_id: 结果}``
    @param windows_by_id 允许引用的窗口 ``{item_id: [[file,start,end], ...]}``
    @param code_root     代码根目录
    @return 处理后的记录列表（与传入的 ``merged`` 同一对象）
    """
    # 0. 没有任何复核结果时整体不动，保证不跑 verify 的老流程行为不变
    if not rechecks:
        return merged

    eligible = {r.get("item_id", "") for r in select_claims(merged)}
    for record in merged:
        item_id = record.get("item_id", "")
        if item_id not in eligible:
            continue

        second = rechecks.get(item_id)
        candidate = _effective_second(second, code_root, windows_by_id.get(item_id))
        placement = decide(record.get("verdict", ""), candidate)

        record["recheck"] = {
            "second_verdict": candidate,
            "evidence": list(second.get("evidence") or []) if second else [],
            "reason": (second.get("reason") or "") if second else "",
            "clauses": list(second.get("clauses") or []) if second else [],
            "placement": placement,
            "bounced": placement == "confirm",
        }
        record["forced_placement"] = placement
        record["checked_by"] = f"AI（对抗复核：{candidate}）"

    return merged


def _effective_second(second: dict | None, code_root: Path, allowed) -> str:
    """把复核结果归一化成一个可信的结论（四种之一）。

    任何不可信的情况都收敛到 ``unverifiable``。

    @param second    复核结果（可能缺失或非法）
    @param code_root 代码根目录
    @param allowed   该条允许引用的窗口列表
    @return ``confirmed`` / ``refuted`` / ``partial`` / ``unverifiable``
    """
    # 1. 结果缺失或结论不在四种之内 → 判不了
    if not isinstance(second, dict):
        return "unverifiable"
    verdict = (second.get("second_verdict") or "").strip()
    if verdict not in _SECOND_VERDICTS:
        return "unverifiable"
    # 自己就承认判不了的，直接沿用
    if verdict == "unverifiable":
        return "unverifiable"

    # 2. 引用要逐字对得上，且落在脚本给过的窗口内
    allowed_windows = allowed or []
    if verify_recheck_evidence(second, code_root, allowed_windows):
        return "unverifiable"

    # 3. 判 confirmed 还要求子条件齐全且都能核到代码
    if verdict == "confirmed" and not _clauses_ok(second, code_root, allowed_windows):
        return "unverifiable"

    return verdict


def _clauses_ok(second: dict, code_root: Path, allowed_windows: list) -> bool:
    """检查 ``confirmed`` 的子条件清单是否"站得住"。

    每条子条件必须给出至少一条能通过验真（逐字 + 在窗口内）的引用。

    @param second         复核结果
    @param code_root      代码根目录
    @param allowed_windows 允许引用的窗口列表
    @return 子条件齐全且每条都有可核验的引用时返回 ``True``
    """
    clauses = second.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        return False

    for clause in clauses:
        if not isinstance(clause, dict):
            return False
        evidence = clause.get("evidence") or []
        # 该子条件至少要有一条引用能通过验真
        if not any(
            not verify_recheck_evidence(
                {"second_verdict": "confirmed", "evidence": [ev]},
                code_root,
                allowed_windows,
            )
            for ev in evidence
        ):
            return False
    return True
