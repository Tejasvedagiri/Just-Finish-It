// Hidden reference test suite -- the agent is instructed not to modify this
// file. This is the objective pass/fail oracle for the task, independent of
// whatever tests the agent's own Testing phase writes for itself.
const test = require('node:test');
const assert = require('node:assert/strict');
const { steps } = require('../collatz_conjecture.js');

test('steps', () => {
  assert.equal(steps(1), 0);
  assert.equal(steps(16), 4);
  assert.equal(steps(12), 9);
  assert.equal(steps(1000000), 152);
});

test('rejects non-positive', () => {
  for (const n of [0, -1, -100]) {
    assert.throws(() => steps(n));
  }
});
