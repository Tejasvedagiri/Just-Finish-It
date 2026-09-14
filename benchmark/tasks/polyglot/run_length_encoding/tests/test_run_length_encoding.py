# Hidden reference test suite -- the agent is instructed not to modify this
# file. This is the objective pass/fail oracle for the task, independent of
# whatever tests the agent's own Testing phase writes for itself.
import pytest

from run_length_encoding import encode, decode


@pytest.mark.parametrize("plain,coded", [
    ("", ""),
    ("XYZ", "XYZ"),
    ("AABBBCCCC", "2A3B4C"),
    ("WWWWWWWWWWWWBWWWWWWWWWWWWBBBWWWWWWWWWWWWWWWWWWWWWWWWB", "12WB12W3B24WB"),
    ("aabbbcccc", "2a3b4c"),
    ("a", "a"),
    ("aaa", "3a"),
])
def test_encode(plain, coded):
    assert encode(plain) == coded


@pytest.mark.parametrize("plain,coded", [
    ("", ""),
    ("XYZ", "XYZ"),
    ("AABBBCCCC", "2A3B4C"),
    ("WWWWWWWWWWWWBWWWWWWWWWWWWBBBWWWWWWWWWWWWWWWWWWWWWWWWB", "12WB12W3B24WB"),
    ("aabbbcccc", "2a3b4c"),
])
def test_decode(plain, coded):
    assert decode(coded) == plain


@pytest.mark.parametrize("plain", ["", "XYZ", "AABBBCCCC", "aabbbcccc", "zzzzzzzzzz"])
def test_round_trip(plain):
    # Input is intentionally letters-only: a plain digit prefix (e.g. "123")
    # is genuinely ambiguous for this scheme (a literal "1" vs. a count of
    # 1), the same restriction the real Exercism run-length-encoding
    # exercise places on its own test data.
    assert decode(encode(plain)) == plain
