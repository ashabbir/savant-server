import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { Operations } from '../src/operations.js';

test('registration permits canonical descendants and refuses path escapes', () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-base-'));
  const repo = path.join(base, 'repo'); fs.mkdirSync(repo);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-outside-'));
  const operations = new Operations({ baseRoots: [base] });
  assert.deepEqual(operations.register('repo', repo), { repo_id: 'repo' });
  assert.throws(() => operations.register('outside', outside), error => error.code === 'PATH_REFUSED');
});

test('registration refuses a symlink escape', () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-base-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-outside-'));
  const link = path.join(base, 'escape'); fs.symlinkSync(outside, link);
  const operations = new Operations({ baseRoots: [base] });
  assert.throws(() => operations.register('escape', link), error => error.code === 'PATH_REFUSED');
});

test('ping reports the exact pinned engine and protocol versions', async () => {
  const operations = new Operations({ baseRoots: [os.tmpdir()] });
  assert.deepEqual(await operations.execute({ op: 'ping' }), { engine_version: '1.4.1', protocol_version: 1 });
});

test('list_symbols is deterministic and cursor bounded', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-list-'));
  fs.writeFileSync(path.join(repo, 'sample.py'), 'def zebra():\n    return 1\n\ndef alpha():\n    return 2\n');
  const operations = new Operations({ baseRoots: [os.tmpdir()], timeoutMs: 10000 });
  operations.register('repo', repo);
  await operations.execute({ id: 'index-list', op: 'ensure_index', repo_id: 'repo', params: {} });
  const first = await operations.execute({ id: 'list-1', op: 'list_symbols', repo_id: 'repo', params: { limit: 1 } });
  assert.equal(first.items.length, 1);
  assert.equal(first.incomplete, true);
  assert.ok(first.next_cursor);
  const second = await operations.execute({ id: 'list-2', op: 'list_symbols', repo_id: 'repo', params: { limit: 1, cursor: first.next_cursor } });
  assert.equal(second.items.length, 1);
  assert.notEqual(second.items[0].id, first.items[0].id);
  operations.close();
});

test('a timed out disposable read releases capacity without blocking health traffic', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-read-'));
  fs.writeFileSync(path.join(repo, 'sample.py'), 'def hello():\n    return 1\n');
  const setup = new Operations({ baseRoots: [os.tmpdir()], timeoutMs: 10000 });
  setup.register('repo', repo);
  await setup.execute({ id: 'index', op: 'ensure_index', repo_id: 'repo', params: {} });
  setup.close();
  const operations = new Operations({ baseRoots: [os.tmpdir()], timeoutMs: 25, cancelGraceMs: 5 });
  operations.register('repo', repo);
  await assert.rejects(operations.execute({ id: 'slow', op: 'search_symbols', repo_id: 'repo', params: { query: 'hello', __fault_delay_ms: 500 } }), error => error.code === 'TIMEOUT');
  assert.equal(operations.active.size, 0);
  assert.equal((await operations.execute({ id: 'ping', op: 'ping', params: {} })).protocol_version, 1);
  operations.close();
});

test('an interrupted writer remains stale until a successful recovery sync', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-write-'));
  fs.writeFileSync(path.join(repo, 'sample.py'), 'def hello():\n    return 1\n');
  const operations = new Operations({ baseRoots: [os.tmpdir()], timeoutMs: 10000, cancelGraceMs: 5 });
  operations.register('repo', repo);
  await operations.execute({ id: 'initial', op: 'ensure_index', repo_id: 'repo', params: {} });
  operations.timeoutMs = 25;
  await assert.rejects(operations.execute({ id: 'interrupted', op: 'ensure_index', repo_id: 'repo', params: { __fault_delay_ms: 500 } }), error => error.code === 'TIMEOUT');
  const stale = await operations.execute({ id: 'health-1', op: 'health', repo_id: 'repo', params: {} });
  assert.equal(stale.freshness, 'stale');
  assert.ok(stale.warnings.some(value => value.includes('interrupted write')));
  operations.timeoutMs = 10000;
  await operations.execute({ id: 'recovery', op: 'ensure_index', repo_id: 'repo', params: {} });
  const recovered = await operations.execute({ id: 'health-2', op: 'health', repo_id: 'repo', params: {} });
  assert.ok(!recovered.warnings.some(value => value.includes('interrupted write')));
  operations.close();
});

test('explicit cancellation terminates and replaces the assigned worker', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'savant-cg-cancel-'));
  fs.writeFileSync(path.join(repo, 'sample.py'), 'def hello():\n    return 1\n');
  const operations = new Operations({ baseRoots: [os.tmpdir()], timeoutMs: 10000, cancelGraceMs: 5 });
  operations.register('repo', repo);
  await operations.execute({ id: 'initial', op: 'ensure_index', repo_id: 'repo', params: {} });
  const pending = operations.execute({ id: 'cancel-me', op: 'search_symbols', repo_id: 'repo', params: { query: 'hello', __fault_delay_ms: 500 } });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(await operations.execute({ id: 'cancel', op: 'cancel', params: { request_id: 'cancel-me' } }), { cancelled: true });
  await assert.rejects(pending, error => error.code === 'TIMEOUT' && error.message.includes('cancelled'));
  assert.equal(operations.active.size, 0);
  operations.close();
});

test('successful writes enable watching and settle once', async () => {
  const operations = new Operations({ baseRoots: [os.tmpdir()] });
  operations.graph = async () => ({ watch: () => true });
  const settled = [];
  const state = {
    repoId: 'repo', write: true,
    worker: { terminate() {} },
    finish(callback, value) { settled.push(value); callback(value); },
    resolve() {}
  };
  const message = { ok: true, result: {} };

  await operations.handleWorkerMessage({ params: {} }, state, message);

  assert.equal(message.result.watching, true);
  assert.equal(settled.length, 1);
});

test('successful writes tolerate watcher startup failures', async () => {
  const operations = new Operations({ baseRoots: [os.tmpdir()] });
  operations.graph = async () => { throw new Error('watch failed'); };
  const state = {
    repoId: 'repo', write: true,
    worker: { terminate() {} },
    finish(callback, value) { callback(value); },
    resolve() {}
  };
  const message = { ok: true, result: {} };

  await operations.handleWorkerMessage({ params: {} }, state, message);

  assert.equal(message.result.watching, false);
});
