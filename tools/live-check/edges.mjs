// R6: edge nodes. Every node in expected-edges.json is listed by
// GET /ops/edges, online, and seen within max_last_seen_s. Then, on the node
// that hears device announcements (windrow.announce), the expected devices
// are announced and online, and every connection that names a device
// (device_name) polls the address that device announced. evaluate() is pure
// (edges.test.mjs); the only POSTs are the named read actions the write
// guard lets through.

export const nodeId = (n) => (typeof n === "string" ? (n.includes("/") ? n : `${n}/${n}`) : `${n.site_id}/${n.instance_id}`);

// BACnet device_address is ip, ip:port or ip/prefix:port (windrow.announce._host_of).
export function hostOf(addr) {
  const a = String(addr || "");
  const host = (a.match(/:/g) || []).length === 1 ? a.slice(0, a.lastIndexOf(":")) : a;
  return host.split("/")[0];
}

const ageS = (iso, nowMs) => (iso ? Math.round((nowMs - Date.parse(iso)) / 1000) : null);

// expected: expected-edges.json; nodes: GET /ops/edges; nowMs: the install's
// clock (the response's Date header); announce: {devices, connections:
// {bacnet, modbus}} from the announce node, or {error}.
export function evaluate(expected, nodes, nowMs, announce = null) {
  const checks = [];
  const raw = [];
  const maxAge = expected.max_last_seen_s ?? 120;
  const byId = new Map(nodes.map((n) => [nodeId(n), n]));
  for (const want of expected.nodes || []) {
    const id = nodeId(want);
    const n = byId.get(id);
    const age = n ? ageS(n.last_seen, nowMs) : null;
    let problem = null;
    if (!n) problem = "is not listed by GET /ops/edges";
    else if (!n.online) problem = `is offline${n.error ? ` (${n.error})` : ""}`;
    else if (age == null || age > maxAge) problem = `was last seen ${age == null ? "never" : `${age} s ago`} (limit ${maxAge} s)`;
    checks.push({ module: "edges", name: `node ${id}`, status: problem ? "fail" : "pass",
                  value: n ? `${n.online ? "online" : "offline"}, seen ${age ?? "?"} s ago` : "missing",
                  budget: `online, seen ≤ ${maxAge} s`, detail: problem || `version ${n.version || "?"}` });
    if (problem) {
      raw.push({ kind: "edge-offline", key: id, scope: "edges", title: `edge node ${id} ${problem.split(" (")[0]}`,
                 detail: `${id} ${problem}. Last seen: ${n?.last_seen || "never"}.`,
                 rows: [["Node", id], ["Online", n ? String(!!n.online) : "not listed"], ["Last seen", n?.last_seen || "never"]] });
    }
  }
  const a = expected.announced;
  if (a && announce) {
    const node = nodeId(a.node || "local");
    if (announce.skip) {
      checks.push({ module: "edges", name: `announced devices on ${node}`, status: "skip", detail: announce.skip });
    } else if (announce.error) {
      checks.push({ module: "edges", name: `announced devices on ${node}`, status: "error", detail: announce.error });
    } else {
      const devs = new Map((announce.devices || []).map((d) => [String(d.name).toLowerCase(), d]));
      for (const want of a.devices || []) {
        const d = devs.get(want.toLowerCase());
        const problem = !d ? "has not announced itself" : !d.online ? `announced, but not for a while (last ${d.last_seen || "?"})` : null;
        checks.push({ module: "edges", name: `announced ${want} on ${node}`, status: problem ? "fail" : "pass",
                      value: d ? `${d.address}${d.online ? "" : " (offline)"}` : "missing", detail: problem || `at ${d.address}` });
        if (problem) {
          raw.push({ kind: "announce-missing", key: `${node}|${want}`, scope: "edges", title: `device ${want} ${problem.split(",")[0]} to ${node}`,
                     detail: `${node}'s announced list: ${want} ${problem}` });
        }
      }
      if (a.check_connections !== false) {
        const conns = [...(announce.connections?.bacnet || []).map((c) => ({ ...c, kind: "bacnet", host: hostOf(c.device_address) })),
                       ...(announce.connections?.modbus || []).map((c) => ({ ...c, kind: "modbus" }))]
          .filter((c) => c.device_name);
        for (const c of conns) {
          const d = devs.get(String(c.device_name).toLowerCase());
          if (!d || !d.online) continue;  // nothing to follow: reported above, or by DNS on the node
          const ok = c.host === d.address;
          checks.push({ module: "edges", name: `${c.kind} connection ${c.id} follows ${c.device_name}`, status: ok ? "pass" : "fail",
                        value: c.host, budget: d.address, detail: ok ? "polls the announced address" : `polls ${c.host}, announced ${d.address}` });
          if (!ok) {
            raw.push({ kind: "announce-mismatch", key: `${node}|${c.kind}|${c.id}`, scope: "edges",
                       title: `${c.kind} connection ${c.id} on ${node} doesn't poll ${c.device_name}'s announced address`,
                       detail: `${c.kind} connection ${c.id} (device_name ${c.device_name}) polls ${c.host}; ${c.device_name} announced ${d.address}` });
          }
        }
      }
    }
  }
  return { checks, raw };
}

export async function run({ api, expected }) {
  let nodes;
  let nowMs;
  try {
    const res = await api.get("/ops/edges");
    if (!res.ok) throw new Error(`GET /ops/edges answered ${res.status}`);
    nowMs = Date.parse(res.headers.get("date") || "") || Date.now();
    nodes = await res.json();
  } catch (e) {
    return { scope: "edges", complete: false, raw: [], checks: [{ module: "edges", name: "GET /ops/edges", status: "error", detail: e.message }] };
  }
  let announce = null;
  let complete = true;
  const a = expected.announced;
  if (a) {
    const id = nodeId(a.node || "local");
    const [site, inst] = id.split("/");
    const node = nodes.find((n) => nodeId(n) === id);
    if (!node || !node.online) {
      announce = { skip: `${id} is not online (reported above), so its announced list can't be read` };
    } else {
      try {
        const act = (name) => api.action(site, inst, name);
        const devices = await act("list_announced");
        const connections = a.check_connections === false ? {} : { bacnet: await act("list_connections"), modbus: await act("modbus_list_connections") };
        announce = { devices, connections };
      } catch (e) {
        announce = { error: e.message };
        complete = false;
      }
    }
  }
  return { scope: "edges", complete, ...evaluate(expected, nodes, nowMs, announce) };
}
