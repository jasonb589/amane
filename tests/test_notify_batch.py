"""通知合并摘要 / 长度守卫 / 错误摘要的用例."""

from amane.notify.service import _batch_summary, _clip, _error_short


def test_batch_summary_single_is_empty() -> None:
    assert _batch_summary(1, ["ABC-1"]) == ""


def test_batch_summary_separates_task_and_movie_counts() -> None:
    # 任务事件数 与 去重番号数 要分开写: 同一部片的多文件 / 子任务会让事件数更大.
    numbers = [f"ABC-{i:03d}" for i in range(1, 13)]
    out = _batch_summary(80, numbers)
    assert out.startswith("\n共 80 个任务 · 涉及 12 部：")
    # 前 8 个列出来, 其余用省略号收掉.
    assert "ABC-008" in out
    assert "ABC-009" not in out
    assert out.endswith(" …")


def test_batch_summary_dedupes_repeated_numbers() -> None:
    out = _batch_summary(3, ["ABC-1", "ABC-1", "ABC-2"])
    assert out == "\n共 3 个任务 · 涉及 2 部：ABC-1、ABC-2"


def test_batch_summary_without_numbers() -> None:
    assert _batch_summary(2, []) == "\n共 2 个任务"


def test_clip_respects_limit_and_cuts_partial_tag() -> None:
    assert _clip("abc", 10) == "abc"
    clipped = _clip("x" * 50, 20)
    assert len(clipped) == 20
    assert clipped.endswith("…")
    # 截断点落在标签中间时不能留下半个 <blockq, 否则 Telegram 会报解析失败.
    assert _clip("头部<blockquote>内容", 8) == "头部…"


def test_error_short_takes_first_line() -> None:
    error = "\n\nValueError: 番号解析失败\nTraceback ...\n"
    assert _error_short(error) == "ValueError: 番号解析失败"
    assert _error_short("") == ""
    assert len(_error_short("x" * 500)) == 121
