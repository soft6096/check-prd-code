"""回归测试：锁定已修复的 6 个缺陷，防止以后改回去。

只用标准库，不引入任何依赖。在仓库根目录执行：

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试在任意目录下都能 import 到 prdcode
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prdcode import matcher, reverse, tasks
from prdcode.codeindex import CodeIndex, _extract_paths
from prdcode.utils import ERROR_CODE_RE


class TestFindUrlMethodFilter(unittest.TestCase):
    """bug1：需求只写路径不写动词时，不能把带显式动词的接口排除掉。"""

    def setUp(self) -> None:
        self.index = CodeIndex()
        self.index.add_url(
            "POST /refund/withdraw",
            {"file": "C.java", "line": 1, "handler": "withdraw",
             "owner": "C", "http": "POST"},
        )

    def test_path_without_verb_matches(self) -> None:
        item = {"id": "R-1", "kind": "existence",
                "assertion": "撤销接口 /refund/withdraw 存在", "text": ""}
        self.assertEqual(matcher.judge_static(item, self.index)["verdict"], "implemented")

    def test_conflicting_verb_still_misses(self) -> None:
        item = {"id": "R-2", "kind": "existence",
                "assertion": "撤销接口 GET /refund/withdraw 存在", "text": ""}
        self.assertEqual(matcher.judge_static(item, self.index)["verdict"], "missing")


class TestErrorCodeRegex(unittest.TestCase):
    """bug2：索引端与判定端共用同一条错误码规则。"""

    def test_five_digit_extracted(self) -> None:
        self.assertIn("41026", matcher.extract_identifiers("错误码 41026")["error_codes"])

    def test_four_digit_not_extracted(self) -> None:
        # 4 位数字不再当错误码，避免"抽得到但索引里没有"的假缺失
        self.assertNotIn("4001", matcher.extract_identifiers("错误码 4001")["error_codes"])

    def test_shared_constant(self) -> None:
        self.assertEqual(ERROR_CODE_RE.findall("41026"), ["41026"])


class TestMultiPathAnnotation(unittest.TestCase):
    """bug3：一个注解写多个路径要全部登记。"""

    def test_two_paths(self) -> None:
        self.assertEqual(_extract_paths('@RequestMapping({"/a", "/b"})'), ["/a", "/b"])

    def test_non_path_string_ignored(self) -> None:
        got = _extract_paths('@RequestMapping(value="/a", produces="application/json")')
        self.assertEqual(got, ["/a"])


class TestMethodCallExtraction(unittest.TestCase):
    """bug4：带参调用也要能抽出方法名。"""

    def test_with_args(self) -> None:
        ids = matcher.extract_identifiers("调用 refundService.withdraw(dto) 与 doIt()")
        self.assertIn("withdraw", ids["methods"])
        self.assertIn("doIt", ids["methods"])

    def test_control_keywords_ignored(self) -> None:
        ids = matcher.extract_identifiers("if (x) { for (y) {} }")
        self.assertNotIn("if", ids["methods"])
        self.assertNotIn("for", ids["methods"])


class TestReverseModuleSuppression(unittest.TestCase):
    """bug5：同模块的常规动作接口不再误报为"多做的"。"""

    def _index(self) -> CodeIndex:
        idx = CodeIndex()
        for path, handler in (
            ("/refund/apply", "apply"),
            ("/refund/withdraw", "withdraw"),
            ("/refund/detail/{refundId}", "detail"),
            ("/refund/retry", "retry"),
        ):
            idx.add_url(
                f"POST {path}",
                {"file": "C.java", "line": 1, "handler": handler,
                 "owner": "RefundController", "http": "POST"},
            )
        return idx

    def _items(self) -> list[dict]:
        return [{"assertion": "撤销接口 POST /refund/withdraw 存在", "text": ""}]

    def test_generic_action_suppressed(self) -> None:
        paths = {e["path"] for e in reverse.find_extra_capabilities(self._index(), self._items())}
        self.assertNotIn("/refund/apply", paths)

    def test_uncommon_action_reported(self) -> None:
        paths = {e["path"] for e in reverse.find_extra_capabilities(self._index(), self._items())}
        self.assertIn("/refund/retry", paths)


class TestMergeCarriesChecks(unittest.TestCase):
    """bug6：静态判定要带 checks，报告才能显示"最接近的实现"。"""

    def test_checks_preserved(self) -> None:
        items = [{"id": "R-1", "assertion": "接口 /x/y 存在",
                  "kind": "existence", "text": ""}]
        static = [{
            "item_id": "R-1", "route": "static", "verdict": "missing",
            "evidence": [], "reason": "没搜到",
            "checks": [{"kind": "url", "target": "/x/y", "hits": [],
                        "nearest": [{"name": "/x/z", "file": "A.java", "line": 9}]}],
        }]
        merged = tasks.merge_results(items, static, {}, None)
        self.assertIn("checks", merged[0])


if __name__ == "__main__":
    unittest.main()
