# -*- coding: utf-8 -*-
"""繁体 → 简体 转换（零依赖，映射表随包分发）。

转换分两级：
  1. 短语级（t2s_phrases.tsv，最长匹配）——必须整词判断的，如「什麼→什么」；
  2. 字级（t2s.tsv）——处理其余繁体字。

映射表里的歧义条目（候选里含源字本身，如 覆→[覆,复]）一律不转换。
"""
from __future__ import annotations

import os
import re

from .patterns import JobCancelledError

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_PATH = os.path.join(_DIR, "t2s.tsv")
_PATH_PHRASES = os.path.join(_DIR, "t2s_phrases.tsv")

_T2S: dict[str, str] | None = None
_PHRASES: dict[str, str] | None = None
_PHRASE_RE: re.Pattern | None = None
_TRANS: dict[int, str] | None = None


def _read_tsv(path: str) -> dict[str, str]:
    m: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) == 2 and parts[0] and parts[1]:
                    m[parts[0]] = parts[1]
    except OSError:
        pass
    return m


def _load() -> tuple[dict[str, str], dict[str, str]]:
    global _T2S, _PHRASES, _PHRASE_RE, _TRANS
    if _T2S is None:
        _T2S = _read_tsv(_PATH)
        _PHRASES = _read_tsv(_PATH_PHRASES)
        if _PHRASES:
            ordered = sorted(_PHRASES, key=lambda k: (-len(k), k))
            _PHRASE_RE = re.compile("|".join(re.escape(k) for k in ordered))
        else:
            _PHRASE_RE = None
        _TRANS = str.maketrans(_T2S) if _T2S else {}
    return _T2S, _PHRASES or {}


def _convert_chunk(text: str, phrases: dict[str, str],
                   rx: re.Pattern | None, trans: dict[int, str]) -> str:
    """单块文本转换：预编译最长短语正则 + C 级单字 translate 查表。"""
    if not text:
        return text
    if rx is None or not phrases:
        return text.translate(trans) if trans else text
    out: list[str] = []
    last = 0
    for mt in rx.finditer(text):
        s, e = mt.span()
        if s > last:
            seg = text[last:s]
            out.append(seg.translate(trans) if trans else seg)
        out.append(phrases[mt.group(0)])
        last = e
    if last == 0:
        return text.translate(trans) if trans else text
    if last < len(text):
        tail = text[last:]
        out.append(tail.translate(trans) if trans else tail)
    return "".join(out)


def char_table() -> dict[str, str]:
    """字级映射表（只读副本，供测试与自检使用）。"""
    return dict(_load()[0])


def phrase_table() -> dict[str, str]:
    """短语级映射表（只读副本）。"""
    return dict(_load()[1])


def to_simplified(text: str, cancel_check=None) -> str:
    """把文本里的繁体字转成简体。表外的字原样保留。"""
    if cancel_check is not None and cancel_check():
        raise JobCancelledError("任务已取消")
    m, phrases = _load()
    if not m and not phrases:
        return text
    rx = _PHRASE_RE
    trans = _TRANS or {}
    if cancel_check is not None and len(text) > 200_000:
        # 仅在需要响应取消且文本较大时按换行边界分块，避免短语跨块截断
        out: list[str] = []
        pos = 0
        n = len(text)
        step = 200_000
        while pos < n:
            if cancel_check():
                raise JobCancelledError("任务已取消")
            end = min(n, pos + step)
            if end < n:
                nl = text.find("\n", end)
                end = (nl + 1) if nl >= 0 else n
            out.append(_convert_chunk(text[pos:end], phrases, rx, trans))
            pos = end
        return "".join(out)
    return _convert_chunk(text, phrases, rx, trans)


def has_traditional(text: str) -> bool:
    m, _phrases = _load()
    if any(ch in m for ch in text):
        return True
    return bool(_PHRASE_RE and _PHRASE_RE.search(text))


def table_size() -> int:
    m, phrases = _load()
    return len(m) + len(phrases)
