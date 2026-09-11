"""对抗复核（feature C）回归测试。

验证四件事：

  1. 复核成立时，原判照旧（已实现 → ok；已偏离 → fix）；
  2. 复核推翻/验不过时，记录被降级进「待确认」，而不是从两份报告里一起消失；
  3. 引用越出脚本给的窗口 → 直接判 unverifiable（防编造）；
  4. 没有复核结果时，老流程行为不变。

在仓库根目录执行：

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 让测试在任意目录下都能 import 到 prdcode
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prdcode import recheck, report, tasks
from prdcode.codeindex import CodeIndex

# 测试用的 Java 源码：withdraw 方法在第 4-7 行
_JAVA = (
    "package demo;\n"
    "\n"
    "public class RefundServiceImpl {\n"
    "    public void withdraw(Long batchId) {\n"
    "        batchService.closeAll(batchId);\n"
    "        return Result.ok();\n"
    "    }\n"
    "}\n"
)
_JAVA_FILE = "RefundServiceImpl.java"
_METHOD_LINE = 4
_METHOD_END = 7
_ASSERTION = "撤销必须按批次整批原子关闭"


class TestRecheck(unittest.TestCase):
    """对抗复核：判定应用 + 报告归属。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / _JAVA_FILE).write_text(_JAVA, encoding="utf-8")

        self.index = CodeIndex()
        self.index.add_method(
            "withdraw",
            {
                "file": _JAVA_FILE,
                "line": _METHOD_LINE,
                "end_line": _METHOD_END,
                "owner": "RefundServiceImpl",
            },
        )

    # ---- 夹具 ----

    def _record(self, verdict: str) -> dict:
        """造一条第一遍判定记录（引用落在第 5 行）。"""
        return {
            "item_id": "R-001",
            "group": "退款撤销",
            "assertion": _ASSERTION,
            "source": {"file": "需求.md", "line": 87},
            "kind": "behavior",
            "route": "ai",
            "verdict": verdict,
            "evidence": [
                {"file": _JAVA_FILE, "line": 5, "quote": "batchService.closeAll(batchId);"}
            ],
            "reason": "第一遍理由",
            "missing": [],
            "checked_by": "AI（已验真）",
            "search_log": {},
        }

    def _clauses(self) -> list[dict]:
        """造一份"逐条子条件都能核到代码"的 clauses。"""
        return [
            {
                "text": "按批次关闭",
                "evidence": [
                    {
                        "file": _JAVA_FILE,
                        "line": _METHOD_LINE,
                        "quote": "public void withdraw(Long batchId) {",
                    }
                ],
            },
            {
                "text": "原子关闭",
                "evidence": [
                    {
                        "file": _JAVA_FILE,
                        "line": 5,
                        "quote": "batchService.closeAll(batchId);",
                    }
                ],
            },
        ]

    def _allowed(self) -> dict:
        """脚本给这条记录的窗口：整个 withdraw 方法体。"""
        return {"R-001": [[_JAVA_FILE, _METHOD_LINE, _METHOD_END]]}

    def _confirmed(self) -> dict:
        """造一份"复核成立"的结果。"""
        return {
            "item_id": "R-001",
            "second_verdict": "confirmed",
            "clauses": self._clauses(),
            "evidence": [
                {"file": _JAVA_FILE, "line": 5, "quote": "batchService.closeAll(batchId);"}
            ],
            "reason": "确实按批次原子关闭",
        }

    def _refuted(self) -> dict:
        """造一份"复核推翻"的结果。"""
        return {
            "item_id": "R-001",
            "second_verdict": "refuted",
            "clauses": [],
            "evidence": [
                {"file": _JAVA_FILE, "line": 5, "quote": "batchService.closeAll(batchId);"}
            ],
            "reason": "这段代码答非所问",
        }

    # ---- 用例 ----

    def test_implemented_confirmed_stays_ok(self) -> None:
        """用例：复核成立时，原判「已实现」维持 ok。"""
        # Arrange
        merged = [self._record("implemented")]

        # Act
        recheck.apply_recheck(merged, {"R-001": self._confirmed()}, self._allowed(), self.root)

        # Assert
        record = merged[0]
        self.assertEqual(record["recheck"]["second_verdict"], "confirmed")
        self.assertEqual(record["forced_placement"], "ok")
        self.assertEqual(record["checked_by"], "AI（对抗复核：confirmed）")
        self.assertEqual(tasks.classify(record), "ok")
        self.assertTrue(report.is_ok(record))
        self.assertFalse(report.is_confirm(record))

    def test_implemented_refuted_demoted_to_confirm(self) -> None:
        """用例：复核推翻时，原判「已实现」降级为待确认。"""
        # Arrange
        merged = [self._record("implemented")]

        # Act
        recheck.apply_recheck(merged, {"R-001": self._refuted()}, self._allowed(), self.root)

        # Assert
        record = merged[0]
        self.assertEqual(record["recheck"]["second_verdict"], "refuted")
        self.assertEqual(record["forced_placement"], "confirm")
        self.assertTrue(record["recheck"]["bounced"])
        self.assertEqual(tasks.classify(record), "confirm")
        self.assertTrue(report.is_confirm(record))
        self.assertFalse(report.is_ok(record))

    def test_deviated_confirmed_becomes_fix(self) -> None:
        """用例：原判「已偏离」经复核成立后进待修复。"""
        # Arrange
        merged = [self._record("deviated")]

        # Act
        recheck.apply_recheck(merged, {"R-001": self._confirmed()}, self._allowed(), self.root)

        # Assert
        record = merged[0]
        self.assertEqual(record["forced_placement"], "fix")
        self.assertFalse(record["recheck"]["bounced"])
        self.assertEqual(tasks.classify(record), "fix")
        self.assertTrue(report.is_fix(record))

    def test_off_window_quote_treated_unverifiable(self) -> None:
        """用例：复核引用了脚本没给过的行 → 按判不了处理，降级待确认。"""
        # Arrange
        merged = [self._record("implemented")]
        second = self._confirmed()
        # 引用第 6 行，但允许窗口只到第 5 行
        second["evidence"] = [
            {"file": _JAVA_FILE, "line": 6, "quote": "return Result.ok();"}
        ]
        windows = {"R-001": [[_JAVA_FILE, _METHOD_LINE, 5]]}

        # Act
        recheck.apply_recheck(merged, {"R-001": second}, windows, self.root)

        # Assert
        record = merged[0]
        self.assertEqual(record["recheck"]["second_verdict"], "unverifiable")
        self.assertEqual(record["forced_placement"], "confirm")
        self.assertEqual(record["checked_by"], "AI（对抗复核：unverifiable）")

    def test_no_recheck_leaves_records_untouched(self) -> None:
        """用例：没有任何复核结果时，记录一个字段都不动（老流程向后兼容）。"""
        # Arrange
        record = self._record("implemented")
        merged = [record]

        # Act
        recheck.apply_recheck(merged, {}, {}, self.root)

        # Assert
        self.assertNotIn("recheck", record)
        self.assertNotIn("forced_placement", record)
        self.assertEqual(record["checked_by"], "AI（已验真）")
        self.assertEqual(tasks.classify(record), "ok")

    def test_demoted_implemented_not_lost_from_reports(self) -> None:
        """用例：被降级的原判「已实现」必须出现在 02-待确认，且从 01 消失。"""
        # Arrange
        merged = [self._record("implemented")]
        recheck.apply_recheck(merged, {"R-001": self._refuted()}, self._allowed(), self.root)
        out = self.root / "out"

        # Act
        fix_path, confirm_path = report.render_reports(merged, [], out, self.root)

        # Assert
        fix_text = fix_path.read_text(encoding="utf-8")
        confirm_text = confirm_path.read_text(encoding="utf-8")
        self.assertIn(_ASSERTION, confirm_text)
        self.assertIn("对抗复核认为原判定不成立/不充分", confirm_text)
        self.assertNotIn(_ASSERTION, fix_text)

    def test_build_recheck_packages_writes_task_and_windows(self) -> None:
        """用例：verify 打包要同时产出任务包、回填模板与窗口清单。"""
        # Arrange
        merged = [self._record("implemented")]
        items = [
            {"id": "R-001", "group": "退款撤销", "text": _ASSERTION, "note": ""}
        ]
        work = self.root / ".checkprd" / "verify"

        # Act
        paths = recheck.build_recheck_packages(
            work, merged, items, self.index, self.root
        )

        # Assert
        self.assertTrue(paths)
        content = paths[0].read_text(encoding="utf-8")
        self.assertIn("对抗复核任务包", content)
        self.assertIn("## 待复核条目", content)
        self.assertIn("R-001", content)
        self.assertTrue((work / "tasks" / "batch-001.json").exists())
        windows = tasks.read_json(work / "windows.json", {})
        self.assertIn("R-001", windows)
        self.assertTrue(windows["R-001"])


if __name__ == "__main__":
    unittest.main()
