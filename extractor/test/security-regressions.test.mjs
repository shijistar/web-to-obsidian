import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  buildSecureRouteHandler,
  extractHtml,
  extractUrl,
} from '../src/extractor.mjs';

const fixtureHtml = await readFile(new URL('./fixtures/article.html', import.meta.url), 'utf8');

test('successful extraction protocol includes the normalized final URL', async () => {
  const result = await extractUrl('https://example.com/input?utm_source=test', {
    allowBrowser: false,
    fetchHtml: async () => ({
      html: fixtureHtml,
      finalUrl: 'https://example.com/final?utm_campaign=test&kept=yes#part',
    }),
  });

  assert.equal(result.url, 'https://example.com/final?kept=yes');
});

test('Defuddle diagnostics are suppressed for untrusted malformed metadata URLs', async () => {
  const html = fixtureHtml.replace(
    '</head>',
    '<meta property="og:image" content="//cdn.example/image.svg"></head>',
  );
  const errorCalls = [];
  const warnCalls = [];
  const originalError = console.error;
  const originalWarn = console.warn;
  console.error = (...args) => errorCalls.push(args);
  console.warn = (...args) => warnCalls.push(args);
  try {
    await extractHtml(html, 'https://example.com/article');
  } finally {
    console.error = originalError;
    console.warn = originalWarn;
  }
  assert.deepEqual(errorCalls, []);
  assert.deepEqual(warnCalls, []);
});

test('secure browser route fulfills HTTP resources through the pinned fetcher', async () => {
  let continued = false;
  let fulfilled;
  const route = {
    request: () => ({ url: () => 'https://cdn.example/app.js', method: () => 'GET' }),
    continue: async () => { continued = true; },
    abort: async () => { throw new Error('unexpected abort'); },
    fulfill: async value => { fulfilled = value; },
  };
  const secureFetcher = async input => {
    assert.equal(input, 'https://cdn.example/app.js');
    return {
      statusCode: 200,
      headers: { 'content-type': 'text/javascript', 'content-encoding': 'gzip' },
      body: Buffer.from('window.loaded = true;'),
    };
  };

  const handler = buildSecureRouteHandler({ secureFetcher, maxRequests: 5 });
  await handler(route);

  assert.equal(continued, false);
  assert.equal(fulfilled.status, 200);
  assert.equal(fulfilled.headers['content-type'], 'text/javascript');
  assert.equal(fulfilled.headers['content-encoding'], undefined);
  assert.equal(fulfilled.body.toString(), 'window.loaded = true;');
});
