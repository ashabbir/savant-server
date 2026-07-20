import fs from 'node:fs';
import net from 'node:net';
import { Operations } from './operations.js';
import { BridgeError, responseError } from './protocol.js';
import { guardSocket } from './socket.js';

const socketPath = process.env.SAVANT_CODEGRAPH_SOCKET || '/run/savant/codegraph.sock';
const baseRoots = (process.env.SAVANT_CODEGRAPH_BASE_ROOTS || process.env.BASE_CODE_DIR || '/base-code').split(':').filter(Boolean);
const maxRequest = Number(process.env.SAVANT_CODEGRAPH_MAX_REQUEST_BYTES || 1024 * 1024);
const timeoutMs = Number(process.env.SAVANT_CODEGRAPH_OPERATION_TIMEOUT_MS || 15000);
const writeTimeoutMs = Number(process.env.SAVANT_CODEGRAPH_WRITE_TIMEOUT_MS || 600000);
const operations = new Operations({ baseRoots, maxRepositories: Number(process.env.SAVANT_CODEGRAPH_MAX_REPOS || 8), timeoutMs, writeTimeoutMs });

function withTimeout(promise, timeout) {
  let timer;
  const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new BridgeError('TIMEOUT', 'operation timed out', true)), timeout + 1500); });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

fs.mkdirSync(new URL('.', `file://${socketPath}`).pathname, { recursive: true });
try { fs.unlinkSync(socketPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }

const server = net.createServer(socket => {
  guardSocket(socket);
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    if (Buffer.byteLength(buffer) > maxRequest) { socket.destroy(new Error('request too large')); return; }
    let newline;
    while ((newline = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
      if (!line) continue;
      let request;
      try { request = JSON.parse(line); } catch { socket.write(`${JSON.stringify(responseError(null, new BridgeError('INTERNAL', 'invalid JSON')))}\n`); continue; }
      const requestTimeout = request.op === 'ensure_index' ? writeTimeoutMs : timeoutMs;
      withTimeout(operations.execute(request), requestTimeout).then(result => socket.write(`${JSON.stringify({ id: request.id, ok: true, result })}\n`)).catch(error => socket.write(`${JSON.stringify(responseError(request.id, error))}\n`));
    }
  });
});
server.listen(socketPath, () => fs.chmodSync(socketPath, 0o600));
const shutdown = () => { server.close(); operations.close(); try { fs.unlinkSync(socketPath); } catch {} };
process.on('SIGTERM', shutdown); process.on('SIGINT', shutdown);
