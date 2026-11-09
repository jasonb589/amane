"""FC2 追加成员前复核原成员归属, 首次规划不接受缺少首段的正片."""

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from structlog.testing import capture_logs

from amane.config import HotSettings
from amane.db.models import MediaFileStatus
from amane.handlers.fc2_parts import resolve_fc2_part
from tests.handlers.test_fc2_append import _add_member
from tests.handlers.test_fc2_specials import _make_group, _organize_one, _output

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["unplaced_source", "placed_target"])
@pytest.mark.parametrize("owner_change", ["metadata", "library"])
async def test_append_rejects_changed_ownership_of_existing_source_or_target(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    stage: Literal["unplaced_source", "placed_target"],
    owner_change: Literal["metadata", "library"],
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K",), internal=True)
    assert group.library.id is not None and group.metadata.id is not None
    main = await repo.get_media_file(group.media_ids[0])
    assert main is not None and main.id is not None
    context = await resolve_fc2_part(repo, main, group.metadata, group.library, HotSettings())
    assert context is not None and context.member.cd == 1
    if stage == "placed_target":
        organized = await _organize_one(repo, resource_store, group, main.id, tmp_path)
        assert (organized.organized, organized.failed, organized.conflicted) == (1, 0, 0)
        main = await repo.get_media_file(main.id)
        assert main is not None and main.id is not None and Path(main.path) == _output(group, 1)
        assert not group.files[0].exists()

    original_path = Path(main.path)
    original_content = original_path.read_bytes()
    if owner_change == "metadata":
        other = await repo.upsert_metadata(number="FC2-9999999", title="Another film")
        assert other.id is not None
        changed = await repo.update_media_file(main.id, metadata_id=other.id)
        expected_library_id, expected_metadata_id = group.library.id, other.id
        reason = "已关联其他影片"
    else:
        other_root = tmp_path / "other-library"
        other_root.mkdir()
        library = await repo.create_library(name="Another library", path=str(other_root))
        assert library.id is not None
        await repo.delete_media_file(main.id)
        changed = await repo.create_media_file(
            library.id,
            path=str(original_path),
            number=group.metadata.number,
            metadata_id=group.metadata.id,
            status=MediaFileStatus.SCRAPED,
        )
        expected_library_id, expected_metadata_id = library.id, group.metadata.id
        reason = "属于其他媒体库"
    assert changed is not None and changed.id is not None
    before = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert before is not None and len(before.members) == 1
    assert before.members[0].placed is (stage == "placed_target")
    outputs_before = {path.name: path.read_bytes() for path in group.output_dir.glob("*")}
    late, late_id = await _add_member(repo, group, "_4K_sp")
    late_content = late.read_bytes()
    subtitle = late.with_suffix(".chs.srt")
    subtitle.write_text("late subtitle", encoding="utf-8")

    with capture_logs() as records:
        rejected = await _organize_one(repo, resource_store, group, late_id, tmp_path)

    assert (rejected.organized, rejected.failed, rejected.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    after = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert after is not None and after.id == before.id
    assert after.members == before.members and after.updated_at == before.updated_at
    assert original_path.read_bytes() == original_content
    assert late.read_bytes() == late_content and subtitle.read_text(encoding="utf-8") == "late subtitle"
    assert {path.name: path.read_bytes() for path in group.output_dir.glob("*")} == outputs_before
    assert not _output(group, 2).exists()
    retained_main = await repo.get_media_file(changed.id)
    assert retained_main is not None and Path(retained_main.path) == original_path
    assert (retained_main.library_id, retained_main.metadata_id) == (expected_library_id, expected_metadata_id)
    retained_late = await repo.get_media_file(late_id)
    assert retained_late is not None and retained_late.path == str(late)
    assert retained_late.metadata_id == group.metadata.id and retained_late.status is MediaFileStatus.SCRAPED


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["_2_4K", "-CD2"])
async def test_solo_second_main_without_plan_remains_in_source_until_first_part_is_available(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, suffix: str
) -> None:
    group = await _make_group(repo, tmp_path, (suffix,))
    assert group.library.id is not None
    source = group.files[0]
    original_content = source.read_bytes()
    subtitle = source.with_suffix(".chs.srt")
    subtitle.write_text("part two subtitle", encoding="utf-8")

    with capture_logs() as records:
        rejected = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)

    assert (rejected.organized, rejected.failed, rejected.conflicted) == (0, 1, 0)
    assert any("正片编号须从 1 连续排列" in str(record.get("error", "")) for record in records)
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None
    assert source.read_bytes() == original_content
    assert subtitle.read_text(encoding="utf-8") == "part two subtitle"
    assert not group.output_dir.exists()
    retained = await repo.get_media_file(group.media_ids[0])
    assert retained is not None and retained.path == str(source)
    assert retained.status is MediaFileStatus.SCRAPED and retained.metadata_id == group.metadata.id
