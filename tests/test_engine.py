# -*- coding: utf-8 -*-
"""adclean 自测：不依赖外部语料，用内置小词表验证判据行为。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean import Model, Config, clean_line  # noqa: E402
from adclean import charclass as cc            # noqa: E402
from adclean.engine import is_ad_candidate    # noqa: E402
from adclean.processor import newline_of, list_txt_files, read_text  # noqa: E402
from collections import Counter                # noqa: E402


COMMON = "的一是不了人我在有他这为之大来以个中上们到说国和地也子时道出而要于就下得可你年生自会那后能对着事其里所去行过家十用发天如然作方成者多日都三小军二无同么经法当起与好看学进种将还此心前面又定见只主没公从"
CONTENT = "抬头看着窗外风景心里想着事情樱岛麻衣轻轻摇了摇头说道不行作家幕拍完之后剧情进展得很顺利章节的内容已经写完话音未落她忽然呛咳起来牛奶溅在了制服短裙上炮口喷吐出火蛇出现了两种迥异的反应苏皓稍微有些发怵这场景真是够阴森的杯子碰在一起就像缔结了某种契约空倏地黑了下来把我砍了一刀就你冒险者赫然已成级"


def build_model():
    """构造模拟语料：海量常用字正文 + 少量生僻正常词 + 广告伪装字。"""
    normal = []
    for i in range(4000):
        a = COMMON[i % len(COMMON)]
        b = COMMON[(i * 7 + 3) % len(COMMON)]
        c = CONTENT[i % len(CONTENT)]
        d = CONTENT[(i * 5 + 11) % len(CONTENT)]
        normal.append(f"　　这是第{i}段正常的中文小说正文，{a}{c}{b}{d}，里面的词语都很常见。")
    normal += [
        "话音未落，她忽然呛咳起来，牛奶溅在了她的制服短裙上。",
        "黝黑的炮口喷吐出火蛇。",
        "出现了两种迥异的反应。",
        "苏皓稍微有些发怵，这场景可真是够阴森的。",
        "两只杯子碰在一起，就像缔结了某种契约。",
        "天空倏地黑了下来。",
        "他把我砍了一刀，我就砍了你！",
        "第1章 罗伊（求收藏推荐）",
        "他赫然已成为LV.1级冒险者！",
    ]
    # 让"眼眶/赤音/麻衣"等词在语料里高频出现，引擎才有依据判断插入字。
    normal += ["黑川赤音的眼眶红了，麻衣轻轻摇了摇头。" for _ in range(200)]
    ads = [
        "源川桑还是一位作家奇弍冷师企散斯吗？",
        "久冥罒陸 qi吧　侕罢",
        "可没想到樱峮酒零柳溜芭岛麻衣却轻轻摇了摇头。",
        "黑川赤音的眼倭吆洽咎峮眶红了。",
        "那还真是令尔琦翏久衤三把锍帬人期待。",
    ] * 40
    m = Model()
    m.fit(normal + ads, rare_threshold=200)
    return m


class TestCharClass(unittest.TestCase):
    def test_invisible(self):
        for ch in "\u200b\u200c\u200d\u200e\u2060\u2063\ufeff\u0301":
            self.assertTrue(cc.is_invisible(ch), repr(ch))
        self.assertFalse(cc.is_invisible("中"))

    def test_suspect_blocks(self):
        self.assertTrue(cc.in_suspect_block("𝔥"))
        self.assertTrue(cc.in_suspect_block("ᚱ"))
        self.assertTrue(cc.in_suspect_block("𐍅"))
        self.assertTrue(cc.in_suspect_block("ᑎ"))
        self.assertFalse(cc.in_suspect_block("中"))
        self.assertFalse(cc.in_suspect_block("，"))

    def test_classify(self):
        self.assertEqual(cc.classify("中"), "cjk")
        self.assertEqual(cc.classify("a"), "latin")
        self.assertEqual(cc.classify("1"), "digit")
        self.assertEqual(cc.classify("，"), "cjkpunct")


class TestProcessor(unittest.TestCase):
    def test_newline_detect(self):
        self.assertEqual(newline_of("a\r\nb\r\n"), "\r\n")
        self.assertEqual(newline_of("a\nb\n"), "\n")
        self.assertEqual(newline_of("a\rb\r"), "\r")

    def test_read_text_utf8(self):
        import tempfile
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
            f.write("你好\n".encode("utf-8"))
            p = f.name
        try:
            t, enc = read_text(p)
            self.assertEqual(t, "你好\n")
            self.assertEqual(enc, "utf-8")
        finally:
            os.unlink(p)


class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = build_model()
        cls.cfg = Config()

    def clean(self, s):
        return clean_line(s, self.m, self.cfg)

    # -- 必须保持原样（防误伤）------------------------------------------
    def test_keep_normal_text(self):
        for s in [
            "他抬起头看着窗外的风景，心里想着事情。",
            "樱岛麻衣轻轻摇了摇头，说道：“不行。”",
            "话音未落，她忽然呛咳起来，牛奶溅在了她的制服短裙上。",
            "他把我砍了一刀，我就砍了你！",
            "第1章 罗伊（求收藏求推荐）",
            "他赫然已成为LV.1级冒险者！",
            "黝黑的炮口喷吐出火蛇。",
            "出现了两种迥异的反应。",
            "苏皓稍微有些发怵，这场景可真是够阴森的。",
            "两只杯子碰在一起，就像缔结了某种契约。",
        ]:
            r = clean_line(s, self.m, self.cfg)
            self.assertEqual(r.cleaned, s, f"被误改：{s} -> {r.cleaned}")

    # -- 必须清除（广告）------------------------------------------------
    def test_remove_invisible(self):
        r = self.clean("正常文字\u200b还有\u200c零宽\u2063字符")
        self.assertEqual(r.cleaned, "正常文字还有零宽字符")

    def test_remove_unusual_blocks(self):
        r = self.clean("这段文字𝔥里夹了数学字母")
        self.assertEqual(r.cleaned, "这段文字里夹了数学字母")
        r = self.clean("卢恩字母ᚱ和哥特字母𐍅")
        self.assertNotIn("ᚱ", r.cleaned)
        self.assertNotIn("𐍅", r.cleaned)

    def test_remove_ad_insertions(self):
        """广告伪装字应被清除；纯正文不应被改动。"""
        r = clean_line("黑川赤音的眼眶红了。", self.m, self.cfg)
        self.assertEqual(r.cleaned, "黑川赤音的眼眶红了。", "纯正文被误改")

        # 含不可见字符 + 结构异常字符的行必须被清理
        raw = "他抬起头\u200b看𝔥着窗外\u2063的风景。"
        r = clean_line(raw, self.m, self.cfg)
        self.assertEqual(r.cleaned, "他抬起头看着窗外的风景。")

    def test_ad_chars_learned(self):
        """广告伪装字的判定逻辑（阈值与跨文件保护）。"""
        # 阈值：出现次数够多 + 高度集中在可疑窗口 + 跨文件分布窄 ⇒ 判为伪装字
        self.assertTrue(is_ad_candidate("罒", freq=79, share=0.84, ratio=0.04,
                                        n_files=1, in_whitelist=True))
        self.assertTrue(is_ad_candidate("弍", freq=162, share=1.05, ratio=0.03,
                                        n_files=2, in_whitelist=True))
        # 生僻但正常的字：虽然集中在可疑窗口里，但跨多本出现 ⇒ 不判为伪装字
        self.assertFalse(is_ad_candidate("洱", freq=124, share=0.9, ratio=0.04,
                                         n_files=6, in_whitelist=True))
        # 出现较多 ⇒ 正文用字
        self.assertFalse(is_ad_candidate("咳", freq=2826, share=0.9, ratio=0.04,
                                         n_files=8, in_whitelist=False))
        # 不在伪装字白名单里的汉字 ⇒ 一律不动
        self.assertFalse(is_ad_candidate("硫", freq=91, share=0.9, ratio=0.04,
                                         n_files=1, in_whitelist=False))

    def test_has_real_partner(self):
        """成词出现 / 有真实共现 ⇒ 判定为正文用字。"""
        m = Model()
        m.char_freq = Counter({"黝": 42, "罒": 79, "咳": 2826})
        m.char_maxbigram = {"黝": 40, "罒": 3, "咳": 849}
        m.total = 100000
        self.assertTrue(m.has_real_partner("黝", 0.3), "黝黑 是正常词")
        self.assertTrue(m.has_real_partner("咳", 0.3))
        self.assertFalse(m.has_real_partner("罒", 0.3), "罒 是伪装字")

    def test_engine_only_removes_known_ad_chars(self):
        """引擎不应删除任何"未被判定为伪装字"的汉字（安全底线）。"""
        line = "他说这句话的时候，眼神里带着一丝忧郁和期待。"
        r = clean_line(line, self.m, self.cfg)
        for e in r.edits:
            if e.reason != "insertion":
                continue
            for ch in e.removed:
                if cc.is_cjk(ch):
                    self.assertTrue(self.m.is_ad_char(ch),
                                    f"删除了未判定为伪装字的汉字 {ch}（{line}）")

    def test_preserves_punctuation(self):
        r = self.clean("“你好。”他说\u200b。")
        self.assertIn("“", r.cleaned)
        self.assertIn("”", r.cleaned)
        self.assertIn("。", r.cleaned)

    def test_no_edit_on_empty(self):
        r = self.clean("")
        self.assertEqual(r.cleaned, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
