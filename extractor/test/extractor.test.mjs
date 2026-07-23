import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { extractHtml, extractUrl, meetsQualityGate } from '../src/extractor.mjs';

const fixtureUrl = new URL('./fixtures/article.html', import.meta.url);
const fixtureHtml = await readFile(fixtureUrl, 'utf8');

test('extractHtml uses Defuddle to produce article metadata and Markdown', async () => {
  const result = await extractHtml(fixtureHtml, 'https://Example.com/articles/secure-clipping?utm_source=test#intro');

  assert.equal(result.title, 'Secure Web Clipping Without Surprises');
  assert.equal(result.author, 'Ada Example');
  assert.equal(result.published, '2026-07-20');
  assert.equal(result.description, 'A practical guide to clipping articles without trusting the network.');
  assert.equal(result.site, 'Example Security Journal');
  assert.equal(result.canonicalUrl, 'https://example.com/articles/secure-clipping');
  assert.deepEqual(result.keywords, ['security', 'clipping', 'obsidian']);
  assert.match(result.markdown, /Secure defaults reduce the attack surface/);
  assert.match(result.markdown, /\[network policy\]\(https:\/\/example\.com\/guides\/network-policy\)/);
  assert.ok(result.markdown.length >= 200);
  assert.ok(result.wordCount >= 40);
});

test('quality gate requires a meaningful title and at least 200 Markdown characters', () => {
  assert.equal(meetsQualityGate({ title: 'A useful article', markdown: 'x'.repeat(200) }), true);
  assert.equal(meetsQualityGate({ title: 'Untitled', markdown: 'x'.repeat(300) }), false);
  assert.equal(meetsQualityGate({ title: 'Useful', markdown: 'x'.repeat(199) }), false);
  assert.equal(meetsQualityGate({ title: '  ', markdown: 'x'.repeat(300) }), false);
});

test('extractUrl returns static method when the static extraction passes quality', async () => {
  const result = await extractUrl('https://example.com/article', {
    allowBrowser: false,
    fetchHtml: async () => ({ html: fixtureHtml, finalUrl: 'https://example.com/articles/secure-clipping' }),
  });

  assert.equal(result.method, 'static');
  assert.equal(result.title, 'Secure Web Clipping Without Surprises');
});

test('extractUrl fails closed when static quality is insufficient and browser use is disabled', async () => {
  await assert.rejects(
    extractUrl('https://example.com/short', {
      allowBrowser: false,
      fetchHtml: async () => ({
        html: '<html><head><title>Short</title></head><body><main><p>Too short.</p></main></body></html>',
        finalUrl: 'https://example.com/short',
      }),
    }),
    error => error.code === 'QUALITY_GATE',
  );
});
