"""上游与自定义数据库均可升级到统一末端, 人工数据与整理记录保持有效."""

from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from amane.db.sqlite_migrate import upgrade_sqlite_database
from tests.helpers import alembic_config


@pytest.mark.parametrize("previous", ["384b2b3fdd6a", "fb4fd486708c"])
def test_upgrade_preserves_both_branch_data(tmp_path: Path, previous: str) -> None:
    path = tmp_path / "amane.db"
    config = alembic_config(path)
    command.upgrade(config, previous)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO metadata(id, number, title, content_type, created_at, updated_at) "
                    "VALUES (1, 'ABC-001', '人工标题', 'CENSORED', '2026-11-08', '2026-11-08')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO libraries(id, name, path, automation, recursive, patterns, move_mode, "
                    "video_template, write_nfo, copy_resources, trailer_pattern, blacklist_patterns, "
                    "subtitle_extensions, min_file_size, link_mode, auto_organize, ingest) "
                    "VALUES (1, 'Library', '/media', 'SCRAPE', 1, '[]', 'MOVE', '{number}.{ext}', "
                    "1, '[]', '(?i)trailer', '[]', '[]', 0, 'STRM', 1, 'NATIVE')"
                )
            )
            if previous == "384b2b3fdd6a":
                connection.execute(
                    text(
                        "INSERT INTO nfo_outputs(metadata_id, library_id, video_path, nfo_path, path_key, "
                        "updated_at) VALUES (1,1,'/moved/v.mp4','/moved/v.nfo','/moved/v.nfo','2026-11-08')"
                    )
                )
            else:
                connection.execute(text("UPDATE libraries SET extrafanart_name_template = 'scene-{index_padded}.jpg'"))
        engine.dispose()

        upgrade_sqlite_database(path)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT title FROM metadata WHERE id=1")).scalar_one() == "人工标题"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
                ScriptDirectory.from_config(config).get_current_head()
            ]
            assert "nfo_outputs" in inspect(connection).get_table_names()
            template = connection.execute(text("SELECT extrafanart_name_template FROM libraries")).scalar_one()
            outputs = connection.execute(text("SELECT video_path, nfo_path FROM nfo_outputs")).all()
            if previous == "384b2b3fdd6a":
                assert template is None
                assert outputs == [("/moved/v.mp4", "/moved/v.nfo")]
            else:
                assert template == "scene-{index_padded}.jpg"
                assert outputs == []
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert len(list(tmp_path.glob("amane.db.pre-migrate-*.bak"))) == 1
        upgrade_sqlite_database(path)
        assert len(list(tmp_path.glob("amane.db.pre-migrate-*.bak"))) == 1
    finally:
        engine.dispose()
