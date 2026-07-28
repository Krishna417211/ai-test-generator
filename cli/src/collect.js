/**
 * collect.js — Decide which files in a directory are "the project".
 *
 * The web app cannot answer this question: it receives a ZIP somebody made by
 * hand, so it guesses with a skip-list (backend/services/file_extractor.py's
 * PUSH_SKIP_DIRECTORIES). A CLI runs *inside* the project and can do better —
 * `git ls-files -co --exclude-standard` is the authoritative answer to "what
 * belongs to this project", because it is git's own, honouring .gitignore,
 * nested .gitignores, .git/info/exclude and the global excludes file.
 *
 * That is a real improvement rather than a convenience: the skip-list has to
 * enumerate build directories it has heard of, and misses `.turbo`, `.astro`,
 * `out/`, `__snapshots__` and whatever this year's tool named its cache. Your
 * .gitignore already lists them, because your own commits depend on it.
 *
 * When the directory is not a git repo we fall back to walking it with the same
 * skip-list the server uses, and say so — a silent fallback would quietly upload
 * node_modules and the user would only notice from the file count.
 */

import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

// Kept in step with file_extractor.PUSH_SKIP_DIRECTORIES. Only consulted on the
// non-git path; inside a repo, .gitignore is the authority.
const SKIP_DIRECTORIES = new Set([
  "node_modules", ".git", "dist", "build", "__pycache__",
  ".pytest_cache", "coverage", ".nyc_output", ".next", ".nuxt",
  ".svelte-kit", "venv", ".venv", "vendor", ".terraform",
  ".idea", ".vscode", ".cache", ".parcel-cache", "target",
]);

// Mirrors PUSH_SKIP_EXTENSIONS: the server drops these anyway (its blobs must
// be valid UTF-8 text), so uploading them wastes the user's bandwidth.
const SKIP_EXTENSIONS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tiff",
  ".woff", ".woff2", ".ttf", ".eot", ".otf",
  ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
  ".mp4", ".mov", ".avi", ".mp3", ".wav",
  ".db", ".sqlite", ".sqlite3",
  ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe", ".bin",
]);

const MAX_FILE_BYTES = 1_000_000;

function gitTrackedFiles(root) {
  try {
    // -c tracked, -o untracked, --exclude-standard applies every ignore source.
    // -z because filenames may contain newlines, and a newline-split list turns
    // one such file into two nonexistent ones.
    const out = execFileSync(
      "git",
      ["-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
      { encoding: "buffer", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] }
    );
    const names = out.toString("utf8").split("\0").filter(Boolean);
    return names;
  } catch {
    return null; // not a repo, or no git binary
  }
}

function walk(root, rel = "", out = []) {
  for (const dirent of fs.readdirSync(path.join(root, rel), { withFileTypes: true })) {
    const relPath = rel ? `${rel}/${dirent.name}` : dirent.name;
    if (dirent.isDirectory()) {
      if (SKIP_DIRECTORIES.has(dirent.name.toLowerCase())) continue;
      walk(root, relPath, out);
    } else if (dirent.isFile()) {
      out.push(relPath);
    }
    // Symlinks are deliberately skipped: following one can leave the project
    // directory entirely, which is the same class of mistake as a zip-slip.
  }
  return out;
}

/**
 * Read the project into upload-ready entries.
 *
 * @returns {{entries: Array<{path,content}>, source: "git"|"walk", skipped: object}}
 */
export function collect(root) {
  const fromGit = gitTrackedFiles(root);
  const names = fromGit ?? walk(root);
  const source = fromGit ? "git" : "walk";

  const entries = [];
  const skipped = { binary: 0, large: 0, unreadable: 0, directories: 0 };

  for (const name of names) {
    const abs = path.join(root, name);
    if (SKIP_EXTENSIONS.has(path.extname(name).toLowerCase())) {
      skipped.binary++;
      continue;
    }
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      skipped.unreadable++;
      continue;
    }
    // `git ls-files` lists submodule roots, which stat as directories.
    if (!stat.isFile()) {
      skipped.directories++;
      continue;
    }
    if (stat.size > MAX_FILE_BYTES) {
      skipped.large++;
      continue;
    }
    try {
      // The server decodes with errors="replace", so match that here rather
      // than refusing a file over one bad byte.
      entries.push({ path: name.split(path.sep).join("/"), content: fs.readFileSync(abs, "utf8") });
    } catch {
      skipped.unreadable++;
    }
  }

  return { entries, source, skipped };
}

export const _internals = { SKIP_DIRECTORIES, SKIP_EXTENSIONS, MAX_FILE_BYTES, walk };
