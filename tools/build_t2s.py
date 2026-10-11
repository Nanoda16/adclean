# -*- coding: utf-8 -*-
"""从 OpenCC 的人工校对表生成 adclean 使用的繁→简映射表。

判据：候选里含源字本身（覆 → [覆, 复]、乾 → [干, 乾]）说明该字在简体里也合法，
不转换；短语表负责「什麼→什么」这类只能整词判断的。

输入：adclean/data/OpenCC_TSCharacters.txt、adclean/data/OpenCC_TSPhrases.txt
输出：adclean/data/t2s.tsv（字级）、adclean/data/t2s_phrases.tsv（短语级）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), 'adclean', 'data')
SRC_CHARS = os.path.join(DATA, 'OpenCC_TSCharacters.txt')
SRC_PHRASES = os.path.join(DATA, 'OpenCC_TSPhrases.txt')

# 这些字在简体里必须保持原样；一旦被映射就说明表坏了
MUST_KEEP = '覆著藉乾幺於麽么台只干里后发斗面种划冲叶复历须蒙苏系咸弦泄绣凶痒'
# 这些是铁定要转换的繁体字，用来防止表被削得过头
MUST_CONVERT = '們這說國會學對時實個沒來後臺裏髮幹隻藍遙鯨澀銫姦嚇墜'


def _bmp(c: str) -> bool:
    return ord(c) <= 0xFFFF


def parse_chars() -> tuple[dict[str, str], dict[str, list[str]]]:
    table: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    with open(SRC_CHARS, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 2:
                parts = line.split()
            src = parts[0]
            targets = [t for t in parts[1].split(' ') if t]
            if not src or not targets:
                continue
            if src in targets:
                ambiguous[src] = targets        # 简体里同样合法 → 不转换
                continue
            pick = next((t for t in targets if _bmp(t) and t != src), None)
            if pick is None:
                continue
            table[src] = pick
    return table, ambiguous


# 歧义字只能在**词**一级消歧：这些字在简体里也合法（乾坤/慰藉/於姓），
# 所以字级不转换，常见繁体词由下面这张表补出。
EXTRA_PHRASES = {
    '乾淨': '干净', '乾脆': '干脆', '乾燥': '干燥', '餅乾': '饼干', '乾杯': '干杯',
    '擦乾': '擦干', '曬乾': '晒干', '乾癟': '干瘪', '乾爹': '干爹', '乾旱': '干旱',
    '風乾': '风干', '烘乾': '烘干', '擰乾': '拧干', '乾咳': '干咳', '乾糧': '干粮',
    '什麽': '什么', '那麽': '那么', '甚麽': '什么', '怎麽': '怎么', '要麽': '要么',
    '多麽': '多么', '這麽': '这么', '麽樣': '么样', '什幺': '什么',
    '甚麼': '什么', '甚麽': '什么', '甚幺': '什么',
    '由於': '由于', '對於': '对于', '關於': '关于', '在於': '在于', '善於': '善于',
    '至於': '至于', '於是': '于是', '終於': '终于', '等於': '等于', '屬於': '属于',
    '基於': '基于', '源於': '源于', '鑒於': '鉴于', '限於': '限于', '忙於': '忙于',
    '藉口': '借口', '藉由': '借由', '藉此': '借此', '藉着': '借着', '藉助': '借助',
    '藉故': '借故',
}


def parse_phrases() -> dict[str, str]:
    phrases: dict[str, str] = dict(EXTRA_PHRASES)
    with open(SRC_PHRASES, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) != 2:
                continue
            src, tgt = parts[0], parts[1].split(' ')[0]
            if len(src) < 2 or not tgt or src == tgt:
                continue
            if not all(_bmp(c) for c in src + tgt):
                continue
            phrases[src] = tgt
    return phrases


def main() -> int:
    table, ambiguous = parse_chars()
    phrases = parse_phrases()

    problems: list[str] = []
    for c in MUST_KEEP:
        if c in table:
            problems.append(f'{c} 被映射成 {table[c]}（简体里本来就合法，不该转）')
    for c in MUST_CONVERT:
        if c not in table:
            problems.append(f'{c} 缺失（繁体字必须能转）')
    bad = [c for c, s in table.items() if not _bmp(s) or c == s]
    if bad:
        problems.append(f'目标非法或自映射: {bad[:20]}')
    if len(table) < 2000:
        problems.append(f'表太小（{len(table)} 行），可能没读全')

    out_chars = os.path.join(DATA, 't2s.tsv')
    out_phr = os.path.join(DATA, 't2s_phrases.tsv')
    with open(out_chars, 'w', encoding='utf-8', newline='\n') as f:
        for c in sorted(table):
            f.write(f'{c}\t{table[c]}\n')
    with open(out_phr, 'w', encoding='utf-8', newline='\n') as f:
        for c in sorted(phrases):
            f.write(f'{c}\t{phrases[c]}\n')

    print(f'字级映射 {len(table)} 条 → {out_chars}')
    print(f'短语映射 {len(phrases)} 条 → {out_phr}')
    kept = ''.join(c for c in '覆藉乾於麽著幺' if c in ambiguous)
    print(f'因歧义跳过 {len(ambiguous)} 条，其中繁体形近的简体常用字: {kept}')
    if problems:
        print('\n自检失败:')
        for p in problems:
            print('  -', p)
        return 1
    print('自检通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
