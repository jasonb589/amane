"""双分支升级保留任务复用、FC2 分配和 NFO 输出, 并合并上游分辨率迁移."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.helpers import alembic_config


@pytest.mark.parametrize("previous", ["0bc5a04e2fb7", "e06def65f72a"])
def test_custom_upstream_merge_preserves_data_from_either_branch(tmp_path: Path, previous: str) -> None:
    path = tmp_path / "merge.db"
    config = alembic_config(path)
    command.upgrade(config, previous)
    custom = previous == "0bc5a04e2fb7"
    definition = "HD" if custom else "720p"
    video = str(tmp_path / "FC2-4800334_HD.mp4")
    nfo = str(tmp_path / "FC2-4800334.nfo")
    members = json.dumps(
        [
            {
                "source_path": video,
                "role": "main",
                "cd": 1,
                "source_size": 12345,
                "source_mtime_ns": 100,
                "target_path": str(tmp_path / "FC2-4800334-CD1.mp4"),
                "target_mtime_ns": 200,
                "placed": True,
            },
            {
                "source_path": str(tmp_path / "fc4800334_sp_4K.mp4"),
                "role": "special",
                "cd": 2,
                "source_size": 23456,
                "source_mtime_ns": 300,
            },
        ]
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO libraries(id,name,path,recursive,patterns,video_template) VALUES (101,'FC2',?,1,'[]',?)",
            (str(tmp_path), "{number}[[{def?|" + definition + "=高清}]].{ext}"),
        )
        connection.execute(
            "INSERT INTO metadata(id,number,title,plot,content_type,raw,created_at,updated_at) VALUES "
            "(101,'FC2-4800334','原始标题','保留剧情','FC2',?, '2026-11-09','2026-11-09')",
            (json.dumps({"test": {"title": "日本語原題"}}),),
        )
        connection.execute(
            "INSERT INTO media_files(path,status,library_id,metadata_id,definition,created_at,updated_at) VALUES "
            "(?,'scraped',101,101,?,'2026-11-09','2026-11-09')",
            (video, definition),
        )
        connection.executescript(
            "INSERT INTO tasks(id,type,status,payload,result,retries,priority,root_task_id,created_at) VALUES "
            "(201,'REFRESH','DONE','{}','{}',0,0,201,'2026-11-09'),"
            "(202,'SCRAPE','QUEUED','{\"number\":\"FC2-4800334\"}',NULL,0,0,201,'2026-11-09');"
            "INSERT INTO task_links(parent_task_id,child_task_id,key,created_at) VALUES "
            "(201,202,'scrape:1','2026-11-09');"
        )
        preserved = ("metadata", "tasks", "task_links")
        if custom:
            connection.execute(
                "INSERT INTO fc2_part_plans(library_id,original_directory,directory_key,number,members,"
                "created_at,updated_at) VALUES (101,?,?,'FC2-4800334',?,'2026-11-09','2026-11-09')",
                (str(tmp_path), str(tmp_path), members),
            )
            connection.execute(
                "INSERT INTO nfo_outputs(metadata_id,library_id,video_path,nfo_path,path_key,updated_at) "
                "VALUES (101,101,?,?,?,'2026-11-09')",
                (video, nfo, nfo),
            )
            connection.execute(
                "INSERT INTO scrape_groups(id,key,status,producer_task_id,result,created_at,updated_at) "
                "VALUES (301,'same-round','DONE',202,'{\"metadata_id\":101}','2026-11-09','2026-11-09')"
            )
            connection.execute("UPDATE tasks SET scrape_group_id=301 WHERE id=202")
            preserved += ("fc2_part_plans", "nfo_outputs", "scrape_groups")
        before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in preserved}
        connection.commit()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (ScriptDirectory.from_config(config).get_current_head(),)
        ]
        for table, rows in before.items():
            upgraded = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            if table == "tasks" and not custom:
                # 上游任务增加组归属列; 既有任务仍保持独立, 其余列与顺序保留.
                assert upgraded == [(*row, None) for row in rows]
            else:
                assert upgraded == rows, table
        assert connection.execute("SELECT definition FROM media_files").fetchall() == [("720p",)]
        assert connection.execute("SELECT video_template FROM libraries WHERE id=101").fetchone() == (
            "{number}[[{def?|720p=高清}]].{ext}",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        if custom:
            connection.execute("DELETE FROM scrape_groups WHERE id=301")
            assert connection.execute("SELECT scrape_group_id FROM tasks WHERE id=202").fetchone() == (None,)
            assert connection.execute("SELECT parent_task_id,child_task_id,key FROM task_links").fetchone() == (
                201,
                202,
                "scrape:1",
            )
        else:
            assert connection.execute("SELECT count(*) FROM fc2_part_plans").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM nfo_outputs").fetchone() == (0,)
