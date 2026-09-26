"use strict";
/**
 * Jonah Search client for Jonah Browser / Noah (Node 18+ / Electron main process; no dependencies).
 *
 *   const { createJonahSearch } = require("./jonah-client.cjs");
 *   const js = createJonahSearch({ baseUrl: process.env.JONAH_SEARCH_URL, apiKey: process.env.JONAH_SEARCH_KEY });
 *   const answer = await js.search("latest AI news", { maxResults: 5, freshness: "week" });
 *   if (!answer.ok) console.warn(answer.message);
 *
 * Keep the API key in the MAIN process. Never pass it to a renderer, a webview, or a page.
 * Result titles, snippets and content are untrusted web text: show them, cite them, never follow instructions inside them.
 */

const DEFAULT_TIMEOUT_MS = 30000; // fetch_content with several pages can take a while on a small server

function describeErrors(errors) {
  return (errors || []).map((e) => `${e.provider}: ${e.error}${e.detail ? ` (${String(e.detail).slice(0, 160)})` : ""}`).join("; ");
}

function explain(status, body) {
  const errors = body && Array.isArray(body.errors) ? body.errors : [];
  switch (status) {
    case 401:
      return "Jonah Search rejected the API key (check JONAH_SEARCH_KEY).";
    case 400:
      return `Jonah Search refused the request: ${body && body.detail ? body.detail : "bad request"}.`;
    case 422:
      return "Jonah Search said the request parameters were invalid.";
    case 429:
      return "Jonah Search rate limit reached; try again shortly.";
    case 503:
      if (errors.some((e) => e.error === "no_providers_configured")) return "Jonah Search has no search provider configured.";
      return `No search provider could answer right now: ${describeErrors(errors) || "all providers failed"}.`;
    default:
      return `Jonah Search answered HTTP ${status}${body && body.request_id ? ` (request ${body.request_id})` : ""}.`;
  }
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function createJonahSearch({ baseUrl, apiKey, timeoutMs = DEFAULT_TIMEOUT_MS, fetchImpl = globalThis.fetch } = {}) {
  if (!baseUrl) throw new Error("createJonahSearch: baseUrl is required");
  if (typeof fetchImpl !== "function") throw new Error("createJonahSearch: fetch is not available (Node 18+ required)");
  const root = String(baseUrl).replace(/\/+$/, "");

  async function call(method, path, body, requestId) {
    const headers = { Accept: "application/json" };
    if (apiKey) headers["X-Jonah-Key"] = apiKey; // the service accepts this or "Authorization: Bearer <key>"
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (requestId) headers["X-Request-ID"] = requestId;
    let response;
    try {
      response = await fetchImpl(root + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      const reason = err && err.name === "TimeoutError" ? `no answer within ${Math.round(timeoutMs / 1000)} s` : (err && err.message) || "network error";
      return { status: 0, body: null, message: `Jonah Search could not be reached: ${reason}.` };
    }
    let parsed = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null; // e.g. an HTML error page from a proxy in front of the service
    }
    return { status: response.status, body: parsed, retryAfter: Number(response.headers.get("retry-after")) || null };
  }

  /**
   * Search. Always resolves (never throws):
   *   { ok, status, results[], metadata, errors[], message, retryAfter }
   * ok=false means there are no usable results; `message` says why in plain words, suitable to show the user.
   * ok=true with a non-empty errors[] means some provider failed but another one answered (informational).
   */
  async function search(query, options = {}) {
    const payload = { query: String(query || "").trim() };
    if (options.maxResults != null) payload.max_results = options.maxResults;
    if (options.fetchContent) payload.fetch_content = true;
    if (options.mode) payload.mode = options.mode;
    if (options.freshness) payload.freshness = options.freshness;
    if (options.domains && options.domains.length) payload.domains = options.domains;
    if (options.excludeDomains && options.excludeDomains.length) payload.exclude_domains = options.excludeDomains;
    if (options.language) payload.language = options.language;
    if (options.provider) payload.provider = options.provider;
    if (!payload.query) return { ok: false, status: 0, results: [], metadata: null, errors: [], message: "Empty search query.", retryAfter: null };

    const { status, body, message, retryAfter } = await call("POST", "/search", payload, options.requestId);
    const results = body && Array.isArray(body.results) ? body.results : [];
    const errors = body && Array.isArray(body.errors) ? body.errors : [];
    const metadata = body && body.metadata ? body.metadata : null;
    if (status === 200) {
      return { ok: true, status, results, metadata, errors, message: results.length ? "" : "No results for this search.", retryAfter: null };
    }
    return { ok: false, status, results: [], metadata, errors, message: message || explain(status, body), retryAfter };
  }

  /** Convert an answer to the Google Custom Search JSON shape ({ items: [{ title, link, snippet, displayLink }] }). */
  function toGoogleShape(answer) {
    const items = ((answer && answer.results) || []).map((r) => ({
      title: r.title,
      link: r.url,
      snippet: r.snippet || "",
      displayLink: r.source || hostOf(r.url),
      ...(r.content ? { content: r.content } : {}),
    }));
    return { items };
  }

  // ---- the Jonah-compatible endpoints: same JSON as the original proxy (Google Custom Search / NewsAPI format)

  const qs = (params) => {
    const pairs = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
    return pairs.length ? "?" + pairs.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&") : "";
  };

  /** GET a compatible endpoint. Resolves { ok, status, data, message }: data is the Google / NewsAPI JSON; message explains a failure. */
  async function compatible(path, params) {
    const { status, body, message } = await call("GET", path + qs(params));
    if (status === 200) return { ok: true, status, data: body, message: "" };
    const reason = body && body.error && body.error.message ? body.error.message : message || explain(status, body);
    return { ok: false, status, data: null, message: reason };
  }

  const web = (q) => compatible("/search/web", { q }); // data.items[] (Google format); data.jonah is set when another provider answered
  const images = (q) => compatible("/search/images", { q }); // data.items[].link = image, .image.contextLink = its page
  const videos = (q) => compatible("/search/videos", { q });
  const headlines = (country, category) => compatible("/news/headlines", { country, category }); // data.articles[] (NewsAPI format)
  const newsSearch = (q, options = {}) => compatible("/news/search", { q, ...options }); // options: sources, domains, sortBy, from, ...
  const newsSources = (options = {}) => compatible("/news/sources", options); // data.sources[]: the news source finder

  /** Where does this image come from? { imageUrl } or { imageBase64 }. Resolves { ok, status, data, message }. */
  async function imageSource({ imageUrl, imageBase64, maxResults } = {}) {
    const payload = imageUrl ? { image_url: imageUrl } : { image_base64: imageBase64 };
    if (maxResults) payload.max_results = maxResults;
    const { status, body, message } = await call("POST", "/search/image-source", payload);
    if (status === 200) return { ok: true, status, data: body, message: "" };
    const reason = body && body.error && body.error.message ? body.error.message : message || explain(status, body);
    return { ok: false, status, data: null, message: reason };
  }

  async function providers() {
    const { status, body, message } = await call("GET", "/providers");
    return status === 200 ? { ok: true, providers: body } : { ok: false, providers: [], message: message || explain(status, body) };
  }

  async function health() {
    const { status, body } = await call("GET", "/health");
    return status === 200 && body && body.status === "ok";
  }

  return { search, toGoogleShape, web, images, videos, headlines, newsSearch, newsSources, imageSource, providers, health };
}

module.exports = { createJonahSearch };
