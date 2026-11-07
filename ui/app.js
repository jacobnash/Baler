// No build step, no framework. The equipment/point tree reads the Haystack
// API (haystack_api.py) through the gateway, so it needs the same login as
// every other page and only shows the caller's own sites; chart data comes
// from ingest_api.py's GET /history, also through the gateway.
const params = new URLSearchParams(location.search);
// Relative by default so this works as served by the gateway at /ui/ (same
// origin - see gateway/nginx.conf). Override with ?api=... / ?ingest=...
// when serving ui/ standalone (ui_server.py).
const HAYSTACK_API_URL = params.get("api") || "/api";
const INGEST_API_URL = params.get("ingest") || "/ingest";

const statusEl = document.getElementById("status");
const treeEl = document.getElementById("tree");
const detailEl = document.getElementById("detail");

// Tags the tree shows structurally (or that are bookkeeping), not in a
// point's tag list.
const STRUCTURAL_TAGS = new Set(["id", "dis", "brickClass", "brickHasPart", "brickFeeds"]);
// Brick location classes: shown in the Site tree, not as equipment classes.
const LOCATION_CLASSES = new Set(["Site", "Building", "Floor", "Zone", "Room", "Space"]);

// Brick's own short names for equipment classes (brick:aliasOf in
// ontology/Brick-only.ttl) -> the class they name. Hand-made records pick
// either spelling, and grouping by the raw class split one kind of
// equipment into "AHU" and "Air_Handling_Unit" (Baler#76).
// tests/test_ui_equip_classes.py checks this against the ontology.
const EQUIP_CLASS_ALIASES = {
  AED: "Automated_External_Defibrillator",
  AHU: "Air_Handling_Unit",
  Air_Handler_Unit: "Air_Handling_Unit",
  CAV: "Constant_Air_Volume_Box",
  CRAC: "Computer_Room_Air_Conditioning",
  CRAH: "Computer_Room_Air_Handler",
  DDAHU: "Dual_Duct_Air_Handling_Unit",
  DOAS: "Dedicated_Outdoor_Air_System_Unit",
  Discharge_Air_Plenum: "Supply_Air_Plenum",
  Discharge_Fan: "Supply_Fan",
  Disconnect_Switch: "Isolation_Switch",
  ESS_Panel: "Embedded_Surface_System_Panel",
  FCU: "Fan_Coil_Unit",
  HX: "Heat_Exchanger",
  Horizontal_Fan_Coil_Unit: "Duct_Fan_Coil_Unit",
  MAU: "Makeup_Air_Unit",
  NVR: "Network_Video_Recorder",
  RC_Panel: "Radiant_Ceiling_Panel",
  RTU: "Rooftop_Unit",
  RVAV: "Variable_Air_Volume_Box_With_Reheat",
  TABS_Panel: "Thermally_Activated_Building_System_Panel",
  VAV: "Variable_Air_Volume_Box",
  VFD: "Variable_Frequency_Drive",
};

// Haystack equipment-type markers, most specific first, for grouping equips
// that carry Haystack tags but no Brick class (e.g. a Windrow/Haystack
// import that was never Brick-classified) - into the same group as the
// Brick class they mean, not a group of their own.
const EQUIP_KIND_TAGS = [
  ["ahu", "Air_Handling_Unit"], ["rtu", "Rooftop_Unit"], ["mau", "Makeup_Air_Unit"],
  ["doas", "Dedicated_Outdoor_Air_System_Unit"], ["fcu", "Fan_Coil_Unit"], ["vav", "Variable_Air_Volume_Box"],
  ["chiller", "Chiller"], ["boiler", "Boiler"], ["coolingTower", "Cooling_Tower"],
  ["heatExchanger", "Heat_Exchanger"], ["pump", "Pump"], ["fan", "Fan"], ["meter", "Meter"],
  ["tank", "Tank"], ["valve", "Valve"], ["damper", "Damper"], ["vfd", "Variable_Frequency_Drive"],
];

const isMarker = (v) => v && v._kind === "marker";

// The canonical Brick class: the record's own class with Brick's alias
// folded in, else the class its Haystack equip-type marker means.
function equipKind(rec) {
  if (typeof rec.brickClass === "string") return EQUIP_CLASS_ALIASES[rec.brickClass] || rec.brickClass;
  const hit = EQUIP_KIND_TAGS.find(([tag]) => isMarker(rec[tag]));
  return hit ? hit[1] : null;
}

// One readable label per class: "Air Handling Unit", not Air_Handling_Unit.
const classLabel = (cls) => cls.replace(/_/g, " ");

class SessionExpired extends Error {}

// GET /api/read?filter=... -> the grid's rows (Hayson, the API's JSON default).
async function haystackRead(filter) {
  const url = `${HAYSTACK_API_URL}/read?filter=${encodeURIComponent(filter)}`;
  const res = await fetch(url, { headers: { ...authHeaders(), Accept: "application/json" } });
  if (res.status === 401) {
    handleUnauthorized();
    throw new SessionExpired("session expired");
  }
  if (!res.ok) throw new Error(`read failed: ${res.status}`);
  const grid = await res.json();
  if (grid.meta && grid.meta.err) throw new Error(grid.meta.dis || "read failed");
  return grid.rows || [];
}

// Ref ids are entity URIs with everything outside Haystack's ref charset
// escaped byte-wise as ~XX (haystack_api.uri_to_ref); this undoes that.
function refToUri(id) {
  const bytes = [];
  for (let i = 0; i < id.length; i++) {
    const hex = id[i] === "~" ? id.slice(i + 1, i + 3) : "";
    if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
      bytes.push(parseInt(hex, 16));
      i += 2;
    } else {
      bytes.push(...new TextEncoder().encode(id[i]));
    }
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

const refId = (v) => (v && v._kind === "ref" ? v.val : null);
const refList = (v) => (Array.isArray(v) ? v : v ? [v] : []).map(refId).filter(Boolean);

function localName(uri) {
  // urn:location:site-1 has no "/" or "#" at all - only colons - so ":" is
  // a third separator; every other URI here has a later "/" or "#".
  const i = Math.max(uri.lastIndexOf("#"), uri.lastIndexOf("/"), uri.lastIndexOf(":"));
  return uri.slice(i + 1);
}

// A record -> what the tree needs. `uri` keys history and the his.html link.
function entity(rec) {
  const id = refId(rec.id);
  const uri = refToUri(id);
  return {
    id,
    uri,
    dis: typeof rec.dis === "string" ? rec.dis : localName(uri),
    brickClass: typeof rec.brickClass === "string" ? rec.brickClass : null,
    hasPart: refList(rec.brickHasPart),
    feeds: refList(rec.brickFeeds),
    rec,
  };
}

async function loadEquipment() {
  const equips = (await haystackRead("equip")).map(entity);
  const byType = new Map();
  for (const e of equips) {
    if (LOCATION_CLASSES.has(e.brickClass)) continue;
    const kind = equipKind(e.rec);
    const label = kind ? classLabel(kind) : "Unclassified";
    if (!byType.has(label)) byType.set(label, []);
    byType.get(label).push(e);
  }
  return { byType, equips };
}

// Derivations by id, for naming computed points (Baler#62). Optional: a
// caller who can't read derivations just gets "computed" with no name.
const DERIVATION_API_URL = params.get("derivation") || "/derivation";
let derivationsPromise = null;
function loadDerivations() {
  if (!derivationsPromise) {
    derivationsPromise = fetch(`${DERIVATION_API_URL}/derivations`, { headers: authHeaders() })
      .then((res) => (res.ok ? res.json() : null))
      .then((list) => (Array.isArray(list) ? new Map(list.map((d) => [d.id, d])) : null))
      .catch(() => null);
  }
  return derivationsPromise;
}

// urn:point:computed/<derivation id>/<target> (derivation_engine._output_uri)
const COMPUTED_PREFIX = "urn:point:computed/";

// What produced a computed point: {id, name} for a live derivation,
// {id, orphan: true} when its derivation was deleted (nothing will write it
// again - Ops -> Orphaned points lists it for removal), null otherwise or
// when the derivation list couldn't be read.
function computedSource(uri, derivations) {
  if (!uri.startsWith(COMPUTED_PREFIX)) return null;
  const id = uri.slice(COMPUTED_PREFIX.length).split("/")[0];
  if (!derivations) return { id, unknown: true };
  const d = derivations.get(id);
  return d ? { id, name: d.name || id } : { id, orphan: true };
}

async function loadPoints(equip) {
  const [rows, derivations] = await Promise.all([haystackRead(`point and equipRef==@${equip.id}`), loadDerivations()]);
  const points = rows.map(entity);
  for (const p of points) p.computed = computedSource(p.uri, derivations);
  // Orphaned outputs of deleted derivations sort after the live points.
  points.sort((a, b) => !!(a.computed && a.computed.orphan) - !!(b.computed && b.computed.orphan)
    || a.dis.localeCompare(b.dis, undefined, { sensitivity: "base" }) || a.uri.localeCompare(b.uri));
  // Two points with the same dis - tell them apart. A computed point says
  // which derivation made it (or that it's an orphan), never a raw hash.
  const seen = new Map();
  for (const p of points) seen.set(p.dis, (seen.get(p.dis) || 0) + 1);
  for (const p of points) {
    if (p.computed && p.computed.orphan) p.note = "orphaned: derivation deleted";
    else if (seen.get(p.dis) > 1 && p.computed && p.computed.name) p.note = `from ${p.computed.name}`;
    else if (seen.get(p.dis) > 1 && !p.computed) p.note = localName(p.uri);
  }
  return points;
}

// Stored values are often float32 (61.02000045776367): show what a person
// would read off the controller.
const fmtNum = (v) => (typeof v === "number" ? String(+v.toFixed(2)) : String(v));
const fmtWhen = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

function tagText(name, val, unit = "") {
  // Hayson writes a unitless Number as a bare JSON number (Baler#60):
  // round it, and give curVal the point's unit tag.
  if (typeof val === "number") return `${name}: ${fmtNum(val)}${unit ? ` ${unit}` : ""}`;
  if (val && typeof val === "object" && !Array.isArray(val)) {
    if (val._kind === "marker") return name;
    if (val._kind === "number") return `${name}: ${fmtNum(val.val)}${val.unit || unit ? ` ${val.unit || unit}` : ""}`;
    if (val._kind === "dateTime") return `${name}: ${fmtWhen(val.val)}`;
    if (val._kind === "ref") return `${name}: ${val.dis || localName(refToUri(val.val))}`;
    if ("val" in val) return `${name}: ${val.val}`;
  }
  return `${name}: ${typeof val === "object" ? JSON.stringify(val) : val}`;
}

function pointUnit(rec) {
  if (typeof rec.unit === "string") return rec.unit;
  return rec.curVal && rec.curVal._kind === "number" && rec.curVal.unit ? rec.curVal.unit : "";
}

function pointTags(rec) {
  const unit = pointUnit(rec);
  return Object.keys(rec)
    .filter((k) => !STRUCTURAL_TAGS.has(k))
    .sort()
    .map((k) => tagText(k, rec[k], k === "curVal" ? unit : ""));
}

// History windows for the point chart (Baler#61): the last day by default,
// not the point's whole history. Seconds; null = everything.
const HISTORY_RANGES = [["1h", 3600], ["24h", 86400], ["7d", 7 * 86400], ["30d", 30 * 86400], ["All", null]];
let historyRange = 86400;
let historyFullScale = false;

async function loadHistory(topic, seconds = null) {
  const since = seconds ? `&start=${Math.floor(Date.now() / 1000 - seconds)}` : "";
  const url = `${INGEST_API_URL}/history?point=${encodeURIComponent(topic)}${since}`;
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) {
    handleUnauthorized();
    throw new SessionExpired("session expired");
  }
  if (!res.ok) throw new Error(`history fetch failed: ${res.status}`);
  return res.json(); // [{ts, value}, ...]
}

// Shared by both the flat by-class tree and the spatial (Site/Floor/Zone)
// tree below - an equipment node's own points load lazily, the same way,
// regardless of which tree it's reached through.
// his.html?expr=... - the Axon-style "show me everything this equipment is
// doing" view (see hisquery.py). `id==@<uri>` selects exactly this entity;
// an equipment match expands to all of its points server-side.
// An inline SVG, not an emoji: a box without an emoji font (headless
// Chrome, many Linux desktops) showed a missing-glyph box instead.
const CHART_ICON = '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false" ' +
  'style="vertical-align:-2px"><path d="M1 14h14M3 11l3-4 3 2 4-6" fill="none" stroke="currentColor" stroke-width="1.6" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

function hisLink(uri, text = null) {
  const a = document.createElement("a");
  a.className = "his-link";
  a.href = `his.html?expr=${encodeURIComponent(`readAll(id==@${uri}).hisRead(today)`)}`;
  a.title = "Chart every point of this equipment for today";
  if (text === null) {
    a.innerHTML = CHART_ICON;
    a.setAttribute("aria-label", "Chart this equipment");
  } else {
    a.textContent = text;
  }
  // Inside a <summary>, a click would also toggle the <details> - harmless
  // since we're navigating away, but don't let the tree react to it.
  a.addEventListener("click", (ev) => ev.stopPropagation());
  return a;
}

// Cx verdict per equipment from the latest commissioning run
// (commissioning_api GET /cx/equipment-status). Optional: a deployment
// without Cx, or a 403, just shows no dots.
const CX_API_URL = params.get("cx") || "/cx";
let cxStatus = {};

async function loadCxStatus() {
  try {
    const res = await fetch(`${CX_API_URL}/equipment-status`, { headers: authHeaders() });
    cxStatus = res.ok ? await res.json() : {};
  } catch {
    cxStatus = {};
  }
}

function cxDot(uri) {
  const s = cxStatus[uri];
  if (!s) return null;
  const a = document.createElement("a");
  a.className = `cx-dot st-${s.status}`;
  a.href = `cx.html?project=${encodeURIComponent(s.project_id)}&run=${encodeURIComponent(s.run_id)}&equip=${encodeURIComponent(uri)}`;
  const c = s.counts || {};
  a.title = `Commissioning (${s.project}): ${s.status} - ${c.fail || 0} fail, ${c.inconclusive || 0} inconclusive, ${c.pass || 0} pass`;
  a.setAttribute("aria-label", a.title);
  a.addEventListener("click", (ev) => ev.stopPropagation());
  return a;
}

function buildEquipNode(equip, label) {
  const equipDetails = document.createElement("details");
  equipDetails.dataset.equip = equip.uri;
  const equipSummary = document.createElement("summary");
  equipSummary.textContent = `${label} `;
  const dot = cxDot(equip.uri);
  if (dot) equipSummary.appendChild(dot);
  equipSummary.appendChild(hisLink(equip.uri));
  if (canEditRecords()) {
    equipSummary.appendChild(recordEditButton(equip, () => init()));
    equipSummary.appendChild(recordNewButton("+ point", "point", equip, null, () => init()));
  }
  equipDetails.appendChild(equipSummary);

  const pointList = document.createElement("ul");
  equipDetails.appendChild(pointList);
  equipDetails.addEventListener(
    "toggle",
    async () => {
      if (!equipDetails.open || pointList.dataset.loaded) return;
      pointList.dataset.loaded = "1";
      let points;
      try {
        points = await loadPoints(equip);
      } catch (err) {
        pointList.dataset.loaded = "";
        pointList.textContent = `failed to load points: ${err.message}`;
        return;
      }
      if (!points.length) pointList.textContent = "no points";
      for (const point of points) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.textContent = point.brickClass ? `${point.dis} — ${point.brickClass}` : point.dis;
        if (point.note) {
          const note = Object.assign(document.createElement("span"), { className: "hint", textContent: ` (${point.note})` });
          btn.appendChild(note);
          if (point.computed && point.computed.orphan) btn.classList.add("orphan");
        }
        btn.addEventListener("click", () => selectPoint(point, btn));
        li.appendChild(btn);
        pointList.appendChild(li);
      }
    },
    { once: false },
  );
  return equipDetails;
}

function renderTree(byType) {
  treeEl.innerHTML = "";
  for (const [typeLabel, equips] of [...byType.entries()].sort()) {
    const details = document.createElement("details");
    details.open = false;
    const summary = document.createElement("summary");
    summary.textContent = `${typeLabel} (${equips.length})`;
    details.appendChild(summary);

    for (const equip of equips.sort((a, b) => a.uri.localeCompare(b.uri))) {
      details.appendChild(buildEquipNode(equip, equip.dis));
    }
    treeEl.appendChild(details);
  }
}

// Site -> Building -> Floor -> Zone, with equipment reached via brick:feeds
// (AHU feeds Floor, VAV feeds Zone) alongside hasPart for the pure
// containment steps - see scripts/seed_derivations_and_faults.py's
// seed_spatial_hierarchy for how this gets built. Central-plant equipment
// (chillers, boilers, pumps, meters) isn't zone-specific, so it's listed
// directly under its Building instead of forcing it into a fake zone.
// One read for every location; the hasPart/feeds links come back as Ref
// lists on each record (haystack_api.build_records), so the tree is built
// client-side with no further round trips. Locations aren't owned by a
// site's topic prefix, so a site-scoped login sees none and gets only the
// by-class tree.
async function renderLocationSection(equips) {
  const locations = (await haystackRead("Site or Building or Floor or Zone")).map(entity);
  const sites = locations.filter((l) => l.brickClass === "Site");
  if (!sites.length) return null;
  const byId = new Map([...locations, ...equips].map((e) => [e.id, e]));
  const fedBy = new Map(); // location id -> equips feeding it
  for (const e of equips) {
    for (const target of e.feeds) {
      if (!fedBy.has(target)) fedBy.set(target, []);
      fedBy.get(target).push(e);
    }
  }
  const parts = (e, cls) => e.hasPart.map((id) => byId.get(id)).filter((c) => c && (!cls || c.brickClass === cls));
  const node = (label) => {
    const d = document.createElement("details");
    d.appendChild(Object.assign(document.createElement("summary"), { textContent: label }));
    return d;
  };
  const li = (child) => {
    const el = document.createElement("li");
    el.appendChild(child);
    return el;
  };

  const root = node("Sites");
  for (const site of sites) {
    const siteNode = node(site.dis);
    const siteList = document.createElement("ul");
    for (const building of parts(site, "Building")) {
      const buildingNode = node(building.dis.replace(/^site\//, ""));
      const buildingList = document.createElement("ul");
      for (const child of parts(building)) {
        if (child.brickClass === "Floor") {
          const ahus = fedBy.get(child.id) || [];
          const floorNode = node(`${child.dis} (fed by ${ahus.map((e) => e.dis).join(", ") || "?"})`);
          const floorList = document.createElement("ul");
          for (const ahu of ahus) floorList.appendChild(li(buildEquipNode(ahu, `${ahu.dis} (${classLabel(equipKind(ahu.rec) || "AHU")})`)));
          for (const zone of parts(child, "Zone")) {
            const zoneNode = node(zone.dis);
            const zoneList = document.createElement("ul");
            for (const vav of fedBy.get(zone.id) || []) {
              zoneList.appendChild(li(buildEquipNode(vav, `${vav.dis} (${classLabel(equipKind(vav.rec) || "VAV")})`)));
            }
            zoneNode.appendChild(zoneList);
            floorList.appendChild(li(zoneNode));
          }
          floorNode.appendChild(floorList);
          buildingList.appendChild(li(floorNode));
        } else if (child.rec.equip) {
          const kind = equipKind(child.rec);
          const label = kind ? `${child.dis} (${classLabel(kind)})` : child.dis;
          buildingList.appendChild(li(buildEquipNode(child, label)));
        }
      }
      buildingNode.appendChild(buildingList);
      siteList.appendChild(li(buildingNode));
    }
    siteNode.appendChild(siteList);
    root.appendChild(siteNode);
  }
  return root;
}

const REFRESH_INTERVAL_MS = 5000;
let selectedButton = null;
let refreshTimer = null;

async function selectPoint(point, btn) {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (selectedButton) selectedButton.classList.remove("selected");
  btn.classList.add("selected");
  selectedButton = btn;

  detailEl.innerHTML = "";
  detailEl.appendChild(Object.assign(document.createElement("h2"), { textContent: point.dis }));
  detailEl.appendChild(Object.assign(document.createElement("p"), { className: "hint", textContent: "loading…" }));

  // /history?point=X reconstructs urn:point:X server-side (see
  // ingest.topic_to_point_uri) - that works identically for a raw
  // MQTT-sourced point (X = its topic, also its td:sourceTopic) and a
  // derivation_engine-computed point (X = "computed/<id>/<name>", which
  // never gets a td:sourceTopic - see derivation_engine._output_uri).
  // Deriving X straight from the URI covers both without caring which
  // kind of point this is.
  const historyKey = point.uri.startsWith("urn:point:") ? point.uri.slice("urn:point:".length) : null;

  const tags = pointTags(point.rec);
  const history = historyKey ? await loadHistory(historyKey, historyRange).catch(() => []) : [];

  detailEl.innerHTML = "";
  const h2 = document.createElement("h2");
  h2.textContent = point.dis;
  if (canEditRecords()) {
    h2.append(" ", recordEditButton(point, () => {
      detailEl.innerHTML = '<p class="hint">Saved. Select the point again to see the change.</p>';
      init();
    }));
  }
  detailEl.appendChild(h2);

  if (point.brickClass) {
    const p = document.createElement("p");
    p.textContent = `Brick class: ${point.brickClass}`;
    detailEl.appendChild(p);
  }
  if (point.computed) {
    const p = document.createElement("p");
    const c = point.computed;
    if (c.orphan) {
      p.className = "field-error";
      p.append(`Computed by derivation ${c.id}, which no longer exists - nothing updates this point. Review it in `);
      p.appendChild(Object.assign(document.createElement("a"), { href: "ops.html#orphans-section", textContent: "Ops → Orphaned points" }));
      p.append(".");
    } else {
      p.append("Computed by derivation ");
      p.appendChild(Object.assign(document.createElement("a"), { href: `derivations.html?id=${encodeURIComponent(c.id)}`, textContent: c.name || c.id }));
    }
    detailEl.appendChild(p);
  }
  const queryP = document.createElement("p");
  queryP.className = "hint";
  queryP.appendChild(hisLink(point.uri, "Open in Query →"));
  detailEl.appendChild(queryP);

  const tagList = document.createElement("ul");
  tagList.className = "tag-list";
  for (const tag of tags) {
    const li = document.createElement("li");
    li.textContent = tag;
    tagList.appendChild(li);
  }
  detailEl.appendChild(tagList);

  // Own container, separate from the tag list/heading above - a refresh
  // tick only ever touches this, so re-clicking tags or losing scroll
  // position on a live-updating point isn't a thing.
  const historyContainer = document.createElement("div");
  detailEl.appendChild(historyContainer);
  const unit = pointUnit(point.rec);
  const hisEnd = point.rec.hisEnd && point.rec.hisEnd.val ? point.rec.hisEnd.val : null;
  const view = { history, unit, hisEnd };
  const redraw = () => renderHistoryInto(historyContainer, view, reload);
  const reload = async () => {
    const fresh = historyKey ? await loadHistory(historyKey, historyRange).catch(() => null) : [];
    if (fresh) view.history = fresh;
    redraw();
  };
  redraw();

  if (historyKey) refreshTimer = setInterval(reload, REFRESH_INTERVAL_MS);
}

// BACnet BinaryPV names, as text (history from before #81's ingest fix).
const BINARY_TEXT = new Map([["active", 1], ["inactive", 0]]);
const isTwoState = (v) => typeof v === "boolean" || BINARY_TEXT.has(v);
const twoStateValue = (v) => (typeof v === "boolean" ? (v ? 1 : 0) : BINARY_TEXT.get(v));

// [{ts, value}] -> runs of one unchanged value, oldest first: a status
// sampled every 5 s was a wall of identical lines (Baler#81).
function valueRuns(history) {
  const runs = [];
  for (const row of history) {
    const key = isTwoState(row.value) ? twoStateValue(row.value) : String(row.value);
    const last = runs[runs.length - 1];
    if (last && last.key === key) {
      last.end = row.ts;
      last.count++;
    } else {
      runs.push({ key, value: row.value, start: row.ts, end: row.ts, count: 1 });
    }
  }
  return runs;
}

function valueRunsList(history) {
  const ul = document.createElement("ul");
  const fmtState = (v) => (typeof v === "boolean" ? (v ? "on" : "off") : String(v));
  for (const run of valueRuns(history).slice(-20).reverse()) {
    const li = document.createElement("li");
    const from = new Date(run.start * 1000).toLocaleString();
    li.textContent = run.count === 1
      ? `${from} — ${fmtState(run.value)}`
      : `${from} – ${new Date(run.end * 1000).toLocaleTimeString()} — ${fmtState(run.value)} (${run.count} samples)`;
    ul.appendChild(li);
  }
  return ul;
}

function renderHistoryInto(container, view, reload) {
  const { history, unit, hisEnd } = view;
  container.innerHTML = "";
  const heading = document.createElement("h3");
  heading.textContent = "History";
  container.appendChild(heading);

  // Range picker: the window persists across points and refresh ticks.
  const picker = document.createElement("div");
  picker.className = "range-picker";
  for (const [label, seconds] of HISTORY_RANGES) {
    const b = Object.assign(document.createElement("button"), { type: "button", textContent: label });
    b.setAttribute("aria-pressed", String(seconds === historyRange));
    b.addEventListener("click", () => {
      historyRange = seconds;
      reload();
    });
    picker.appendChild(b);
  }
  container.appendChild(picker);

  if (!history.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = hisEnd
      ? `No samples in this window - the last one was ${fmtWhen(hisEnd)}. Pick a longer range.`
      : "No history yet.";
    container.appendChild(p);
    return;
  }

  // A two-state status (Bool, or BACnet's text from before MQTT ingest
  // stored it as a Bool) charts as an off/on step line (Baler#81).
  const twoState = history.every((row) => isTwoState(row.value));
  const numeric = !twoState && history.every((row) => typeof row.value === "number");
  if (!numeric && !twoState) {
    container.appendChild(valueRunsList(history));
    return;
  }
  const series = twoState ? history.map((row) => ({ ts: row.ts, value: twoStateValue(row.value) })) : history;

  const canvas = document.createElement("canvas");
  canvas.width = 640;
  canvas.height = 220;
  container.appendChild(canvas);
  const now = Date.now() / 1000;
  const win = historyRange ? { start: now - historyRange, end: now } : {};
  const opts = { ...win, fullRange: historyFullScale, states: twoState ? ["off", "on"] : undefined };
  const drawn = drawLineChart(canvas, series, 30, twoState ? "" : unit, opts);
  const caption = document.createElement("p");
  caption.className = "hint";
  caption.textContent = twoState
    ? `${history.length.toLocaleString()} samples · on ${Math.round((100 * series.filter((r) => r.value).length) / series.length)}% of them`
    : `${history.length.toLocaleString()} samples${unit ? ` · ${unit}` : " · no unit recorded"}`;
  if (twoState) {
    container.appendChild(caption);
    container.appendChild(valueRunsList(history));
    return;
  }
  if (drawn.clipped || historyFullScale) {
    const fmt = (v) => `${fmtNum(v)}${unit ? ` ${unit}` : ""}`;
    caption.append(
      drawn.clipped
        ? ` · ${drawn.outliers} sample${drawn.outliers === 1 ? "" : "s"} off the scale (▲▼ at the edge; full range ${fmt(drawn.fullMin)} – ${fmt(drawn.fullMax)}) `
        : " · full range ",
    );
    const toggle = Object.assign(document.createElement("button"), {
      type: "button",
      textContent: historyFullScale ? "Fit to typical range" : "Show full range",
    });
    toggle.addEventListener("click", () => {
      historyFullScale = !historyFullScale;
      reload();
    });
    caption.appendChild(toggle);
  }
  container.appendChild(caption);
}

// drawLineChart itself now lives in chart.js (shared with derivations.js) -
// see index.html's script tags for load order.

async function init() {
  await Promise.all([recordRoleKnown(), loadCxStatus()]); // record_edit.js: operators and admins get Edit buttons
  try {
    const { byType, equips } = await loadEquipment();
    statusEl.textContent = byType.size === 0 ? "connected, but no equipment visible yet" : `${byType.size} equipment classes`;
    renderTree(byType);

    const locationSection = await renderLocationSection(equips);
    if (locationSection) treeEl.insertBefore(locationSection, treeEl.firstChild);
    if (canEditRecords()) {
      // parents a new equip can be part of: the locations, then equipment
      const locations = (await haystackRead("Site or Building or Floor or Zone").catch(() => [])).map(entity);
      const bar = Object.assign(document.createElement("p"), { className: "hint" });
      bar.appendChild(recordNewButton("+ New equipment", "equip", null, [...locations, ...equips], () => init()));
      treeEl.insertBefore(bar, treeEl.firstChild);
    }
    revealEquip(params.get("equip"));
  } catch (err) {
    if (err instanceof SessionExpired) return;
    statusEl.textContent = `failed to load from the Haystack API at ${HAYSTACK_API_URL}: ${err.message}`;
  }
}

// ?equip=<uri> (the Connectors page links each connector here, Baler#110):
// open the tree down to that equipment, expand it and put focus on it.
function revealEquip(uri) {
  if (!uri) return;
  const node = [...treeEl.querySelectorAll("details[data-equip]")].find((d) => d.dataset.equip === uri);
  if (!node) {
    statusEl.textContent = `${uri.replace(/^urn:equip:/, "")} isn't in the equipment tree yet - its points appear once they report`;
    return;
  }
  for (let d = node; d; d = d.parentElement && d.parentElement.closest("details")) d.open = true;
  const summary = node.querySelector("summary");
  summary.scrollIntoView({ block: "center" });
  summary.focus();
  summary.classList.add("selected");
}

initAuth(init);
