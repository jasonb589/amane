"""DMM 里番标题搜索里的纯函数: 关键词归一化与标题相似度."""

from amane.crawlers.sites.dmm import DmmCrawler


class TestHentaiKeywords:
    def test_cleaned_form_first(self) -> None:
        # 装饰符先洗掉再搜 (含「〜」的原串命中率明显更差).
        keywords = DmmCrawler._hentai_keywords("クール DEM〜眠れないオンナ〜")
        assert keywords[0] == "クール DEM 眠れないオンナ"

    def test_strips_decoration_marks(self) -> None:
        keywords = DmmCrawler._hentai_keywords("クール DEM〜眠れないオンナ〜")
        assert "クール DEM 眠れないオンナ" in keywords

    def test_blank_gives_empty_list(self) -> None:
        assert DmmCrawler._hentai_keywords("   ") == []

    def test_at_most_max_keywords(self) -> None:
        keywords = DmmCrawler._hentai_keywords("【新作】「テスト」〜名前〜")
        assert len(keywords) <= DmmCrawler._HENTAI_MAX_KEYWORDS

    def test_original_filename_seed_keeps_parenthetical(self) -> None:
        # 解析器会把「（彼女）」当垃圾串剥掉, 站点标题却是完整写法, 所以文件名优先级最高.
        keywords = DmmCrawler._hentai_keywords(
            "陰湿オタクにイカれる妹3 精液奴隷",
            "/media/gx/里番/陰湿オタクにイカれる妹（彼女）3 精液奴隷.mp4",
        )
        assert keywords[0] == "陰湿オタクにイカれる妹（彼女）3 精液奴隷"
        assert "陰湿オタクにイカれる妹（彼女）3" in keywords


class TestTitleRatio:
    def test_identical_is_one(self) -> None:
        assert DmmCrawler._title_ratio("眠れないオンナ", "眠れないオンナ") == 1.0

    def test_ignores_punctuation_and_spaces(self) -> None:
        assert DmmCrawler._title_ratio("クール DEM〜眠れないオンナ〜", "クールDEM 眠れないオンナ") == 1.0

    def test_unrelated_is_below_threshold(self) -> None:
        assert DmmCrawler._title_ratio("眠れないオンナ", "全然ちがう別作品のタイトル") < DmmCrawler._HENTAI_MIN_RATIO

    def test_missing_parenthetical_still_matches(self) -> None:
        # 真实案例: 番号字段少了「（彼女）」, 站点标题是完整写法, 相似度仍然达标.
        ratio = DmmCrawler._title_ratio("陰湿オタクにイカれる妹3 精液奴隷", "陰湿オタクにイカれる妹（彼女）3 精液奴隷")
        assert ratio >= DmmCrawler._HENTAI_MIN_RATIO

    def test_contains_accepts_prefix_forms(self) -> None:
        assert DmmCrawler._title_contains("陰湿オタクにイカれる妹", "陰湿オタクにイカれる妹（彼女）3 精液奴隷")
        assert not DmmCrawler._title_contains("陰湿", "全然ちがう別作品のタイトル")

    def test_blank_is_zero(self) -> None:
        assert DmmCrawler._title_ratio("", "眠れないオンナ") == 0.0


class TestDetailUrls:
    def test_parses_escaped_and_plain_json(self) -> None:
        escaped = '<html><body><script>var x = {"detailUrl\\":\\"https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_1a/\\"};</script></body></html>'
        plain = '<html><body><script>{"detailUrl":"https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_2b/"}</script></body></html>'
        assert DmmCrawler._detail_urls(escaped) == ["https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_1a/"]
        assert DmmCrawler._detail_urls(plain) == ["https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_2b/"]

    def test_no_links_gives_empty(self) -> None:
        assert DmmCrawler._detail_urls("<html><body>nothing</body></html>") == []
