# -*- coding: utf-8 -*-
"""adclean.cli —— 命令行入口。"""
from __future__ import annotations

import argparse
import os
import sys

from .engine import Config
from .processor import build_model, list_txt_files, process_files


def _progress(done: int, total: int, msg: str) -> None:
    total = max(total, 1)
    pct = int(done / total * 100)
    sys.stderr.write(f"\r[{pct:3d}%] {msg[:60]:<60}")
    sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adclean",
        description="中文小说 TXT 广告（QQ群 / TG频道）校正工具："
                    "删除被逐字穿插进正文的广告字，原文件不会被修改。",
    )
    p.add_argument("inputs", nargs="+", help="txt 文件或包含 txt 的目录")
    p.add_argument("-o", "--out", default="cleaned",
                   help="输出目录（默认 ./cleaned）")
    p.add_argument("--in-place-suffix", action="store_true",
                   help="输出到原目录，命名为 原名.cleaned.txt")
    p.add_argument("--report", default="",
                   help="变更报告 JSON 路径（默认写到输出目录/report.json）")
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")

    g = p.add_argument_group("引擎参数（一般无需修改）")
    g.add_argument("--alpha", type=float, default=Config.alpha,
                   help="从未共现的字对的惩罚（默认 2.0，越大越激进）")
    g.add_argument("--gain-threshold", type=float, default=Config.gain_threshold,
                   help="删除片段的增益阈值（默认 1.0）")
    g.add_argument("--rare-threshold", type=int, default=Config.rare_threshold,
                   help="稀有汉字判定阈值：全语料出现次数（默认 200）")
    g.add_argument("--common-threshold", type=int, default=Config.common_threshold,
                   help="候选汉字判定阈值（默认 12000）")
    g.add_argument("--max-span", type=int, default=Config.max_span,
                   help="单次删除片段的最大长度（默认 24）")
    g.add_argument("--keep-unusual-blocks", action="store_true",
                   help="保留数学字母/卢恩/哥特等异常字符（默认删除）")
    g.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    g.add_argument("--model-cache", default=".adclean-model.json",
                   help="语料模型缓存路径；留空表示不缓存")
    g.add_argument("--no-model-cache", action="store_true", help="禁用模型缓存")
    g.add_argument("--whitelist", default="",
                   help="永不删除的字符白名单（例如被误伤的生僻字：--whitelist 洱遛鸠）")
    g.add_argument("--aggressive", action="store_true",
                   help="强力模式：连夹在常用字里的广告碎片一起清，"
                        "但会误删少量生僻正常字；"
                        "被误删的字可用 --whitelist 永久保护")
    g.add_argument("--v2", action="store_true",
                   help="V2 引擎：不再按「广告字」判断，改为"
                        "「污染检测 + 最小编辑恢复」（KEEP/DELETE/REPLACE + "
                        "语言模型评分 + 置信度三档）")
    g.add_argument("--v2-cache", default=".adclean-lm.json",
                   help="V2 语言模型缓存路径（默认 .adclean-lm.json）")
    g.add_argument("--v2-rebuild", action="store_true",
                   help="忽略 V2 语言模型缓存，强制重建")
    g.add_argument("--no-simplify", action="store_true",
                   help="关闭繁→简转换（默认开启，仅影响 --v2 分支）")
    return p


def _run_v2(args, files: list[str], progress) -> int:
    """V2 引擎分支：污染检测 + 最小编辑恢复。"""
    from .v2engine import V2Config
    from .v2runner import get_language_model, process_files_v2

    cache = "" if args.no_model_cache else (args.v2_cache or "")
    simplify = not args.no_simplify
    sys.stderr.write("V2 引擎：构建字符级 unigram/bigram/trigram 语言模型…"
                     "（字形：%s）\n" % ("繁→简" if simplify else "保留原字形"))
    lm = get_language_model(files, cache, progress=progress,
                            rebuild=args.v2_rebuild, simplify=simplify)
    st = lm.stats()
    sys.stderr.write(
        "\n语言模型：%d 行 / %d 字 / 单字 %d / 双字对 %d / 三字组 %d "
        "（排除污染行 %d）\n"
        % (st["lines"], st["total"], st["uni"], st["bi"], st["tri"],
           st["skipped_lines"]))

    cfg = V2Config(whitelist=frozenset(args.whitelist))
    out_dir = args.out
    report_path = args.report or (
        os.path.join(os.path.dirname(files[0]), "adclean_report_v2.json")
        if args.in_place_suffix else os.path.join(out_dir, "report.json"))

    reports = process_files_v2(
        files, out_dir, lm, cfg, progress=progress,
        report_path=report_path, same_folder=args.in_place_suffix,
        simplify=simplify)
    sys.stderr.write("\n")
    tot_changed = tot_deleted = tot_repl = tot_review = 0
    for r in reports:
        if r.get("error"):
            sys.stderr.write("  %s 失败：%s\n" % (os.path.basename(r["src"]), r["error"]))
            continue
        tot_changed += r["changed_lines"]
        tot_deleted += r["deleted_chars"]
        tot_repl += r["replaced_chars"]
        tot_review += r["review_pending"]
        sys.stderr.write(
            "  %s → %s\n      行 %d / 改动 %d / 删除 %d 字 / 替换 %d 字 / "
            "待审核 %d (%ss)\n"
            % (os.path.basename(r["src"]), r["dst"], r["lines"],
               r["changed_lines"], r["deleted_chars"], r["replaced_chars"],
               r["review_pending"], r["seconds"]))
    sys.stderr.write(
        "\n完成：%d 个文件，改动 %d 行，删除 %d 字，替换 %d 字，待审核 %d 处。\n"
        "报告：%s\n"
        % (len(reports), tot_changed, tot_deleted, tot_repl, tot_review, report_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    files: list[str] = []
    for it in args.inputs:
        if os.path.isdir(it):
            files.extend(list_txt_files(it, recursive=not args.no_recursive))
        elif os.path.isfile(it):
            files.append(it)
        else:
            sys.stderr.write(f"跳过不存在的路径：{it}\n")
    if not files:
        sys.stderr.write("没有找到任何 txt 文件。\n")
        return 2

    cfg = Config(
        alpha=args.alpha,
        gain_threshold=args.gain_threshold,
        rare_threshold=args.rare_threshold,
        common_threshold=args.common_threshold,
        max_span=args.max_span,
        drop_unusual_blocks=not args.keep_unusual_blocks,
        whitelist=frozenset(args.whitelist),
        aggressive=args.aggressive,
    )

    sys.stderr.write(f"待处理文件：{len(files)} 个\n")

    if args.v2:
        return _run_v2(args, files, _progress)

    cache = "" if args.no_model_cache else (args.model_cache or "")
    model = None
    if cache and os.path.exists(cache):
        try:
            from .engine import Model
            model = Model.load(cache)
            sys.stderr.write(f"已加载模型缓存：{cache}\n")
        except Exception as e:                       # noqa: BLE001
            sys.stderr.write(f"模型缓存不可用（{e}），重新学习。\n")
            model = None
    if model is None:
        sys.stderr.write("正在学习语料用字统计…\n")
        model = build_model(files, cfg.rare_threshold, progress=_progress)
        if cache:
            try:
                model.save(cache)
                sys.stderr.write(f"\n模型已缓存到 {cache}\n")
            except OSError:
                pass
    sys.stderr.write("\n")
    st = model.stats()
    sys.stderr.write(
        f"语料统计：字符 {st['chars']} 种 / 相邻对 {st['bigrams']} 种 / "
        f"总字数 {st['total_chars']} / 稀有字 {st['rare_cjk']} 个 / "
        f"疑似污染行 {st['contaminated_lines']}\n"
    )

    if args.dry_run:
        reports = []
        from .engine import clean_text
        from .processor import read_text
        for src in files:
            text, _enc = read_text(src)
            res = clean_text(text, model, cfg, sample_limit=5)
            sys.stderr.write(
                f"  {os.path.basename(src)}: 改动行 {res.changed_lines} / "
                f"删除字符 {res.removed_chars}\n")
            reports.append((src, res))
        total = sum(r.removed_chars for _s, r in reports)
        sys.stderr.write(f"合计将删除 {total} 个字符（dry-run，未写文件）\n")
        return 0

    out_dir = args.out
    report_path = args.report or (
        os.path.join(os.path.dirname(files[0]), "adclean_report.json")
        if args.in_place_suffix else os.path.join(out_dir, "report.json"))

    reports, _model = process_files(
        files, out_dir, cfg, same_folder=args.in_place_suffix,
        model=model, progress=_progress, report_path=report_path,
    )
    sys.stderr.write("\n")
    tot_changed = sum(r.changed_lines for r in reports)
    tot_removed = sum(r.removed_chars for r in reports)
    for r in reports:
        sys.stderr.write(
            f"  {os.path.basename(r.src)} → {r.dst}\n"
            f"      行 {r.lines} / 改动 {r.changed_lines} / 删除字符 {r.removed_chars}"
            f" / 剩余可疑 {r.exotic_left}   ({r.seconds}s)\n")
    sys.stderr.write(
        f"\n完成：{len(reports)} 个文件，改动 {tot_changed} 行，删除 {tot_removed} 个广告字符。\n"
        f"报告：{report_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
