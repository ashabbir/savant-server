import fs from 'node:fs';
import path from 'node:path';
import { BridgeError } from './protocol.js';

export function canonicalizeBaseRoots(baseRoots) {
  return baseRoots.map(root => fs.realpathSync(root));
}

export function registerRepo(baseRoots, registrations, repoId, root) {
  if (!repoId || !root) throw new BridgeError('PATH_REFUSED', 'repo_id and root are required');
  let real;
  try {
    real = fs.realpathSync(root);
  } catch {
    throw new BridgeError('PATH_REFUSED', 'repository root does not exist');
  }
  const allowed = baseRoots.some(base => real === base || real.startsWith(`${base}${path.sep}`));
  if (!allowed) throw new BridgeError('PATH_REFUSED', 'repository root is outside configured base roots');
  registrations.set(repoId, real);
  return { repo_id: repoId };
}

export function getRepoRoot(registrations, repoId) {
  const root = registrations.get(repoId);
  if (!root) throw new BridgeError('PATH_REFUSED', 'repository is not registered');
  return root;
}
