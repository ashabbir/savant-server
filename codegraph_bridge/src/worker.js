import { Worker } from 'node:worker_threads';
import { BridgeError } from './protocol.js';

export function createWorkerState(operations, request, write) {
  const worker = new Worker(new URL('./task-worker.js', import.meta.url), {
    workerData: {
      operation: request.op,
      root: operations.root(request.repo_id),
      repoId: request.repo_id,
      params: request.params || {}
    }
  });
  const state = { worker, write, repoId: request.repo_id, settled: false };
  operations.active.set(request.id, state);
  return state;
}

export function finishTask(operations, requestId, state, callback, value) {
  if (state.settled) return;
  state.settled = true;
  clearTimeout(state.timer);
  operations.active.delete(requestId);
  if (state.write) operations.writes.delete(state.repoId);
  callback(value);
}

export async function terminateWorkerWithGrace(operations, state) {
  state.terminating = true;
  state.worker.postMessage('cancel');
  await new Promise(done => setTimeout(done, operations.cancelGraceMs));
  await state.worker.terminate();
}

export async function handleTaskTimeout(operations, request, state) {
  await terminateWorkerWithGrace(operations, state);
  if (state.write) operations.recoveryRequired.add(state.repoId);
  state.finish(state.reject, new BridgeError('TIMEOUT', 'operation timed out and worker was replaced', true));
}

export async function startWatching(operations, repoId) {
  try {
    const watcher = await operations.graph(repoId);
    return watcher.watch();
  } catch {
    return false;
  }
}

export async function completeSuccessfulTask(operations, request, state, result) {
  if (!state.write) return result;
  operations.recoveryRequired.delete(state.repoId);
  operations.successfullySynced.add(state.repoId);
  if (request.params?.watch !== false) {
    result.watching = await startWatching(operations, state.repoId);
  }
  return result;
}

export async function handleWorkerMessage(operations, request, state, message) {
  state.completing = true;
  state.worker.terminate();
  if (message.ok) {
    const result = await completeSuccessfulTask(operations, request, state, message.result);
    state.finish(state.resolve, result);
    return;
  }
  state.finish(state.reject, new BridgeError(message.error?.code || 'INTERNAL', message.error?.message || 'worker error'));
}

export function runTask(operations, request, write = false) {
  const state = createWorkerState(operations, request, write);
  const timeout = write ? (operations.writeTimeoutMs ?? operations.timeoutMs) : operations.timeoutMs;
  return new Promise((resolve, reject) => {
    state.resolve = resolve;
    state.reject = reject;
    state.finish = (callback, value) => finishTask(operations, request.id, state, callback, value);

    state.timer = setTimeout(() => handleTaskTimeout(operations, request, state), timeout);

    state.worker.once('message', message => handleWorkerMessage(operations, request, state, message));
    
    state.worker.once('error', error => {
      if (write) operations.recoveryRequired.add(request.repo_id);
      state.finish(reject, new BridgeError('INTERNAL', error.message));
    });

    state.worker.once('exit', code => {
      if (state.settled || state.terminating || state.completing || code === 0) return;
      if (write) operations.recoveryRequired.add(request.repo_id);
      state.finish(reject, new BridgeError('ENGINE_UNAVAILABLE', 'operation worker exited', true));
    });
  });
}

export async function cancelTask(operations, requestId) {
  const state = operations.active.get(requestId);
  if (!state) return { cancelled: false };
  await terminateWorkerWithGrace(operations, state);
  if (state.write) operations.recoveryRequired.add(state.repoId);
  state.finish(state.reject, new BridgeError('TIMEOUT', 'operation cancelled and worker was replaced', true));
  return { cancelled: true };
}
