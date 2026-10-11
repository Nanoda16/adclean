# -*- coding: utf-8 -*-
"""V2 语言模型：字符级 unigram / bigram / trigram（纯标准库，零依赖）。

1. 只用**这本书自己**的统计，不引入固定中文频率表作为主要判据。
2. 用 **Markov-1 缓存插值**平滑：

       P(c | a b) = L3 * C3(a,b,c) / C2(a,b)
                  + L2 * C2(b,c)  / C1(b)
                  + L1 * C1(c)    / N

3. 语料里的污染行不参与 n-gram 统计（行过滤器由调用方给）。
4. 建 forward / backward 邻接索引供候选裁剪，避免遍历全表。
"""

from __future__ import annotations

import math

BOS = "\u0001"          # 行首
EOS = "\u0002"          # 行尾

L1, L2, L3 = 0.15, 0.35, 0.50      # 插值权重，和 = 1
UNSEEN_LOG = -12.0                  # 完全没见过的对数概率下限


class LanguageModel:
    """字符级 n-gram 语言模型（可缓存到磁盘）。"""

    CACHE_VERSION = 3

    def __init__(self) -> None:
        self.uni: dict[str, int] = {}
        self.bi: dict[str, int] = {}
        self.tri: dict[str, int] = {}
        self.fwd: dict[str, set[str]] = {}      # c -> 能接在 c 后面的字
        self.bwd: dict[str, set[str]] = {}      # c -> 能接在 c 前面的字
        self.best_pair: dict[str, int] = {}     # c -> 与任一邻居的最强搭配次数
        self.nbr: dict[str, dict[str, int]] = {}   # c -> {邻居: 共现次数}（双向）
        self.lines = 0
        self.total = 0
        self.skipped_lines = 0

    # ---- 构建 -----------------------------------------------------------
    def fit(self, texts: list[str], line_filter=None) -> "LanguageModel":
        """统计 n-gram。line_filter(line)->bool 为 False 的行不参与统计。"""
        uni: dict[str, int] = {}
        bi: dict[str, int] = {}
        tri: dict[str, int] = {}
        lines = 0
        skipped = 0
        for text in texts:
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if not line:
                    continue
                if line_filter is not None and not line_filter(line):
                    skipped += 1
                    continue
                lines += 1
                a, b = BOS, BOS
                for c in line:
                    uni[c] = uni.get(c, 0) + 1
                    bi[b + c] = bi.get(b + c, 0) + 1
                    tri[a + b + c] = tri.get(a + b + c, 0) + 1
                    a, b = b, c
                bi[b + EOS] = bi.get(b + EOS, 0) + 1
                tri[a + b + EOS] = tri.get(a + b + EOS, 0) + 1
        self.uni, self.bi, self.tri = uni, bi, tri
        self.lines, self.skipped_lines = lines, skipped
        self.total = sum(uni.values())
        self._build_index()
        return self

    def _build_index(self, min_count: int = 2) -> None:
        fwd: dict[str, set[str]] = {}
        bwd: dict[str, set[str]] = {}
        best: dict[str, int] = {}
        nbr: dict[str, dict[str, int]] = {}
        for k, n in self.bi.items():
            a, c = k[0], k[1]
            if n > best.get(a, 0):
                best[a] = n
            if n > best.get(c, 0):
                best[c] = n
            if n < min_count:
                continue
            s = fwd.get(a)
            if s is None:
                fwd[a] = {c}
            else:
                s.add(c)
            t = bwd.get(c)
            if t is None:
                bwd[c] = {a}
            else:
                t.add(a)
            d = nbr.get(a)
            if d is None:
                nbr[a] = {c: n}
            else:
                d[c] = n
            e = nbr.get(c)
            if e is None:
                nbr[c] = {a: n}
            else:
                e[a] = n
        self.fwd, self.bwd, self.best_pair, self.nbr = fwd, bwd, best, nbr

    # ---- 打分 -----------------------------------------------------------
    def logp(self, a: str, b: str, c: str) -> float:
        """log P(c | a b)，三项插值。"""
        if self.total == 0:
            return UNSEEN_LOG
        p = 0.0
        n_ab = self.bi.get(a + b, 0) if a and b else 0
        if n_ab:
            n_abc = self.tri.get(a + b + c, 0)
            if n_abc:
                p += L3 * (n_abc / n_ab)
        n_b = self.uni.get(b, 0) if b else 0
        if n_b:
            n_bc = self.bi.get(b + c, 0)
            if n_bc:
                p += L2 * (n_bc / n_b)
        n_c = self.uni.get(c, 0)
        if n_c:
            p += L1 * (n_c / self.total)
        return math.log(p) if p > 0 else UNSEEN_LOG

    def text_score(self, s: str) -> float:
        """一段文字的"像不像正常中文"总分（BOS/EOS 包裹）。"""
        if not s:
            return 0.0
        a, b = BOS, BOS
        total = 0.0
        for c in s:
            total += self.logp(a, b, c)
            a, b = b, c
        total += self.logp(a, b, EOS)
        return total

    # ---- REPLACE 候选：预测某个位置最可能是什么字 -------------------------
    def predict(self, prev2: str, prev1: str, nxt1: str, topk: int = 8) -> list[tuple[str, float]]:
        """在 `prev2 prev1 [洞] nxt1` 里，模型认为最可能填什么字。

        对候选集（能接在 prev1 后、或能接在 nxt1 前的字）按
            log P(c | prev2, prev1) + log P(nxt1 | prev1, c)
        排序。候选集由邻接索引给出。
        """
        cands = self.fwd.get(prev1, set()) | self.bwd.get(nxt1, set())
        if not cands:
            return []
        a, b = prev2, prev1
        scored = []
        for c in cands:
            if c in (BOS, EOS):
                continue
            s = self.logp(a, b, c) + self.logp(b, c, nxt1)
            scored.append((c, s))
        scored.sort(key=lambda kv: -kv[1])
        return scored[:topk]

    # ---- 缓存 -----------------------------------------------------------
    def save(self, path: str) -> None:
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": self.CACHE_VERSION,
                "lines": self.lines,
                "total": self.total,
                "skipped_lines": self.skipped_lines,
                "uni": self.uni,
                "bi": self.bi,
                "tri": self.tri,
            }, f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "LanguageModel":
        import json
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") != cls.CACHE_VERSION:
            raise ValueError("语言模型缓存版本不匹配")
        m = cls()
        m.lines = d["lines"]
        m.total = d["total"]
        m.skipped_lines = d.get("skipped_lines", 0)
        m.uni, m.bi, m.tri = d["uni"], d["bi"], d["tri"]
        m._build_index()
        return m

    def stats(self) -> dict:
        return {
            "lines": self.lines,
            "total": self.total,
            "uni": len(self.uni),
            "bi": len(self.bi),
            "tri": len(self.tri),
            "skipped_lines": self.skipped_lines,
        }
