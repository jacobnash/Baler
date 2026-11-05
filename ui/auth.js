// Shared gateway-auth login-gate logic (token storage, login form, 401
// handling) - used by every page that talks to a JWT-gated API through the
// gateway (index.html, derivations.html, alarms.html), and also by
// devices.html purely to require sign-in before the page renders at all -
// devices.js itself still talks straight to a separate, unauthenticated
// API (windrow.api) and never calls authHeaders()/handleUnauthorized().
// Plain global functions, no module system - same "no build step" spirit
// as every other file in ui/. Expects a page to declare the login-gate
// markup with these exact ids (see index.html) before loading this script.
const AUTH_API_URL = new URLSearchParams(location.search).get("auth") || "/auth";
const TOKEN_KEY = "td_token";

const getToken = () => localStorage.getItem(TOKEN_KEY);
const setToken = (token) => localStorage.setItem(TOKEN_KEY, token);
const clearToken = () => localStorage.removeItem(TOKEN_KEY);
const authHeaders = () => {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};

const authMainEl = document.getElementById("main");
const authLoginGateEl = document.getElementById("login-gate");
const authLoginFormEl = document.getElementById("login-form");
const authLoginErrorEl = document.getElementById("login-error");
const authStatusEl = document.getElementById("status");

// Inline style.display, not the `hidden` attribute: `hidden`'s UA-stylesheet
// `display: none` always loses to any author rule setting `display` on the
// same element (origin/importance is compared before specificity in the
// cascade - CSS Cascade Sort, https://www.w3.org/TR/css-cascade-4/#cascade-sort),
// so a page-specific rule like `.console-main { display: block }` (needed
// to override style.css's bare `main { display: grid }` for these
// non-two-column pages) silently defeats `mainEl.hidden = true`. Confirmed
// live: the login gate and the page content both rendered at once.
// Inline style always wins over any external/embedded stylesheet, so it's
// immune to whatever display rule a given page declares for #main.
function showLoginGate() {
  authMainEl.style.display = "none";
  authLoginGateEl.style.display = "";
}

function hideLoginGate() {
  authLoginGateEl.style.display = "none";
  authMainEl.style.display = "";
}

// Covers token expiry: any gateway-fronted fetch that comes back 401 drops
// the stale token and re-shows the login form. Callers pass their fetch's
// 401 response through this before treating it as a real error.
function handleUnauthorized() {
  clearToken();
  showLoginGate();
}

// The top-nav lives in <header>, outside #main/#login-gate, so it's on
// screen regardless of auth state - every page already omits a link back
// to itself, so "current page" was only ever inferable from the <h1>.
// Marks the matching link aria-current="page"; style.css renders that as
// inert, muted text instead of a link.
function highlightCurrentNavLink() {
  const here = location.pathname.split("/").pop() || "index.html";
  for (const link of document.querySelectorAll(".top-nav a[href]")) {
    if (link.getAttribute("href") === here) link.setAttribute("aria-current", "page");
  }
}
highlightCurrentNavLink();

// Wires the login form and, once signed in (or already holding a token),
// calls onReady() to let the page do its own data loading.
function initAuth(onReady) {
  authLoginFormEl.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    authLoginErrorEl.hidden = true;
    const email = document.getElementById("login-email").value;
    const password = document.getElementById("login-password").value;
    try {
      const res = await fetch(`${AUTH_API_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        throw new Error(res.status === 401 ? "invalid email or password" : `login failed: ${res.status}`);
      }
      const body = await res.json();
      setToken(body.token);
      hideLoginGate();
      if (authStatusEl) authStatusEl.textContent = "connecting…";
      onReady();
    } catch (err) {
      authLoginErrorEl.textContent = err.message;
      authLoginErrorEl.hidden = false;
    }
  });

  if (getToken()) {
    hideLoginGate();
    onReady();
  } else {
    showLoginGate();
  }
}
