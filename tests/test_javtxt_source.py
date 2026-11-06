"""JAVTXT 源的解析用例 (按番号检索 + 详情页 dl 结构 + 只取日文剧情)."""

from amane.crawlers.sites.javtxt import detail_links, parse_detail

DETAIL_URL = "https://javtxt.com/v/396334"

_PAGE = """
<html><body>
<h1 class="title is-4 text-jp">三上悠亜の有頂天ローションSP 人生最高ヌルヌルご奉仕で射精させてあげる</h1>
<h2 class="title is-4 text-zh">三上悠亚的极致润滑SP</h2>
<div class="info"><div class="intro">
  <dl class="dl-actress subtitle">
    <dd>出演女优</dd>
    <dt><a class="actress" href="/actress/17345"><img class="avatar"> 三上悠亜</a></dt>
  </dl>
  <dl>
    <dd>🆔 番号</dd><dt>ssis-313 (ssis00313)</dt>
    <dd>📅 发行时间</dd><dt>2022-02-04</dt>
    <dd>🗂️ 系列</dd><dt>有頂天ローションSP</dt>
    <dd>🎥 片商</dd><dt>S1 NO.1 STYLE</dt>
    <dd>👨‍🏫 导演</dd><dt>TAKE-D</dt>
    <dd>🔖 厂牌</dd><dt>S1 NO.1 STYLE</dt>
    <dd>🏷️ 类别</dd>
    <dt>
      <a class="tag is-white" href="/genre/445">高清</a>
      <a class="tag is-white" href="/genre/469">4K</a>
    </dt>
  </dl>
</div></div>
<p class="text-jp">たっぷり大量ローションでヌルヌルの光沢ボディを味わい尽くす！</p>
<div class="text-zh"><p>尽情享受润滑液下的光滑身体！</p></div>
</body></html>
"""


def test_parse_detail_reads_dl_fields() -> None:
    meta = parse_detail(_PAGE, DETAIL_URL)

    assert meta is not None
    assert meta.number == "SSIS-313"
    assert meta.title == "三上悠亜の有頂天ローションSP 人生最高ヌルヌルご奉仕で射精させてあげる"
    assert [actor.name for actor in meta.actors] == ["三上悠亜"]
    assert meta.release == "2022-02-04"
    assert meta.series == "有頂天ローションSP"
    assert meta.studio == "S1 NO.1 STYLE"
    assert meta.publisher == "S1 NO.1 STYLE"
    assert meta.directors == ["TAKE-D"]
    assert meta.tags == ["高清", "4K"]
    # 只要日文原文: 站点自带的中文是机翻, 交给管线重译更好.
    assert meta.plot == "たっぷり大量ローションでヌルヌルの光沢ボディを味わい尽くす！"
    assert meta.source_url == DETAIL_URL
    assert meta.external_id == "396334"
    # 这个源不提供图片与时长.
    assert meta.poster_urls == []
    assert meta.thumb_urls == []
    assert meta.runtime is None


def test_parse_detail_rejects_page_without_number() -> None:
    assert parse_detail("<html><body><h1>没有番号</h1></body></html>", DETAIL_URL) is None


def test_detail_links_dedup_and_absolute() -> None:
    html = '<a href="/v/396334">a</a><a href="/v/396334">b</a><a href="/v/401122">c</a>'

    assert detail_links(html) == ["https://javtxt.com/v/396334", "https://javtxt.com/v/401122"]
