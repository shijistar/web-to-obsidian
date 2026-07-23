import { Defuddle } from 'defuddle/node';
import { parseHTML } from 'linkedom';
import { chromium } from 'playwright';

import {
  DEFAULT_TIMEOUT_MS,
  PolicyError,
  fetchResourceOnce,
  fetchHtml as secureFetchHtml,
  normalizeUrl,
  resolveAndValidateUrl,
} from './network-policy.mjs';

export const MIN_MARKDOWN_CHARS = 200;
export const MAX_BROWSER_REQUESTS = 80;
export const BROWSER_TIMEOUT_MS = 30_000;
export const MAX_BROWSER_RESOURCE_BYTES = 4 * 1024 * 1024;
export const MAX_BROWSER_TOTAL_BYTES = 32 * 1024 * 1024;

const GENERIC_TITLES = new Set([
  'home',
  'homepage',
  'index',
  'new tab',
  'untitled',
  'untitled page',
]);

export class ExtractorError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ExtractorError';
    this.code = code;
  }
}

function cleanString(value) {
  const cleaned = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  return cleaned || null;
}

function countWords(markdown) {
  const words = markdown
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .match(/[\p{L}\p{N}]+(?:['’_-][\p{L}\p{N}]+)*/gu);
  return words?.length || 0;
}

function meaningfulTitle(title) {
  const cleaned = cleanString(title);
  if (!cleaned || cleaned.length < 3) return false;
  if (GENERIC_TITLES.has(cleaned.toLowerCase())) return false;
  return /[\p{L}\p{N}]/u.test(cleaned);
}

function collectKeywords(document) {
  const selectors = [
    'meta[name="keywords" i]',
    'meta[name="news_keywords" i]',
    'meta[property="article:tag" i]',
  ];
  const seen = new Set();
  const keywords = [];
  for (const selector of selectors) {
    for (const node of document.querySelectorAll(selector)) {
      const raw = node.getAttribute('content');
      if (!raw) continue;
      for (const part of raw.split(/[;,\n]+/)) {
        const cleaned = cleanString(part);
        if (!cleaned || !/[\p{L}\p{N}]/u.test(cleaned)) continue;
        const key = cleaned.toLocaleLowerCase('en-US');
        if (seen.has(key)) continue;
        seen.add(key);
        keywords.push(cleaned);
        if (keywords.length >= 32) return keywords;
      }
    }
  }
  return keywords;
}

export function meetsQualityGate(result) {
  return meaningfulTitle(result?.title) && typeof result?.markdown === 'string' && result.markdown.trim().length >= MIN_MARKDOWN_CHARS;
}

function safeCanonical(document, sourceUrl) {
  const canonical = document.querySelector('link[rel~="canonical" i]');
  const href = canonical?.getAttribute('href');
  if (!href) return sourceUrl.href;
  try {
    const normalized = normalizeUrl(new URL(href, sourceUrl).href);
    if (normalized.origin !== sourceUrl.origin) return sourceUrl.href;
    canonical.setAttribute('href', normalized.href);
    return normalized.href;
  } catch {
    canonical?.remove();
    return sourceUrl.href;
  }
}

function absolutizeLinks(document, sourceUrl) {
  for (const anchor of document.querySelectorAll('a[href]')) {
    const href = anchor.getAttribute('href')?.trim();
    if (!href || href.startsWith('#')) continue;
    try {
      const absolute = new URL(href, sourceUrl);
      if (absolute.protocol === 'http:' || absolute.protocol === 'https:' || absolute.protocol === 'mailto:') {
        anchor.setAttribute('href', absolute.href);
      } else {
        anchor.removeAttribute('href');
      }
    } catch {
      anchor.removeAttribute('href');
    }
  }
  for (const element of document.querySelectorAll('img[src], source[src]')) {
    const src = element.getAttribute('src')?.trim();
    if (!src) continue;
    try {
      const absolute = new URL(src, sourceUrl);
      if (absolute.protocol === 'http:' || absolute.protocol === 'https:') element.setAttribute('src', absolute.href);
      else element.removeAttribute('src');
    } catch {
      element.removeAttribute('src');
    }
  }
}

/** Parse already-fetched HTML without evaluating page scripts or allowing Defuddle network fallbacks. */
export async function extractHtml(html, inputUrl) {
  if (typeof html !== 'string') throw new ExtractorError('INVALID_HTML', 'The HTML input is invalid.');
  const sourceUrl = normalizeUrl(inputUrl);
  const { document } = parseHTML(html);
  const canonicalUrl = safeCanonical(document, sourceUrl);
  absolutizeLinks(document, sourceUrl);

  let parsed;
  const originalConsoleError = console.error;
  const originalConsoleWarn = console.warn;
  try {
    console.error = () => {};
    console.warn = () => {};
    parsed = await Defuddle(document, sourceUrl.href, {
      markdown: true,
      useAsync: false,
      debug: false,
    });
  } catch {
    throw new ExtractorError('EXTRACTION_FAILED', 'Article extraction failed.');
  } finally {
    // Defuddle can log page-controlled malformed metadata URLs. Keep the CLI
    // protocol and Hermes logs free of untrusted diagnostics and stack traces.
    console.error = originalConsoleError;
    console.warn = originalConsoleWarn;
  }

  const markdown = typeof parsed.content === 'string' ? parsed.content.trim() : '';
  return {
    title: cleanString(parsed.title),
    author: cleanString(parsed.author) || '',
    published: cleanString(parsed.published) || '',
    description: cleanString(parsed.description) || '',
    site: cleanString(parsed.site) || cleanString(parsed.domain) || sourceUrl.hostname,
    canonicalUrl,
    keywords: collectKeywords(document),
    markdown,
    wordCount: Number.isSafeInteger(parsed.wordCount) && parsed.wordCount >= 0
      ? parsed.wordCount
      : countWords(markdown),
  };
}

function qualityError() {
  return new ExtractorError('QUALITY_GATE', 'The page did not contain a substantial article.');
}

async function closeQuietly(target) {
  try {
    await target?.close();
  } catch {
    // Cleanup must not hide the extraction result or its original error.
  }
}

function responseHeadersForBrowser(headers) {
  const blocked = new Set([
    'connection', 'content-encoding', 'content-length', 'keep-alive',
    'proxy-authenticate', 'proxy-authorization', 'set-cookie', 'te',
    'trailer', 'transfer-encoding', 'upgrade',
  ]);
  const output = {};
  for (const [name, value] of Object.entries(headers || {})) {
    const lower = name.toLowerCase();
    if (blocked.has(lower) || value == null) continue;
    output[lower] = Array.isArray(value) ? value.join(', ') : String(value);
  }
  return output;
}

/** Build a browser route that never lets Chromium perform HTTP(S) I/O. */
export function buildSecureRouteHandler({
  resolver,
  allowNonDefaultPorts = false,
  secureFetcher = fetchResourceOnce,
  timeoutMs = BROWSER_TIMEOUT_MS,
  maxRequests = MAX_BROWSER_REQUESTS,
  maxResourceBytes = MAX_BROWSER_RESOURCE_BYTES,
  maxTotalBytes = MAX_BROWSER_TOTAL_BYTES,
} = {}) {
  let requestCount = 0;
  let totalBytes = 0;
  return async route => {
    requestCount += 1;
    if (requestCount > maxRequests) {
      await route.abort('blockedbyclient');
      return;
    }

    const request = route.request();
    let requestUrl;
    try {
      requestUrl = new URL(request.url());
    } catch {
      await route.abort('blockedbyclient');
      return;
    }

    if (requestUrl.protocol === 'data:' || requestUrl.protocol === 'about:') {
      await route.continue();
      return;
    }
    if ((requestUrl.protocol !== 'http:' && requestUrl.protocol !== 'https:') || request.method() !== 'GET') {
      await route.abort('blockedbyclient');
      return;
    }

    const remaining = maxTotalBytes - totalBytes;
    if (remaining <= 0) {
      await route.abort('blockedbyclient');
      return;
    }
    try {
      const response = await secureFetcher(requestUrl.href, {
        resolver,
        allowNonDefaultPorts,
        timeoutMs,
        maxBytes: Math.min(maxResourceBytes, remaining),
      });
      totalBytes += response.body.length;
      await route.fulfill({
        status: response.statusCode,
        headers: responseHeadersForBrowser(response.headers),
        body: response.body,
      });
    } catch {
      await route.abort('blockedbyclient');
    }
  };
}

async function extractWithPlaywright(inputUrl, {
  resolver,
  allowNonDefaultPorts,
  browserLauncher = chromium,
  timeoutMs = BROWSER_TIMEOUT_MS,
  maxRequests = MAX_BROWSER_REQUESTS,
} = {}) {
  const approved = await resolveAndValidateUrl(inputUrl, { resolver, allowNonDefaultPorts });
  let browser;
  let context;
  try {
    browser = await browserLauncher.launch({
      headless: true,
      args: [
        '--host-resolver-rules=MAP * ~NOTFOUND',
        '--disable-webrtc',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      ],
    });
    context = await browser.newContext({
      serviceWorkers: 'block',
      acceptDownloads: false,
    });
    context.setDefaultTimeout(timeoutMs);
    context.setDefaultNavigationTimeout(timeoutMs);

    await context.route('**/*', buildSecureRouteHandler({
      resolver,
      allowNonDefaultPorts,
      timeoutMs,
      maxRequests,
    }));
    if (typeof context.routeWebSocket === 'function') {
      await context.routeWebSocket('**/*', socket => socket.close({ code: 1008, reason: 'blocked' }));
    }

    const page = await context.newPage();
    await page.goto(approved.url.href, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    const finalUrl = page.url();
    await resolveAndValidateUrl(finalUrl, { resolver, allowNonDefaultPorts });
    const result = await extractHtml(await page.content(), finalUrl);
    if (!meetsQualityGate(result)) throw qualityError();
    return { ...result, url: normalizeUrl(finalUrl).href, method: 'playwright' };
  } catch (cause) {
    if (cause instanceof PolicyError || cause instanceof ExtractorError) throw cause;
    throw new ExtractorError('BROWSER_FAILED', 'Browser extraction failed.');
  } finally {
    await closeQuietly(context);
    await closeQuietly(browser);
  }
}

/** Fetch and extract a URL statically, with an isolated Chromium fallback for low-quality pages. */
export async function extractUrl(inputUrl, options = {}) {
  const normalized = normalizeUrl(inputUrl, { allowNonDefaultPorts: options.allowNonDefaultPorts });
  const fetcher = options.fetchHtml || secureFetchHtml;
  const fetched = await fetcher(normalized.href, {
    resolver: options.resolver,
    maxRedirects: options.maxRedirects,
    maxBytes: options.maxBytes,
    timeoutMs: options.staticTimeoutMs || DEFAULT_TIMEOUT_MS,
    allowNonDefaultPorts: options.allowNonDefaultPorts,
  });
  if (!fetched || typeof fetched.html !== 'string' || typeof fetched.finalUrl !== 'string') {
    throw new ExtractorError('INVALID_RESPONSE', 'The static fetch returned an invalid response.');
  }

  const staticResult = await extractHtml(fetched.html, fetched.finalUrl);
  if (meetsQualityGate(staticResult)) {
    return { ...staticResult, url: normalizeUrl(fetched.finalUrl).href, method: 'static' };
  }
  if (options.allowBrowser === false || options.dynamic === false || options.browser === false) throw qualityError();

  return extractWithPlaywright(normalized.href, {
    resolver: options.resolver,
    allowNonDefaultPorts: options.allowNonDefaultPorts,
    browserLauncher: options.browserLauncher,
    timeoutMs: options.browserTimeoutMs || BROWSER_TIMEOUT_MS,
    maxRequests: options.maxBrowserRequests || MAX_BROWSER_REQUESTS,
  });
}
