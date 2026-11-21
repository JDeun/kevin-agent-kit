import sys
from threading import Event

import pytest

from core.loop.runtime import CommandFailed, Retry, run


def command(body):
    return [sys.executable, "-I", "-c", "import os, time\n" + body]


@pytest.mark.parametrize(("finish", "error_type", "message"), [
    ("raise SystemExit(2)", CommandFailed, "status 2"),
    ("raise SystemExit(75)", Retry, "temporary failure"),
    ("time.sleep(10)", Retry, "timed out"),
    ("os.write(1, b'\\xff')", ValueError, "not UTF-8"),
    ("os.write(2, b'x' * (1024 * 1024 + 1))", ValueError, "exceeded 1 MiB"),
])
def test_review_failure_retains_only_stdout(tmp_path, finish, error_type, message):
    argv = command("os.write(1, b'review fixture')\nos.write(2, b'stderr fixture')\n" + finish)
    with pytest.raises(error_type, match=message) as caught:
        run(argv, tmp_path, Event(), 1, capture_failed_output=True)
    expected = "review fixture" + ("\0" if message == "not UTF-8" else "")
    assert caught.value.output == expected
    assert "fixture" not in str(caught.value) and "fixture" not in repr(caught.value)


def test_failed_output_is_bounded_at_one_mib(tmp_path):
    argv = command("os.write(1, b'x' * (1024 * 1024) + b'out of bounds')")
    with pytest.raises(ValueError, match="exceeded 1 MiB") as caught:
        run(argv, tmp_path, Event(), 5, capture_failed_output=True)
    assert caught.value.output == "x" * (1024 * 1024)


def test_failed_capture_preserves_invalid_utf8_as_invalid_json(tmp_path):
    raw = b'{"findings":[{"detail":"corrupt\xff"},{"detail":"clean"}]}'
    argv = command(f"os.write(1, {raw!r})")
    with pytest.raises(ValueError, match="not UTF-8") as caught:
        run(argv, tmp_path, Event(), 5, capture_failed_output=True)
    assert caught.value.output == '{"findings":[{"detail":"corrupt\0"},{"detail":"clean"}]}'


def test_failed_output_is_opt_in(tmp_path):
    argv = command("os.write(1, b'private worker output')\nraise SystemExit(3)")
    with pytest.raises(CommandFailed) as caught:
        run(argv, tmp_path, Event(), 5)
    assert not hasattr(caught.value, "output")
    assert "private" not in str(caught.value)
