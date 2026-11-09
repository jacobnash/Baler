// Shared helper for the ?api=/?ingest=/?ops=/... query overrides every page
// accepts for its API base URL (dev setups and tests serve ui/ standalone
// and point it at another path). Loaded first, before auth.js, on every
// page. Plain global, no build step - same style as the rest of ui/.
//
// apiBase(param, fallback) returns the query value only when it is a
// same-origin absolute path ("/api", "/x/y"); anything else - another
// origin, a scheme, a protocol-relative or backslash path, whitespace or
// control characters - is ignored with a console warning and the fallback
// is used instead. Pages send credentials and bearer tokens to this base.

function apiBase(param, fallback) {
  const value = new URLSearchParams(location.search).get(param);
  if (value === null || value === "") return fallback;
  // Callers append "/sources" etc., so drop trailing slashes; a bare "/"
  // would then yield "//sources" (protocol-relative), so it is refused.
  const trimmed = value.replace(/\/+$/, "");
  if (trimmed && isSameOriginPath(trimmed)) return trimmed;
  console.warn(`Ignoring ?${param}= override: only same-origin paths starting with "/" are allowed`);
  return fallback;
}

function isSameOriginPath(value) {
  if (typeof value !== "string") return false;
  // Single leading slash, not "//" (protocol-relative) or "/\" (browsers
  // treat backslash as a slash in special-scheme URLs).
  if (!/^\/(?![\/\\])/.test(value)) return false;
  // No backslashes, whitespace or control characters anywhere.
  if (/[\\\s\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(value)) return false;
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
