"""共享刮削迁移保留旧任务与任务链, 外键仅约束新组归属."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.helpers import alembic_config


def test_scrape_group_migration_preserves_tasks_and_links(tmp_path: Path) -> None:
    path = tmp_path / "groups.db"
    config = alembic_config(path)
    command.upgrade(config, "2023ee887911")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tasks(id,type,status,payload,result,retries,priority,root_task_id,created_at) VALUES "
                "(1,'REFRESH','DONE','{}','{}',0,0,1,'2026-11-09'),"
                "(2,'SCRAPE','QUEUED','{\"number\":\"ABC-123\"}',NULL,0,0,1,'2026-11-09')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO task_links(parent_task_id,child_task_id,key,created_at) VALUES(1,2,'scrape:1','2026-11-09')"
            )
        )
    engine.dispose()
    command.upgrade(config, "0bc5a04e2fb7")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.execute(text("SELECT scrape_group_id FROM tasks ORDER BY id")).scalars().all() == [None, None]
        assert connection.execute(text("SELECT parent_task_id,child_task_id,key FROM task_links")).one() == (
            1,
            2,
            "scrape:1",
        )
        connection.execute(
            text(
                "INSERT INTO scrape_groups(id,key,status,producer_task_id,result,created_at,updated_at) "
                "VALUES (1,'key','DONE',2,:result,'2026-11-09','2026-11-09')"
            ),
            {"result": '{"metadata_id":10}'},
        )
        connection.execute(text("UPDATE tasks SET scrape_group_id=1 WHERE id=2"))
        connection.commit()
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            connection.execute(text("UPDATE tasks SET scrape_group_id=999 WHERE id=2"))
        connection.rollback()
        connection.execute(text("DELETE FROM scrape_groups WHERE id=1"))
        assert connection.execute(text("SELECT scrape_group_id FROM tasks WHERE id=2")).scalar_one() is None
        connection.rollback()
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()

    command.downgrade(config, "2023ee887911")
    with engine.connect() as connection:
        assert "scrape_groups" not in inspect(connection).get_table_names()
        assert "scrape_group_id" not in {column["name"] for column in inspect(connection).get_columns("tasks")}
        assert connection.execute(text("SELECT type,status,root_task_id FROM tasks WHERE id=2")).one() == (
            "SCRAPE",
            "QUEUED",
            1,
        )
        assert connection.execute(text("SELECT parent_task_id,child_task_id,key FROM task_links")).one() == (
            1,
            2,
            "scrape:1",
        )
    engine.dispose()
