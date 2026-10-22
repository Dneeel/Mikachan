// MikaSub publication boundary. The existing Mikachan bot builds snapshots in
// bounded batches; this Worker holds owner sources and immutable KV versions.
const LIMIT = 100;
const MAX_BODY = 1_200_000;
const VERSION = /^[a-f0-9]{24}$/;
const HASH = /^[a-f0-9]{64}$/;
const encoder = new TextEncoder();

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", ...extra },
  });
}
function fail(message, status = 400) { return json({ error: message }, status); }
function hex(bytes) { return [...bytes].map(n => n.toString(16).padStart(2, "0")).join(""); }
async function digest(value) { return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)))); }
function token() { return hex(crypto.getRandomValues(new Uint8Array(32))); }
function equal(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
async function body(request, cap = MAX_BODY) {
  if (Number(request.headers.get("content-length") || 0) > cap) throw new Error("request too large");
  const reader = request.body?.getReader();
  if (!reader) return "";
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > cap) { await reader.cancel(); throw new Error("request too large"); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let position = 0;
  for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.length; }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}
async function payload(request, cap) {
  try { return JSON.parse(await body(request, cap)); }
  catch { throw new Error("invalid JSON or oversized request"); }
}
function validSource(raw) {
  if (typeof raw !== "string" || raw.length < 10 || raw.length > 4096 || /[\s\x00-\x1f]/.test(raw)) return false;
  try {
    const url = new URL(raw);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash ||
        !["", "80", "443"].includes(url.port)) return false;
    const host = url.hostname.toLowerCase();
    return !/^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|\[::1\])/.test(host) &&
      !host.endsWith(".local") && !host.endsWith(".internal");
  } catch { return false; }
}
function cleanName(value) {
  if (typeof value !== "string" || !value.trim() || value.length > 80) throw new Error("source name must be 1–80 characters");
  return value.trim();
}
async function audit(db, action, sourceId = null, detail = "") {
  await db.prepare("INSERT INTO mika_audit(at,action,source_id,detail) VALUES (?,?,?,?)")
    .bind(new Date().toISOString(), action, sourceId, detail).run();
}
async function meta(db) {
  return await db.prepare("SELECT * FROM mika_meta WHERE singleton=1").first();
}
async function snapshot(env, version) {
  if (!VERSION.test(version || "")) return null;
  const value = await env.VPN_KV.get(`v/${version}/meta`, "json");
  return value && value.version === version ? value : null;
}
function checkVersion(value) {
  if (!VERSION.test(value || "")) throw new Error("invalid snapshot version");
  return value;
}
function checkBuild(row) {
  if (!row || row.state !== "staging") throw new Error("snapshot build not active");
  return row;
}
async function publicSubscription(request, env, path) {
  const publicToken = path.slice(5);
  if (!/^[a-f0-9]{64}$/.test(publicToken)) return fail("subscription unavailable", 404);
  const info = await meta(env.VPN_DB);
  if (!info?.public_token_hash || !equal(await digest(publicToken), info.public_token_hash))
    return fail("subscription unavailable", 404);
  const format = new URL(request.url).searchParams.get("format") || "base64";
  if (!["base64", "raw"].includes(format)) return fail("unknown subscription format");
  for (const version of [info.current_version, info.previous_version]) {
    if (!version) continue;
    const text = await env.VPN_KV.get(`v/${version}/${format === "raw" ? "raw" : "b64"}`);
    if (text !== null) return new Response(text, { headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Content-Disposition": `attachment; filename="MikaSub${format === "raw" ? ".txt" : ".base64"}"`,
      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      "X-MikaSub-Snapshot": version,
    }});
  }
  return fail("no published snapshot", 503);
}

async function handleAdmin(request, env, path, url) {
  const method = request.method;
  const db = env.VPN_DB;
  if (path === "/api/v1/sources" && method === "GET") {
    const rows = await db.prepare("SELECT * FROM mika_sources ORDER BY id DESC LIMIT 100").all();
    return json({ sources: rows.results || [] });
  }
  if (path === "/api/v1/sources" && method === "POST") {
    const input = await payload(request, 10_000);
    if (!validSource(input.url)) return fail("invalid public subscription URL");
    const name = cleanName(input.name);
    const count = await db.prepare("SELECT COUNT(*) AS n FROM mika_sources").first();
    if (count.n >= 50) return fail("source limit reached", 409);
    const now = new Date().toISOString();
    const inserted = await db.prepare("INSERT INTO mika_sources(name,url,created_at,updated_at) VALUES (?,?,?,?)")
      .bind(name, input.url, now, now).run();
    await audit(db, "source_add", inserted.meta.last_row_id);
    return json({ id: inserted.meta.last_row_id }, 201);
  }
  const sourceMatch = path.match(/^\/api\/v1\/sources\/(\d+)$/);
  if (sourceMatch && method === "PATCH") {
    const id = Number(sourceMatch[1]);
    const row = await db.prepare("SELECT id FROM mika_sources WHERE id=?").bind(id).first();
    if (!row) return fail("source not found", 404);
    const input = await payload(request, 10_000);
    const updates = [];
    const values = [];
    for (const key of ["name", "url", "enabled", "last_ok", "raw_count", "valid_count", "exported_count", "last_error"]) {
      if (!Object.hasOwn(input, key)) continue;
      let value = input[key];
      if (key === "name") value = cleanName(value);
      if (key === "url" && !validSource(value)) return fail("invalid public subscription URL");
      if (key === "enabled") value = value ? 1 : 0;
      if (["raw_count", "valid_count", "exported_count"].includes(key) && (!Number.isInteger(value) || value < 0))
        return fail("invalid count");
      if (key === "last_error") value = String(value || "").slice(0, 120);
      updates.push(`${key}=?`); values.push(value);
    }
    if (!updates.length) return fail("no source changes supplied");
    updates.push("updated_at=?"); values.push(new Date().toISOString(), id);
    await db.prepare(`UPDATE mika_sources SET ${updates.join(",")} WHERE id=?`).bind(...values).run();
    await audit(db, "source_update", id, updates.map(item => item.split("=")[0]).filter(item => item !== "url").join(","));
    return json({ ok: true });
  }
  if (sourceMatch && method === "DELETE") {
    const id = Number(sourceMatch[1]);
    await db.prepare("DELETE FROM mika_sources WHERE id=?").bind(id).run();
    await audit(db, "source_delete", id);
    return json({ ok: true });
  }
  if (path === "/api/v1/node-ids" && method === "POST") {
    const input = await payload(request, 10_000);
    if (!Array.isArray(input.hashes) || input.hashes.length > LIMIT ||
        input.hashes.some(value => typeof value !== "string" || !HASH.test(value))) return fail("invalid node hashes");
    const hashes = [...new Set(input.hashes)];
    if (!hashes.length) return json({ ids: {} });
    const placeholders = hashes.map(() => "(?)").join(",");
    await db.prepare(`INSERT OR IGNORE INTO mika_node_ids(hash) VALUES ${placeholders}`).bind(...hashes).run();
    const found = await db.prepare(`SELECT hash,id FROM mika_node_ids WHERE hash IN (${hashes.map(() => "?").join(",")})`)
      .bind(...hashes).all();
    return json({ ids: Object.fromEntries((found.results || []).map(row => [row.hash, row.id])) });
  }
  if (path === "/api/v1/maintenance/versions" && method === "GET") {
    const cutoff = new Date(Date.now() - 48 * 3600_000).toISOString();
    const stale = new Date(Date.now() - 3600_000).toISOString();
    const found = await db.prepare("SELECT b.version,b.parts FROM mika_builds b JOIN mika_meta m ON m.singleton=1 " +
      "WHERE b.version!=COALESCE(m.current_version,'') AND b.version!=COALESCE(m.previous_version,'') " +
      "AND (b.state='published' AND b.created_at<? OR b.state='staging' AND b.created_at<?) " +
      "ORDER BY b.created_at LIMIT 2").bind(cutoff, stale).all();
    return json({ versions: found.results || [] });
  }
  if (path === "/api/v1/maintenance/prune" && method === "POST") {
    const input = await payload(request, 1000);
    const version = checkVersion(input.version);
    const offset = input.offset;
    if (!Number.isInteger(offset) || offset < 0 || offset > 100) return fail("invalid cleanup offset");
    const current = await meta(db);
    const row = await db.prepare("SELECT version,parts,created_at,state FROM mika_builds WHERE version=?")
      .bind(version).first();
    if (!row || [current.current_version, current.previous_version].includes(version) ||
        Date.parse(row.created_at) > Date.now() - (row.state === "staging" ? 3600_000 : 48 * 3600_000))
      return fail("snapshot still protected", 409);
    const keys = Array.from({ length: row.parts }, (_, part) => `v/${version}/items/${part}`);
    keys.push(`v/${version}/raw`, `v/${version}/b64`, `v/${version}/meta`);
    for (const key of keys.slice(offset, offset + 10)) await env.VPN_KV.delete(key);
    if (offset + 10 >= keys.length) {
      await db.prepare("DELETE FROM mika_build_parts WHERE version=?").bind(version).run();
      await db.prepare("DELETE FROM mika_builds WHERE version=?").bind(version).run();
      await audit(db, "snapshot_prune", null, `version=${version}`);
    }
    return json({ done: offset + 10 >= keys.length, next: offset + 10 });
  }
  if (path === "/api/v1/publication" && method === "GET") {
    const info = await meta(db);
    const published = info.current_version ? await snapshot(env, info.current_version) : null;
    return json({ ready: Boolean(published), version: info.current_version,
      previous_version: info.previous_version, published_at: info.published_at,
      total: published?.total ?? 0, stats: published?.stats ?? null,
      link_available: Boolean(info.public_token_hash) });
  }
  if (path === "/api/v1/public-token/rotate" && method === "POST") {
    const next = token();
    await db.prepare("UPDATE mika_meta SET public_token_hash=? WHERE singleton=1")
      .bind(await digest(next)).run();
    await audit(db, "public_link_rotate");
    return json({ url: `${url.origin}/sub/${next}` });
  }
  if (path === "/api/v1/snapshot/metadata" && method === "GET") {
    const info = await meta(db);
    const version = url.searchParams.get("version") || info.current_version;
    const data = await snapshot(env, version);
    return data ? json(data) : fail("snapshot unavailable", 404);
  }
  if (path === "/api/v1/snapshot/items" && method === "GET") {
    const version = checkVersion(url.searchParams.get("version"));
    const offset = Number(url.searchParams.get("offset") || 0);
    const limit = Number(url.searchParams.get("limit") || 5);
    if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) || limit < 1 || limit > LIMIT)
      return fail("invalid page bounds");
    const info = await snapshot(env, version);
    if (!info) return fail("snapshot unavailable", 404);
    if (offset >= info.total) return json({ version, total: info.total, items: [] });
    const first = Math.floor(offset / LIMIT);
    const last = Math.floor((Math.min(info.total, offset + limit) - 1) / LIMIT);
    const items = [];
    for (let part = first; part <= last; part++) {
      const chunk = await env.VPN_KV.get(`v/${version}/items/${part}`, "json");
      if (!Array.isArray(chunk)) return fail("snapshot propagation pending", 503);
      items.push(...chunk.slice(part === first ? offset % LIMIT : 0,
                                part === last ? (offset + limit - part * LIMIT) : undefined));
    }
    return json({ version, total: info.total, items });
  }
  if (path === "/api/v1/publish/start" && method === "POST") {
    const input = await payload(request, 10_000);
    if (!Number.isInteger(input.total) || input.total < 1 || input.total > 5000 ||
        !Number.isInteger(input.parts) || input.parts !== Math.ceil(input.total / LIMIT) ||
        !Number.isInteger(input.raw_bytes) || input.raw_bytes < 1 || input.raw_bytes > 8_000_000 ||
        !Number.isInteger(input.b64_bytes) || input.b64_bytes < 1 || input.b64_bytes > 11_000_000)
      return fail("snapshot size exceeds free-tier budget");
    const active = await db.prepare("SELECT version FROM mika_builds WHERE state='staging' AND created_at>?")
      .bind(new Date(Date.now() - 900_000).toISOString()).first();
    if (active) return fail("a snapshot build is in progress", 409);
    const version = hex(crypto.getRandomValues(new Uint8Array(12)));
    const now = new Date().toISOString();
    await db.prepare("INSERT INTO mika_builds(version,created_at,total,parts,raw_bytes,b64_bytes,stats) VALUES (?,?,?,?,?,?,?)")
      .bind(version, now, input.total, input.parts, input.raw_bytes, input.b64_bytes,
            JSON.stringify(input.stats || {})).run();
    return json({ version }, 201);
  }
  const buildRoute = path.match(/^\/api\/v1\/publish\/([a-f0-9]{24})\/(items\/(\d+)|raw|b64|commit)$/);
  if (buildRoute && method === "POST") {
    const version = buildRoute[1];
    const row = checkBuild(await db.prepare("SELECT * FROM mika_builds WHERE version=?").bind(version).first());
    const action = buildRoute[2];
    if (action.startsWith("items/")) {
      const part = Number(buildRoute[3]);
      if (part >= row.parts) return fail("part outside snapshot");
      const data = await payload(request, MAX_BODY);
      const expected = Math.min(LIMIT, row.total - part * LIMIT);
      if (!Array.isArray(data) || data.length !== expected || data.some(item =>
          typeof item?.uri !== "string" || item.uri.length > 16384 || !item.uri.includes("://") ||
          typeof item?.protocol !== "string" || typeof item?.name !== "string"))
        return fail("invalid snapshot part");
      const key = `v/${version}/items/${part}`;
      await env.VPN_KV.put(key, JSON.stringify(data));
      if (!await env.VPN_KV.get(key)) return fail("snapshot write not readable", 503);
      await db.prepare("INSERT OR REPLACE INTO mika_build_parts(version,part,count) VALUES (?,?,?)")
        .bind(version, part, expected).run();
      return json({ ok: true });
    }
    if (action === "raw" || action === "b64") {
      const text = await body(request, action === "raw" ? 8_000_000 : 11_000_000);
      const length = encoder.encode(text).length;
      if (length !== row[action === "raw" ? "raw_bytes" : "b64_bytes"])
        return fail("snapshot payload size mismatch");
      const key = `v/${version}/${action}`;
      await env.VPN_KV.put(key, text);
      if (await env.VPN_KV.get(key) === null) return fail("snapshot write not readable", 503);
      await db.prepare(`UPDATE mika_builds SET ${action}_written=1 WHERE version=?`).bind(version).run();
      return json({ ok: true });
    }
    if (action === "commit") {
      const parts = await db.prepare("SELECT COUNT(*) AS n,COALESCE(SUM(count),0) AS total FROM mika_build_parts WHERE version=?")
        .bind(version).first();
      if (parts.n !== row.parts || parts.total !== row.total || !row.raw_written || !row.b64_written)
        return fail("snapshot incomplete", 409);
      const info = { version, total: row.total, parts: row.parts, created_at: row.created_at,
        stats: JSON.parse(row.stats), country_available: false };
      const metaKey = `v/${version}/meta`;
      await env.VPN_KV.put(metaKey, JSON.stringify(info));
      if (!await snapshot(env, version)) return fail("snapshot metadata not readable", 503);
      // The previous version remains available while KV propagates globally.
      await db.prepare("UPDATE mika_meta SET previous_version=current_version,current_version=?,published_at=? WHERE singleton=1")
        .bind(version, new Date().toISOString()).run();
      await db.prepare("UPDATE mika_builds SET state='published' WHERE version=?").bind(version).run();
      await audit(db, "snapshot_publish", null, `version=${version}; count=${row.total}`);
      return json(info);
    }
  }
  return fail("route unavailable", 404);
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      const path = url.pathname;
      if (path === "/healthz" && request.method === "GET") {
        const info = await meta(env.VPN_DB);
        return json({ service: "MikaSub", ready: Boolean(info?.current_version) });
      }
      if (path.startsWith("/sub/") && request.method === "GET")
        return await publicSubscription(request, env, path);
      if (!path.startsWith("/api/v1/")) return fail("route unavailable", 404);
      const provided = request.headers.get("Authorization") || "";
      const expected = env.MIKASUB_ADMIN_TOKEN ? `Bearer ${env.MIKASUB_ADMIN_TOKEN}` : "";
      if (!expected || !equal(provided, expected)) return fail("unauthorized", 401);
      return await handleAdmin(request, env, path, url);
    } catch (error) {
      // Do not reflect source URLs, share tokens or provider response bodies.
      return fail(error instanceof Error && /^(invalid|snapshot|source|subscription|request|a snapshot|no source)/.test(error.message)
        ? error.message : "operation failed", 400);
    }
  },
};

export const testHooks = { validSource, equal, digest, body };
