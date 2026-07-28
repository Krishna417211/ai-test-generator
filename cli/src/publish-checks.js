/**
 * publish-checks.js — Warnings about the upload that only the client can see.
 */

// Kept in step with COMMON_ROOT_DIRS in backend/services/file_extractor.py.
const SERVER_KEEPS_THESE_ROOTS = new Set([
  "src", "app", "lib", "source", "dist", "build", "public",
  "components", "pages", "views", "assets", "static", "tests", "test",
]);

/**
 * Does the server's ZIP unwrapping apply to this file set?
 *
 * `extract_zip()` strips a single shared top-level directory, because a ZIP
 * downloaded from GitHub wraps everything in `repo-main/` and nobody wants that
 * folder in their repository. Perfectly right for that case — and wrong for a
 * CLI run at the root of a project whose files all happen to live under one
 * directory (a monorepo where everything is under `frontend/`, say). The server
 * would strip `frontend/` and the published repo would silently have a different
 * shape from the one on disk.
 *
 * The server can't distinguish the two cases; it never saw the directory the
 * archive was built from. The client can, so it warns here rather than letting
 * someone discover it after the push.
 *
 * @returns {string|null} the directory that would be stripped, or null.
 */
export function strippedRootDir(paths) {
  if (paths.length === 0) return null;
  const first = paths[0];
  if (!first.includes("/")) return null;
  const candidate = first.slice(0, first.indexOf("/"));
  if (SERVER_KEEPS_THESE_ROOTS.has(candidate.toLowerCase())) return null;
  const prefix = `${candidate}/`;
  return paths.every((p) => p.startsWith(prefix)) ? candidate : null;
}
