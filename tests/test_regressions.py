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

from prdcode import javasrc, matcher, reverse, tasks
from prdcode.codeindex import CodeIndex
from prdcode.utils import error_code_regex, resolve_within


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
        self.assertEqual(error_code_regex().findall("41026"), ["41026"])


class TestMultiPathAnnotation(unittest.TestCase):
    """bug3：一个注解写多个路径要全部登记。"""

    def test_two_paths(self) -> None:
        syms = javasrc.parse_java('@RequestMapping({"/a", "/b"}) class C { }')
        self.assertEqual(syms.types[0]["base_paths"], ["/a", "/b"])

    def test_non_path_string_ignored(self) -> None:
        syms = javasrc.parse_java(
            '@RequestMapping(value="/a", produces="application/json") class C { }'
        )
        self.assertEqual(syms.types[0]["base_paths"], ["/a"])


class TestJavaParser(unittest.TestCase):
    """token 化解析器：字段多声明符、初始化块、枚举常量的边界。"""

    SRC = """
    public class T {
        private int a, b, c = 3;
        private static final Map<String, List<Integer>> CACHE = new HashMap<>();
        private Runnable task = () -> { doIt(); };
        private Comparator<String> cmp = new Comparator<String>() {
            @Override public int compare(String x, String y) { return 0; }
        };
        static { init(); }
        { init(); }
        public T() { this.a = 1; }
        public <X extends Comparable<X>> List<X> sort(List<X> in) { return in; }
        interface Inner { void run(); }
        enum Status { NEW, DONE; }
        void doIt() {}
        void init() {}
    }
    """

    def setUp(self) -> None:
        self.syms = javasrc.parse_java(self.SRC)

    def test_types(self) -> None:
        names = {t["name"] for t in self.syms.types}
        self.assertEqual(names, {"T", "Inner", "Status"})

    def test_multi_declarator_fields(self) -> None:
        names = {f["name"] for f in self.syms.fields}
        for expected in ("a", "b", "c", "CACHE", "task", "cmp"):
            self.assertIn(expected, names)

    def test_enum_constants(self) -> None:
        names = {e["name"] for e in self.syms.enum_constants}
        self.assertEqual(names, {"NEW", "DONE"})

    def test_methods(self) -> None:
        # 匿名类里的方法（如 Comparator.compare）不属于类的对外 API，不索引
        names = {m["name"] for m in self.syms.methods}
        for expected in ("T", "sort", "doIt", "init", "run"):
            self.assertIn(expected, names)

    def test_comment_and_string_do_not_leak(self) -> None:
        syms = javasrc.parse_java(
            'class C { /* int ghost; */ String s = "int fake;"; void real() {} }'
        )
        self.assertEqual({f["name"] for f in syms.fields}, {"s"})
        self.assertEqual({m["name"] for m in syms.methods}, {"real"})


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


class TestPathSandbox(unittest.TestCase):
    """AI 给的引用路径不能越出 code_root。"""

    def setUp(self) -> None:
        self.root = ROOT

    def test_resolve_within_blocks_escape(self) -> None:
        self.assertIsNone(resolve_within(self.root, "../../etc/passwd"))
        self.assertIsNone(resolve_within(self.root, "/etc/passwd"))

    def test_resolve_within_allows_inside(self) -> None:
        self.assertIsNotNone(resolve_within(self.root, "README.md"))

    def test_verify_evidence_flags_escape(self) -> None:
        problems = tasks.verify_evidence(
            [{"file": "../../etc/passwd", "line": 1, "quote": "root"}], self.root
        )
        self.assertTrue(any("越界" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
