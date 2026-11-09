"""FC2 正片、特集与 FIX 命名, 以及组内分集规划的明确与歧义边界."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from amane.parsing.fc2_parts import Fc2Member, has_fc2_special_marker, parse_fc2_member, plan_fc2_parts
from amane.parsing.file_info import Mosaic, set_escape_strings


@pytest.mark.parametrize(
    ("filename", "role", "part", "definition", "subtitle", "mosaic"),
    [
        ("fc4800334_4K", "main", None, "4K", False, None),
        ("fc4800334_4K.mp4", "main", None, "4K", False, None),
        ("fc4800334_sp_4K.mp4", "special", None, "4K", False, None),
        ("fc4800334_4K_sp.mp4", "special", None, "4K", False, None),
        ("FC2-4800334-1080p-SP1.mp4", "special", 1, "1080p", False, None),
        ("fc4800334_4K_sp2-UC.mp4", "special", 2, "4K", True, Mosaic.CRACKED),
        ("fc4800334_H265_sp1_4K.mp4", "special", 1, "4K", False, None),
        ("FC2PPV4800334-4K-SP99-C.mp4", "special", 99, "4K", True, None),
        ("fc4800334_1_4K.mp4", "main", 1, "4K", False, None),
        ("fc4800334_2_4K.mp4", "main", 2, "4K", False, None),
        ("fc4800334_sp1_4K.mp4", "special", 1, "4K", False, None),
        ("fc4800334_sp2_4K.mp4", "special", 2, "4K", False, None),
        ("FC2-4800334.mp4", "main", None, None, False, None),
        ("FC2-PPV-4800334_SP01.mkv", "special", 1, None, False, None),
        ("FC2PPV4800334_sp99.mp4", "special", 99, None, False, None),
        ("FC24800334-SP-1080p.mp4", "special", None, "1080p", False, None),
        ("FC-4800334_02_1080p60.ts", "main", 2, "1080p", False, None),
        ("FC_4800334-A.mp4", "main", 1, None, False, None),
        ("FC2_4800334_B.mp4", "main", 2, None, False, None),
        ("FC2.PPV.4800334", "main", None, None, False, None),
        ("FC2-4800334-CD2.mp4", "main", 2, None, False, None),
        ("FC2-4800334-Part 2.mp4", "main", 2, None, False, None),
        ("FC2-4800334_sp1.chs.srt", "special", 1, None, False, None),
        ("FC2-4800334.chs.SP1.srt", "special", 1, None, False, None),
        ("FC2-4800334_sp2.en.ass", "special", 2, None, False, None),
        ("FC2-4800334_1.zh-Hant.srt", "main", 1, None, False, None),
        ("FC2-4800334_sp-UC-4K.mp4", "special", None, "4K", True, Mosaic.CRACKED),
        ("FC2-4800334_sp1-中文字幕.mp4", "special", 1, None, True, None),
        ("FC2-4800334_sp1-UNCENSORED-2160p.mp4", "special", 1, "4K", False, Mosaic.UNCENSORED),
        ("ＦＣ２－ＰＰＶ－４８００３３４＿ＳＰ２＿４Ｋ.mp4", "special", 2, "4K", False, None),
        ("ＦＣ２－４８００３３４＿ＳＰ２＿４Ｋ．ｍｐ４", "special", 2, "4K", False, None),
        ("ＦＣ２－４８００３３４＿４Ｋ＿ＳＰ２．ｍｐ４", "special", 2, "4K", False, None),
        ("FC2-4800334_sp1.ＳＲＴ", "special", 1, None, False, None),
        ("hhd800.com@fc4800334_4K.mp4", "main", None, "4K", False, None),
        ("hhd800.com@fc4800334_sp_4K.mp4", "special", None, "4K", False, None),
        ("hhd800_com@fc4800334_4K.mp4", "main", None, "4K", False, None),
        ("hhd800_com@fc4800334_4K_sp.mp4", "special", None, "4K", False, None),
        ("hhd800_com@fc4800334_sp2_4K.mp4", "special", 2, "4K", False, None),
        ("4K688.com@fc4800334_sp.mp4", "special", None, None, False, None),
        ("4K688_com@fc4800334_sp.mp4", "special", None, None, False, None),
        ("mirror.media.example@FC2-4800334_sp2.mp4", "special", 2, None, False, None),
        ("mirror_media.example@FC2-4800334_4K_sp2.mp4", "special", 2, "4K", False, None),
        ("fc4800334_FIX.mp4", "fix", None, None, False, None),
        ("fc4800334_FIX_4K.mp4", "fix", None, "4K", False, None),
        ("fc4800334_4K_FIX.mp4", "fix", None, "4K", False, None),
        ("fc4800334_H265_FIX1_4K.mp4", "fix", 1, "4K", False, None),
        ("FC2-PPV-4800334_FIX01.mkv", "fix", 1, None, False, None),
        ("FC24800334-1080p-FIX2.mp4", "fix", 2, "1080p", False, None),
        ("FC-4800334-4K-FIX99-C.mp4", "fix", 99, "4K", True, None),
        ("FC2-4800334_4K_FIX2-UC.mp4", "fix", 2, "4K", True, Mosaic.CRACKED),
        ("FC2-4800334_FIX1-UNCENSORED-2160p.mp4", "fix", 1, "4K", False, Mosaic.UNCENSORED),
        ("FC2-4800334.chs.FIX1.srt", "fix", 1, None, False, None),
        ("ＦＣ２－ＰＰＶ－４８００３３４＿４Ｋ＿ＦＩＸ２．ｍｐ４", "fix", 2, "4K", False, None),
        ("hhd800_com@fc4800334_4K_FIX.mp4", "fix", None, "4K", False, None),
        ("mirror.media_example@FC2-4800334_FIX2_4K.mp4", "fix", 2, "4K", False, None),
        ("4K688_com@fc4800334_FIX.mp4", "fix", None, None, False, None),
    ],
)
def test_parse_member_preserves_fc2_identity_and_local_role(
    filename: str,
    role: Literal["main", "special", "fix"],
    part: int | None,
    definition: str | None,
    subtitle: bool,
    mosaic: Mosaic | None,
) -> None:
    assert parse_fc2_member(filename) == Fc2Member(
        number="FC2-4800334", role=role, part=part, definition=definition, has_subtitle=subtitle, mosaic=mosaic
    )


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "BCD-1.mp4",
        "CD-1.mp4",
        "FC-1.mp4",
        "FC2-1234_sp.mp4",
        "XFC2-4800334_sp.mp4",
        "AFC4800334_sp.mp4",
        "FC2-4800334A_sp.mp4",
        "FC2-4800334_sp0.mp4",
        "FC2-4800334_sp00.mp4",
        "FC2-4800334_sp100.mp4",
        "FC2-4800334_sp1copy.mp4",
        "FC2-4800334_sp1_extra.mp4",
        "FC2-4800334_sp_1.mp4",
        "FC2-4800334_sp-1.mp4",
        "FC2-4800334_sp1_sp2.mp4",
        "FC2-4800334_1_sp.mp4",
        "FC2-4800334_4K_sp0.mp4",
        "FC2-4800334_4K_sp100.mp4",
        "FC2-4800334_4K_sp1copy.mp4",
        "FC2-4800334_4K_sp_1.mp4",
        "FC2-4800334_4K_sp1_sp2.mp4",
        "FC2-4800334_sp1_4K_sp1.mp4",
        "FC2-4800334_CD1_4K_sp.mp4",
        "FC2-4800334_4K_sp1_PART2.mp4",
        "FC2-4800334_4K_1_sp.mp4",
        "FC2-4800334_4K_sp1_extra.mp4",
        "FC2-4800334_4K_sp_.mp4",
        "FC2-4800334_0.mp4",
        "FC2-4800334_100.mp4",
        "FC2-4800334.1.mp4",
        "FC2-4800334-CD-1.mp4",
        "FC2-4800334-PART-1.mp4",
        "FC2-4800334_2_COPY.mp4",
        "FC2-4800334_sp_.mp4",
        "FC2-4800334_sp1_HDTV.mp4",
        "FC2-4800334_sp1_4K120COPY.mp4",
        "FC2-4800334_sp1.unknown",
        "download@fc4800334_sp.mp4",
        "-mirror.com@fc4800334_sp.mp4",
        "mirror-.com@fc4800334_sp.mp4",
        "mirror..com@fc4800334_sp.mp4",
        "mirror.123@fc4800334_sp.mp4",
        "mirror__com@fc4800334_4K_sp.mp4",
        "mirror._com@fc4800334_4K_sp.mp4",
        "-mirror_com@fc4800334_4K_sp.mp4",
        "mirror-_com@fc4800334_4K_sp.mp4",
        "mirror_123@fc4800334_4K_sp.mp4",
        "/media/FC2-4800334_sp.mp4",
        "folder\\FC2-4800334_sp.mp4",
        "BCD-1_FIX.mp4",
        "FC2-1234_FIX.mp4",
        "XFC2-4800334_FIX.mp4",
        "FC2-4800334_FIX0.mp4",
        "FC2-4800334_FIX00.mp4",
        "FC2-4800334_FIX001.mp4",
        "FC2-4800334_FIX100.mp4",
        "FC2-4800334_FIX1copy.mp4",
        "FC2-4800334_FIXED.mp4",
        "FC2-4800334_FIX_1.mp4",
        "FC2-4800334_FIX-1.mp4",
        "FC2-4800334_FIX 1.mp4",
        "FC2-4800334_FIX1_FIX2.mp4",
        "FC2-4800334_FIX1_4K_FIX1.mp4",
        "FC2-4800334_SP_FIX.mp4",
        "FC2-4800334_FIX_SP.mp4",
        "FC2-4800334_SP1_4K_FIX2.mp4",
        "FC2-4800334_FIX2_4K_SP1.mp4",
        "FC2-4800334_1_FIX.mp4",
        "FC2-4800334_FIX1_A.mp4",
        "FC2-4800334_B_FIX1.mp4",
        "FC2-4800334_CD1_4K_FIX.mp4",
        "FC2-4800334_4K_FIX1_PART2.mp4",
        "FC2-4800334_4K_1_FIX.mp4",
        "FC2-4800334_4K_FIX1_extra.mp4",
        "FC2-4800334_4K_FIX_.mp4",
        "FC2-4800334_FIX1.unknown",
    ],
)
def test_parse_member_rejects_other_catalogs_and_ambiguous_suffixes(filename: str) -> None:
    assert parse_fc2_member(filename) is None


def test_parse_member_uses_configured_junk_rules_for_prefix_and_tail() -> None:
    set_escape_strings(["release-copy@", "r:__upload[0-9]+"])
    try:
        assert parse_fc2_member("release-copy@fc4800334_sp2_4K__upload17.mp4") == Fc2Member(
            number="FC2-4800334", role="special", part=2, definition="4K"
        )
        assert has_fc2_special_marker("release-copy@fc4800334_spX.mp4")
        assert parse_fc2_member("other-copy@fc4800334_sp2_4K.mp4") is None
        assert parse_fc2_member("release-copy@fc4800334_4K_FIX2__upload17.mp4") == Fc2Member(
            number="FC2-4800334", role="fix", part=2, definition="4K"
        )
        assert has_fc2_special_marker("release-copy@fc4800334_FIX0.mp4")
    finally:
        set_escape_strings([])


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("fc4800334_sp.mp4", True),
        ("FC2-4800334_sp1.chs.srt", True),
        ("FC2-4800334_sp0.mp4", True),
        ("FC2-4800334_sp100.mp4", True),
        ("FC2-4800334_spX.mp4", True),
        ("FC2-4800334_sp_1.mp4", True),
        ("hhd800.com@fc4800334_spX.mp4", True),
        ("hhd800_com@fc4800334_4K_sp1.mp4", True),
        ("hhd800_com@fc4800334_4K_spX.mp4", True),
        ("ＦＣ２－４８００３３４＿ＳＰ１００．ｍｐ４", True),
        ("FC2-4800334_4K_sp.mp4", True),
        ("FC2-4800334-1080p-SP1.mp4", True),
        ("FC2-4800334_1_SP1.mp4", True),
        ("FC2-4800334_1_SPX.mp4", True),
        ("release FC2-4800334-SP1.mp4", True),
        ("download@fc4800334_4K_spX.mp4", True),
        ("FC2-4800334_1.mp4", False),
        ("FC2-4800334SP.mp4", False),
        ("FC2-4800334_1080pSP1.mp4", False),
        ("FC2-4800334_ASP1.mp4", False),
        ("SP1_FC2-4800334.mp4", False),
        ("release XFC2-4800334_sp.mp4", False),
        ("FC2-4800334A_sp.mp4", False),
        ("XFC2-4800334_sp.mp4", False),
        ("BCD-1_sp.mp4", False),
        ("CD-1_sp.mp4", False),
        ("FC2-1234_sp.mp4", False),
        ("/media/FC2-4800334_sp.mp4", False),
        ("fc4800334_FIX.mp4", True),
        ("FC2-4800334_4K_FIX1.mp4", True),
        ("FC2-4800334_FIX0.mp4", True),
        ("FC2-4800334_FIX100.mp4", True),
        ("FC2-4800334_FIXED.mp4", True),
        ("FC2-4800334_FIX_1.mp4", True),
        ("FC2-4800334_SP1_FIX1.mp4", True),
        ("hhd800_com@fc4800334_4K_FIX2.mp4", True),
        ("ＦＣ２－４８００３３４＿４Ｋ＿ＦＩＸ１００．ｍｐ４", True),
        ("download@fc4800334_4K_FIX0.mp4", True),
        ("FC2-4800334FIX.mp4", False),
        ("FC2-4800334_1080pFIX1.mp4", False),
        ("FC2-4800334_AFIX1.mp4", False),
        ("FIX1_FC2-4800334.mp4", False),
        ("XFC2-4800334_FIX.mp4", False),
        ("BCD-1_FIX.mp4", False),
        ("FC2-1234_FIX.mp4", False),
        ("/media/FC2-4800334_FIX.mp4", False),
    ],
)
def test_special_marker_detection_keeps_invalid_specials_visible(filename: str, expected: bool) -> None:
    assert has_fc2_special_marker(filename) is expected


@pytest.mark.parametrize(
    "filename",
    [
        "FC2-4800334-1080p-SP0.mp4",
        "FC2-4800334_1_SP1.mp4",
        "FC2-4800334_1_SPX.mp4",
        "release FC2-4800334-SP1.mp4",
        "download@fc4800334_4K_spX.mp4",
        "FC2-4800334-1080p-FIX0.mp4",
        "FC2-4800334_1_FIX1.mp4",
        "FC2-4800334_FIX1_SP1.mp4",
        "release FC2-4800334-FIX1.mp4",
        "download@fc4800334_4K_FIX0.mp4",
    ],
)
def test_special_intention_does_not_accept_ambiguous_members(filename: str) -> None:
    assert has_fc2_special_marker(filename)
    assert parse_fc2_member(filename) is None


@pytest.mark.parametrize(
    ("suffixes", "expected"),
    [
        ([], []),
        (["_4K"], [None]),
        (["_2", "_1"], [2, 1]),
        (["_3", "_1"], [3, 1]),
        (["_4K", "_1080p"], [None, None]),
        (["_4K", "_sp_4K"], [1, 2]),
        (["_4K", "_4K_sp"], [1, 2]),
        (["_4K_sp", "_4K"], [2, 1]),
        (["_sp_4K", "_4K"], [2, 1]),
        (["_2_4K", "_sp_4K", "_1_4K"], [2, 3, 1]),
        (["_1", "_2", "_sp1", "_sp2"], [1, 2, 3, 4]),
        (["_1_4K", "_2_4K", "_4K_sp1", "_4K_sp2"], [1, 2, 3, 4]),
        (["_4K_sp2", "_2_4K", "_4K_sp1", "_1_4K"], [4, 2, 3, 1]),
        (["_sp2", "_2", "_sp1", "_1"], [4, 2, 3, 1]),
        (["_1_4K", "_2_1080p", "_sp_720p"], [1, 2, 3]),
        (["-A", "-B", "_sp1"], [1, 2, 3]),
        (["-CD1", "-CD2", "_sp1", "_sp2"], [1, 2, 3, 4]),
        (["_4K", "_FIX_4K"], [1, 2]),
        (["_4K_FIX", "_4K"], [2, 1]),
        (["_1", "_2", "_FIX1", "_FIX2"], [1, 2, 3, 4]),
        (["_4K_FIX2", "_2_4K", "_4K_FIX1", "_1_4K"], [4, 2, 3, 1]),
        (["_FIX", "_SP", "_4K"], [3, 2, 1]),
        (["_4K", "_SP1", "_SP2", "_FIX"], [1, 2, 3, 4]),
        (["_FIX2", "_SP2", "_2", "_FIX1", "_1", "_SP1"], [6, 4, 2, 5, 1, 3]),
    ],
)
def test_plan_keeps_input_mapping_and_places_specials_after_mains(
    suffixes: list[str], expected: list[int | None]
) -> None:
    members = _parse_members(suffixes)
    snapshot = tuple(members)
    assert plan_fc2_parts(members) == expected
    assert tuple(members) == snapshot


@pytest.mark.parametrize(
    ("suffixes", "reason"),
    [
        (["_sp"], "特集缺少同组正片"),
        (["_sp1", "_sp2"], "特集缺少同组正片"),
        (["", "_4K", "_sp"], "正片存在多个未编号的画质或字幕版本"),
        (["", "", "_sp"], "正片存在多个未编号文件"),
        (["", "_1", "_sp"], "正片同时包含未编号文件与已编号分段"),
        (["_1", "_1", "_sp"], "正片第 1 段存在重复文件或不同版本"),
        (["_1_4K", "_1_1080p", "_sp"], "正片第 1 段存在重复文件或不同版本"),
        (["_1", "_1-C", "_sp"], "正片第 1 段存在重复文件或不同版本"),
        (["_2", "_sp"], "正片编号须从 1 连续排列"),
        (["_1", "_3", "_sp"], "正片编号须从 1 连续排列"),
        (["", "_sp", "_sp"], "特集存在多个未编号文件"),
        (["", "_sp_4K", "_sp_1080p"], "特集存在多个未编号的画质或字幕版本"),
        (["", "_sp", "_sp1"], "特集同时包含未编号文件与已编号分段"),
        (["", "_sp1", "_sp1"], "特集第 1 段存在重复文件或不同版本"),
        (["", "_sp2"], "特集编号须从 1 连续排列"),
        (["", "_sp1", "_sp3"], "特集编号须从 1 连续排列"),
        (["_FIX"], "FIX 缺少同组正片"),
        (["_FIX1", "_FIX2"], "FIX 缺少同组正片"),
        (["_SP", "_FIX"], "特集缺少同组正片"),
        (["", "_FIX", "_FIX"], "FIX 存在多个未编号文件"),
        (["", "_FIX_4K", "_FIX_1080p"], "FIX 存在多个未编号的画质或字幕版本"),
        (["", "_FIX", "_FIX1"], "FIX 同时包含未编号文件与已编号分段"),
        (["", "_FIX1", "_FIX1"], "FIX 第 1 段存在重复文件或不同版本"),
        (["", "_FIX1_4K", "_FIX1_1080p"], "FIX 第 1 段存在重复文件或不同版本"),
        (["", "_FIX2"], "FIX 编号须从 1 连续排列"),
        (["", "_SP", "_FIX1", "_FIX3"], "FIX 编号须从 1 连续排列"),
    ],
)
def test_plan_rejects_ambiguous_roles_versions_and_missing_segments(suffixes: list[str], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        plan_fc2_parts(_parse_members(suffixes))


@pytest.mark.parametrize("extra_role", ["special", "fix"])
def test_plan_validates_shared_number_and_output_cd_limit(extra_role: Literal["special", "fix"]) -> None:
    with pytest.raises(ValueError, match="不同 FC2 番号"):
        plan_fc2_parts([Fc2Member(number="FC2-4800334", role="main"), Fc2Member(number="FC2-4800335", role="main")])
    members = [Fc2Member(number="FC2-4800334", role="main", part=part) for part in range(1, 100)]
    members.append(Fc2Member(number="FC2-4800334", role=extra_role))
    with pytest.raises(ValueError, match="合计超过 99 段"):
        plan_fc2_parts(members)
    allowed = [*members[:98], members[-1]]
    assert plan_fc2_parts(allowed) == list(range(1, 100))


@pytest.mark.parametrize(
    ("suffixes", "expected"),
    [
        ([], []),
        (["_4K"], [1]),
        (["_1_4K"], [1]),
        (["_2", "_1"], [2, 1]),
        (["-B", "-A"], [2, 1]),
        (["_FIX", "_4K"], [2, 1]),
        (["_FIX2", "_SP2", "_2", "_FIX1", "_1", "_SP1"], [6, 4, 2, 5, 1, 3]),
    ],
)
def test_required_main_plan_assigns_complete_cd_sequence(suffixes: list[str], expected: list[int]) -> None:
    members = _parse_members(suffixes)
    snapshot = tuple(members)
    assert plan_fc2_parts(members, require_main_plan=True) == expected
    assert tuple(members) == snapshot


@pytest.mark.parametrize(
    ("suffixes", "reason"),
    [
        (["_2"], "正片编号须从 1 连续排列"),
        (["_1", "_3"], "正片编号须从 1 连续排列"),
        (["", "_1"], "正片同时包含未编号文件与已编号分段"),
        (["", ""], "正片存在多个未编号文件"),
        (["_4K", "_1080p"], "正片存在多个未编号的画质或字幕版本"),
        (["_1", "_1"], "正片第 1 段存在重复文件或不同版本"),
        (["_1_4K", "_1_1080p"], "正片第 1 段存在重复文件或不同版本"),
        (["_SP"], "特集缺少同组正片"),
        (["_FIX"], "FIX 缺少同组正片"),
    ],
)
def test_required_main_plan_rejects_unproven_main_sequence(suffixes: list[str], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        plan_fc2_parts(_parse_members(suffixes), require_main_plan=True)


@pytest.mark.parametrize("part", [0, 100, -1, True, "1"])
def test_member_requires_a_supported_local_part(part: object) -> None:
    with pytest.raises(ValidationError, match="part"):
        Fc2Member.model_validate({"number": "FC2-4800334", "role": "main", "part": part})


def test_member_normalizes_identity_and_rejects_other_catalogs() -> None:
    member = Fc2Member(number="fc4800334", role="special", part=1)
    assert member.number == "FC2-4800334"
    with pytest.raises(ValidationError, match="完整商品番号"):
        Fc2Member(number="BCD-1", role="main")


def _parse_members(suffixes: list[str]) -> list[Fc2Member]:
    members: list[Fc2Member] = []
    for suffix in suffixes:
        member = parse_fc2_member(f"fc4800334{suffix}.mp4")
        assert member is not None
        members.append(member)
    return members
