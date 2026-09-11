"""单元测试：覆盖切块、静态判定、结果合并/验真、报告渲染、缓存、错误码配置。

在仓库根目录执行：

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# 让测试在任意目录下都能 import 到 prdcode
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prdcode import codeindex, matcher, prd, recall, report, tasks
from prdcode.codeindex import CodeIndex
from prdcode.utils import find_error_codes


class TestPrdSplit(unittest.TestCase):
    """PRD 切块：按标题 / 列表 / 表格 / 代码块 / 引用切。"""

    SRC = """# 退款模块

## 1. 背景

退款是逆向流程。

## 2. 撤销退款

- 按批次整批撤销
- 入参必填 refundBatchNo

## 3. 变更记录

| 版本 | 说明 |
| :--- | :--- |
| v1.0 | 初稿 |

## 4. 示例

```java
int x = 1;
```

> 引用说明
"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.md = Path(self.tmp.name) / "需求.md"
        self.md.write_text(self.SRC, encoding="utf-8")

    def test_collect_md_files(self) -> None:
        found = prd.collect_md_files([Path(self.tmp.name)])
        self.assertEqual([f.name for f in found], ["需求.md"])

    def test_split_kinds(self) -> None:
        blocks = prd.split_prd([self.md], base=Path(self.tmp.name))
        kinds = {b["kind"] for b in blocks}
        for expected in ("heading", "list", "table", "code", "quote"):
            self.assertIn(expected, kinds)

    def test_non_requirement_flagged(self) -> None:
        blocks = prd.split_prd([self.md], base=Path(self.tmp.name))
        self.assertTrue(any(not b["likely_requirement"] for b in blocks))

    def test_block_line_range(self) -> None:
        blocks = prd.split_prd([self.md], base=Path(self.tmp.name))
        for b in blocks:
            self.assertLessEqual(b["start_line"], b["end_line"])


class TestStaticJudge(unittest.TestCase):
    """静态判定的各分支。"""

    def setUp(self) -> None:
        self.index = CodeIndex()
        self.index.add_type(
            "RefundService",
            {"file": "R.java", "line": 1, "kind": "interface", "name": "RefundService"},
        )
        self.index.add_method(
            "withdraw", {"file": "R.java", "line": 5, "end_line": 9, "owner": "RefundService"}
        )
        self.index.add_url(
            "POST /refund/withdraw",
            {"file": "C.java", "line": 10, "handler": "withdraw", "owner": "C", "http": "POST"},
        )

    def _judge(self, assertion: str, kind: str = "existence") -> dict:
        item = {"id": "R-1", "kind": kind, "assertion": assertion, "text": ""}
        return matcher.judge_static(item, self.index)

    def test_existence_implemented(self) -> None:
        self.assertEqual(self._judge("接口 /refund/withdraw 存在")["verdict"], "implemented")

    def test_existence_missing(self) -> None:
        self.assertEqual(self._judge("接口 /refund/cancel 存在")["verdict"], "missing")

    def test_type_implemented(self) -> None:
        self.assertEqual(self._judge("类型 RefundService 存在")["verdict"], "implemented")

    def test_behavior_routes_to_ai(self) -> None:
        result = self._judge("撤销必须整批原子关闭", kind="behavior")
        self.assertEqual(result["route"], "ai")
        self.assertEqual(result["verdict"], "need_ai")

    def test_no_identifier_routes_to_ai(self) -> None:
        self.assertEqual(self._judge("系统要稳定", kind="existence")["route"], "ai")

    def test_ambiguous_when_multiple_hits(self) -> None:
        index = CodeIndex()
        index.add_method("dup", {"file": "A.java", "line": 1, "end_line": 2, "owner": "X"})
        index.add_method("dup", {"file": "B.java", "line": 1, "end_line": 2, "owner": "Y"})
        item = {"id": "R", "kind": "existence", "assertion": "方法 `dup()` 存在", "text": ""}
        self.assertEqual(matcher.judge_static(item, index)["verdict"], "ambiguous")


class TestMergeAndVerify(unittest.TestCase):
    """结果回收、引用验真、合并、分类。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "Foo.java").write_text(
            "line1\nline2\npublic void foo() {}\n", encoding="utf-8"
        )

    def test_verify_evidence_ok(self) -> None:
        problems = tasks.verify_evidence(
            [{"file": "Foo.java", "line": 3, "quote": "public void foo() {}"}], self.root
        )
        self.assertEqual(problems, [])

    def test_verify_evidence_bad_quote(self) -> None:
        problems = tasks.verify_evidence(
            [{"file": "Foo.java", "line": 3, "quote": "public void bar() {}"}], self.root
        )
        self.assertTrue(problems)

    def test_verify_evidence_missing_file(self) -> None:
        problems = tasks.verify_evidence(
            [{"file": "Nope.java", "line": 1, "quote": "x"}], self.root
        )
        self.assertTrue(any("不存在" in p for p in problems))

    def test_merge_bounces_bad_quote(self) -> None:
        items = [{"id": "R-1", "assertion": "a", "kind": "behavior", "text": ""}]
        static = [{"item_id": "R-1", "route": "ai"}]
        ai = {
            "R-1": {
                "item_id": "R-1",
                "verdict": "implemented",
                "evidence": [{"file": "Foo.java", "line": 3, "quote": "wrong"}],
                "reason": "x",
                "missing": [],
            }
        }
        merged = tasks.merge_results(items, static, ai, self.root)
        self.assertEqual(merged[0]["verdict"], "undecidable")
        self.assertEqual(merged[0]["checked_by"], "AI（已打回）")

    def test_merge_ai_not_processed(self) -> None:
        items = [{"id": "R-1", "assertion": "a", "kind": "behavior", "text": ""}]
        static = [{"item_id": "R-1", "route": "ai"}]
        merged = tasks.merge_results(items, static, {}, self.root)
        self.assertEqual(merged[0]["checked_by"], "待处理")

    def test_classify(self) -> None:
        self.assertEqual(tasks.classify({"verdict": "implemented"}), "ok")
        self.assertEqual(tasks.classify({"verdict": "missing", "checked_by": "脚本"}), "fix")
        self.assertEqual(
            tasks.classify({"verdict": "missing", "checked_by": "AI（已验真）"}), "confirm"
        )
        self.assertEqual(
            tasks.classify({"verdict": "deviated", "checked_by": "AI（已验真）"}), "fix"
        )
        self.assertEqual(
            tasks.classify({"verdict": "partial", "checked_by": "AI（已验真）"}), "confirm"
        )


class TestReportRender(unittest.TestCase):
    """报告渲染：两份文件、分类正确。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _record(self, verdict: str, assertion: str) -> dict:
        return {
            "item_id": assertion,
            "group": "g",
            "assertion": assertion,
            "source": {"file": "需求.md", "line": 1},
            "kind": "existence",
            "route": "static",
            "verdict": verdict,
            "evidence": [{"file": "A.java", "line": 1, "quote": "x"}],
            "reason": "没搜到" if verdict == "missing" else "",
            "missing": [],
            "checked_by": "脚本",
            "search_log": {},
        }

    def test_render_reports_creates_two_files(self) -> None:
        records = [self._record("missing", "缺失项"), self._record("implemented", "已实现项")]
        out = Path(self.tmp.name) / "out"
        fix, confirm = report.render_reports(records, [], out, self.root)
        self.assertTrue(fix.exists())
        self.assertTrue(confirm.exists())
        fix_text = fix.read_text(encoding="utf-8")
        self.assertIn("缺失项", fix_text)
        self.assertNotIn("已实现项", fix_text.split("## 抽查")[0])

    def test_is_ok_and_is_confirm(self) -> None:
        self.assertTrue(report.is_ok({"verdict": "implemented"}))
        self.assertFalse(report.is_confirm({"verdict": "implemented"}))
        self.assertTrue(
            report.is_confirm({"verdict": "undecidable", "checked_by": "AI（已验真）"})
        )

    def test_sample_gate_warning(self) -> None:
        """抽查必须作为 01-待修复 的可信度闸门显式提示。"""
        records = [self._record("missing", "缺失项"), self._record("implemented", "已实现项")]
        out = Path(self.tmp.name) / "out2"
        fix, _ = report.render_reports(records, [], out, self.root)
        text = fix.read_text(encoding="utf-8")
        self.assertIn("先抽查", text)
        self.assertIn("可信度闸门", text)


class TestIndexCache(unittest.TestCase):
    """索引缓存：指纹没变复用，源码变了重扫。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "A.java").write_text("class A { void f() {} }", encoding="utf-8")
        self.cache = self.root / "cache.json"

    def test_second_build_hits_cache(self) -> None:
        _, first = codeindex.build_index_cached(
            [self.root], base=self.root, cache_path=self.cache
        )
        _, second = codeindex.build_index_cached(
            [self.root], base=self.root, cache_path=self.cache
        )
        self.assertFalse(first)
        self.assertTrue(second)

    def test_change_invalidates_cache(self) -> None:
        codeindex.build_index_cached([self.root], base=self.root, cache_path=self.cache)
        (self.root / "A.java").write_text(
            "class A { void f() {} void g() {} }", encoding="utf-8"
        )
        index, cached = codeindex.build_index_cached(
            [self.root], base=self.root, cache_path=self.cache
        )
        self.assertFalse(cached)
        self.assertIn("g", index.methods)


class TestErrorCodeConfig(unittest.TestCase):
    """错误码位数可配置。"""

    def test_default_five_digits(self) -> None:
        self.assertEqual(find_error_codes("错误码 41026"), ["41026"])
        self.assertEqual(find_error_codes("错误码 4001"), [])

    def test_configurable_digits(self) -> None:
        os.environ["CHECKPRD_ERROR_CODE_DIGITS"] = "4"
        self.addCleanup(os.environ.pop, "CHECKPRD_ERROR_CODE_DIGITS", None)
        self.assertEqual(find_error_codes("错误码 4001"), ["4001"])


class TestRecall(unittest.TestCase):
    """BM25 召回：中文注释代码 + 纯中文需求。"""

    REFUND = """
package demo;
/** 退款服务实现。 */
public class RefundServiceImpl {
    /** 提交退款申请：按商品行原子拆单，失败不留半单。 */
    public void apply(RefundDTO dto) {}
    /** 按批次整批原子撤销退款，本批次之外的不能撤。 */
    public void withdraw(RefundDTO dto) {}
}
"""
    COUPON = """
package demo;
/** 优惠券服务。 */
public class CouponServiceImpl {
    /** 优惠券过期后必须落一条失效记录，便于对账。 */
    public void expire(Long id) {}
}
"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "RefundServiceImpl.java").write_text(self.REFUND, encoding="utf-8")
        (self.root / "CouponServiceImpl.java").write_text(self.COUPON, encoding="utf-8")
        self.index = codeindex.build_index([self.root], base=self.root)

    def test_tokenize_cjk_bigram(self) -> None:
        tokens = recall.tokenize("提交申请")
        self.assertIn("提交", tokens)
        self.assertIn("申请", tokens)

    def test_tokenize_camel(self) -> None:
        self.assertIn("refund", recall.tokenize("RefundApplyDTO"))

    def test_ranks_correct_file(self) -> None:
        ri = recall.RecallIndex.from_code_index(self.index)
        hits = ri.search("提交退款申请按商品行拆单", top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["file"], "RefundServiceImpl.java")

    def test_english_only_no_signal(self) -> None:
        index = CodeIndex()
        index.add_method(
            "apply",
            {"file": "A.java", "line": 1, "end_line": 2, "owner": "X",
             "doc": "Submit refund application"},
        )
        ri = recall.RecallIndex.from_code_index(index)
        self.assertEqual(ri.search("提交退款申请"), [])

    def test_task_includes_recalled_code(self) -> None:
        items = [
            {
                "id": "R-1",
                "group": "退款",
                "kind": "behavior",
                "assertion": "提交退款申请按商品行原子拆单",
                "text": "提交退款申请按商品行原子拆单，失败不留半单",
                "source": {"file": "需求.md", "line": 1},
            }
        ]
        static = [{"item_id": "R-1", "route": "ai"}]
        work = Path(self.tmp.name) / ".checkprd"
        paths = tasks.build_task_packages(work, items, static, self.index, self.root)
        self.assertTrue(paths)
        content = paths[0].read_text(encoding="utf-8")
        self.assertIn("RefundServiceImpl.java", content)


if __name__ == "__main__":
    unittest.main()
