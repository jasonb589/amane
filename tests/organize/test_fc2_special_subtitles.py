"""FC2 特集与 FIX 字幕按源角色配对, 整理输出采用已规划的 CD."""

from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from amane.db.models import Library, Metadata
from amane.enums import MoveMode
from amane.library import DEFAULT_SUBTITLE_EXTENSIONS
from amane.organize.subtitles import discover_subtitles, place_subtitles
from amane.parsing import parse_file_info
from amane.parsing.fc2_parts import parse_fc2_member


class _Pairing(NamedTuple):
    id: str
    video: str
    output_cd: int
    subtitles: tuple[str, ...]
    expected: tuple[str, ...]


_MAIN_SPECIAL = (
    "FC2-4982441.chs.srt",
    "FC2-4982441-SP.chs.srt",
    "FC2-4982441-SP1.chs.srt",
    "FC2-4982441-SP2.chs.srt",
    "FC2-4982441-FIX.chs.srt",
    "FC2-4982441-FIX1.chs.srt",
    "FC2-4982441-FIX2.chs.srt",
    "FC2-4982441_1.chs.srt",
    "FC2-4982441_2.chs.srt",
    "chs.srt",
)

_LANGUAGES = (
    "chs.srt",
    "cht.ass",
    "chi.ssa",
    "zho.vtt",
    "zh.sub",
    "zh-cn.srt",
    "zh-TW.srt",
    "zh-HK.srt",
    "zh-Hans.srt",
    "zh.Hant.srt",
    "en.srt",
    "eng.srt",
    "ja.srt",
    "jpn.srt",
)

_AMBIGUOUS = (
    "FC2-4982441-SP0.srt",
    "FC2-4982441-SP100.srt",
    "FC2-4982441-SPA.srt",
    "FC2-4982441-SP1-bonus.srt",
    "FC2-4982441-extra.srt",
    "FC2-4982441-SP1_2.srt",
    "FC2-4982441-SP1-SP2.srt",
    "FC2-4982441_1-SP1.srt",
    "FC2-4982441-FIX0.srt",
    "FC2-4982441-FIX100.srt",
    "FC2-4982441-FIXED.srt",
    "FC2-4982441-FIX1-bonus.srt",
    "FC2-4982441-FIX1_2.srt",
    "FC2-4982441-FIX1-FIX2.srt",
    "FC2-4982441-FIX1-SP1.srt",
    "FC2-4982441_1-FIX1.srt",
    "FC2-4982442-SP1.srt",
    "FC2-4982442-FIX1.srt",
    "FC2-4982442.chs.srt",
    "FC2-498244.chs.srt",
    "MIDV-123.srt",
    "SP.srt",
    "SP1.srt",
    "SP2.chs.srt",
    "FIX.srt",
    "FIX1.srt",
    "bonus.srt",
    "a-CD1.srt",
    "012.ass",
    "chs.forced.srt",
    "en.sdh.srt",
    "unknown.srt",
)

_CASES = (
    _Pairing(
        "main-with-special-uses-only-main-subtitles",
        "FC2-4982441.mp4",
        1,
        _MAIN_SPECIAL,
        ("FC2-4982441.chs.srt", "chs.srt"),
    ),
    _Pairing(
        "unnumbered-special-is-distinct-from-sp1",
        "FC2-4982441-SP.mp4",
        2,
        _MAIN_SPECIAL,
        ("FC2-4982441-SP.chs.srt",),
    ),
    _Pairing(
        "special1-matches-source-number-before-output-cd3",
        "FC2-4982441-SP1.mp4",
        3,
        _MAIN_SPECIAL,
        ("FC2-4982441-SP1.chs.srt",),
    ),
    _Pairing(
        "special2-matches-source-number-before-output-cd4",
        "FC2-4982441-SP2.mp4",
        4,
        _MAIN_SPECIAL,
        ("FC2-4982441-SP2.chs.srt",),
    ),
    _Pairing(
        "unnumbered-fix-is-distinct-from-fix1-and-special",
        "FC2-4982441-FIX.mp4",
        3,
        _MAIN_SPECIAL,
        ("FC2-4982441-FIX.chs.srt",),
    ),
    _Pairing(
        "fix1-matches-source-number-before-output-cd5",
        "FC2-4982441-FIX1.mp4",
        5,
        _MAIN_SPECIAL,
        ("FC2-4982441-FIX1.chs.srt",),
    ),
    _Pairing(
        "fix2-matches-source-number-before-output-cd6",
        "FC2-4982441-FIX2.mp4",
        6,
        _MAIN_SPECIAL,
        ("FC2-4982441-FIX2.chs.srt",),
    ),
    _Pairing(
        "main1-keeps-unmarked-main-fallback",
        "FC2-4982441_1.mp4",
        1,
        _MAIN_SPECIAL,
        ("FC2-4982441.chs.srt", "FC2-4982441_1.chs.srt", "chs.srt"),
    ),
    _Pairing(
        "main2-rejects-unmarked-main-and-generic-language",
        "FC2-4982441_2.mp4",
        2,
        _MAIN_SPECIAL,
        ("FC2-4982441_2.chs.srt",),
    ),
    _Pairing(
        "special1-canonical-aliases-and-languages",
        "FC2-PPV-4982441-SP1.mp4",
        3,
        (
            "fc4982441-sp01.zh-Hans.SRT",
            "FC2-4982441-SP1.en.ass",
            "FC2-PPV-4982441-SP1.jpn.vtt",
            "FC2-4982441-SP2.zh-Hans.srt",
            "FC2-4982441-CD3.zh-Hans.srt",
        ),
        ("fc4982441-sp01.zh-Hans.SRT", "FC2-4982441-SP1.en.ass", "FC2-PPV-4982441-SP1.jpn.vtt"),
    ),
    _Pairing(
        "main1-canonical-aliases-and-languages",
        "fc4982441_01.mp4",
        1,
        (
            "FC2-PPV-4982441-CD1.zh-TW.srt",
            "FC2-4982441-Part 1.en.ass",
            "fc4982441_1.chs.srt",
            "FC2-4982441-SP1.zh-TW.srt",
            "FC2-4982442_1.chs.srt",
        ),
        ("FC2-PPV-4982441-CD1.zh-TW.srt", "FC2-4982441-Part 1.en.ass", "fc4982441_1.chs.srt"),
    ),
    _Pairing(
        "fix1-canonical-aliases-movable-marker-and-languages",
        "hhd800_com@fc4982441_4K_FIX1.mp4",
        5,
        (
            "fc4982441-FIX01.zh-Hans.SRT",
            "FC2-4982441.en.FIX1.ass",
            "hhd800_com@fc4982441_4K_FIX1.chs.srt",
            "FC2-PPV-4982441-FIX1.jpn.vtt",
            "FC2-4982441-FIX2.zh-Hans.srt",
            "FC2-4982441-CD5.zh-Hans.srt",
            "FC2-4982441-SP1.zh-Hans.srt",
            "FC2-4982441-FIX.zh-Hans.srt",
            "FC2-4982441.zh-Hans.srt",
        ),
        (
            "fc4982441-FIX01.zh-Hans.SRT",
            "FC2-4982441.en.FIX1.ass",
            "hhd800_com@fc4982441_4K_FIX1.chs.srt",
            "FC2-PPV-4982441-FIX1.jpn.vtt",
        ),
    ),
    _Pairing("main-generic-known-languages", "FC2-4982441.mp4", 1, _LANGUAGES, _LANGUAGES),
    _Pairing("main1-generic-known-languages", "FC2-4982441_1.mp4", 1, _LANGUAGES, _LANGUAGES),
    _Pairing("main2-rejects-generic-languages", "FC2-4982441_2.mp4", 2, _LANGUAGES, ()),
    _Pairing("special-rejects-generic-languages", "FC2-4982441-SP.mp4", 2, _LANGUAGES, ()),
    _Pairing("special1-rejects-generic-languages", "FC2-4982441-SP1.mp4", 3, _LANGUAGES, ()),
    _Pairing("fix-rejects-generic-languages", "FC2-4982441-FIX.mp4", 3, _LANGUAGES, ()),
    _Pairing("fix1-rejects-generic-languages", "FC2-4982441-FIX1.mp4", 5, _LANGUAGES, ()),
    _Pairing("main-rejects-unknown-and-other-numbers", "FC2-4982441.mp4", 1, _AMBIGUOUS, ()),
    _Pairing("main1-rejects-unknown-and-other-numbers", "FC2-4982441_1.mp4", 1, _AMBIGUOUS, ()),
    _Pairing("special1-rejects-unknown-and-other-numbers", "FC2-4982441-SP1.mp4", 3, _AMBIGUOUS, ()),
    _Pairing("fix1-rejects-unknown-and-other-numbers", "FC2-4982441-FIX1.mp4", 5, _AMBIGUOUS, ()),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
def test_fc2_subtitles_match_source_role_and_local_part(tmp_path: Path, case: _Pairing) -> None:
    video = tmp_path / case.video
    video.write_bytes(b"video")
    for name in case.subtitles:
        (tmp_path / name).write_bytes(b"subtitle")
    source = parse_fc2_member(video.name)
    assert source is not None
    output = replace(parse_file_info(video), cd=case.output_cd)

    found = discover_subtitles.sync(video, DEFAULT_SUBTITLE_EXTENSIONS, output, fc2_member=source)

    assert [path.name for path in found] == sorted(case.expected, key=str.casefold)


@pytest.mark.parametrize("extensions", [(), (".ass",), (".SRT",)])
def test_fc2_subtitle_extensions_and_direct_parent(tmp_path: Path, extensions: tuple[str, ...]) -> None:
    video = tmp_path / "FC2-4982441-SP1.mp4"
    video.write_bytes(b"video")
    (tmp_path / "FC2-4982441-SP1.chs.srt").write_bytes(b"srt")
    (tmp_path / "FC2-4982441-SP1.en.ass").write_bytes(b"ass")
    nested = tmp_path / "subs"
    nested.mkdir()
    (nested / "FC2-4982441-SP1.ja.srt").write_bytes(b"nested")
    member = parse_fc2_member(video.name)
    assert member is not None

    found = discover_subtitles.sync(video, extensions, replace(parse_file_info(video), cd=3), fc2_member=member)

    expected: dict[tuple[str, ...], list[str]] = {
        (): [],
        (".ass",): ["FC2-4982441-SP1.en.ass"],
        (".SRT",): ["FC2-4982441-SP1.chs.srt"],
    }
    assert [path.name for path in found] == expected[extensions]


@pytest.mark.parametrize(
    ("video_name", "subtitles", "expected"),
    [
        (
            "FC2-4982441-CD3.mp4",
            ("FC2-4982441-CD3.chs.srt", "FC2-4982441-CD1.chs.srt", "chs.srt"),
            ("FC2-4982441-CD3.chs.srt",),
        ),
        ("MIDV-123-CD1.mp4", ("a-CD1.srt", "chs.srt", "a-CD2.srt", "FC2-4982441-SP1.srt"), ("a-CD1.srt", "chs.srt")),
        ("MIDV-123.mp4", ("bonus.srt", "012.ass", "SP.srt", "FC2-4982441-SP1.srt"), ("012.ass", "bonus.srt", "SP.srt")),
    ],
    ids=(
        "organized-cd3-keeps-generic-cd-matching",
        "existing-cd1-keeps-generic-fallback",
        "unrelated-generic-keeps-original-rules",
    ),
)
def test_no_fc2_plan_preserves_existing_pairing(
    tmp_path: Path, video_name: str, subtitles: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    video = tmp_path / video_name
    video.write_bytes(b"video")
    for name in subtitles:
        (tmp_path / name).write_bytes(b"subtitle")

    found = discover_subtitles.sync(video, DEFAULT_SUBTITLE_EXTENSIONS, parse_file_info(video))

    assert [path.name for path in found] == list(expected)


@pytest.mark.parametrize(("marker", "output_cd"), [("SP1", 3), ("FIX1", 5)])
def test_extra_subtitle_uses_planned_video_name_on_placement(tmp_path: Path, marker: str, output_cd: int) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    video = inbox / f"hhd800_com@fc4982441_4K_{marker}.mp4"
    video.write_bytes(b"video")
    subtitle = inbox / f"hhd800_com@fc4982441_4K_{marker}.chs.srt"
    subtitle.write_bytes(b"extra subtitle")
    main_subtitle = inbox / "FC2-4982441_1.chs.srt"
    main_subtitle.write_bytes(b"main subtitle")
    other_extra = inbox / f"FC2-4982441-{'FIX1' if marker == 'SP1' else 'SP1'}.chs.srt"
    other_extra.write_bytes(b"other extra subtitle")
    destination = tmp_path / "output" / f"FC2-4982441-CD{output_cd}.mp4"
    destination.parent.mkdir()
    destination.write_bytes(b"placed video")
    member = parse_fc2_member(video.name)
    assert member is not None
    output = replace(parse_file_info(video), cd=output_cd)
    library = Library(name="测试", path=str(inbox), subtitle_template="{video_dir}/{video_name}.chs.{ext}")
    metadata = Metadata(number="FC2-4982441")

    selected = discover_subtitles.sync(video, DEFAULT_SUBTITLE_EXTENSIONS, output, fc2_member=member)
    conflicts = place_subtitles.sync(
        selected, video, destination, library, metadata, output, MoveMode.MOVE, safe_dirs=None
    )

    assert conflicts == []
    assert (destination.parent / f"FC2-4982441-CD{output_cd}.chs.srt").read_bytes() == b"extra subtitle"
    assert not subtitle.exists()
    assert main_subtitle.read_bytes() == b"main subtitle"
    assert other_extra.read_bytes() == b"other extra subtitle"
