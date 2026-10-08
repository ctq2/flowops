/**
 * Static integrity check for the zero-build frontend.
 *
 * There is no bundler, so nothing verifies that an `import './x.js'` actually
 * resolves — a typo ships silently and the browser shows a blank page. This
 * script walks every module, resolves each relative specifier against the file
 * system, and fails on anything that would 404 at runtime.
 *
 * It also checks that every named import is actually exported by the target,
 * which catches the other silent white-screen: `import { x } from './y.js'`
 * where `y.js` never exported `x`.
 *
 * Usage: node scripts/check-imports.js [rootDir]
 */

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

// `import.meta.url` percent-encodes non-ASCII paths (this repository lives under
// a 中文 directory), so it must be converted rather than string-sliced.
const ROOT = resolve(process.argv[2] ?? fileURLToPath(new URL('../src', import.meta.url)));

const IMPORT_RE = /(?:^|\n)\s*import\s+(?:([\s\S]*?)\s+from\s+)?['"]([^'"]+)['"]/g;
const EXPORT_RE = /export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)/g;
const EXPORT_LIST_RE = /export\s*\{([^}]*)\}/g;

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.js')) out.push(full);
  }
  return out;
}

function parseNamedImports(clause) {
  const match = clause.match(/\{([\s\S]*)\}/);
  if (!match) return [];
  return match[1]
    .split(',')
    .map((item) => item.trim().split(/\s+as\s+/)[0].trim())
    .filter(Boolean);
}

function exportedNames(source) {
  const names = new Set();
  for (const match of source.matchAll(EXPORT_RE)) names.add(match[1]);
  for (const match of source.matchAll(EXPORT_LIST_RE)) {
    for (const item of match[1].split(',')) {
      const name = item.trim().split(/\s+as\s+/).pop().trim();
      if (name) names.add(name);
    }
  }
  if (/export\s+default/.test(source)) names.add('default');
  return names;
}

const files = walk(ROOT);
const problems = [];
const edges = [];
const exportCache = new Map();

for (const file of files) {
  const source = readFileSync(file, 'utf8');
  exportCache.set(file, exportedNames(source));

  for (const match of source.matchAll(IMPORT_RE)) {
    const [, clause = '', specifier] = match;
    if (!specifier.startsWith('.')) continue; // bare specifiers cannot be checked

    const target = resolve(dirname(file), specifier);
    const line = source.slice(0, match.index).split('\n').length;

    if (!existsSync(target)) {
      problems.push({
        file: relative(ROOT, file),
        line,
        specifier,
        reason: `target does not exist (looked for ${relative(ROOT, target)})`,
      });
      continue;
    }

    edges.push([relative(ROOT, file), relative(ROOT, target)]);

    const wanted = parseNamedImports(clause);
    if (!wanted.length) continue;
    const available = exportCache.get(target) ?? exportedNames(readFileSync(target, 'utf8'));
    exportCache.set(target, available);
    for (const name of wanted) {
      if (!available.has(name)) {
        problems.push({
          file: relative(ROOT, file),
          line,
          specifier,
          reason: `"${name}" is not exported by ${relative(ROOT, target)}`,
        });
      }
    }
  }
}

console.log(`checked ${files.length} modules, ${edges.length} relative imports`);
if (problems.length) {
  console.error(`\n${problems.length} broken import(s) — these are blank-page bugs in the browser:\n`);
  for (const item of problems) {
    console.error(`  ${item.file}:${item.line}  "${item.specifier}"  -> ${item.reason}`);
  }
  process.exit(1);
}
console.log('all relative imports resolve and every named import exists');
