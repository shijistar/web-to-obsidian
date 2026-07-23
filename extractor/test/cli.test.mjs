import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const cliPath = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));

test('CLI emits exactly one safe JSON error for a malformed URL', () => {
  const result = spawnSync(process.execPath, [cliPath, 'not a URL'], { encoding: 'utf8' });

  assert.notEqual(result.status, 0);
  assert.equal(result.stderr, '');
  assert.equal(result.stdout.trim().split('\n').length, 1);
  const output = JSON.parse(result.stdout);
  assert.deepEqual(Object.keys(output).sort(), ['code', 'error', 'ok']);
  assert.equal(output.ok, false);
  assert.equal(output.code, 'INVALID_URL');
  assert.doesNotMatch(output.error, /not a URL|stack|at file:/i);
});
