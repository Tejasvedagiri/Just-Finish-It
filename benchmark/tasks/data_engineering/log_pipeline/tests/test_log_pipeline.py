# Hidden reference test suite -- the agent is instructed not to modify this
# file, nor the fixed input dataset at tests/access.log. This is the
# objective pass/fail oracle for the task, independent of whatever tests the
# agent's own Testing phase writes for itself.
#
# Expected values below were computed by actually running a reference
# implementation against tests/access.log (not hand-derived) -- see
# benchmark/README.md's Evaluation methodology section for why that matters.
import os

from log_pipeline import parse_logs

LOG_PATH = os.path.join(os.path.dirname(__file__), "access.log")


def test_total_lines():
    result = parse_logs(LOG_PATH)
    assert result["total_lines"] == 102


def test_error_count_by_service():
    result = parse_logs(LOG_PATH)
    assert result["error_count_by_service"] == {
        "auth": 3,
        "billing": 4,
        "checkout": 0,
        "search": 5,
    }


def test_overall_error_rate():
    result = parse_logs(LOG_PATH)
    assert result["overall_error_rate"] == 0.118


def test_busiest_hour():
    result = parse_logs(LOG_PATH)
    assert result["busiest_hour"] == "02"


def test_exact_keys():
    result = parse_logs(LOG_PATH)
    assert set(result.keys()) == {
        "total_lines", "error_count_by_service", "overall_error_rate", "busiest_hour",
    }
