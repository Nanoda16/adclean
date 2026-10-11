# -*- coding: utf-8 -*-
"""adclean 真实语料基准测试。

用法：
    python benchmarks/validate.py <小说目录> [--sample 400]

A. 误伤率：随机抽 N 行正常正文，统计被改动的比例。
B. 清除率：含非常规字符的广告行，清洗后仍残留非常规字符的比例。
C. 打印对照样本，便于人工抽检。
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean import Model, Config, clean_line          # noqa: E402
from adclean.processor import list_txt_files, read_text  # noqa: E402

EXOTIC = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
    r"\u0370-\u03ff\u0400-\u04ff\u3040-\u30ff"
    r"\u1d00-\u1d7f\u1e00-\u1eff\ua720-\ua7ff\uab00-\uab6f"
    r"\U0001D400-\U0001D7FF\u16a0-\u16ff\u10330-\u1034f"
    r"\u1400-\u167f\u13a0-\u13ff\U00016F00-\U00016F9F]"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="adclean 真实语料基准测试")
    ap.add_argument("folder", help="包含 txt 的目录")
    ap.add_argument("--sample", type=int, default=400, help="随机抽检行数")
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--show", type=int, default=12, help="展示多少个对照样本")
    args = ap.parse_args()

    files = list_txt_files(args.folder)
    if not files:
        print("没有找到 txt 文件", file=sys.stderr)
        return 2
    texts = {}
    for p in files:
        try:
            texts[os.path.basename(p)] = read_text(p)[0]
        except OSError:
            continue

    t0 = time.time()
    model = Model()
    model.fit(list(texts.values()), rare_threshold=Config.rare_threshold)
    st = model.stats()
    print(f"语料：{len(texts)} 个文件 / {st['total_chars']:,} 字 / "
          f"字符 {st['chars']:,} 种 / 搭配 {st['bigrams']:,} 种")
    print(f"学习到广告伪装字 {st['ad_chars']} 个，可疑窗口 {st['suspicious_windows']} 个"
          f"（学习耗时 {time.time() - t0:.1f}s）\n")

    cfg = Config()

    # ---- A. 误伤率 ----
    rnd = random.Random(args.seed)
    pool = []
    for name, text in texts.items():
        for line in text.split("\n"):
            s = line.strip()
            if 20 <= len(s) <= 120:
                pool.append((name, s))
    sample = rnd.sample(pool, min(args.sample, len(pool)))
    hurt = []
    for name, line in sample:
        r = clean_line(line, model, cfg)
        if r.changed:
            hurt.append((name, line, r))
    print(f"[A] 随机正文抽检 {len(sample)} 行，被改动 {len(hurt)} 行 "
          f"（误伤率 {len(hurt) / max(len(sample), 1) * 100:.2f}%）")
    for name, line, r in hurt[:12]:
        print(f"    {name[:12]} | {line[:56]}")
        print(f"        -> {r.cleaned[:56]}   (删 {r.removed!r})")

    # ---- B. 清除率 ----
    total = left = 0
    examples = []
    for name, text in texts.items():
        for line in text.split("\n"):
            if len(line.strip()) <= 10 or not EXOTIC.search(line):
                continue
            total += 1
            r = clean_line(line, model, cfg)
            if EXOTIC.search(r.cleaned):
                left += 1
            elif len(examples) < args.show:
                examples.append((name, line, r.cleaned))
    print(f"\n[B] 含非常规字符的广告行 {total} 行，清洗后仍残留 {left} 行 "
          f"（清除率 {(total - left) / max(total, 1) * 100:.1f}%）")
    for name, before, after in examples:
        print(f"    {name[:12]}")
        print(f"      原: {before[:100]}")
        print(f"      后: {after[:100]}")

    # ---- C. 逐文件改动统计 ----
    print("\n[C] 逐文件统计（改动行数 / 总行数）")
    for name, text in texts.items():
        lines = text.split("\n")
        changed = 0
        for line in lines:
            if line and clean_line(line, model, cfg).changed:
                changed += 1
        print(f"    {name[:26]:<28} {changed:>7} / {len(lines):>7} "
              f"({changed / max(len(lines), 1) * 100:.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
