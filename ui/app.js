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

// Haystack equipment-type markers, most specific first, for grouping equips
// that carry Haystack tags but no Brick class (e.g. a Windrow/Haystack
// import that was never Brick-classified).
const EQUIP_KIND_TAGS = [
  ["ahu", "AHU"], ["rtu", "RTU"], ["mau", "MAU"], ["doas", "DOAS"], ["fcu", "FCU"], ["vav", "VAV"],
  ["chiller", "Chiller"], ["boiler", "Boiler"], ["coolingTower", "Cooling Tower"],
  ["heatExchanger", "Heat Exchanger"], ["pump", "Pump"], ["fan", "Fan"], ["meter", "Meter"],
  ["tank", "Tank"], ["valve", "Valve"], ["damper", "Damper"], ["vfd", "VFD"],
];

const isMarker = (v) => v && v._kind === "marker";

// Brick class if the graph has one, else the Haystack equip-type marker.
function equipKind(rec) {
  if (typeof rec.brickClass === "string") return rec.brickClass;
  const hit = EQUIP_KIND_TAGS.find(([tag]) => isMarker(rec[tag]));
  return hit ? hit[1] : null;
}

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
    const label = equipKind(e.rec) || "Unclassified";
    if (!byType.has(label)) byType.set(label, []);
    byType.get(label).push(e);
  }
  return { byType, equips };
}

async function loadPoints(equip) {
  const rows = await haystackRead(`point and equipRef==@${equip.id}`);
  const points = rows.map(entity).sort((a, b) => a.dis.localeCompare(b.dis, undefined, { sensitivity: "base" }) || a.uri.localeCompare(b.uri));
  // Two points with the same dis (e.g. computed ones) - tell them apart.
  const seen = new Map();
  for (const p of points) seen.set(p.dis, (seen.get(p.dis) || 0) + 1);
  for (const p of points) if (seen.get(p.dis) > 1) p.dis = `${p.dis} (${p.uri.split(/[/:]/).slice(-2).join("/")})`;
  return points;
}

// Stored values are often float32 (61.02000045776367): show what a person
// would read off the controller.
const fmtNum = (v) => (typeof v === "number" ? String(+v.toFixed(2)) : String(v));

function tagText(name, val) {
  if (val && typeof val === "object" && !Array.isArray(val)) {
    if (val._kind === "marker") return name;
    if (val._kind === "number") return `${name}: ${fmtNum(val.val)}${val.unit ? ` ${val.unit}` : ""}`;
    if (val._kind === "ref") return `${name}: ${val.dis || localName(refToUri(val.val))}`;
    if ("val" in val) return `${name}: ${val.val}`;
  }
  return `${name}: ${typeof val === "object" ? JSON.stringify(val) : val}`;
}

function pointTags(rec) {
  return Object.keys(rec)
    .filter((k) => !STRUCTURAL_TAGS.has(k))
    .sort()
    .map((k) => tagText(k, rec[k]));
}

async function loadHistory(topic) {
  const url = `${INGEST_API_URL}/history?point=${encodeURIComponent(topic)}`;
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
function hisLink(uri, text = "📈") {
  const a = document.createElement("a");
  a.className = "his-link";
  a.href = `his.html?expr=${encodeURIComponent(`readAll(id==@${uri}).hisRead(today)`)}`;
  a.title = "Chart every point of this equipment for today";
  a.textContent = text;
  // Inside a <summary>, a click would also toggle the <details> - harmless
  // since we're navigating away, but don't let the tree react to it.
  a.addEventListener("click", (ev) => ev.stopPropagation());
  return a;
}

function buildEquipNode(equip, label) {
  const equipDetails = document.createElement("details");
  const equipSummary = document.createElement("summary");
  equipSummary.textContent = `${label} `;
  equipSummary.appendChild(hisLink(equip.uri));
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

  const root = node("📍 Site");
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
          for (const ahu of ahus) floorList.appendChild(li(buildEquipNode(ahu, `${ahu.dis} (${equipKind(ahu.rec) || "AHU"})`)));
          for (const zone of parts(child, "Zone")) {
            const zoneNode = node(zone.dis);
            const zoneList = document.createElement("ul");
            for (const vav of fedBy.get(zone.id) || []) {
              zoneList.appendChild(li(buildEquipNode(vav, `${vav.dis} (${equipKind(vav.rec) || "VAV"})`)));
            }
            zoneNode.appendChild(zoneList);
            floorList.appendChild(li(zoneNode));
          }
          floorNode.appendChild(floorList);
          buildingList.appendChild(li(floorNode));
        } else if (child.rec.equip) {
          const kind = equipKind(child.rec);
          const label = kind ? `${child.dis} (${kind})` : child.dis;
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
  const history = historyKey ? await loadHistory(historyKey).catch(() => []) : [];

  detailEl.innerHTML = "";
  const h2 = document.createElement("h2");
  h2.textContent = point.dis;
  detailEl.appendChild(h2);

  if (point.brickClass) {
    const p = document.createElement("p");
    p.textContent = `Brick class: ${point.brickClass}`;
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
  const unit = typeof point.rec.unit === "string" ? point.rec.unit : "";
  renderHistoryInto(historyContainer, history, unit);

  if (historyKey) {
    refreshTimer = setInterval(async () => {
      const fresh = await loadHistory(historyKey).catch(() => null);
      if (fresh) renderHistoryInto(historyContainer, fresh, unit);
    }, REFRESH_INTERVAL_MS);
  }
}

function renderHistoryInto(container, history, unit = "") {
  container.innerHTML = "";
  const heading = document.createElement("h3");
  heading.textContent = "History";
  container.appendChild(heading);

  if (!history.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "No history yet.";
    container.appendChild(p);
    return;
  }

  const numeric = history.every((row) => typeof row.value === "number");
  if (!numeric) {
    const ul = document.createElement("ul");
    for (const row of history.slice(-20)) {
      const li = document.createElement("li");
      li.textContent = `${new Date(row.ts * 1000).toLocaleString()} — ${row.value}`;
      ul.appendChild(li);
    }
    container.appendChild(ul);
    return;
  }

  const canvas = document.createElement("canvas");
  canvas.width = 640;
  canvas.height = 220;
  container.appendChild(canvas);
  drawLineChart(canvas, history, 30, unit);
}

// drawLineChart itself now lives in chart.js (shared with derivations.js) -
// see index.html's script tags for load order.

async function init() {
  try {
    const { byType, equips } = await loadEquipment();
    statusEl.textContent = byType.size === 0 ? "connected, but no equipment visible yet" : `${byType.size} equipment classes`;
    renderTree(byType);

    const locationSection = await renderLocationSection(equips);
    if (locationSection) treeEl.insertBefore(locationSection, treeEl.firstChild);
  } catch (err) {
    if (err instanceof SessionExpired) return;
    statusEl.textContent = `failed to load from the Haystack API at ${HAYSTACK_API_URL}: ${err.message}`;
  }
}

initAuth(init);
