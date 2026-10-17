"""DMM 里番标题搜索里的纯函数: 关键词归一化与标题相似度."""

from amane.crawlers.sites.dmm import DmmCrawler


class TestHentaiKeywords:
    def test_keeps_original_first(self) -> None:
        keywords = DmmCrawler._hentai_keywords("クール DEM〜眠れないオンナ〜")
        assert keywords[0] == "クール DEM〜眠れないオンナ〜"

    def test_strips_decoration_marks(self) -> None:
        keywords = DmmCrawler._hentai_keywords("クール DEM〜眠れないオンナ〜")
        assert "クール DEM 眠れないオンナ" in keywords

    def test_blank_gives_empty_list(self) -> None:
        assert DmmCrawler._hentai_keywords("   ") == []

    def test_at_most_three_variants(self) -> None:
        assert len(DmmCrawler._hentai_keywords("【新作】「テスト」〜名前〜")) <= DmmCrawler._HENTAI_MAX_TRIES


class TestTitleRatio:
    def test_identical_is_one(self) -> None:
        assert DmmCrawler._title_ratio("眠れないオンナ", "眠れないオンナ") == 1.0

    def test_ignores_punctuation_and_spaces(self) -> None:
        assert DmmCrawler._title_ratio("クール DEM〜眠れないオンナ〜", "クールDEM 眠れないオンナ") == 1.0

    def test_unrelated_is_below_threshold(self) -> None:
        assert DmmCrawler._title_ratio("眠れないオンナ", "全然ちがう別作品のタイトル") < DmmCrawler._HENTAI_MIN_RATIO

    def test_blank_is_zero(self) -> None:
        assert DmmCrawler._title_ratio("", "眠れないオンナ") == 0.0


class TestDetailUrls:
    def test_parses_escaped_and_plain_json(self) -> None:
        escaped = 'var x = {"detailUrl\\":\\"https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_1a/\\"};'
        plain = '{"detailUrl":"https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_2b/"}'
        assert DmmCrawler._detail_urls(escaped) == ["https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_1a/"]
        assert DmmCrawler._detail_urls(plain) == ["https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_2b/"]

    def test_no_links_gives_empty(self) -> None:
        assert DmmCrawler._detail_urls("<html><body>nothing</body></html>") == []
