"""カリビアンコム 源的解析用例 (番号变体 / 配信日 / 时长 / 两套模板的详情页解析)."""

import pytest

from amane.crawlers.sites.caribbean import (
    is_detail_page,
    number_variants,
    parse_detail,
    release_from_number,
    runtime_minutes,
)

# 经典模板 (-NNN): 封面走 l_l.jpg, 剧照是真实锚点.
_CLASSIC = """
<html><head><title>テスト | 無修正アダルト動画 カリビアンコム</title></head>
<body>
<div class="movie-info section">
  <div class="heading"><h1 itemprop="name">母性溢れる美熟女とバブバブ授乳プレイ</h1></div>
  <p itemprop="description">色っぽい雰囲気がたまらない美熟女の話。</p>
  <ul itemscope itemtype="http://schema.org/VideoObject">
    <li class="movie-spec">
      <span class="spec-title">出演</span>
      <span class="spec-content">
        <a class="spec__tag" itemprop="actor" itemscope itemtype="http://schema.org/Person" href="/search_act/8524/1.html">
          <span itemprop="name">星野さやか</span>
        </a>
      </span>
    </li>
    <li class="movie-spec">
      <span class="spec-title">再生時間</span>
      <span class="spec-content"><span itemprop="duration" content="T00H59M06S">00:59:06</span></span>
    </li>
    <li class="movie-spec">
      <span class="spec-title">タグ</span>
      <span class="spec-content">
        <a class="spec-item" href="/listpages/chijyo1.htm">痴女</a>
        <a class="spec-item" href="/listpages/kyonyu1.htm">巨乳</a>
      </span>
    </li>
  </ul>
  <img src="/moviepages/122524-001/images/l_l.jpg" />
  <a data-fancybox-group="gallery" href="/moviepages/122524-001/images/l/001.jpg">1</a>
  <a data-fancybox-group="gallery" href="/moviepages/122524-001/images/l/002.jpg">2</a>
  <a data-fancybox-group="gallery" href="/moviepages/999999-999/images/l/001.jpg">别的片</a>
</div>
</body></html>
"""

# 另一套模板 (_NN, 常是 10musume 的片挂在 caribbean 域名下): 没有 l_l.jpg 也没有剧照锚点,
# 封面/预告片只能从页面内嵌的 var Movie 里取; 相关影片卡片里也带 itemprop="actor" (要滤掉).
_PLAIN = """
<html><head><title>性なる夜に大サービス！ちっちゃ可愛いサンタちゃん 島河渚 | 無修正動画 カリビアンコム</title></head>
<body>
<h1 itemprop="name">性なる夜に大サービス！ちっちゃ可愛いサンタちゃん</h1>
<p itemprop="description">サンタ姿の女の子。</p>
<ul itemscope itemtype="http://schema.org/VideoObject">
  <li class="movie-spec">
    <span class="spec-title">出演</span>
    <span class="spec-content">
      <span class="spec__tag" itemprop="actor" itemscope itemtype="http://schema.org/Person" itemprop="name">島河渚</span>
    </span>
  </li>
  <li class="movie-spec">
    <span class="spec-title">再生時間</span>
    <span class="spec-content"><span itemprop="duration" content="T00H51M10S">00:51:10</span></span>
  </li>
</ul>
<div class="related">
  <div class="meta-data"><a itemprop="actor" href="/search/a/1.html">神楽麻衣,酒井まみ,安住美春</a></div>
</div>
<script>
var Movie = {"movie_id":"122524_01","movie_site":"10m","image_name":"str.jpg",
"sample_flash_url":"https://smovie.10musume.com/sample/movies/122524_01/480p.mp4",
"site_name":"10musume.com"};
</script>
</body></html>
"""

# 出演行里一个节点塞了多个名字 (逗号/顿号混用).
_MULTI_ACTORS = """
<html><body>
<h1 itemprop="name">タイトル</h1>
<ul>
  <li class="movie-spec">
    <span class="spec-title">出演</span>
    <span class="spec-content"><span itemprop="actor">香川洋子,玉置れに、神山ももか</span></span>
  </li>
</ul>
</body></html>
"""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 两种写法是**两部不同的片**: 变体只换分隔符, 序号不补零 (补零就抓到另一部).
        ("122524_01", ["122524_01", "122524-01"]),
        ("122524-001", ["122524-001", "122524_001"]),
        ("SSIS-001", ["SSIS-001"]),
    ],
)
def test_number_variants(raw: str, expected: list[str]) -> None:
    assert number_variants(raw) == expected


@pytest.mark.parametrize("number", ["122524-001", "122524_01"])
def test_release_from_number(number: str) -> None:
    # 前六位是 MMDDYY: 122524 → 2024-12-25 (与 javdb 上该片的配信日一致).
    assert release_from_number(number) == "2024-12-25"


def test_release_from_number_invalid() -> None:
    assert release_from_number("999999-001") is None
    assert release_from_number("SSIS-001") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("T00H59M06S", 60), ("PT1H02M00S", 62), ("T00H30M00S", 30), ("", None)],
)
def test_runtime_minutes(raw: str, expected: int | None) -> None:
    assert runtime_minutes(raw) == expected


def test_parse_detail_classic_template() -> None:
    meta = parse_detail(_CLASSIC, "122524-001", "https://www.caribbeancom.com/moviepages/122524-001/index.html")
    assert meta is not None
    assert meta.number == "122524-001"
    assert meta.title == "母性溢れる美熟女とバブバブ授乳プレイ"
    assert meta.plot is not None and meta.plot.startswith("色っぽい雰囲気")
    assert [actor.name for actor in meta.actors] == ["星野さやか"]
    assert meta.tags == ["痴女", "巨乳"]
    assert meta.studio == "カリビアンコム"
    assert meta.release == "2024-12-25"
    assert meta.runtime == 60
    assert meta.thumb_urls == ["https://www.caribbeancom.com/moviepages/122524-001/images/l_l.jpg"]
    assert meta.poster_urls == meta.thumb_urls
    # 剧照只收本番号真实存在的锚点, 别的片混进来要滤掉.
    assert meta.extrafanart == [
        "https://www.caribbeancom.com/moviepages/122524-001/images/l/001.jpg",
        "https://www.caribbeancom.com/moviepages/122524-001/images/l/002.jpg",
    ]


def test_parse_detail_other_template_uses_movie_js_assets() -> None:
    """``_01`` 模板: 封面与预告片来自页面内嵌的 var Movie, 剧照没有就不给."""
    meta = parse_detail(_PLAIN, "122524_01", "https://www.caribbeancom.com/moviepages/122524_01/index.html")
    assert meta is not None
    assert meta.number == "122524_01"
    assert meta.title == "性なる夜に大サービス！ちっちゃ可愛いサンタちゃん"
    assert meta.release == "2024-12-25"
    assert meta.runtime == 52
    # 站外素材站 (10musume) 的横向封面, 之前这里是空的, 导致片库没图.
    assert meta.thumb_urls == ["https://www.10musume.com/assets/sample/122524_01/str.jpg"]
    assert meta.poster_urls == meta.thumb_urls
    assert meta.trailer_urls == ["https://smovie.10musume.com/sample/movies/122524_01/480p.mp4"]
    assert meta.extrafanart == []


def test_actors_only_from_spec_row() -> None:
    """相关影片卡片里的 itemprop="actor" 不能混进来 (那里的文本是多部片的演员串)."""
    meta = parse_detail(_PLAIN, "122524_01", "https://www.caribbeancom.com/moviepages/122524_01/index.html")
    assert meta is not None
    assert [actor.name for actor in meta.actors] == ["島河渚"]


def test_actors_split_by_separators() -> None:
    meta = parse_detail(_MULTI_ACTORS, "010101_01", "https://www.caribbeancom.com/moviepages/010101_01/index.html")
    assert meta is not None
    assert [actor.name for actor in meta.actors] == ["香川洋子", "玉置れに", "神山ももか"]


def test_is_detail_page() -> None:
    assert is_detail_page(_CLASSIC) is True
    assert is_detail_page("<html>お探しのページは見つかりません</html>") is False
