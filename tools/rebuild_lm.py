# -*- coding: utf-8 -*-
"""重建语言模型（修 t2s bug 造成的"常用字在语料里不存在"问题）。

用修好的 t2s 从 SRC 目录的语料重建：
  A0c  = `.adclean-lm-a0c.json`   全量干净模型（诊断基准）
  A5   = `.adclean-lm-a5.json`    生产模型：把"疑似注入字"从语料里**按字剔除**
                                  （不是整行丢弃 —— 整行丢弃会让真字跟着消失）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"F:\xwechat_files\txt-ad-cleaner")

from adclean import charclass as cc                             # noqa: E402
from adclean.processor import list_txt_files, read_text         # noqa: E402
from adclean.t2s import to_simplified                           # noqa: E402
from adclean.v2lm import LanguageModel                          # noqa: E402
from adclean.v2runner import clean_filter                       # noqa: E402

SRC = r"F:\xwechat_files\wxid_1jan9t1vnpsb22_064d\msg\file\2026-10"
TAGD = r"F:\xwechat_files\txt-ad-cleaner"
A0C = os.path.join(TAGD, ".adclean-lm-a0c.json")
A5 = os.path.join(TAGD, ".adclean-lm-a5.json")

# 必须永远留在语料里的真字
MUST_KEEP = set(
    "蒙瑙玛般赞融坑拐骗承若赤真厚布达灰西原红德丝面沉载"
    "焖焗汀邬裠夯钙嶙翱童整看严"
    "荷尔朦胧酸碱酶蛋白质脂肪胺酊酯醚酚酮"
)


def load_texts():
    files = [p for p in sorted(glob.glob(os.path.join(SRC, "*.txt")))]
    files = list_txt_files(SRC, recursive=False) or files
    texts = []
    for i, p in enumerate(files, 1):
        t, _e = read_text(p)
        texts.append(to_simplified(t))
        print("  [%d/%d] %-46s %d 字" % (i, len(files), os.path.basename(p)[:46],
                                         len(t)))
    return files, texts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=("diag", "a4", "all"))
    ap.add_argument("--minf", type=int, default=6)
    ap.add_argument("--maxf", type=int, default=120)
    ap.add_argument("--maxpair", type=int, default=2)
    args = ap.parse_args()

    print("=== 读取 14 本书（修好的 t2s）===")
    t0 = time.time()
    files, texts = load_texts()
    print("  %.1fM 字符，%.0fs" % (sum(len(t) for t in texts) / 1e6,
                                   time.time() - t0))

    # ---- A0c：全量干净模型 ----
    if os.path.exists(A0C):
        print("\n复用 A0c 缓存 %s" % os.path.basename(A0C))
        a0 = LanguageModel.load(A0C)
    else:
        t1 = time.time()
        a0 = LanguageModel().fit(texts, line_filter=clean_filter)
        print("\nA0c 训练完成：行 %d / 总量 %d / 字表 %d  (%.0fs)"
              % (a0.lines, a0.total, len(a0.uni), time.time() - t1))
        a0.save(A0C)

    print("\n=== 诊断：常用字是否还在 ===")
    for c in "蒙瑙玛般赞融坑拐骗承若赤真厚布达灰原红丝面":
        print("   %s  freq=%-6d best_pair=%-6d" % (c, a0.uni.get(c, 0),
                                                   a0.best_pair.get(c, 0)))
    nb = [c for c in a0.uni if ord(c) > 0xFFFF]
    print("   非 BMP 字表项：%d" % len(nb))

    # ---- 选"疑似注入字" ----
    bad = {c for c, f in a0.uni.items()
           if cc.is_cjk(c) and args.minf <= f <= args.maxf
           and a0.best_pair.get(c, 0) <= args.maxpair
           and c not in MUST_KEEP}
    print("\n=== 按字剔除的疑似注入字：%d 个（判据 %d<=freq<=%d 且 best_pair<=%d）==="
          % (len(bad), args.minf, args.maxf, args.maxpair))
    cand = sorted(bad, key=lambda c: -a0.uni.get(c, 0))
    print("  频次最高的 30 个：" + "".join(cand[:30]))
    print("  样例频次：" + " ".join("%s=%d" % (c, a0.uni[c]) for c in cand[:12]))
    # 反查：常见字里有没有被误判成 bad 的
    hi = [c for c in bad if a0.uni.get(c, 0) > args.maxf]
    print("  被剔掉的字里 freq > %d 的：%d 个" % (args.maxf, len(hi)))

    if args.stage == "diag":
        json.dump({"bad": "".join(cand),
                   "counts": {c: a0.uni.get(c, 0) for c in cand}},
                  open(r"F:\xwechat_files\_ad_probe\lm_bad.json", "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n（诊断模式，未写模型）")
        return 0

    # ---- A4'：按**行**过滤，统计量取自干净的 A0c ----
    if args.stage == "a4":
        REF_UNI = dict(a0.uni)
        REF_BEST = dict(a0.best_pair)

        def a4_filter(line: str) -> bool:
            if not clean_filter(line):
                return False
            for ch in line:
                if cc.is_cjk(ch):
                    f = REF_UNI.get(ch, 0)
                    if REF_BEST.get(ch, 0) <= args.maxpair and \
                            args.minf <= f <= args.maxf:
                        return False
            return True

        t3 = time.time()
        kept = 0
        total = 0
        out_lines = []
        for t in texts:
            for line in t.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if not line:
                    continue
                total += 1
                if a4_filter(line):
                    kept += 1
                    out_lines.append(line)
        print("\nA4' 过滤：保留 %d / %d 行（丢 %.1f%%）"
              % (kept, total, 100.0 * (total - kept) / max(1, total)))
        a4 = LanguageModel().fit(out_lines)
        print("A4' 训练完成：行 %d / 总量 %d / 字表 %d  (%.0fs)"
              % (a4.lines, a4.total, len(a4.uni), time.time() - t3))
        a4.save(A5)
        print("\n=== A4' 自检 ===")
        for c in "蒙瑙玛般赞融焖焗荷尔":
            print("   %s  freq=%-6d best_pair=%d" % (c, a4.uni.get(c, 0),
                                                     a4.best_pair.get(c, 0)))
        for c in "裠鸠紦弍洱遛捌俬熘":
            print("   广告字 %s  freq=%-6d best_pair=%d"
                  % (c, a4.uni.get(c, 0), a4.best_pair.get(c, 0)))
        raw = {}
        for t in texts:
            for c in t:
                raw[c] = raw.get(c, 0) + 1
        danger = [(c, raw[c]) for c, n in raw.items()
                  if n >= 20 and 0x4E00 <= ord(c) <= 0x9FFF
                  and a4.uni.get(c, 0) == 0]
        danger.sort(key=lambda kv: -kv[1])
        print("   原书出现>=20次、但模型里 freq==0 的 CJK 字：%d 种" % len(danger))
        print("      " + " ".join("%s(%d)" % (c, n) for c, n in danger[:24]))
        json.dump({"kept_lines": kept, "total_lines": total,
                   "danger": danger[:200], "a4_total": a4.total,
                   "a4_vocab": len(a4.uni)},
                  open(r"F:\xwechat_files\_ad_probe\lm_rebuild.json", "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n-> %s" % A5)
        return 0

    # ---- A5：按字剔除后重训 ----
    keep_lines = []
    for t in texts:
        for line in t.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line:
                continue
            if not clean_filter(line):
                continue
            keep_lines.append("".join(c for c in line if c not in bad))
    print("\nA5 语料：%d 行，%.1fM 字符" % (len(keep_lines),
                                          sum(len(x) for x in keep_lines) / 1e6))
    t2 = time.time()
    a5 = LanguageModel().fit(keep_lines)
    print("A5 训练完成：行 %d / 总量 %d / 字表 %d  (%.0fs)"
          % (a5.lines, a5.total, len(a5.uni), time.time() - t2))
    a5.save(A5)

    print("\n=== A5 自检 ===")
    for c in "蒙瑙玛般赞融":
        print("   %s  freq=%-6d best_pair=%d" % (c, a5.uni.get(c, 0),
                                                 a5.best_pair.get(c, 0)))
    # 模型里 freq==0 但原书里常见的字 = 危险字
    raw = {}
    for t in texts:
        for c in t:
            raw[c] = raw.get(c, 0) + 1
    danger = [(c, raw[c]) for c, n in raw.items()
              if n >= 20 and 0x4E00 <= ord(c) <= 0x9FFF and a5.uni.get(c, 0) == 0]
    danger.sort(key=lambda kv: -kv[1])
    print("   原书出现>=20次、但模型里 freq==0 的 CJK 字：%d 种" % len(danger))
    for c, n in danger[:30]:
        print("      %s 原书 %d 次%s" % (c, n, "  ← 被剔除" if c in bad else ""))
    json.dump({"bad": "".join(cand), "danger": danger[:200],
               "a5_total": a5.total, "a5_vocab": len(a5.uni)},
              open(r"F:\xwechat_files\_ad_probe\lm_rebuild.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n-> %s\n-> %s" % (A5, r"F:\xwechat_files\_ad_probe\lm_rebuild.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
