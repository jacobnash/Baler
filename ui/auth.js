// Shared gateway-auth login-gate logic (token storage, login form, 401
// handling) - used by every page that talks to a JWT-gated API through the
// gateway (index.html, derivations.html, alarms.html), and also by
// devices.html purely to require sign-in before the page renders at all -
// devices.js itself still talks straight to a separate, unauthenticated
// API (windrow.api) and never calls authHeaders()/handleUnauthorized().
// Plain global functions, no module system - same "no build step" spirit
// as every other file in ui/. Expects a page to declare the login-gate
// markup with these exact ids (see index.html) before loading this script.
// A deploy restarts the APIs; for ~30-60 s the gateway answers 502/503/504
// and every page looked broken (and login failed). Safe requests - GETs,
// and the login POST - are retried with backoff for up to ~45 s while the
// page says so, instead of surfacing the error at once.
(function () {
  const RETRY_STATUS = new Set([502, 503, 504]);
  const BUDGET_MS = 45000;
  const realFetch = window.fetch.bind(window);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function retryable(input, init) {
    const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    const url = String(typeof input === "string" ? input : (input && input.url) || "");
    return method === "GET" || method === "HEAD" || (method === "POST" && /\/auth\/login$/.test(url));
  }
  window.fetch = async function (input, init) {
    if (!retryable(input, init)) return realFetch(input, init);
    const started = Date.now();
    let delay = 1000;
    for (;;) {
      let res = null;
      try {
        res = await realFetch(input, init);
      } catch (err) {
        if (Date.now() - started > BUDGET_MS) throw err;
      }
      if (res && !RETRY_STATUS.has(res.status)) return res;
      if (Date.now() - started > BUDGET_MS) return res || realFetch(input, init);
      const el = document.getElementById("status");
      if (el) el.textContent = "Baler is restarting - retrying…";
      await sleep(delay);
      delay = Math.min(delay * 2, 8000);
    }
  };
})();

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

// The second sign-in step (#278): shown in the same form once the password
// was right for an account with two-step sign-in. Built here, not in each
// page's markup, so every page with a login gate gets it.
function authMfaField() {
  let row = document.getElementById("login-mfa-row");
  if (!row) {
    row = document.createElement("label");
    row.id = "login-mfa-row";
    row.textContent = "Code from your authenticator app (or a recovery code) ";
    const input = Object.assign(document.createElement("input"), {
      id: "login-mfa-code", autocomplete: "one-time-code", inputMode: "text", required: true,
    });
    row.appendChild(input);
    authLoginFormEl.insertBefore(row, authLoginFormEl.querySelector("button[type=submit]"));
  }
  return row;
}

// "Forgot your password?" under the form: a link to the reset page when this
// install sends email, otherwise who to ask (admin-led reset).
async function authForgotHint() {
  if (document.getElementById("login-forgot")) return;
  const p = Object.assign(document.createElement("p"), { id: "login-forgot", className: "hint" });
  authLoginFormEl.appendChild(p);
  let selfService = false;
  try {
    const res = await fetch(`${AUTH_API_URL}/password-reset/config`);
    selfService = res.ok && (await res.json()).self_service === true;
  } catch {
    selfService = false;
  }
  if (selfService) {
    const a = Object.assign(document.createElement("a"), { href: "reset.html", textContent: "Forgot your password?" });
    p.appendChild(a);
  } else {
    p.textContent = "Forgot your password? Ask an admin of your organization to reset it.";
  }
}

// Wires the login form and, once signed in (or already holding a token),
// calls onReady() to let the page do its own data loading.
function initAuth(onReady) {
  let mfaToken = null; // set between the password step and the code step
  const passwordInputs = () => [document.getElementById("login-email"), document.getElementById("login-password")];

  function done(body) {
    setToken(body.token);
    mfaToken = null;
    const row = document.getElementById("login-mfa-row");
    if (row) row.remove();
    passwordInputs().forEach((el) => { el.disabled = false; });
    if (body.mfa_enrollment_required && !/ops\.html$/.test(location.pathname)) {
      // The org requires two-step sign-in: set it up first (Ops -> Your account).
      location.href = "ops.html#account-section";
      return;
    }
    hideLoginGate();
    if (authStatusEl) authStatusEl.textContent = "connecting…";
    onReady();
  }

  authLoginFormEl.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    authLoginErrorEl.hidden = true;
    try {
      if (mfaToken) {
        const code = document.getElementById("login-mfa-code").value.trim();
        const res = await fetch(`${AUTH_API_URL}/login/mfa`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mfa_token: mfaToken, code }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          if (res.status === 401 && /expired/.test(body.error || "")) {
            mfaToken = null;
            document.getElementById("login-mfa-row").remove();
            passwordInputs().forEach((el) => { el.disabled = false; });
          }
          throw new Error(body.error || `sign-in failed: ${res.status}`);
        }
        done(body);
        return;
      }
      const email = document.getElementById("login-email").value;
      const password = document.getElementById("login-password").value;
      const res = await fetch(`${AUTH_API_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        throw new Error(res.status === 401 ? "invalid email or password" : `login failed: ${res.status}`);
      }
      const body = await res.json();
      if (body.mfa_required) {
        mfaToken = body.mfa_token;
        passwordInputs().forEach((el) => { el.disabled = true; });
        authMfaField().querySelector("input").focus();
        return;
      }
      done(body);
    } catch (err) {
      authLoginErrorEl.textContent = err.message;
      authLoginErrorEl.hidden = false;
    }
  });
  authForgotHint();

  if (getToken()) {
    hideLoginGate();
    onReady();
  } else {
    showLoginGate();
  }
}
