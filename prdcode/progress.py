"""进度显示。

工具分三个阶段跑，中间还要等大模型处理任务包，整个过程可能十几分钟。
人不该对着黑屏猜"它是不是死了"，所以进度要出现在两个地方：

  1. 终端 —— 实时刷，每步一行
  2. ``.checkprd/进度.md`` —— 随时能打开看，同时也是断点记录

第 2 点尤其重要：等 AI 处理的那一步最慢，终端是静止的，
这时候只看进度文件就知道跑到第几批了。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import write_text

# 清掉光标所在行，避免 \r 刷新时留下残影
_CLEAR = "\033[K"


class Progress:
    """一个够用的进度器。

    用法::

        p = Progress(work_dir, stage="compare", total_steps=6)
        p.begin("读取需求条目")
        p.ok("87 条")
        p.begin("建立代码索引")
        p.tick("3200/6554")          # 长任务原地刷新
        p.ok("6554 个文件")
        p.waiting("等 AI 处理 tasks/ 里的任务包")
        p.finish()
    """

    def __init__(
        self,
        work_dir: str | Path,
        stage: str,
        total_steps: int,
        echo: bool = True,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.work_dir / "进度.md"
        self.stage = stage
        self.total_steps = max(1, total_steps)
        self.echo = echo
        # 输出被重定向或捕获时（比如从别的程序里调），\r 覆盖会失效，
        # 那就别搞原地刷新，老老实实一行一行打
        self.tty = bool(getattr(sys.stdout, "isatty", lambda: False)())

        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.steps: list[dict[str, Any]] = []  # {title, detail, seconds, status}
        self.subs: list[dict[str, Any]] = []   # 当前步骤的子任务（如批次）
        self.note_text = ""

        self._index = 0
        self._title = ""
        self._t0 = 0.0
        self._dirty = True

    # ------------------------------------------------------------ 终端输出

    def _prefix(self) -> str:
        return f"[{self._index}/{self.total_steps}]"

    def begin(self, title: str) -> None:
        """开始一步。"""
        self._flush_current(status="done")
        self._index += 1
        self._title = title
        self._t0 = time.time()
        self.subs = []
        self._dirty = True
        if self.echo and self.tty:
            print(f"{self._prefix()} {title}", end="", flush=True)

    def tick(self, msg: str) -> None:
        """长任务原地刷新，不换行。"""
        if self.echo and self.tty:
            print(f"\r{self._prefix()} {self._title} ... {msg}{_CLEAR}", end="", flush=True)

    def ok(self, detail: str = "") -> None:
        """结束当前步，附结果摘要。"""
        cost = time.time() - self._t0 if self._t0 else 0.0
        # 先把标题记下来，_flush_current 会把它清掉
        index, title = self._index, self._title
        self._flush_current(status="done", detail=detail, seconds=cost)
        if self.echo:
            tail = f" → {detail}" if detail else ""
            line = f"[{index}/{self.total_steps}] {title}{tail} ({cost:.1f}s)"
            print(f"\r{line}{_CLEAR}" if self.tty else line)
            sys.stdout.flush()

    def sub(self, index: int, total: int, title: str, done: bool = False) -> None:
        """登记一条子任务（比如"第 3/6 批"），写进进度文件。"""
        self.subs.append(
            {"index": index, "total": total, "title": title, "done": done}
        )
        self._dirty = True
        if self.echo and not done and self.tty:
            print(f"\r{self._prefix()} {self._title} ... {index}/{total} {title}{_CLEAR}",
                  end="", flush=True)

    def waiting(self, msg: str) -> None:
        """标记"这一步在等外部（通常是大模型）"，必须留在进度文件里。"""
        self.note_text = msg
        self._flush_current(status="waiting", note=msg)
        self._dirty = True
        if self.echo:
            if self.tty:
                print(f"\r{self._prefix()} {self._title} ... {msg}{_CLEAR}")
            else:
                print(f"      {msg}")

    def info(self, msg: str) -> None:
        """打一行普通信息，不进步骤表。"""
        if self.echo:
            print(f"      {msg}")

    def finish(self, summary: str = "") -> None:
        """收尾：把最后一步落盘。"""
        self._flush_current(status="done")
        if summary:
            self.note_text = summary
            self._dirty = True
        self._write()
        if self.echo:
            print()
            if summary:
                print(summary)

    # ------------------------------------------------------------ 进度文件

    def _flush_current(
        self,
        status: str,
        detail: str = "",
        seconds: float = 0.0,
        note: str = "",
    ) -> None:
        """把"当前正在跑的那一步"结算进 steps。"""
        if not self._title:
            return
        self.steps.append(
            {
                "title": self._title,
                "detail": detail,
                "seconds": seconds,
                "status": status,
                "note": note,
                "subs": list(self.subs),
            }
        )
        self._title = ""
        self._dirty = True

    def _render(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "# 执行进度",
            "",
            f"- 阶段：`{self.stage}`",
            f"- 开始：{self.started_at}",
            f"- 更新：{now}",
        ]
        if self.note_text:
            lines.append(f"- 当前：{self.note_text}")
        lines.append("")
        lines.append("---")
        lines.append("")

        for i, step in enumerate(self.steps, start=1):
            mark = "x" if step["status"] == "done" else " "
            detail = f" → {step['detail']}" if step["detail"] else ""
            cost = f"（{step['seconds']:.1f}s）" if step["seconds"] else ""
            lines.append(f"- [{mark}] {i}/{self.total_steps} {step['title']}{detail}{cost}")
            for sub in step.get("subs", []):
                sub_mark = "x" if sub["done"] else " "
                lines.append(
                    f"      - [{sub_mark}] {sub['index']}/{sub['total']} {sub['title']}"
                )
            if step["note"]:
                lines.append(f"      > {step['note']}")

        if self._title:
            lines.append(f"- [ ] {self._index}/{self.total_steps} {self._title} ← 正在进行")

        return "\n".join(lines) + "\n"

    def _write(self) -> None:
        if not self._dirty:
            return
        write_text(self.file, self._render())
        self._dirty = False
