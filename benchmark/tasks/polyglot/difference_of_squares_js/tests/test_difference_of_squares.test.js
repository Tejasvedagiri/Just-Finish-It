// Hidden reference test suite -- the agent is instructed not to modify this
// file. This is the objective pass/fail oracle for the task, independent of
// whatever tests the agent's own Testing phase writes for itself.
const test = require('node:test');
const assert = require('node:assert/strict');
const { squareOfSum, sumOfSquares } = require('../difference_of_squares.js');

test('squareOfSum', () => {
  assert.equal(squareOfSum(1), 1);
  assert.equal(squareOfSum(5), 225);
  assert.equal(squareOfSum(10), 3025);
  assert.equal(squareOfSum(100), 25502500);
});

test('sumOfSquares', () => {
  assert.equal(sumOfSquares(1), 1);
  assert.equal(sumOfSquares(5), 55);
  assert.equal(sumOfSquares(10), 385);
  assert.equal(sumOfSquares(100), 338350);
});

test('difference', () => {
  assert.equal(squareOfSum(1) - sumOfSquares(1), 0);
  assert.equal(squareOfSum(5) - sumOfSquares(5), 170);
  assert.equal(squareOfSum(10) - sumOfSquares(10), 2640);
});
