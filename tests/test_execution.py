from hermes_wecom_code_governor.execution import (
    MAX_OUTPUT_CHARS,
    TIMEOUT_EXIT_CODE,
    combine_output,
)


def test_timeout_exit_code_is_the_shared_124() -> None:
    assert TIMEOUT_EXIT_CODE == 124


def test_combine_output_joins_nonempty_streams_and_strips() -> None:
    assert combine_output("  out  ", "err") == "out\nerr"
    assert combine_output("", "only-err") == "only-err"
    assert combine_output("only-out\n", "") == "only-out"
    assert combine_output("  ", "  ") == ""


def test_combine_output_keeps_the_tail_and_flags_the_omission() -> None:
    body = "x" * (MAX_OUTPUT_CHARS + 500)
    result = combine_output(body, "")
    assert result.startswith("[前面 500 个字符已省略]\n")
    assert result.endswith("x" * MAX_OUTPUT_CHARS)
    assert len(result.split("\n", 1)[1]) == MAX_OUTPUT_CHARS
