# Hidden reference test suite -- the agent is instructed not to modify this
# file. This is the objective pass/fail oracle for the task, independent of
# whatever tests the agent's own Testing phase writes for itself.
import pytest

from difference_of_squares import square_of_sum, sum_of_squares


@pytest.mark.parametrize("n,expected", [(1, 1), (5, 225), (10, 3025), (100, 25502500)])
def test_square_of_sum(n, expected):
    assert square_of_sum(n) == expected


@pytest.mark.parametrize("n,expected", [(1, 1), (5, 55), (10, 385), (100, 338350)])
def test_sum_of_squares(n, expected):
    assert sum_of_squares(n) == expected


@pytest.mark.parametrize("n,expected", [(1, 0), (5, 170), (10, 2640)])
def test_difference(n, expected):
    assert square_of_sum(n) - sum_of_squares(n) == expected
