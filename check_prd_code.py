#!/usr/bin/env python3
"""check-prd-code 命令行入口。

三步走：

    python3 check_prd_code.py extract --prd ./需求文档.md

        把 PRD 切成块，生成一份"条目化任务包"给 AI。
        AI 读完写成 .checkprd/extract/items.json。

    python3 check_prd_code.py compare --code ./你的工程

        读条目、建代码索引、能直接判的判掉，剩下的做成"判定任务包"给 AI。
        AI 读完写成 .checkprd/compare/tasks/batch-*.json。

    python3 check_prd_code.py verify --code ./你的工程      （可选）

        对抗复核：把上面判成"已实现 / 已偏离"的条目挑出来，交给
        **另一个强推理模型**专门推翻。结果写回 .checkprd/verify/tasks/。
        不跑这一步，后面照样出报告。

    python3 check_prd_code.py report --code ./你的工程 --out ./报告

        验真、合并、出两份报告。

中间产物默认放 .checkprd/，随时能删、能重跑。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让脚本在任意目录下都能 import 到 prdcode
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prdcode import (
    STAGE_COMPARE,
    STAGE_EXTRACT,
    STAGE_REPORT,
    STAGE_VERIFY,
    WORK_DIR_NAME,
    __version__,
)
from prdcode import codeindex, matcher, prd, recheck, report, reverse, tasks
from prdcode.codeindex import CodeIndex
from prdcode.progress import Progress
from prdcode.utils import read_json, read_text, write_json, write_text


# ---------------------------------------------------------------- 工具

def _die(msg: str, code: int = 1) -> None:
    print(f"\n出错了：{msg}\n", file=sys.stderr)
    raise SystemExit(code)


def _require(path: Path, hint: str) -> None:
    if not path.exists():
        _die(f"找不到 {path}。{hint}")


def _load_index(work: Path) -> CodeIndex:
    data = read_json(work / "compare" / "index.json")
    if not data:
        _die("还没有代码索引，请先跑 compare。")
    return CodeIndex.from_dict(data)


# ---------------------------------------------------------------- 第一步

def cmd_extract(args: argparse.Namespace) -> None:
    work = Path(args.work) / STAGE_EXTRACT
    prd_input = Path(args.prd)
    _require(prd_input, "检查一下 --prd 指的路径。")
    prd_root = prd_input if prd_input.is_dir() else prd_input.parent

    progress = Progress(work, STAGE_EXTRACT, total_steps=4)
    progress.info(f"check-prd-code {__version__}")

    progress.begin("扫描需求文档")
    files = prd.collect_md_files([prd_input])
    if not files:
        _die(f"{prd_input} 下面没有找到 .md 文件。")
    progress.ok(f"{len(files)} 个文件")

    progress.begin("切成内容块")
    blocks = prd.split_prd([prd_input], base=prd_root)
    write_json(work / "blocks.json", blocks)
    write_json(
        work / "meta.json",
        {"prd": str(prd_input), "prd_root": str(prd_root.resolve())},
    )
    image_blocks = [b for b in blocks if b.get("images")]
    progress.ok(f"{len(blocks)} 块，其中 {len(image_blocks)} 块带图")

    progress.begin("生成条目化任务包")
    work_label = f"{args.work}/{STAGE_EXTRACT}"
    plan = _write_extract_tasks(work, blocks, prd_root, work_label)
    progress.ok(f"文本 {len(plan['text'])} 批，图片 {len(plan['image'])} 批")

    progress.begin("检查 AI 产出")
    got_text, got_image = _count_extract_results(work)
    items = _collect_items(work)
    if items:
        write_json(work / "items.json", items)
    progress.ok(
        f"文本 {got_text}/{len(plan['text'])}，"
        f"图片 {got_image}/{len(plan['image'])}，"
        f"已收集 {len(items)} 条需求"
    )

    progress.finish()
    _print_extract_next(work, plan, got_text, got_image, len(items), work_label)


def _print_extract_next(
    work: Path,
    plan: dict,
    got_text: int,
    got_image: int,
    items_count: int,
    work_label: str,
) -> None:
    """把"该换模型了"这件事，明确打到屏幕上。"""
    pending_text = len(plan["text"]) - got_text
    pending_image = len(plan["image"]) - got_image
    bar = "=" * 64

    print()
    if pending_text <= 0 and pending_image <= 0:
        print(bar)
        print(f"  条目化完成，共 {items_count} 条需求")
        print(bar)
        print()
        print("下一步：")
        print(f"    python3 check_prd_code.py compare --code <代码目录>")
        return

    print(bar)
    print("  停一下 —— 接下来两批任务，要用不同的模型")
    print(bar)
    print()

    if pending_image > 0:
        print(f"① 图片批（{pending_image} 个文件待处理，共 {len(plan['image'])} 个）")
        print()
        print(f"   >>> 请把模型切换到【能看图的模型】（多模态 / 视觉模型）")
        print(f"       参考：GLM-5v-Turbo、GPT-4o、Claude Sonnet、Qwen-VL 等")
        print(f"       判断标准：这个模型能不能直接读图片文件？")
        print(f"       读不了就别用 —— 看不到图，模型只能照着标题瞎猜，条目全废。")
        print()
        print(f"   任务： {work_label}/任务/图片/")
        print(f"   产出： {work_label}/结果/图片批-*.json")
        print(f"   注意：不是服务端的图（纯页面布局、前端交互、样式）直接跳过，")
        print(f"         在 skipped.md 里记一行，别硬造条目")
        print()

    if pending_text > 0:
        print(f"② 文本批（{pending_text} 个文件待处理，共 {len(plan['text'])} 个）")
        print()
        print(f"   >>> 请把模型切换到【便宜的文本模型】")
        print(f"       参考：GLM-5.3-Flash、DeepSeek、GPT-4o-mini 等")
        print(f"       这批是中文改写活 —— 量最大、也最简单，不值得用贵模型。")
        print()
        print(f"   任务： {work_label}/任务/文本/")
        print(f"   产出： {work_label}/结果/文本批-*.json")
        print()

    print("   （这两批之间要各切一次模型；同一批内可以连着跑完）")
    print()

    print("两批都写完之后，重跑同一条命令，结果会自动合并成 items.json：")
    print(f"    python3 check_prd_code.py extract --prd <同一个 PRD>")
    print()
    print(f"当前进度：文本 {got_text}/{len(plan['text'])}，图片 {got_image}/{len(plan['image'])}")
    print()


def _render_extract_pack(
    name: str,
    blocks: list[dict],
    prd_root: Path,
    work_label: str,
    has_images: bool = False,
    base_context: list[dict] | None = None,
) -> str:
    """渲染一份"把内容块改写成可验证断言"的任务包。"""
    lines: list[str] = []
    lines.append(f"# 条目化任务 · {name}")
    lines.append("")
    if has_images:
        lines.append(f"这一批有 {len(blocks)} 个内容块，**每一块都带页面原型图**。")
        lines.append("需求就藏在图里，**请逐个打开图片看**，不要只读文字。")
    else:
        lines.append(f"下面有 {len(blocks)} 个内容块，从需求文档里切出来的。")
    lines.append("请把它们改写成一条条**能判对错**的需求条目。")
    lines.append("")
    lines.append("**再强调一遍：只提取服务端相关的。** 这份 PRD 里混着服务端、后台前端、")
    lines.append("小程序端、APP 端的需求，而这些条目最终要拿去跟【后端代码】比对。")
    lines.append("怎么分见下面的硬规矩第 1 条。")
    lines.append("")

    if base_context:
        lines.append("---")
        lines.append("")
        lines.append("## 基础定义（当已读过的前文用）")
        lines.append("")
        lines.append("你这批是独立的一份任务，看不到别的批次。")
        lines.append("下面是从文档「总纲」章节抽出来的定义 —— 字段、字典、状态、业务规则。")
        lines.append("后面的内容块会引用它们，**遇到引用就回来这里查**，不要自己猜一个。")
        lines.append("")
        for block in base_context:
            path = " / ".join(block["heading"]) or "（无标题）"
            lines.append(f"### {path}")
            lines.append("")
            lines.append("```text")
            lines.append(block["text"])
            lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("## 输出要求")
    lines.append("")
    lines.append("写成一个 JSON 数组，保存到：")
    lines.append("")
    lines.append(f"    {work_label}/结果/{name}.json")
    lines.append("")
    lines.append("```json")
    lines.append("[")
    lines.append("  {")
    lines.append('    "id": "R-001",')
    lines.append('    "group": "退款撤销",')
    lines.append('    "kind": "behavior",')
    lines.append('    "assertion": "撤销必须按批次整批原子关闭，本批次之外的不能撤",')
    lines.append('    "source": {"file": "需求文档.md", "line": 87},')
    lines.append('    "text": "原始文字，用于留痕",')
    lines.append('    "note": ""')
    lines.append("  }")
    lines.append("]")
    lines.append("```")
    lines.append("")
    lines.append("字段说明：")
    lines.append("")
    lines.append("| 字段 | 说明 |")
    lines.append("| :--- | :--- |")
    lines.append("| `id` | 从 R-001 开始顺序编号 |")
    lines.append("| `group` | 这一条属于哪个功能组，用来分组展示 |")
    lines.append("| `kind` | **必填**，见下 |")
    lines.append("| `assertion` | 一句话，必须能判对错 |")
    lines.append('| `source` | 这条出自哪个块的哪一行 |')
    lines.append("| `text` | 原始文字，可截断 |")
    lines.append("| `note` | 需要额外说明时填，没有就空字符串 |")
    lines.append("")
    lines.append("`kind` 只有两种，按下面的标准选：")
    lines.append("")
    lines.append("| kind | 什么时候用 | 例子 |")
    lines.append("| :--- | :--- | :--- |")
    lines.append('| `existence` | 能用「搜得到 / 搜不到」回答的 | '
                 '"接口 `/refund/withdraw` 存在"、"字段 `refundBatchNo` 存在" |')
    lines.append('| `behavior` | 要读懂代码逻辑才知道的 | '
                 '"撤销必须按批次整批原子关闭"、"重复提交要返回上次结果" |')
    lines.append("")
    lines.append("**这个字段很重要**：标 `existence` 的会交给脚本直接判断，"
                 "标 `behavior` 的才交给 AI。标错了两边都浪费。")
    lines.append("")
    lines.append("硬规矩：")
    lines.append("")
    lines.append("**1. 只管服务端。** 这份 PRD 里混着服务端、后台前端、小程序端、APP 端的需求，")
    lines.append("   而这些条目最终是拿去跟【后端代码】比对的，所以**只有服务端能落地的才提取**。")
    lines.append("")
    lines.append("| 提取 | 跳过 |")
    lines.append("| :--- | :--- |")
    lines.append("| 字段、表结构、字段约束（长度/必填/唯一） | 页面布局、区块位置、间距字号 |")
    lines.append("| 业务规则、校验规则、计算口径 | 按钮摆哪、什么颜色、什么图标 |")
    lines.append("| 状态定义、流转条件、触发时机 | 页面跳转、前端路由 |")
    lines.append("| 接口路径、入参出参、错误码 | 弹窗动效、加载态、埋点 |")
    lines.append("| 字典与枚举值 | 小程序 / APP 的原生交互 |")
    lines.append("| 事务、幂等、补偿、定时任务 | 背景、价值、术语、变更记录 |")
    lines.append("")
    lines.append("   **同一块里常常混着两种内容，按句子拆**：")
    lines.append("   「商品名称：必填，最长 60 字」要提取；「保存按钮固定在右下角」跳过。")
    lines.append("   不要因为这块整体像前端就整块扔，也不要因为里面有字段就把按钮位置也抄进来。")
    lines.append("")
    lines.append("**2. `assertion` 必须是一句能判对错的话。**"
                 "「提升用户体验」「系统要稳定」这种判不了对错的，不要产出条目。")
    lines.append("")
    lines.append("**3. 处理不掉的要记账。** 跳过的东西、拆不动的内容，"
                 "都写进 `skipped.md`，并注明是「跳过」还是「拆不动」——")
    lines.append("   **漏提取比错提取危险**，错了能发现，漏了永远没人知道。")
    lines.append("")
    lines.append("**4. 遇上引用，回开头的「基础定义」里查，不要自己猜。**"
                 "看到「状态变更为 X」就去那里找 X 的定义，看到字段名就对照字段定义。")
    lines.append("   确实查不到的，在 `note` 里写明「依赖 XX 定义，本批未见」，别硬猜一个。")
    lines.append("")
    lines.append("**5. `source.line` 填所属块的起始行号；编号从 R-001 连续往下排。**")
    lines.append("")

    if has_images:
        lines.append("**这批是图片批。先看图，再决定提不提取：**")
        lines.append("")
        lines.append("一句话判断法 —— 这张图能不能提炼出「代码里找得到的名字或规则」？")
        lines.append("")
        lines.append("- 能（字段名、状态值、接口路径、校验规则）→ 提取")
        lines.append("- 不能（只有位置、样式、交互感）→ **跳过，不要为了凑数硬造条目**")
        lines.append("")
        lines.append("跳过也要在 `skipped.md` 里记一行，写明是判断后主动跳过的：")
        lines.append("")
        lines.append("```")
        lines.append("图片批-001 / B0131 / image 79.png —— 纯页面布局线框图，无服务端信息")
        lines.append("```")
        lines.append("")
        lines.append("一个实用信号：小节标题叫「页面布局」的多半是线框图，"
                     "叫「各区域元素与交互」的多半有字段和规则 —— **但最终看内容**。")
        lines.append("")

    lines.append("另外再写一个文件，说明哪些内容你没拆成条目、为什么：")
    lines.append("")
    lines.append("    .checkprd/extract/skipped.md")
    lines.append("")
    lines.append("**漏拆比错拆更危险** —— 错了能发现，漏了永远没人知道。"
                 "所以拆不动的地方要显式写出来。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 内容块")
    lines.append("")

    for block in blocks:
        path = " / ".join(block["heading"]) or "（无标题）"
        flag = "" if block.get("likely_requirement", True) else "（看着不像需求）"
        lines.append(f"### {block['id']} {path}{flag}")
        lines.append("")
        lines.append(
            f"来源：`{block['file']}:{block['start_line']}-{block['end_line']}`"
            f"　类型：{block['kind']}"
        )
        lines.append("")

        images = block.get("images") or []
        if images:
            lines.append("**这一块带的图**（逐个用文件读取工具打开）：")
            lines.append("")
            for img in images:
                lines.append(f"- `{(prd_root / img).resolve()}`")
            lines.append("")

        lines.append("```text")
        lines.append(block["text"])
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# 每批容量上限。宁可多分几批，也不要让一批大到模型读不完 ——
# 读不完不是"慢一点"，是它会静默漏掉后半截。
TEXT_BATCH_LINES = 1200
TEXT_BATCH_BLOCKS = 60
IMAGE_BATCH_BLOCKS = 18


# 「总纲」性质的章节。后面每个页面都会引用它们定义的字段、字典、状态、规则。
# 每个批次是一个独立对话、互相看不到，所以必须把这些抽出来附给每一批。
BASE_SECTION_HINTS = (
    "数据字段", "字段定义", "数据结构", "系统数据字典", "数据字典", "枚举定义",
    "核心业务逻辑规则", "业务逻辑规则", "业务规则", "核心业务流程", "业务流程",
    "状态定义", "状态机", "术语", "名词解释",
)


def _pick_base_blocks(blocks: list[dict], max_lines: int = 320) -> list[dict]:
    """挑出总纲性质的块，作为公共上下文附给每一批。

    不是让模型"记得前面读过什么"，而是把它需要的东西直接塞到手上。
    """
    picked: list[dict] = []
    total = 0
    for b in blocks:
        # 纯标题块不用带 —— 内容块自己已经带了完整标题路径
        if b.get("images") or b.get("kind") == "heading":
            continue
        heading = " / ".join(b.get("heading") or [])
        if not any(hint in heading for hint in BASE_SECTION_HINTS):
            continue
        cost = (b.get("text") or "").count("\n") + 3
        if total + cost > max_lines:
            break
        picked.append(b)
        total += cost
    return picked


def _write_extract_tasks(
    work: Path, blocks: list[dict], prd_root: Path, work_label: str
) -> dict:
    """把内容块切成分批任务包。

    图片块单独归一类 —— 它们要用不同的模型跑，混在一起就白搭。
    """
    text_blocks = [b for b in blocks if not b.get("images") and b["kind"] != "heading"]
    image_blocks = [b for b in blocks if b.get("images")]

    text_batches = _batch_blocks(text_blocks, TEXT_BATCH_LINES, TEXT_BATCH_BLOCKS)
    image_batches = _batch_blocks(image_blocks, 10**6, IMAGE_BATCH_BLOCKS)

    # 总纲抽出来，附给每一批 —— 补上批次之间断裂的上下文
    base_context = _pick_base_blocks(blocks)

    task_text = work / "任务" / "文本"
    task_image = work / "任务" / "图片"
    (work / "结果").mkdir(parents=True, exist_ok=True)

    plan: dict[str, list[Path]] = {"text": [], "image": []}

    for i, batch in enumerate(text_batches, start=1):
        name = f"文本批-{i:03d}"
        path = task_text / f"{name}.md"
        write_text(
            path,
            _render_extract_pack(
                name, batch, prd_root, work_label, base_context=base_context
            ),
        )
        plan["text"].append(path)

    for i, batch in enumerate(image_batches, start=1):
        name = f"图片批-{i:03d}"
        path = task_image / f"{name}.md"
        write_text(
            path,
            _render_extract_pack(
                name,
                batch,
                prd_root,
                work_label,
                has_images=True,
                base_context=base_context,
            ),
        )
        plan["image"].append(path)

    return plan


def _batch_blocks(
    blocks: list[dict], max_lines: int, max_blocks: int
) -> list[list[dict]]:
    """按行数和块数双重上限分批，并尽量在章节边界处断开。

    把同一个页面的内容拆到两批去，两边的模型都只能看到半截。
    """
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_lines = 0
    cur_section = ""

    for b in blocks:
        # 用"除去最后一级"的路径当章节标识
        section = " / ".join((b.get("heading") or [])[:-1])
        cost = (b.get("text") or "").count("\n") + 14

        over_limit = cur_lines + cost > max_lines or len(cur) >= max_blocks
        # 章节变了、而且已经装了大半批，就在这儿断开
        section_break = cur and section != cur_section and cur_lines > max_lines * 0.6

        if cur and (over_limit or section_break):
            batches.append(cur)
            cur, cur_lines = [], 0

        cur.append(b)
        cur_lines += cost
        cur_section = section

    if cur:
        batches.append(cur)
    return batches


def _is_filled(path: Path) -> bool:
    """这个结果文件是真写完了，还是只留了个空模板。"""
    data = read_json(path)
    return isinstance(data, list) and len(data) > 0


def _count_extract_results(work: Path) -> tuple[int, int]:
    """数一数 AI 写回来几批了。"""
    result_dir = work / "结果"
    text = sum(1 for p in result_dir.glob("文本批-*.json") if _is_filled(p))
    image = sum(1 for p in result_dir.glob("图片批-*.json") if _is_filled(p))
    return text, image


def _collect_items(work: Path) -> list[dict]:
    """把所有批次的结果合并成一份条目清单。

    同一句话在多批里重复出现只留一条 —— 原型图和正文常常写的是同一件事。
    编号在这里统一重排，避免跨批次打架。
    """
    result_dir = work / "结果"
    items: list[dict] = []
    seen: set[str] = set()

    for path in sorted(result_dir.glob("*.json")):
        data = read_json(path, [])
        if not isinstance(data, list):
            continue
        for it in data:
            if not isinstance(it, dict):
                continue
            assertion = (it.get("assertion") or "").strip()
            if not assertion or assertion in seen:
                continue
            seen.add(assertion)
            it["assertion"] = assertion
            items.append(it)

    for i, it in enumerate(items, start=1):
        it["id"] = f"R-{i:03d}"
    return items


# ---------------------------------------------------------------- 第二步

def cmd_compare(args: argparse.Namespace) -> None:
    work = Path(args.work)
    code_root = Path(args.code)
    _require(code_root, "检查一下 --code 指的路径。")

    items = read_json(work / STAGE_EXTRACT / "items.json", [])
    if not items:
        _die(
            "还没拿到需求条目。先跑 extract，把生成的任务包交给 AI，"
            "让它产出 .checkprd/extract/items.json。"
        )

    stage_dir = work / STAGE_COMPARE
    progress = Progress(stage_dir, STAGE_COMPARE, total_steps=4)

    # 错误码位数：命令行优先，其次环境变量（utils 里读取）
    if getattr(args, "error_code_digits", None):
        os.environ["CHECKPRD_ERROR_CODE_DIGITS"] = str(args.error_code_digits)

    progress.begin("读取需求条目")
    progress.ok(f"{len(items)} 条")

    progress.begin("建立代码索引")
    index, cached = codeindex.build_index_cached(
        [code_root],
        base=code_root,
        cache_path=stage_dir / "index_cache.json",
        progress=progress,
    )
    write_json(stage_dir / "index.json", index.to_dict())
    progress.ok(("缓存命中，" if cached else "") + codeindex.index_summary(index))

    progress.begin("静态判定")
    static_results = [matcher.judge_static(item, index) for item in items]
    write_json(stage_dir / "static.json", static_results)
    counter = matcher.summarize(static_results)
    progress.ok(
        f"脚本直接判完 {counter['static']} 条，{counter['ai']} 条要交给 AI"
    )

    progress.begin("生成 AI 判定任务包")
    task_files = tasks.build_task_packages(
        stage_dir,
        items,
        static_results,
        index,
        code_root,
        work_label=f"{args.work}/{STAGE_COMPARE}",
    )
    progress.ok(f"{len(task_files)} 批")

    if task_files:
        progress.waiting("等 AI 处理 tasks/ 里的任务包")
        progress.info("")
        progress.info("下一步：把下面这个目录交给 AI")
        progress.info(f"    {stage_dir / 'tasks'}")
        progress.info("每批读 batch-NNN.md，结果写回同名的 batch-NNN.json")
        progress.info("处理完再跑 report。")
    else:
        progress.waiting("没有需要 AI 判定的条目，直接跑 report 就行")

    progress.finish()


# ---------------------------------------------------------------- 对抗复核（可选）

def cmd_verify(args: argparse.Namespace) -> None:
    """可选的对抗复核阶段：挑出「已实现 / 已偏离」的判定，打包交给另一个模型推翻。

    跑在 compare 之后、report 之前。不跑它，report 的行为与以前完全一致。
    """
    work = Path(args.work)
    code_root = Path(args.code)
    _require(code_root, "检查一下 --code 指的路径。")

    items = read_json(work / STAGE_EXTRACT / "items.json", [])
    static_results = read_json(work / STAGE_COMPARE / "static.json", [])
    if not items or not static_results:
        _die(
            "缺前置产物，请先依次跑 extract 和 compare —— "
            "对抗复核需要 items.json、static.json 和代码索引（index.json）。"
        )
    index = _load_index(work)

    stage_dir = work / STAGE_VERIFY
    progress = Progress(stage_dir, STAGE_VERIFY, total_steps=3)

    progress.begin("读回第一遍判定")
    ai_results = tasks.load_results(work / STAGE_COMPARE)
    merged = tasks.merge_results(items, static_results, ai_results, code_root)
    progress.ok(f"{len(merged)} 条")

    progress.begin("挑出需要对抗复核的条目")
    claims = recheck.select_claims(merged)
    if not claims:
        progress.ok("0 条")
        progress.waiting("没有需要对抗日核的条目")
        progress.finish()
        print("没有需要对抗日核的条目。")
        return
    progress.ok(f"{len(claims)} 条")

    progress.begin("生成对抗复核任务包")
    paths = recheck.build_recheck_packages(
        stage_dir,
        merged,
        items,
        index,
        code_root,
        batch_size=args.batch_size,
        max_code_lines=args.max_code_lines,
        work_label=f"{args.work}/{STAGE_VERIFY}",
    )
    progress.ok(f"{len(paths)} 批")

    progress.waiting("等对抗复核模型处理 tasks/ 里的任务包")
    progress.info("")
    progress.info("下一步：把下面这个目录交给一个**与第一遍不同的强推理模型**")
    progress.info(f"    {stage_dir / 'tasks'}")
    progress.info(
        "逐条读 batch-NNN.md，结果写回同名的 batch-NNN.json；然后跑 report。"
    )
    progress.finish()


# ---------------------------------------------------------------- 第三步

def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.work)
    code_root = Path(args.code)
    out_dir = Path(args.out)
    _require(code_root, "检查一下 --code 指的路径。")

    items = read_json(work / STAGE_EXTRACT / "items.json", [])
    static_results = read_json(work / STAGE_COMPARE / "static.json", [])
    if not items or not static_results:
        _die("缺前置产物，请先依次跑 extract 和 compare。")

    index = _load_index(work)
    stage_dir = work / STAGE_REPORT
    progress = Progress(stage_dir, STAGE_REPORT, total_steps=4)

    progress.begin("读回 AI 判定")
    ai_results = tasks.load_results(work / STAGE_COMPARE)
    progress.ok(f"{len(ai_results)} 条")

    progress.begin("验真与合并")
    merged = tasks.merge_results(items, static_results, ai_results, code_root)
    # 跑过对抗复核（可选 verify 阶段）就先应用复核结论，再落盘、再出报告。
    # 没有复核结果时 rechecks 为空，apply_recheck 原样返回，行为与以前完全一致。
    rechecks = recheck.load_recheck_results(work / STAGE_VERIFY)
    if rechecks:
        windows = read_json(work / STAGE_VERIFY / "windows.json", {})
        merged = recheck.apply_recheck(merged, rechecks, windows, code_root)
    write_json(stage_dir / "merged.json", merged)
    bounced = sum(1 for r in merged if r.get("verify_problems"))
    detail = f"{len(merged)} 条"
    if bounced:
        detail += f"，打回 {bounced} 条"
    progress.ok(detail)

    progress.begin("反向覆盖：代码里有、需求里没提的")
    extras = reverse.find_extra_capabilities(index, items)
    write_json(stage_dir / "extras.json", extras)
    progress.ok(f"{len(extras)} 条")

    progress.begin("生成报告")
    fix_path, confirm_path = report.render_reports(
        merged, extras, out_dir, code_root, sample_size=args.samples
    )
    progress.ok("两份报告已生成")

    progress.finish()
    progress.info("")
    progress.info(f"  {fix_path}")
    progress.info(f"  {confirm_path}")
    progress.info("")
    progress.info("01 可以直接交给 AI 去改；02 要人先看一眼再给 AI。")


# ---------------------------------------------------------------- 查看进度

def cmd_status(args: argparse.Namespace) -> None:
    work = Path(args.work)
    found = False
    for stage in (STAGE_EXTRACT, STAGE_COMPARE, STAGE_VERIFY, STAGE_REPORT):
        path = work / stage / "进度.md"
        if path.exists():
            found = True
            print(read_text(path).rstrip())
            print()
    if not found:
        print(f"{work} 下面还没有任何进度记录。")


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-prd-code",
        description="把 PRD 和代码摆在一起核对，挑出没做的、做错的、多做的。",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("extract", help="第一步：把 PRD 切成块，生成条目化任务包")
    p1.add_argument("--prd", required=True, help="PRD 文件或目录")
    p1.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录（默认 .checkprd）")
    p1.set_defaults(func=cmd_extract)

    p2 = sub.add_parser("compare", help="第二步：建索引、静态判定、生成 AI 任务包")
    p2.add_argument("--code", required=True, help="代码根目录")
    p2.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录")
    p2.add_argument(
        "--error-code-digits",
        type=int,
        default=None,
        help="错误码位数（默认 5，也可用环境变量 CHECKPRD_ERROR_CODE_DIGITS）",
    )
    p2.set_defaults(func=cmd_compare)

    p3 = sub.add_parser("report", help="第三步：验真、合并、出报告")
    p3.add_argument("--code", required=True, help="代码根目录")
    p3.add_argument("--out", default=".", help="报告输出目录（默认当前目录）")
    p3.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录")
    p3.add_argument("--samples", type=int, default=20, help="抽查条数（默认 20）")
    p3.set_defaults(func=cmd_report)

    p5 = sub.add_parser(
        "verify",
        help="可选：对抗复核（在 compare 与 report 之间，换模型专门推翻原判）",
    )
    p5.add_argument("--code", required=True, help="代码根目录")
    p5.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录")
    p5.add_argument(
        "--batch-size",
        type=int,
        default=recheck.DEFAULT_RECHECK_BATCH,
        help=f"每批复核几条（默认 {recheck.DEFAULT_RECHECK_BATCH}）",
    )
    p5.add_argument(
        "--max-code-lines",
        type=int,
        default=recheck.DEFAULT_RECHECK_MAX_LINES,
        help=f"每批最多给多少行共用代码（默认 {recheck.DEFAULT_RECHECK_MAX_LINES}）",
    )
    p5.set_defaults(func=cmd_verify)

    p4 = sub.add_parser("status", help="看当前跑到哪一步了")
    p4.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录")
    p4.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
