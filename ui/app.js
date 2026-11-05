// No build step, no framework - direct SPARQL to Oxigraph (CORS-open,
// see docker-compose.yml's `--cors` flag) for the equipment/point tree,
// and fetch() against ingest_api.py's GET /history for chart data (also
// CORS-open already, via its end_headers() override).
const params = new URLSearchParams(location.search);
const OXIGRAPH_URL = params.get("oxigraph") || "http://localhost:7878/query";
// Relative by default so this works as served by the gateway at /ui/ (same
// origin, so /ingest/... proxies through it - see gateway/nginx.conf).
// Override with ?ingest=... when serving ui/ standalone (ui_server.py).
const INGEST_API_URL = params.get("ingest") || "/ingest";

const BRICK = "https://brickschema.org/schema/Brick#";
const TD = "urn:baler:td#";
const HAYSTACK = "urn:baler:haystack#";

const statusEl = document.getElementById("status");
const treeEl = document.getElementById("tree");
const detailEl = document.getElementById("detail");

// Gateway auth (see gateway/njs/policy.js, ui/auth.js) - only routes
// proxied through the gateway need a Bearer token; the direct-to-Oxigraph
// SPARQL calls below are unauthenticated (CORS-open, see
// docker-compose.yml's `--cors` flag) and untouched by any of this.

async function sparql(query) {
  const res = await fetch(OXIGRAPH_URL, {
    method: "POST",
    headers: { "Content-Type": "application/sparql-query", Accept: "application/sparql-results+json" },
    body: query,
  });
  if (!res.ok) throw new Error(`SPARQL query failed: ${res.status}`);
  const body = await res.json();
  return body.results.bindings;
}

function localName(uri) {
  // urn:location:site-1 has no "/" or "#" at all - only colons - so those
  // two alone left the whole URI un-trimmed for location URIs. ":" is a
  // safe third separator here: every other URI scheme in this app
  // (urn:point:.../..., urn:equip:.../..., https://.../...#...) always has
  // a later "/" or "#" that still wins the max().
  const i = Math.max(uri.lastIndexOf("#"), uri.lastIndexOf("/"), uri.lastIndexOf(":"));
  return uri.slice(i + 1);
}

async function loadEquipment() {
  const rows = await sparql(`
    PREFIX brick: <${BRICK}>
    SELECT DISTINCT ?equip ?type WHERE {
      ?equip brick:hasPoint ?p .
      OPTIONAL { ?equip a ?type }
    }
  `);
  const byEquip = new Map();
  for (const row of rows) {
    const equip = row.equip.value;
    const type = row.type ? localName(row.type.value) : null;
    if (!byEquip.has(equip)) byEquip.set(equip, new Set());
    if (type) byEquip.get(equip).add(type);
  }

  const byType = new Map();
  for (const [equip, types] of byEquip) {
    const label = types.size ? [...types].sort().join(", ") : "Unclassified";
    if (!byType.has(label)) byType.set(label, []);
    byType.get(label).push(equip);
  }
  return byType;
}

async function loadPoints(equipUri) {
  const rows = await sparql(`
    PREFIX brick: <${BRICK}>
    PREFIX td: <${TD}>
    SELECT ?point ?type ?topic WHERE {
      <${equipUri}> brick:hasPoint ?point .
      OPTIONAL { ?point a ?type }
      OPTIONAL { ?point td:sourceTopic ?topic }
    }
  `);
  const byPoint = new Map();
  for (const row of rows) {
    const point = row.point.value;
    if (!byPoint.has(point)) {
      byPoint.set(point, { uri: point, topic: row.topic ? row.topic.value : null, types: new Set() });
    }
    if (row.type) byPoint.get(point).types.add(localName(row.type.value));
  }
  return [...byPoint.values()].sort((a, b) => (a.topic || a.uri).localeCompare(b.topic || b.uri));
}

async function loadTags(pointUri) {
  const rows = await sparql(`
    PREFIX haystack: <${HAYSTACK}>
    SELECT ?tag WHERE { <${pointUri}> haystack:hasTag ?tag }
  `);
  return rows.map((r) => r.tag.value).sort();
}

async function loadHistory(topic) {
  const url = `${INGEST_API_URL}/history?point=${encodeURIComponent(topic)}`;
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("session expired");
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

function buildEquipNode(equipUri, label) {
  const equipDetails = document.createElement("details");
  const equipSummary = document.createElement("summary");
  equipSummary.textContent = `${label} `;
  equipSummary.appendChild(hisLink(equipUri));
  equipDetails.appendChild(equipSummary);

  const pointList = document.createElement("ul");
  equipDetails.appendChild(pointList);
  equipDetails.addEventListener(
    "toggle",
    async () => {
      if (!equipDetails.open || pointList.dataset.loaded) return;
      pointList.dataset.loaded = "1";
      const points = await loadPoints(equipUri);
      for (const point of points) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        const pointLabel = point.topic ? point.topic.split("/").pop() : localName(point.uri);
        btn.textContent = point.types.size ? `${pointLabel} — ${[...point.types].join(", ")}` : pointLabel;
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
  for (const [typeLabel, equipUris] of [...byType.entries()].sort()) {
    const details = document.createElement("details");
    details.open = false;
    const summary = document.createElement("summary");
    summary.textContent = `${typeLabel} (${equipUris.length})`;
    details.appendChild(summary);

    for (const equipUri of equipUris.sort()) {
      details.appendChild(buildEquipNode(equipUri, localName(equipUri).replace(/^windrow\//, "")));
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
async function loadHasPart(uri) {
  const rows = await sparql(`
    PREFIX brick: <${BRICK}>
    SELECT ?child ?type WHERE {
      <${uri}> brick:hasPart ?child .
      OPTIONAL { ?child a ?type }
    }
  `);
  const byChild = new Map();
  for (const row of rows) {
    const child = row.child.value;
    if (!byChild.has(child)) byChild.set(child, new Set());
    if (row.type) byChild.get(child).add(localName(row.type.value));
  }
  return [...byChild.entries()].map(([uri, types]) => ({ uri, types }));
}

async function loadFeeds(uri) {
  const rows = await sparql(`
    PREFIX brick: <${BRICK}>
    SELECT ?equip WHERE { ?equip brick:feeds <${uri}> }
  `);
  return rows.map((r) => r.equip.value);
}

async function renderLocationSection() {
  const siteRows = await sparql(`PREFIX brick: <${BRICK}> SELECT ?site WHERE { ?site a brick:Site }`);
  if (!siteRows.length) return null;

  const root = document.createElement("details");
  const rootSummary = document.createElement("summary");
  rootSummary.textContent = "📍 Site";
  root.appendChild(rootSummary);

  for (const siteRow of siteRows) {
    const siteUri = siteRow.site.value;
    const siteNode = document.createElement("details");
    siteNode.appendChild(Object.assign(document.createElement("summary"), { textContent: localName(siteUri) }));
    const siteList = document.createElement("ul");

    for (const child of await loadHasPart(siteUri)) {
      if (!child.types.has("Building")) continue;
      const buildingLi = document.createElement("li");
      const buildingNode = document.createElement("details");
      buildingNode.appendChild(
        Object.assign(document.createElement("summary"), { textContent: localName(child.uri).replace(/^site\//, "") }),
      );
      const buildingList = document.createElement("ul");

      for (const bChild of await loadHasPart(child.uri)) {
        if (bChild.types.has("Floor")) {
          const floorLi = document.createElement("li");
          const floorNode = document.createElement("details");
          const ahuUris = await loadFeeds(bChild.uri);
          floorNode.appendChild(
            Object.assign(document.createElement("summary"), {
              textContent: `${localName(bChild.uri)} (fed by ${ahuUris.map(localName).join(", ") || "?"})`,
            }),
          );
          const floorList = document.createElement("ul");
          for (const ahuUri of ahuUris) {
            const ahuLi = document.createElement("li");
            ahuLi.appendChild(buildEquipNode(ahuUri, `${localName(ahuUri)} (AHU)`));
            floorList.appendChild(ahuLi);
          }
          for (const zone of await loadHasPart(bChild.uri)) {
            if (!zone.types.has("Zone")) continue;
            const zoneLi = document.createElement("li");
            const zoneNode = document.createElement("details");
            zoneNode.appendChild(Object.assign(document.createElement("summary"), { textContent: localName(zone.uri) }));
            const zoneList = document.createElement("ul");
            for (const vavUri of await loadFeeds(zone.uri)) {
              const li = document.createElement("li");
              li.appendChild(buildEquipNode(vavUri, `${localName(vavUri)} (VAV)`));
              zoneList.appendChild(li);
            }
            zoneNode.appendChild(zoneList);
            zoneLi.appendChild(zoneNode);
            floorList.appendChild(zoneLi);
          }
          floorNode.appendChild(floorList);
          floorLi.appendChild(floorNode);
          buildingList.appendChild(floorLi);
        } else {
          const li = document.createElement("li");
          const label = `${localName(bChild.uri).replace(/^windrow\//, "")}${bChild.types.size ? ` (${[...bChild.types].join(", ")})` : ""}`;
          li.appendChild(buildEquipNode(bChild.uri, label));
          buildingList.appendChild(li);
        }
      }
      buildingNode.appendChild(buildingList);
      buildingLi.appendChild(buildingNode);
      siteList.appendChild(buildingLi);
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

  detailEl.innerHTML = `<h2>${point.topic || localName(point.uri)}</h2><p class="hint">loading…</p>`;

  // /history?point=X reconstructs urn:point:X server-side (see
  // ingest.topic_to_point_uri) - that works identically for a raw
  // MQTT-sourced point (X = its topic, also its td:sourceTopic) and a
  // derivation_engine-computed point (X = "computed/<id>/<name>", which
  // never gets a td:sourceTopic - see derivation_engine._output_uri).
  // Deriving X straight from the URI covers both without caring which
  // kind of point this is.
  const historyKey = point.uri.startsWith("urn:point:") ? point.uri.slice("urn:point:".length) : null;

  const [tags, history] = await Promise.all([
    loadTags(point.uri),
    historyKey ? loadHistory(historyKey).catch(() => []) : Promise.resolve([]),
  ]);

  detailEl.innerHTML = "";
  const h2 = document.createElement("h2");
  h2.textContent = point.topic || localName(point.uri);
  detailEl.appendChild(h2);

  if (point.types.size) {
    const p = document.createElement("p");
    p.textContent = `Brick class: ${[...point.types].join(", ")}`;
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
  renderHistoryInto(historyContainer, history);

  if (historyKey) {
    refreshTimer = setInterval(async () => {
      const fresh = await loadHistory(historyKey).catch(() => null);
      if (fresh) renderHistoryInto(historyContainer, fresh);
    }, REFRESH_INTERVAL_MS);
  }
}

function renderHistoryInto(container, history) {
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
  drawLineChart(canvas, history);
}

// drawLineChart itself now lives in chart.js (shared with derivations.js) -
// see index.html's script tags for load order.

async function init() {
  try {
    const byType = await loadEquipment();
    if (byType.size === 0) {
      statusEl.textContent = "connected, but no equipment ingested yet";
    } else {
      statusEl.textContent = `${byType.size} equipment classes`;
    }
    renderTree(byType);

    const locationSection = await renderLocationSection();
    if (locationSection) treeEl.insertBefore(locationSection, treeEl.firstChild);
  } catch (err) {
    statusEl.textContent = `failed to reach Oxigraph at ${OXIGRAPH_URL}: ${err.message}`;
  }
}

initAuth(init);
