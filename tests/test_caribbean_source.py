"""カリビアンコム 源的解析用例 (番号归一化 / 配信日 / 时长 / 详情页解析)."""

import pytest

from amane.crawlers.sites.caribbean import (
    canonical_number,
    parse_detail,
    release_from_number,
    runtime_minutes,
)

_DETAIL = """
<html><head><title>テスト題名 | 無修正アダルト動画 カリビアンコム</title></head>
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
  <a href="/moviepages/122524-001/images/l/001.jpg">1</a>
  <a href="/moviepages/122524-001/images/l/002.jpg">2</a>
  <a href="/moviepages/999999-999/images/l/001.jpg">别的片</a>
</div>
</body></html>
"""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("122524-001", "122524-001"),
        ("122524_01", "122524-001"),
        ("122524_1", "122524-001"),
        ("122524001", "122524-001"),
        ("122524-0001", "122524-0001"),
        ("SSIS-001", None),
    ],
)
def test_canonical_number(raw: str, expected: str | None) -> None:
    assert canonical_number(raw) == expected


def test_release_from_number() -> None:
    # 前六位是 MMDDYY: 122524 → 2024-12-25 (与 javdb 上该片的配信日一致).
    assert release_from_number("122524-001") == "2024-12-25"
    assert release_from_number("010115-001") == "2015-01-01"
    assert release_from_number("999999-001") is None  # 非法日期


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("T00H59M06S", 60), ("PT1H02M00S", 62), ("T00H30M00S", 30), ("", None)],
)
def test_runtime_minutes(raw: str, expected: int | None) -> None:
    assert runtime_minutes(raw) == expected


def test_parse_detail_fields() -> None:
    meta = parse_detail(_DETAIL, "122524-001", "https://www.caribbeancom.com/moviepages/122524-001/index.html")
    assert meta is not None
    assert meta.number == "122524-001"
    assert meta.title == "母性溢れる美熟女とバブバブ授乳プレイ"
    assert meta.plot is not None and meta.plot.startswith("色っぽい雰囲気")
    assert [actor.name for actor in meta.actors] == ["星野さやか"]
    assert meta.tags == ["痴女", "巨乳"]
    assert meta.studio == "カリビアンコム"
    assert meta.release == "2024-12-25"
    assert meta.runtime == 60
    # 横版封面同时作封面与海报兜底.
    assert meta.thumb_urls == ["https://www.caribbeancom.com/moviepages/122524-001/images/l_l.jpg"]
    assert meta.poster_urls == meta.thumb_urls
    # 剧照只收本番号的, 别的片混进来要滤掉.
    assert meta.extrafanart == [
        "https://www.caribbeancom.com/moviepages/122524-001/images/l/001.jpg",
        "https://www.caribbeancom.com/moviepages/122524-001/images/l/002.jpg",
    ]
    assert meta.source_url.endswith("/index.html")


def test_parse_detail_without_title_returns_none() -> None:
    assert parse_detail("<html><body>提示页</body></html>", "122524-001", "https://x/index.html") is None
