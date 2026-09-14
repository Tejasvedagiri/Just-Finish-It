// Hidden reference test suite -- the agent is instructed not to modify this
// file. This is the objective pass/fail oracle for the task, independent of
// whatever tests the agent's own Testing phase writes for itself.
//
// Expected values mirror the Python variant's reference-computed output --
// JS's default Array.prototype.sort() on plain-ASCII strings compares by
// UTF-16 code unit, same ordering as Python's default string sort here.
const test = require('node:test');
const assert = require('node:assert/strict');
const { groupAnagrams } = require('../anagram_groups.js');

const CASES = [
  [[], []],
  [['hello'], [['hello']]],
  [['eat', 'tea', 'tan', 'ate', 'nat', 'bat'],
    [['ate', 'eat', 'tea'], ['bat'], ['nat', 'tan']]],
  [['Listen', 'Silent', 'Enlist'], [['Enlist', 'Listen', 'Silent']]],
  [['abc', 'cab', 'abc'], [['abc', 'abc', 'cab']]],
  [['a'], [['a']]],
  [['a', 'a', 'b'], [['a', 'a'], ['b']]],
];

test('groupAnagrams', () => {
  for (const [words, expected] of CASES) {
    assert.deepEqual(groupAnagrams(words), expected, JSON.stringify(words));
  }
});
