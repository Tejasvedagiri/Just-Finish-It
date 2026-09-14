# Hidden reference test suite -- the agent is instructed not to modify this
# file. This is the objective pass/fail oracle for the task, independent of
# whatever tests the agent's own Testing phase writes for itself.
#
# Expected values below were computed by actually running a reference
# implementation (not hand-derived) -- see benchmark/README.md's Evaluation
# methodology section for why that matters.
import pytest

from anagram_groups import group_anagrams


@pytest.mark.parametrize("words,expected", [
    ([], []),
    (["hello"], [["hello"]]),
    (["eat", "tea", "tan", "ate", "nat", "bat"],
     [["ate", "eat", "tea"], ["bat"], ["nat", "tan"]]),
    (["Listen", "Silent", "Enlist"], [["Enlist", "Listen", "Silent"]]),
    (["abc", "cab", "abc"], [["abc", "abc", "cab"]]),
    (["a"], [["a"]]),
    (["a", "a", "b"], [["a", "a"], ["b"]]),
])
def test_group_anagrams(words, expected):
    assert group_anagrams(words) == expected
