"""FC2 分段分配迁移的唯一归属、外键及原有数据保留."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.helpers import alembic_config


def test_fc2_part_plan_upgrade_constraints_and_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    config = alembic_config(path)
    command.upgrade(config, "b214ab0f2c78")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO libraries (id,name,path,automation,auto_organize,ingest,recursive,patterns,"
                "move_mode,video_template,link_mode,write_nfo,copy_resources,blacklist_patterns,"
                "subtitle_extensions,min_file_size) VALUES "
                "(101,'FC2','/fc2','SCRAPE',1,'NATIVE',1,'[]','MOVE','{number}.{ext}',"
                "'STRM',0,'[]','[]','[]',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO metadata(id,number,title,content_type,created_at,updated_at) "
                "VALUES (101,'FC2-4800334','Keep','FC2','2026-11-09','2026-11-09')"
            )
        )
    engine.dispose()
    command.upgrade(config, "2023ee887911")

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        foreign_keys = inspect(connection).get_foreign_keys("fc2_part_plans")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "libraries"
        assert foreign_keys[0].get("options", {}).get("ondelete") == "CASCADE"
        assert connection.execute(text("SELECT title FROM metadata WHERE id=101")).scalar_one() == "Keep"
        insert = text(
            "INSERT INTO fc2_part_plans(library_id,original_directory,directory_key,number,members,"
            "created_at,updated_at) VALUES (:library,'/fc2',:directory,'FC2-4800334','[]',"
            "'2026-11-09','2026-11-09')"
        )
        connection.execute(insert, {"library": 101, "directory": "/fc2"})
        connection.commit()
        with pytest.raises(IntegrityError, match="UNIQUE"):
            connection.execute(insert, {"library": 101, "directory": "/fc2"})
        connection.rollback()
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            connection.execute(insert, {"library": 999, "directory": "/other"})
        connection.rollback()
        connection.execute(text("DELETE FROM metadata WHERE id=101"))
        assert connection.execute(text("SELECT count(*) FROM fc2_part_plans")).scalar_one() == 1
        connection.execute(text("DELETE FROM libraries WHERE id=101"))
        assert connection.execute(text("SELECT count(*) FROM fc2_part_plans")).scalar_one() == 0
        connection.rollback()

    command.downgrade(config, "b214ab0f2c78")
    with engine.connect() as connection:
        assert "fc2_part_plans" not in inspect(connection).get_table_names()
        assert connection.execute(text("SELECT title FROM metadata WHERE id=101")).scalar_one() == "Keep"
        assert connection.execute(text("SELECT name FROM libraries WHERE id=101")).scalar_one() == "FC2"
    engine.dispose()
