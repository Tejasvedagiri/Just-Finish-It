# Hidden reference test suite -- the agent is instructed not to modify this
# file. This is the objective pass/fail oracle for the task, independent of
# whatever tests the agent's own Testing phase writes for itself.
import pytest

from collatz_conjecture import steps


@pytest.mark.parametrize("n,expected", [(1, 0), (16, 4), (12, 9), (1000000, 152)])
def test_steps(n, expected):
    assert steps(n) == expected


@pytest.mark.parametrize("n", [0, -1, -100])
def test_rejects_non_positive(n):
    with pytest.raises(ValueError):
        steps(n)
