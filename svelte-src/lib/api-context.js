// API context — eliminates prop drilling for API callbacks through the
// node widget component tree.  NodeWidget calls provideApi() once at mount;
// children call useApi() to get the same object without prop threading.

import { setContext, getContext } from "svelte";

const API_KEY = "pcr-api";

export function provideApi(api) { setContext(API_KEY, api); }
export function useApi() { return getContext(API_KEY); }

// Parse a Response's JSON body safely.  Rationale:
//   - `.json()` throws on empty bodies (HTTP 204) and on HTML error pages
//     from backend 500s, and those throws propagate as uncaught promise
//     rejections — which kill the whole extension.
//   - Non-OK responses should become structured errors the UI can display,
//     not silent empty objects.
// Returns {} for 204 / empty body; throws HttpError with status + text on
// non-OK so callers can surface it in a toast or error row.
export class HttpError extends Error {
  constructor(status, statusText, body) {
    super(`HTTP ${status} ${statusText}`);
    this.name = "HttpError";
    this.status = status;
    this.statusText = statusText;
    this.body = body;
  }
}

export async function safeJson(resp) {
  if (!resp.ok) {
    let body = null;
    try { body = await resp.text(); } catch { /* body unavailable */ }
    throw new HttpError(resp.status, resp.statusText, body);
  }
  if (resp.status === 204) return {};
  const len = resp.headers.get("content-length");
  if (len === "0") return {};
  const text = await resp.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new HttpError(resp.status, "Invalid JSON body", text);
  }
}
