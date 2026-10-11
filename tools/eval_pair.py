# -*- coding: utf-8 -*-
"""成对书评测：用「有广版 vs 精校版」这一对文件当金标准，量化引擎捞得干不干净。

金标准：有广版有、精校版没有的字 = 该删的注入字（召回分母）；
两版都有的字 = 真字（引擎删了就是误删）。两版排版不同、精校版偶有改写，
所以先归一化空白再对齐，只保留高相似度的配对，并排除卷标题/分隔线/纯标点。

用法
----
    python tools/eval_pair.py                     # 默认评测生产引擎（V4 语义）
    python tools/eval_pair.py --engine v3         # 对比旧 V3
    python tools/eval_pair.py --json out.json     # 落盘详细结果
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSG = r"F:\xwechat_files\wxid_1jan9t1vnpsb22_064d\msg\file\2026-10"
AD_BOOK = os.path.join(MSG, "哪有人的青春恋爱物语从魔女审判开始啊！.txt")
CLEAN_BOOK = os.path.join(MSG, "哪有人的青春恋爱物语从魔女审判开始啊！ (1).txt")
CACHE = os.path.join(r"F:\xwechat_files\_ad_probe", "pair_gold.json")

WS = str.maketrans("", "", " \t\r\u00a0\u3000")
GAP = re.compile(r"[ \u3000]{6,}")
PUNCT = set("，。！？；：、“”‘’（）《》—…·「」【】~～!?,.;:'\"()[]{}<>-—_/\\|@#$%^&*+=")
BANNED = ["common_char_freq", "anchor_min", "min_freq", "max_freq"]


def _norm(s: str) -> str:
    return s.translate(WS)


def _pure_punct(s: str) -> bool:
    return all(c in PUNCT or c.isspace() for c in s)


def load_books():
    with open(AD_BOOK, encoding="utf-8-sig") as f:
        a = f.read().split("\n")
    with open(CLEAN_BOOK, encoding="utf-8-sig") as f:
        b = f.read().split("\n")
    return a, b


def build_gold(force: bool = False):
    """返回 (gold, clean_ok, a, b)。gold: {行号: set(该删的位置)}。"""
    if not force and os.path.exists(CACHE):
        d = json.load(open(CACHE, encoding="utf-8"))
        a, b = load_books()
        return ({int(k): set(v) for k, v in d["gold"].items()},
                d["clean_ok"], a, b)

    a, b = load_books()
    na, nb = [_norm(x) for x in a], [_norm(x) for x in b]
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1))
        elif tag == "replace":
            al, bl = list(range(i1, i2)), list(range(j1, j2))
            if len(al) > 400 or len(bl) > 400:
                pairs.extend(zip(al, bl))
                continue
            cand = sorted(((difflib.SequenceMatcher(None, na[x], nb[y]).ratio(), x, y)
                           for x in al for y in bl), reverse=True)
            ua, ub = set(), set()
            for _r, x, y in cand:
                if x in ua or y in ub:
                    continue
                ua.add(x)
                ub.add(y)
                pairs.append((x, y))
    pairs.sort()

    gold: dict[int, set] = {}
    clean_ok: list[int] = []
    stats = collections.Counter()
    for ai, bi in pairs:
        ra, rb = a[ai], b[bi]
        same = _norm(ra) == _norm(rb)
        if GAP.search(ra) or GAP.search(rb):
            stats["跳过·长空格折行"] += 1
            continue
        if same:
            clean_ok.append(ai)
            continue
        na_i, nb_i = _norm(ra), _norm(rb)
        if difflib.SequenceMatcher(None, na_i, nb_i).ratio() < 0.80:
            stats["跳过·相似度过低"] += 1
            continue
        ops = difflib.SequenceMatcher(None, na_i, nb_i, autojunk=False).get_opcodes()
        aonly, breal = [], []
        for tag, i1, i2, j1, j2 in ops:
            if tag in ("delete", "replace") and na_i[i1:i2].strip():
                aonly.append((i1, i2))
            if tag in ("insert", "replace") and nb_i[j1:j2].strip() \
                    and not _pure_punct(nb_i[j1:j2]):
                breal.append(nb_i[j1:j2])
        if breal:
            stats["跳过·精校版有改写"] += 1
            continue
        pos = [p for p, c in enumerate(ra) if c not in " \t\r\u00a0\u3000"]
        s = set()
        for i1, i2 in aonly:
            seg = na_i[i1:i2]
            if i1 == 0 and na_i.startswith("第") and "卷" in seg[:6]:
                stats["忽略·卷标题"] += 1
                continue
            if all(c in "—–-" for c in seg):
                stats["忽略·分隔线"] += 1
                continue
            if _pure_punct(seg):
                stats["忽略·标点"] += 1
                continue
            stats["注入字"] += i2 - i1
            for q in range(i1, i2):
                if q < len(pos):
                    s.add(pos[q])
        if s:
            gold[ai] = s

    json.dump({"gold": {str(k): sorted(v) for k, v in gold.items()},
               "clean_ok": clean_ok, "stats": dict(stats)},
              open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("金标准已生成 →", CACHE)
    return gold, clean_ok, a, b


def evaluate(engine, a, gold, clean_ok, kind=""):
    removed: dict[int, set] = {}
    t0 = time.time()
    for i, line in enumerate(a):
        if not line:
            continue
        r = engine.clean_line(line)
        if r.removed:
            removed[i] = set(r.removed)
    dt = time.time() - t0

    tp = fp = fn = 0
    full = part = none = 0
    for ai, g in gold.items():
        e = removed.get(ai, set())
        tp += len(g & e)
        fn += len(g - e)
        fp += len(e - g)
        if not e:
            none += 1
        elif not (g - e) and not (e - g):
            full += 1
        else:
            part += 1
    fp_clean = sum(len(removed.get(ai, set())) for ai in clean_ok)
    fp_clean_lines = sum(1 for ai in clean_ok if removed.get(ai, set()))
    prec = tp / (tp + fp + fp_clean) if tp + fp + fp_clean else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {
        "engine": kind or type(engine.cfg).__name__,
        "gold_lines": len(gold),
        "gold_chars": tp + fn,
        "fully_cleaned": full,
        "fully_cleaned_rate": full / max(1, len(gold)),
        "partially_cleaned": part,
        "untouched": none,
        "tp": tp, "fp_on_gold_lines": fp, "fn": fn,
        "clean_lines": len(clean_ok),
        "fp_on_clean_lines": fp_clean,
        "clean_lines_damaged": fp_clean_lines,
        "precision": prec, "recall": rec, "f1": f1,
        "seconds": round(dt, 1),
        "per_clean_line": fp_clean / max(1, len(clean_ok)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="semantic", choices=("semantic", "v3"))
    ap.add_argument("--json", default="")
    ap.add_argument("--rebuild-gold", action="store_true")
    args = ap.parse_args()

    from adclean.pipeline import build_engine
    from adclean.semantic import SemanticConfig

    gold, clean_ok, a, b = build_gold(force=args.rebuild_gold)
    print("金标准：%d 行含注入字（共 %d 字）｜可靠干净行 %d"
          % (len(gold), sum(len(v) for v in gold.values()), len(clean_ok)))
    eng = build_engine(kind=args.engine)
    cfg = eng.cfg
    forbidden = [x for x in BANNED if hasattr(cfg, x)]
    if forbidden:
        print("！！引擎配置里仍有字频判据：%s" % forbidden)
    res = evaluate(eng, a, gold, clean_ok, kind=args.engine)
    print("\n引擎 = %s" % res["engine"])
    print("  完全修好      %4d/%d = %5.1f%%"
          % (res["fully_cleaned"], res["gold_lines"],
             100 * res["fully_cleaned_rate"]))
    print("  字符级  TP %5d  漏删 %5d  误删 %5d（金标准行）/ %5d（干净行）"
          % (res["tp"], res["fn"], res["fp_on_gold_lines"], res["fp_on_clean_lines"]))
    print("  召回 %.1f%%  准确 %.1f%%  F1 %.1f%%"
          % (100 * res["recall"], 100 * res["precision"], 100 * res["f1"]))
    print("  干净行被误删：%d/%d 行（每行平均 %.5f 字）"
          % (res["clean_lines_damaged"], res["clean_lines"], res["per_clean_line"]))
    print("  耗时 %.1fs" % res["seconds"])
    if args.json:
        json.dump(res, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("→", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
