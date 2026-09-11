"""BM25 语义召回（纯标准库）。

工具原本只按"标识符"召回代码：需求写中文"提交申请"、代码是英文 ``apply`` 时
搜不到，容易误判"没做"。这里补一层词面相似度召回——把代码里的**注释 + 标识符**
当语料，用 BM25 把和需求文字最接近的类型/方法找出来。

中文没有空格，用"字符 bigram"当词；英文按单词取，并把驼峰拆开。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_CJK = r"\u4e00-\u9fff"
_WORD_RE = re.compile(r"[a-z][a-z0-9_]{1,}")
_CJK_RE = re.compile(rf"[{_CJK}]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# BM25 常规参数
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """分词：英文单词（驼峰拆开）+ 中文字符 bigram。"""
    text = _CAMEL_RE.sub(" ", text).lower()
    tokens = _WORD_RE.findall(text)
    for seg in _CJK_RE.findall(text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def build_docs(index) -> list[dict]:
    """把索引里的类型/方法变成一篇篇"文档"（注释 + 名字 + 归属）。"""
    docs: list[dict] = []
    for name, items in index.types.items():
        for it in items:
            docs.append(
                {
                    "file": it.get("file", ""),
                    "line": it.get("line", 1),
                    "name": name,
                    "owner": "",
                    "text": f"{it.get('doc', '')} {name} {it.get('file', '')}",
                }
            )
    for name, items in index.methods.items():
        for it in items:
            docs.append(
                {
                    "file": it.get("file", ""),
                    "line": it.get("line", 1),
                    "name": name,
                    "owner": it.get("owner", ""),
                    "text": f"{it.get('doc', '')} {name} {it.get('owner', '')}",
                }
            )
    return docs


class RecallIndex:
    """对一批"文档"建好的 BM25 索引，可反复查询。"""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self._tf = [Counter(tokenize(d["text"])) for d in docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(docs)
        self._idf = {
            t: math.log((n - c + 0.5) / (c + 0.5) + 1) for t, c in df.items()
        }

    @classmethod
    def from_code_index(cls, index) -> "RecallIndex":
        """从一个代码索引进建 BM25 索引。"""
        return cls(build_docs(index))

    def search(self, query: str, top_k: int = 5, min_ratio: float = 0.0) -> list[dict]:
        """返回得分最高的若干条 ``{file, line, name, owner, score}``。

        ``min_ratio`` 是相对阈值：得分低于"最高分 × min_ratio"的一律丢掉，
        避免把只有一两个词偶然重合的无关文件也塞进来。
        """
        terms = set(tokenize(query))
        if not terms or not self.docs:
            return []
        scores: list[float] = []
        for tf, dl in zip(self._tf, self._len):
            s = 0.0
            for t in terms:
                freq = tf.get(t, 0)
                if not freq:
                    continue
                s += self._idf.get(t, 0.0) * freq * (_K1 + 1) / (
                    freq + _K1 * (1 - _B + _B * dl / max(1.0, self._avg))
                )
            scores.append(s)
        ranked = sorted(range(len(self.docs)), key=lambda i: -scores[i])
        top = scores[ranked[0]] if ranked else 0.0
        out: list[dict] = []
        for i in ranked[:top_k]:
            if scores[i] <= 0 or scores[i] < top * min_ratio:
                break
            d = self.docs[i]
            out.append(
                {
                    "file": d["file"],
                    "line": d["line"],
                    "name": d["name"],
                    "owner": d["owner"],
                    "score": scores[i],
                }
            )
        return out
