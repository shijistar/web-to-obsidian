import { promises as dns } from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { Transform, Writable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createBrotliDecompress, createGunzip, createInflate } from 'node:zlib';

export const MAX_BODY_BYTES = 8 * 1024 * 1024;
export const DEFAULT_TIMEOUT_MS = 20_000;
export const DEFAULT_MAX_REDIRECTS = 5;

const USER_AGENT = 'Web-to-Obsidian/0.1 (+local article extractor)';
const HTML_CONTENT_TYPES = new Set(['text/html', 'application/xhtml+xml']);
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

export class PolicyError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'PolicyError';
    this.code = code;
  }
}

function ipv4Number(address) {
  if (!net.isIPv4(address)) return null;
  return address.split('.').reduce((value, part) => (value * 256) + Number(part), 0) >>> 0;
}

function ipv4InCidr(value, base, prefix) {
  const shift = 32 - prefix;
  return (value >>> shift) === (base >>> shift);
}

const BLOCKED_IPV4_RANGES = [
  ['0.0.0.0', 8],
  ['10.0.0.0', 8],
  ['100.64.0.0', 10],
  ['127.0.0.0', 8],
  ['169.254.0.0', 16],
  ['172.16.0.0', 12],
  ['192.0.0.0', 24],
  ['192.0.2.0', 24],
  ['192.88.99.0', 24],
  ['192.168.0.0', 16],
  ['198.18.0.0', 15],
  ['198.51.100.0', 24],
  ['203.0.113.0', 24],
  ['224.0.0.0', 4],
  ['240.0.0.0', 4],
].map(([base, prefix]) => [ipv4Number(base), prefix]);

function parseIpv6(address) {
  if (typeof address !== 'string' || address.includes('%') || !net.isIPv6(address)) return null;

  let source = address.toLowerCase();
  const dottedIndex = source.lastIndexOf(':');
  if (source.includes('.')) {
    const dotted = source.slice(dottedIndex + 1);
    const value = ipv4Number(dotted);
    if (value === null) return null;
    source = `${source.slice(0, dottedIndex)}:${(value >>> 16).toString(16)}:${(value & 0xffff).toString(16)}`;
  }

  const halves = source.split('::');
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(':') : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const missing = 8 - left.length - right.length;
  if ((halves.length === 1 && missing !== 0) || missing < 0) return null;

  const words = [
    ...left,
    ...Array(halves.length === 2 ? missing : 0).fill('0'),
    ...right,
  ].map(part => Number.parseInt(part, 16));
  if (words.length !== 8 || words.some(word => !Number.isInteger(word) || word < 0 || word > 0xffff)) return null;

  return words.reduce((value, word) => (value << 16n) | BigInt(word), 0n);
}

function ipv6InCidr(value, base, prefix) {
  const shift = 128n - BigInt(prefix);
  return (value >> shift) === (base >> shift);
}

const BLOCKED_IPV6_RANGES = [
  ['::', 96],
  ['::', 128],
  ['::1', 128],
  ['64:ff9b::', 96],
  ['64:ff9b:1::', 48],
  ['100::', 64],
  ['2001::', 23],
  ['2001:2::', 48],
  ['2001:10::', 28],
  ['2001:20::', 28],
  ['2001:db8::', 32],
  ['2002::', 16],
  ['3fff::', 20],
  ['5f00::', 16],
  ['fc00::', 7],
  ['fe80::', 10],
  ['fec0::', 10],
  ['ff00::', 8],
].map(([base, prefix]) => [parseIpv6(base), prefix]);

/** Return true for invalid, non-routable, private, or special-use IP addresses. */
export function isBlockedIp(address) {
  const ipv4 = ipv4Number(address);
  if (ipv4 !== null) {
    return BLOCKED_IPV4_RANGES.some(([base, prefix]) => ipv4InCidr(ipv4, base, prefix));
  }

  const ipv6 = parseIpv6(address);
  if (ipv6 === null) return true;

  // IPv4-mapped IPv6 (::ffff:0:0/96) is classified by its embedded IPv4 value.
  if ((ipv6 >> 32n) === 0xffffn) {
    const mapped = Number(ipv6 & 0xffffffffn);
    return BLOCKED_IPV4_RANGES.some(([base, prefix]) => ipv4InCidr(mapped, base, prefix));
  }

  return BLOCKED_IPV6_RANGES.some(([base, prefix]) => ipv6InCidr(ipv6, base, prefix));
}

function error(code, message) {
  return new PolicyError(code, message);
}

/** Parse and canonicalize a URL before any DNS or network operation. */
export function normalizeUrl(input, { allowNonDefaultPorts = false } = {}) {
  let url;
  try {
    url = new URL(input);
  } catch {
    throw error('INVALID_URL', 'The URL is malformed.');
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw error('UNSUPPORTED_SCHEME', 'Only HTTP and HTTPS URLs are allowed.');
  }
  if (url.username || url.password) {
    throw error('URL_CREDENTIALS', 'URLs containing credentials are not allowed.');
  }
  if (!url.hostname) throw error('INVALID_URL', 'The URL has no hostname.');
  if (url.port && !allowNonDefaultPorts) {
    throw error('NON_DEFAULT_PORT', 'Non-default ports are not allowed.');
  }

  url.hash = '';
  const entries = [...url.searchParams.entries()]
    .filter(([key]) => !/^utm_/i.test(key) && !['fbclid', 'gclid', 'share_token'].includes(key.toLowerCase()))
    .map(([key, value], index) => ({ key, value, index }))
    .sort((a, b) => {
      if (a.key !== b.key) return a.key < b.key ? -1 : 1;
      if (a.value !== b.value) return a.value < b.value ? -1 : 1;
      return a.index - b.index;
    });
  url.search = '';
  for (const { key, value } of entries) url.searchParams.append(key, value);
  return url;
}

function hostnameForDns(url) {
  return url.hostname.startsWith('[') && url.hostname.endsWith(']')
    ? url.hostname.slice(1, -1)
    : url.hostname;
}

/** Resolve a URL once, reject it if any answer is unsafe, and return a pinned answer. */
export async function resolveAndValidateUrl(input, {
  resolver = dns.lookup,
  allowNonDefaultPorts = false,
} = {}) {
  const url = input instanceof URL ? normalizeUrl(input.href, { allowNonDefaultPorts }) : normalizeUrl(input, { allowNonDefaultPorts });
  let answers;
  try {
    answers = await resolver(hostnameForDns(url), { all: true, verbatim: true });
  } catch {
    throw error('DNS_FAILED', 'The hostname could not be resolved.');
  }

  if (!Array.isArray(answers) || answers.length === 0) {
    throw error('DNS_FAILED', 'The hostname returned no addresses.');
  }
  for (const answer of answers) {
    if (!answer || (answer.family !== 4 && answer.family !== 6) || isBlockedIp(answer.address)) {
      throw error('BLOCKED_ADDRESS', 'The hostname resolves to a blocked address.');
    }
  }

  return {
    url,
    address: answers[0].address,
    family: answers[0].family,
    addresses: answers.map(answer => ({ address: answer.address, family: answer.family })),
  };
}

function firstHeader(headers, name) {
  const value = headers?.[name];
  return Array.isArray(value) ? value[0] : value;
}

function contentType(headers) {
  return String(firstHeader(headers, 'content-type') || '').split(';', 1)[0].trim().toLowerCase();
}

function byteLimit(maxBytes) {
  let seen = 0;
  return new Transform({
    transform(chunk, encoding, callback) {
      seen += chunk.length;
      if (seen > maxBytes) callback(error('BODY_TOO_LARGE', 'The response body is too large.'));
      else callback(null, chunk);
    },
  });
}

function decoderFor(headers) {
  const encoding = String(firstHeader(headers, 'content-encoding') || 'identity').trim().toLowerCase();
  if (!encoding || encoding === 'identity') return null;
  if (encoding === 'gzip' || encoding === 'x-gzip') return createGunzip();
  if (encoding === 'deflate') return createInflate();
  if (encoding === 'br') return createBrotliDecompress();
  throw error('UNSUPPORTED_ENCODING', 'The response uses an unsupported content encoding.');
}

async function collectBody(response, maxBytes) {
  const chunks = [];
  const collector = new Writable({
    write(chunk, encoding, callback) {
      chunks.push(Buffer.from(chunk));
      callback();
    },
  });
  const decoder = decoderFor(response.headers);
  const streams = decoder
    ? [response, byteLimit(maxBytes), decoder, byteLimit(maxBytes), collector]
    : [response, byteLimit(maxBytes), collector];
  try {
    await pipeline(streams);
  } catch (cause) {
    if (cause instanceof PolicyError) throw cause;
    throw error('INVALID_RESPONSE', 'The response body could not be decoded.');
  }
  return Buffer.concat(chunks);
}

/** Make one HTTP request while forcing Node to use the already-approved DNS answer. */
export function performPinnedRequest({ url, address, family, timeoutMs, maxBytes, headers }) {
  return new Promise((resolve, reject) => {
    const transport = url.protocol === 'https:' ? https : http;
    const dnsHostname = hostnameForDns(url);
    const request = transport.request({
      protocol: url.protocol,
      hostname: dnsHostname,
      port: url.port || undefined,
      method: 'GET',
      path: `${url.pathname}${url.search}`,
      agent: false,
      family,
      servername: net.isIP(dnsHostname) ? undefined : dnsHostname,
      headers: {
        'User-Agent': USER_AGENT,
        Accept: 'text/html, application/xhtml+xml;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        ...headers,
        Host: url.host,
      },
      lookup(_hostname, lookupOptions, callback) {
        if (lookupOptions?.all) callback(null, [{ address, family }]);
        else callback(null, address, family);
      },
    }, async response => {
      try {
        const statusCode = response.statusCode || 0;
        if (REDIRECT_STATUSES.has(statusCode)) {
          response.resume();
          resolve({ statusCode, headers: response.headers, body: Buffer.alloc(0) });
          return;
        }
        const declaredSize = Number(firstHeader(response.headers, 'content-length'));
        if (Number.isFinite(declaredSize) && declaredSize > maxBytes) {
          response.destroy();
          reject(error('BODY_TOO_LARGE', 'The response body is too large.'));
          return;
        }
        const body = await collectBody(response, maxBytes);
        resolve({ statusCode, headers: response.headers, body });
      } catch (cause) {
        reject(cause);
      }
    });

    request.setTimeout(timeoutMs, () => request.destroy(error('TIMEOUT', 'The request timed out.')));
    request.once('error', cause => {
      if (cause instanceof PolicyError) reject(cause);
      else reject(error('NETWORK_ERROR', 'The request failed.'));
    });
    request.end();
  });
}

/**
 * Fetch exactly one resource hop through an already-validated and pinned DNS
 * answer. Redirects are returned to the caller so an interception layer can
 * revalidate the browser's next request before any network I/O.
 */
export async function fetchResourceOnce(input, {
  resolver = dns.lookup,
  requestImpl = performPinnedRequest,
  maxBytes = MAX_BODY_BYTES,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  allowNonDefaultPorts = false,
  headers = { Accept: '*/*' },
} = {}) {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw error('INVALID_OPTIONS', 'The body limit is invalid.');
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw error('INVALID_OPTIONS', 'The timeout is invalid.');
  const approved = await resolveAndValidateUrl(input, { resolver, allowNonDefaultPorts });
  const response = await requestImpl({
    url: approved.url,
    address: approved.address,
    family: approved.family,
    timeoutMs,
    maxBytes,
    headers,
  });
  if (!response || !Number.isInteger(Number(response.statusCode)) || !Buffer.isBuffer(response.body)) {
    throw error('INVALID_RESPONSE', 'The response body is invalid.');
  }
  return {
    statusCode: Number(response.statusCode),
    headers: response.headers || {},
    body: response.body,
    url: approved.url.href,
  };
}

/** Securely fetch an HTML document, validating and pinning every redirect hop. */
export async function fetchHtml(input, {
  resolver = dns.lookup,
  requestImpl = performPinnedRequest,
  maxRedirects = DEFAULT_MAX_REDIRECTS,
  maxBytes = MAX_BODY_BYTES,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  allowNonDefaultPorts = false,
  headers,
} = {}) {
  if (!Number.isInteger(maxRedirects) || maxRedirects < 0) throw error('INVALID_OPTIONS', 'The redirect limit is invalid.');
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw error('INVALID_OPTIONS', 'The body limit is invalid.');
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw error('INVALID_OPTIONS', 'The timeout is invalid.');

  let current = normalizeUrl(input, { allowNonDefaultPorts });
  for (let redirects = 0; ; redirects += 1) {
    const approved = await resolveAndValidateUrl(current, { resolver, allowNonDefaultPorts });
    const response = await requestImpl({
      url: approved.url,
      address: approved.address,
      family: approved.family,
      timeoutMs,
      maxBytes,
      headers,
    });
    const statusCode = Number(response?.statusCode || 0);

    if (REDIRECT_STATUSES.has(statusCode)) {
      const location = firstHeader(response.headers, 'location');
      if (!location) throw error('INVALID_REDIRECT', 'The redirect has no destination.');
      if (redirects >= maxRedirects) throw error('TOO_MANY_REDIRECTS', 'The response redirected too many times.');
      let target;
      try {
        target = new URL(location, approved.url);
      } catch {
        throw error('INVALID_REDIRECT', 'The redirect destination is malformed.');
      }
      current = normalizeUrl(target.href, { allowNonDefaultPorts });
      continue;
    }

    if (statusCode < 200 || statusCode >= 300) {
      throw error('HTTP_STATUS', 'The server returned an unsuccessful status.');
    }
    if (!HTML_CONTENT_TYPES.has(contentType(response.headers))) {
      throw error('UNSUPPORTED_CONTENT_TYPE', 'The response is not HTML.');
    }
    if (!Buffer.isBuffer(response.body)) throw error('INVALID_RESPONSE', 'The response body is invalid.');
    if (response.body.length > maxBytes) throw error('BODY_TOO_LARGE', 'The response body is too large.');

    return { html: response.body.toString('utf8'), finalUrl: approved.url.href };
  }
}
