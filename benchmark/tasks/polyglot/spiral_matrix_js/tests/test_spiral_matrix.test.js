// Hidden reference test suite -- the agent is instructed not to modify this
// file. This is the objective pass/fail oracle for the task, independent of
// whatever tests the agent's own Testing phase writes for itself.
//
// Expected matrices mirror the Python variant's reference-computed output.
const test = require('node:test');
const assert = require('node:assert/strict');
const { spiral } = require('../spiral_matrix.js');

const CASES = [
  [0, []],
  [1, [[1]]],
  [2, [[1, 2], [4, 3]]],
  [3, [[1, 2, 3], [8, 9, 4], [7, 6, 5]]],
  [4, [[1, 2, 3, 4], [12, 13, 14, 5], [11, 16, 15, 6], [10, 9, 8, 7]]],
  [5, [[1, 2, 3, 4, 5], [16, 17, 18, 19, 6], [15, 24, 25, 20, 7],
       [14, 23, 22, 21, 8], [13, 12, 11, 10, 9]]],
];

test('spiral', () => {
  for (const [n, expected] of CASES) {
    assert.deepEqual(spiral(n), expected, `n=${n}`);
  }
});

test('rejects negative', () => {
  for (const n of [-1, -5]) {
    assert.throws(() => spiral(n));
  }
});
