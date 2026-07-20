import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';

import { guardSocket } from '../src/socket.js';

test('guardSocket absorbs expected disconnect errors', () => {
  const socket = new EventEmitter();
  const errors = [];
  guardSocket(socket, { error: value => errors.push(value) });

  assert.doesNotThrow(() => socket.emit('error', Object.assign(new Error('closed'), { code: 'EPIPE' })));
  assert.doesNotThrow(() => socket.emit('error', Object.assign(new Error('reset'), { code: 'ECONNRESET' })));
  assert.deepEqual(errors, []);
});

test('guardSocket reports unexpected socket errors without crashing', () => {
  const socket = new EventEmitter();
  const errors = [];
  guardSocket(socket, { error: value => errors.push(value) });

  assert.doesNotThrow(() => socket.emit('error', Object.assign(new Error('bad socket'), { code: 'EBADF' })));
  assert.equal(errors.length, 1);
  assert.match(errors[0], /EBADF/);
});
