// CodeMirror 6 wrapper, loaded from jsdelivr's dependency-resolving +esm
// endpoint - no bundler of our own, same buildless spirit as the rest of
// ui/. Declarative: any <textarea data-editor-lang="json|python"> gets
// swapped for a real editor on load. derivations.js/alarms.js don't know
// this file exists - they keep reading/writing textarea.value and
// listening for "input" exactly like before.
// Pinned via esm.sh's ?deps= so codemirror/lang-json/lang-python resolve
// the *same* @codemirror/state,view,language,autocomplete,@lezer/common
// builds - without this they each drag in their own copy and CodeMirror
// throws "multiple instances of @codemirror/state" at construction time.
const DEPS = "@codemirror/state@6.7.4,@codemirror/view@6.43.11,@codemirror/language@6.12.4,@codemirror/autocomplete@6.20.3,@lezer/common@1.5.2,@lezer/highlight@1.2.5";
const [{ EditorView, basicSetup }, { json }, { python }, { Compartment }, { HighlightStyle, syntaxHighlighting }, { tags: t }] = await Promise.all([
  import(`https://esm.sh/codemirror@6.0.2?deps=${DEPS}`),
  import(`https://esm.sh/@codemirror/lang-json@6.0.2?deps=${DEPS}`),
  import(`https://esm.sh/@codemirror/lang-python@6.2.1?deps=${DEPS}`),
  // Direct imports pass each package's *own* deps only: esm.sh builds an
  // entry URL per ?deps= list, but rewrites inner imports to just the
  // dependencies a package has. These three then resolve to the same
  // modules codemirror/lang-* import, so there's still one instance each.
  import("https://esm.sh/@codemirror/state@6.7.4"),
  import("https://esm.sh/@codemirror/language@6.12.4?deps=@codemirror/state@6.7.4,@codemirror/view@6.43.11,@lezer/common@1.5.2,@lezer/highlight@1.2.5"),
  import("https://esm.sh/@lezer/highlight@1.2.5?deps=@lezer/common@1.5.2"),
]);

const LANGS = { json, python };

// basicSetup's default highlight colours (#708, #a11, #00f...) assume a
// light background; on the dark page they fall to 1-2.5:1. In dark mode
// the editor switches to CodeMirror's dark base theme plus these colours,
// each over 7:1 on the #111 page.
const darkHighlight = HighlightStyle.define([
  { tag: t.meta, color: "#c0c0c0" },
  { tag: t.link, textDecoration: "underline" },
  { tag: t.heading, textDecoration: "underline", fontWeight: "bold" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strong, fontWeight: "bold" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: t.keyword, color: "#d2a8ff" },
  { tag: [t.atom, t.bool, t.url, t.contentSeparator, t.labelName], color: "#79c0ff" },
  { tag: [t.literal, t.inserted], color: "#7ee787" },
  { tag: [t.string, t.deleted], color: "#ffa198" },
  { tag: [t.regexp, t.escape, t.special(t.string)], color: "#ffa657" },
  { tag: t.definition(t.variableName), color: "#79c0ff" },
  { tag: t.local(t.variableName), color: "#d2a8ff" },
  { tag: [t.typeName, t.namespace], color: "#56d4dd" },
  { tag: t.className, color: "#56d4dd" },
  { tag: [t.special(t.variableName), t.macroName], color: "#a5d6ff" },
  { tag: t.definition(t.propertyName), color: "#79c0ff" },
  { tag: t.comment, color: "#a0a0a0" },
  { tag: t.invalid, color: "#ff7b72" },
]);
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const schemeExtensions = () => (darkQuery.matches ? [EditorView.theme({}, { dark: true }), syntaxHighlighting(darkHighlight)] : []);

// The <label> wrapping the textarea names it, but CodeMirror's own
// contenteditable (role=textbox) isn't inside that association, so it had
// no accessible name (Baler#145). Its text, minus the textarea, is the name.
function labelText(textarea) {
  if (textarea.getAttribute("aria-label")) return textarea.getAttribute("aria-label");
  const label = textarea.labels && textarea.labels[0];
  if (!label) return textarea.id || "Code editor";
  const copy = label.cloneNode(true);
  copy.querySelectorAll("textarea, input, select, .cm-editor").forEach((el) => el.remove());
  return copy.textContent.replace(/\s+/g, " ").trim() || "Code editor";
}

function attach(textarea) {
  const lang = LANGS[textarea.dataset.editorLang];
  const scheme = new Compartment();
  const view = new EditorView({
    doc: textarea.value,
    extensions: [
      basicSetup,
      ...(lang ? [lang()] : []),
      scheme.of(schemeExtensions()),
      // tabindex=0 on the (already tabbable) content: the scroller around
      // it is a scrollable region, and axe only counts it keyboard
      // reachable when its content says it's focusable (Baler#146). One
      // tab stop, same as before.
      EditorView.contentAttributes.of({ "aria-label": labelText(textarea), tabindex: "0" }),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) textarea.dispatchEvent(new Event("input"));
      }),
    ],
  });

  // The rest of the app only ever touches textarea.value - overriding the
  // accessor (instead of rewriting every caller to a CodeMirror API) is
  // the entire integration.
  Object.defineProperty(textarea, "value", {
    get: () => view.state.doc.toString(),
    set: (v) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: v ?? "" } }),
  });

  // validateJsonField() toggles .invalid on the (now hidden) textarea -
  // mirror it onto the visible editor.
  const mirrorInvalid = () => view.dom.classList.toggle("invalid", textarea.classList.contains("invalid"));
  new MutationObserver(mirrorInvalid).observe(textarea, { attributes: true, attributeFilter: ["class"] });
  mirrorInvalid();

  // Lets callers jump the user to a 1-based line number an error refers to
  // (a sandbox.py SandboxError, a SyntaxError, a JSON.parse position) -
  // selecting the line is enough to make it visually distinct, no
  // Decoration extension needed.
  textarea.markErrorLine = (line) => {
    const clamped = Math.min(Math.max(1, line), view.state.doc.lines);
    const { from, to } = view.state.doc.line(clamped);
    view.dispatch({ selection: { anchor: from, head: to }, scrollIntoView: true });
    view.focus();
  };

  darkQuery.addEventListener("change", () => view.dispatch({ effects: scheme.reconfigure(schemeExtensions()) }));

  view.dom.style.height = `${(textarea.rows || 8) * 1.4}em`;
  textarea.style.display = "none";
  textarea.after(view.dom);
}

document.querySelectorAll("textarea[data-editor-lang]").forEach(attach);
