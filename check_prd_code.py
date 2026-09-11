#!/usr/bin/env python3
"""check-prd-code 命令行入口。

三步走：

    python3 check_prd_code.py extract --prd ./需求文档.md

        把 PRD 切成块，生成一份"条目化任务包"给 AI。
        AI 读完写成 .checkprd/extract/items.json。

    python3 check_prd_code.py compare --code ./你的工程

        读条目、建代码索引、能直接判的判掉，剩下的做成"判定任务包"给 AI。
        AI 读完写成 .checkprd/compare/tasks/batch-*.json。

    python3 check_prd_code.py report --code ./你的工程 --out ./报告

        验真、合并、出两份报告。

中间产物默认放 .checkprd/，随时能删、能重跑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本在任意目录下都能 import 到 prdcode
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prdcode import STAGE_COMPARE, STAGE_EXTRACT, STAGE_REPORT, WORK_DIR_NAME, __version__
from prdcode import codeindex, matcher, prd, report, reverse, tasks
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
    base = prd_input if prd_input.is_dir() else prd_input.parent

    progress = Progress(work, STAGE_EXTRACT, total_steps=3)
    progress.info(f"check-prd-code {__version__}")

    progress.begin("扫描需求文档")
    files = prd.collect_md_files([prd_input])
    if not files:
        _die(f"{prd_input} 下面没有找到 .md 文件。")
    progress.ok(f"{len(files)} 个文件")

    progress.begin("切成内容块")
    blocks = prd.split_prd([prd_input], base=base)
    write_json(work / "blocks.json", blocks)
    progress.ok(f"{len(blocks)} 块")

    progress.begin("生成条目化任务包")
    task_path = _write_extract_task(work, blocks)
    progress.ok(str(task_path.relative_to(work.parent)))

    items_path = work / "items.json"
    items = read_json(items_path, [])
    if items:
        progress.waiting(f"已读到 {len(items)} 条需求条目，接着跑 compare 吧")
    else:
        progress.waiting("等 AI 把条目写进 items.json")
        progress.info("")
        progress.info("下一步：把下面这个文件交给 AI，让它按里面的要求产出条目")
        progress.info(f"    {task_path}")
        progress.info(f"    结果写到：{items_path}")

    progress.finish()


def _write_extract_task(work: Path, blocks: list[dict]) -> Path:
    """写"把内容块改写成可验证断言"的任务包。"""
    lines: list[str] = []
    lines.append("# 条目化任务")
    lines.append("")
    lines.append(f"下面有 {len(blocks)} 个内容块，从需求文档里切出来的。")
    lines.append("请把它们改写成一条条**能判对错**的需求条目。")
    lines.append("")
    lines.append("## 输出要求")
    lines.append("")
    lines.append("写成一个 JSON 数组，保存到：")
    lines.append("")
    lines.append("    .checkprd/extract/items.json")
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
    lines.append("四条硬规矩：")
    lines.append("")
    lines.append("1. `assertion` 必须是一句能判对错的话。"
                 "「提升用户体验」「系统要稳定」这种判不了对错的，**不要产出条目**。")
    lines.append("2. 拆不动的内容（背景、术语、流程图说明、变更记录、写得含糊的），"
                 "不要硬凑成条目。")
    lines.append("3. `source.line` 填所属块的起始行号。")
    lines.append("4. 编号不要断号，从 R-001 连续往下排。")
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
        lines.append(
            f"### {block['id']} {path}{flag}"
        )
        lines.append("")
        lines.append(
            f"来源：`{block['file']}:{block['start_line']}-{block['end_line']}`"
            f"　类型：{block['kind']}"
        )
        lines.append("")
        lines.append("```text")
        lines.append(block["text"])
        lines.append("```")
        lines.append("")

    path = work / "条目化任务.md"
    write_text(path, "\n".join(lines))
    return path


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

    progress.begin("读取需求条目")
    progress.ok(f"{len(items)} 条")

    progress.begin("建立代码索引")
    index = codeindex.build_index([code_root], base=code_root, progress=progress)
    write_json(stage_dir / "index.json", index.to_dict())
    progress.ok(codeindex.index_summary(index))

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
    for stage in (STAGE_EXTRACT, STAGE_COMPARE, STAGE_REPORT):
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
    p2.set_defaults(func=cmd_compare)

    p3 = sub.add_parser("report", help="第三步：验真、合并、出报告")
    p3.add_argument("--code", required=True, help="代码根目录")
    p3.add_argument("--out", default=".", help="报告输出目录（默认当前目录）")
    p3.add_argument("--work", default=WORK_DIR_NAME, help="中间产物目录")
    p3.add_argument("--samples", type=int, default=6, help="抽查条数（默认 6）")
    p3.set_defaults(func=cmd_report)

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
