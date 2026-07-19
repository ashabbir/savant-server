export const ERROR_CODES = new Set([
  'NOT_INDEXED', 'PATH_REFUSED', 'BUSY', 'TIMEOUT', 'UNSUPPORTED',
  'ENGINE_UNAVAILABLE', 'INTERNAL'
]);

export class BridgeError extends Error {
  constructor(code, message, retryable = false) {
    super(message);
    this.code = ERROR_CODES.has(code) ? code : 'INTERNAL';
    this.retryable = retryable;
  }
}

export function responseError(id, error) {
  return { id, ok: false, error: { code: error.code || 'INTERNAL', message: error.message || 'bridge error', retryable: Boolean(error.retryable) } };
}
