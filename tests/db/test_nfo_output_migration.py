"""NFO 输出记录迁移保留现有数据并强制唯一归属与外键."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.helpers import alembic_config


def test_nfo_output_upgrade_and_downgrade_preserve_metadata(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    config = alembic_config(path)
    command.upgrade(config, "b7c8d9e0f1a2")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO metadata(id, number, title, content_type, created_at, updated_at) VALUES (1, 'ABC-001', 'Keep', 'censored', '2026-11-07', '2026-11-07')"
            )
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT title FROM metadata WHERE id=1")).scalar_one() == "Keep"
        assert {fk["referred_table"] for fk in inspect(connection).get_foreign_keys("nfo_outputs")} == {
            "metadata",
            "libraries",
        }
        connection.execute(
            text(
                "INSERT INTO nfo_outputs(metadata_id, library_id, video_path, nfo_path, path_key, updated_at) VALUES (1,1,'/v.mp4','/v.nfo','/v.nfo','2026-11-07')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO nfo_outputs(metadata_id, library_id, video_path, nfo_path, path_key, updated_at) VALUES (2,1,'/other.mp4','/v.nfo','/v.nfo','2026-11-07')"
                )
            )
    command.downgrade(config, "b7c8d9e0f1a2")
    with engine.connect() as connection:
        assert "nfo_outputs" not in inspect(connection).get_table_names()
        assert connection.execute(text("SELECT title FROM metadata WHERE id=1")).scalar_one() == "Keep"
    engine.dispose()
