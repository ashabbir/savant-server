const EXPECTED_DISCONNECT_ERRORS = new Set(['EPIPE', 'ECONNRESET']);

export function guardSocket(socket, logger = console) {
  socket.on('error', error => {
    if (EXPECTED_DISCONNECT_ERRORS.has(error?.code)) return;
    logger.error(`CodeGraph bridge socket error${error?.code ? ` (${error.code})` : ''}: ${error?.message || error}`);
  });
  return socket;
}
