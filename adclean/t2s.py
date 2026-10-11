# -*- coding: utf-8 -*-
"""繁体 → 简体 转换（零依赖，映射表随包分发）。

转换分两级：
  1. 短语级（t2s_phrases.tsv，最长匹配）——必须整词判断的，如「什麼→什么」；
  2. 字级（t2s.tsv）——处理其余繁体字。

映射表里的歧义条目（候选里含源字本身，如 覆→[覆,复]）一律不转换。
"""
from __future__ import annotations

import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_PATH = os.path.join(_DIR, 't2s.tsv')
_PATH_PHRASES = os.path.join(_DIR, 't2s_phrases.tsv')

_T2S: dict[str, str] | None = None
_PHRASES: dict[str, str] | None = None
_MAX_PHRASE = 0


def _read_tsv(path: str) -> dict[str, str]:
    m: dict[str, str] = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) == 2 and parts[0] and parts[1]:
                    m[parts[0]] = parts[1]
    except OSError:
        pass
    return m


def _load() -> tuple[dict[str, str], dict[str, str]]:
    global _T2S, _PHRASES, _MAX_PHRASE
    if _T2S is None:
        _T2S = _read_tsv(_PATH)
        _PHRASES = _read_tsv(_PATH_PHRASES)
        _MAX_PHRASE = max((len(k) for k in _PHRASES), default=0)
    return _T2S, _PHRASES or {}


def _convert_phrases(text: str, phrases: dict[str, str], m: dict[str, str]) -> str:
    """短语优先，最长匹配；短语之外的单字再走字级表。"""
    if not phrases:
        return ''.join(m.get(ch, ch) for ch in text)
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        hit = None
        for L in range(min(_MAX_PHRASE, n - i), 1, -1):
            seg = text[i:i + L]
            if seg in phrases:
                hit = (L, phrases[seg])
                break
        if hit:
            out.append(hit[1])
            i += hit[0]
        else:
            out.append(m.get(text[i], text[i]))
            i += 1
    return ''.join(out)


def char_table() -> dict[str, str]:
    """字级映射表（只读副本，供测试与自检使用）。"""
    return dict(_load()[0])


def phrase_table() -> dict[str, str]:
    """短语级映射表（只读副本）。"""
    return dict(_load()[1])


def to_simplified(text: str) -> str:
    """把文本里的繁体字转成简体。表外的字原样保留。

    不要改用 str.translate：对超大映射表会静默失效。
    """
    m, phrases = _load()
    if not m and not phrases:
        return text
    if not any(ch in m for ch in text) and not any(p in text for p in phrases):
        return text
    return _convert_phrases(text, phrases, m)


def has_traditional(text: str) -> bool:
    m, phrases = _load()
    return any(ch in m for ch in text) or any(p in text for p in phrases)


def table_size() -> int:
    m, phrases = _load()
    return len(m) + len(phrases)
