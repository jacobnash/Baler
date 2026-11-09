// Shared helper for the ?api=/?ingest=/?ops=/... query overrides every page
// accepts for its API base URL (dev setups and tests serve ui/ standalone
// and point it at another path). Loaded first, before auth.js, on every
// page. Plain global, no build step - same style as the rest of ui/.
//
// apiBase(param, fallback) returns an absolute URL on this page's own
// origin: location.origin plus the query value when that is a same-origin
// absolute path ("/api", "/x/y"), else location.origin plus the fallback.
// Anything else - another origin, a scheme, a protocol-relative or
// backslash path, "?"/"#", whitespace or control characters - is ignored
// with a console warning. Pages send credentials and bearer tokens to this
// base, so the host is never taken from the query string: it always comes
// from location.origin (see onOwnOrigin). No trailing slash, so callers can
// append "/sources" etc.

function apiBase(param, fallback) {
  const value = new URLSearchParams(location.search).get(param);
  let path = fallback;
  if (value !== null && value !== "") {
    // Drop trailing slashes; a bare "/" leaves nothing and is refused.
    const trimmed = value.replace(/\/+$/, "");
    if (trimmed && isSameOriginPath(trimmed)) {
      path = trimmed;
    } else {
      console.warn(`Ignoring ?${param}= override: only same-origin paths starting with "/" are allowed`);
    }
  }
  return onOwnOrigin(path);
}

// "<origin>/<path>" with the scheme and host fixed by construction: the path
// is resolved as a "./"-relative reference against "<origin>/", which can
// only ever change the path, and the result is rebuilt from location.origin
// and that pathname.
function onOwnOrigin(path) {
  const rest = String(path).replace(/^[\/\\]+/, "");
  const url = new URL("./" + rest, location.origin + "/");
  return (location.origin + url.pathname).replace(/\/+$/, "");
}

function isSameOriginPath(value) {
  if (typeof value !== "string") return false;
  // Single leading slash, not "//" (protocol-relative) or "/\" (browsers
  // treat backslash as a slash in special-scheme URLs).
  if (!/^\/(?![\/\\])/.test(value)) return false;
  // No backslashes, query/fragment markers, whitespace or control characters.
  if (/[\\?#\s\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(value)) return false;
  // Percent-encoded slashes/backslashes/colons/controls could be decoded by
  // something downstream; a legitimate API base never needs them.
  if (/%(?:2f|5c|3a|[01][0-9a-f]|7f)/i.test(value)) return false;
  let resolved;
  try {
    resolved = new URL(value, location.origin);
  } catch (_e) {
    return false;
  }
  return resolved.origin === location.origin;
}
