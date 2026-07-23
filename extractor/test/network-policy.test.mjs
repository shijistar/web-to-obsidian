import assert from 'node:assert/strict';
import test from 'node:test';

import {
  MAX_BODY_BYTES,
  PolicyError,
  fetchHtml,
  isBlockedIp,
  normalizeUrl,
  resolveAndValidateUrl,
} from '../src/network-policy.mjs';

test('normalizeUrl strips fragments and tracking parameters and sorts the query', () => {
  assert.equal(
    normalizeUrl('HTTPS://Example.COM/story?utm_source=newsletter&b=2&a=3&FBCLID=secret&a=1&gclid=x&share_token=y#comments').href,
    'https://example.com/story?a=1&a=3&b=2',
  );
});

test('normalizeUrl accepts only HTTP(S), rejects credentials, and rejects non-default ports', () => {
  assert.throws(() => normalizeUrl('file:///etc/passwd'), error => error instanceof PolicyError && error.code === 'UNSUPPORTED_SCHEME');
  assert.throws(() => normalizeUrl('https://user:password@example.com/'), error => error instanceof PolicyError && error.code === 'URL_CREDENTIALS');
  assert.throws(() => normalizeUrl('https://example.com:444/'), error => error instanceof PolicyError && error.code === 'NON_DEFAULT_PORT');
  assert.equal(normalizeUrl('http://example.com:80/a').href, 'http://example.com/a');
  assert.equal(normalizeUrl('https://example.com:443/a').href, 'https://example.com/a');
});

test('isBlockedIp rejects private and special-use IPv4 ranges', () => {
  const blocked = [
    '0.0.0.0', '10.0.0.1', '100.64.0.1', '127.0.0.1', '169.254.1.1',
    '172.16.0.1', '192.0.0.1', '192.0.2.1', '192.168.1.1', '198.18.0.1',
    '198.51.100.1', '203.0.113.1', '224.0.0.1', '240.0.0.1', '255.255.255.255',
  ];
  for (const address of blocked) assert.equal(isBlockedIp(address), true, address);
  for (const address of ['8.8.8.8', '93.184.216.34']) assert.equal(isBlockedIp(address), false, address);
});

test('isBlockedIp rejects private and special-use IPv6 ranges, including mapped IPv4', () => {
  const blocked = [
    '::', '::1', 'fc00::1', 'fd12:3456::1', 'fe80::1', 'ff02::1',
    '2001:db8::1', '::ffff:127.0.0.1', '::ffff:192.168.1.1', '64:ff9b:1::1',
  ];
  for (const address of blocked) assert.equal(isBlockedIp(address), true, address);
  for (const address of ['2606:4700:4700::1111', '2001:4860:4860::8888']) assert.equal(isBlockedIp(address), false, address);
});

test('resolveAndValidateUrl rejects a hostname if any DNS answer is blocked', async () => {
  let options;
  const resolver = async (hostname, suppliedOptions) => {
    assert.equal(hostname, 'example.com');
    options = suppliedOptions;
    return [
      { address: '93.184.216.34', family: 4 },
      { address: '127.0.0.1', family: 4 },
    ];
  };

  await assert.rejects(
    resolveAndValidateUrl('https://example.com/', { resolver }),
    error => error instanceof PolicyError && error.code === 'BLOCKED_ADDRESS',
  );
  assert.deepEqual(options, { all: true, verbatim: true });
});

test('fetchHtml validates every redirect before issuing the next request', async () => {
  const requested = [];
  const resolver = async hostname => hostname === 'public.example'
    ? [{ address: '93.184.216.34', family: 4 }]
    : [{ address: '10.0.0.8', family: 4 }];
  const requestImpl = async ({ url }) => {
    requested.push(url.href);
    return {
      statusCode: 302,
      headers: { location: 'http://internal.example/admin' },
      body: Buffer.alloc(0),
    };
  };

  await assert.rejects(
    fetchHtml('https://public.example/start', { resolver, requestImpl }),
    error => error instanceof PolicyError && error.code === 'BLOCKED_ADDRESS',
  );
  assert.deepEqual(requested, ['https://public.example/start']);
});

test('fetchHtml enforces the redirect limit', async () => {
  const resolver = async () => [{ address: '93.184.216.34', family: 4 }];
  const requestImpl = async ({ url }) => ({
    statusCode: 302,
    headers: { location: `/again?hop=${Number(url.searchParams.get('hop') || 0) + 1}` },
    body: Buffer.alloc(0),
  });

  await assert.rejects(
    fetchHtml('https://example.com/again', { resolver, requestImpl, maxRedirects: 1 }),
    error => error instanceof PolicyError && error.code === 'TOO_MANY_REDIRECTS',
  );
});

test('fetchHtml accepts only HTML and XHTML response content types', async () => {
  const resolver = async () => [{ address: '93.184.216.34', family: 4 }];
  const requestImpl = async () => ({
    statusCode: 200,
    headers: { 'content-type': 'application/json' },
    body: Buffer.from('{}'),
  });

  await assert.rejects(
    fetchHtml('https://example.com/data', { resolver, requestImpl }),
    error => error instanceof PolicyError && error.code === 'UNSUPPORTED_CONTENT_TYPE',
  );
});

test('fetchHtml rejects bodies over the configured limit', async () => {
  const resolver = async () => [{ address: '93.184.216.34', family: 4 }];
  const requestImpl = async () => ({
    statusCode: 200,
    headers: { 'content-type': 'text/html; charset=utf-8' },
    body: Buffer.alloc(11),
  });

  await assert.rejects(
    fetchHtml('https://example.com/large', { resolver, requestImpl, maxBytes: 10 }),
    error => error instanceof PolicyError && error.code === 'BODY_TOO_LARGE',
  );
  assert.equal(MAX_BODY_BYTES, 8 * 1024 * 1024);
});

test('fetchHtml returns HTML and the normalized final URL', async () => {
  const resolver = async () => [{ address: '93.184.216.34', family: 4 }];
  const requestImpl = async ({ address, family }) => {
    assert.equal(address, '93.184.216.34');
    assert.equal(family, 4);
    return {
      statusCode: 200,
      headers: { 'content-type': 'application/xhtml+xml' },
      body: Buffer.from('<html><body>ok</body></html>'),
    };
  };

  assert.deepEqual(
    await fetchHtml('https://EXAMPLE.com/?utm_medium=email#part', { resolver, requestImpl }),
    { html: '<html><body>ok</body></html>', finalUrl: 'https://example.com/' },
  );
});
