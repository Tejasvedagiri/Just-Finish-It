// Hidden reference test suite -- the agent is instructed not to modify this
// file. This is the objective pass/fail oracle for the task, independent of
// whatever tests the agent's own Testing phase writes for itself.
const test = require('node:test');
const assert = require('node:assert/strict');
const { encode, decode } = require('../run_length_encoding.js');

const CASES = [
  ['', ''],
  ['XYZ', 'XYZ'],
  ['AABBBCCCC', '2A3B4C'],
  ['WWWWWWWWWWWWBWWWWWWWWWWWWBBBWWWWWWWWWWWWWWWWWWWWWWWWB', '12WB12W3B24WB'],
  ['aabbbcccc', '2a3b4c'],
  ['a', 'a'],
  ['aaa', '3a'],
];

test('encode', () => {
  for (const [plain, coded] of CASES) {
    assert.equal(encode(plain), coded, `encode(${JSON.stringify(plain)})`);
  }
});

test('decode', () => {
  for (const [plain, coded] of CASES) {
    assert.equal(decode(coded), plain, `decode(${JSON.stringify(coded)})`);
  }
});

test('round trip', () => {
  for (const plain of ['', 'XYZ', 'AABBBCCCC', 'aabbbcccc', 'zzzzzzzzzz']) {
    assert.equal(decode(encode(plain)), plain);
  }
});
