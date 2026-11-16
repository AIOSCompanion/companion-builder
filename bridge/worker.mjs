// GitHub App webhook receiver. No product source, commit messages or payload logging.
const OWNER = "AIOSCompanion";
const BUILDER = `${OWNER}/companion-builder`;
const SOURCES = new Set(["aios-desktop-app", "aiosxaegra", "companion-memory"]);
const SHA = /^[0-9a-f]{40}$/;
const VERSION = /^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const ZERO = "0".repeat(40);
const encoder = new TextEncoder();

export async function verifySignature(body, signature, secret) {
  if (!secret || !/^sha256=[0-9a-f]{64}$/.test(signature || "")) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const bytes = Uint8Array.from(signature.slice(7).match(/../g), x => parseInt(x, 16));
  return crypto.subtle.verify("HMAC", key, bytes, body);
}

export function envelope(event, payload) {
  const repo = payload.repository;
  if (repo?.owner?.login !== OWNER || !SOURCES.has(repo.name) ||
      repo.full_name !== `${OWNER}/${repo.name}` || repo.private !== true) return null;
  if (event === "push") {
    if (payload.deleted) return null;
    if (!SHA.test(payload.after) || !SHA.test(payload.before)) throw new Error("invalid push");
    if (payload.ref?.startsWith("refs/tags/")) {
      const tag = payload.ref.slice(10);
      if (repo.name !== "aios-desktop-app" || !VERSION.test(tag)) return null;
      // A moved release tag must not silently create a different release.
      if (payload.before !== ZERO || payload.forced) throw new Error("moved release tag");
      return { repo: repo.name, commits: [], tag, after: payload.after, before: ZERO };
    }
    if (!payload.ref?.startsWith("refs/heads/")) return null;
    if (!Array.isArray(payload.commits)) throw new Error("missing commits");
    const commits = payload.commits.map(c => c.id);
    if (commits.some(c => !SHA.test(c))) throw new Error("invalid commit");
    return { repo: repo.name, commits: [...new Set([...commits, payload.after])],
      after: payload.after, before: payload.before,
      expand: payload.commits.length >= 2048, page: 1 };
  }
  if (event === "pull_request" && ["opened", "reopened", "synchronize", "ready_for_review"].includes(payload.action)) {
    const head = payload.pull_request?.head;
    // Forks never supply code to this pipeline, even with a valid webhook signature.
    if (head?.repo?.full_name !== repo.full_name || !SHA.test(head?.sha || "")) return null;
    return { repo: repo.name, commits: [head.sha], before: ZERO, after: head.sha };
  }
  return null;
}

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function der(tag, bytes) {
  const n = bytes.length;
  const length = n < 128 ? [n] : n < 256 ? [129, n] : [130, n >> 8, n & 255];
  return new Uint8Array([tag, ...length, ...bytes]);
}

export async function appJWT(id, pem, now = Date.now()) {
  if (!/^\d+$/.test(String(id))) throw new Error("app configuration missing");
  let bytes = Uint8Array.from(atob(pem.replace(/-----[^-]+-----|\s/g, "")), c => c.charCodeAt(0));
  // GitHub downloads PKCS#1; WebCrypto imports PKCS#8.
  if (pem.includes("BEGIN RSA PRIVATE KEY")) {
    bytes = der(48, new Uint8Array([2, 1, 0, 48, 13, 6, 9, 42, 134, 72, 134, 247, 13, 1, 1, 1, 5, 0,
      ...der(4, bytes)]));
  }
  const key = await crypto.subtle.importKey("pkcs8", bytes,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const time = Math.floor(now / 1000);
  const data = [base64url(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT" }))),
    base64url(encoder.encode(JSON.stringify({ iat: time - 60, exp: time + 540, iss: String(id) })))].join(".");
  return `${data}.${base64url(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(data)))}`;
}

async function github(path, token, body, request = fetch) {
  const response = await request(`https://api.github.com/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
      "User-Agent": "companion-builder-bridge", "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  // Never propagate GitHub response bodies, which may contain private metadata.
  if (!response.ok) throw new Error(`github status ${response.status}`);
  return response.status === 204 ? null : response.json();
}

export async function installationToken(env, repository, permissions, request = fetch) {
  const jwt = await appJWT(env.BRIDGE_APP_ID, env.BRIDGE_APP_PRIVATE_KEY);
  const installation = await github(`repos/${OWNER}/${repository}/installation`, jwt, undefined, request);
  const result = await github(`app/installations/${installation.id}/access_tokens`, jwt,
    { repositories: [repository], permissions }, request);
  return result.token;
}

export async function dispatch(job, token, request = fetch) {
  if (!SOURCES.has(job.repo)) throw new Error("unknown repository");
  let workflow, inputs;
  if (job.expand) {
    if (!SHA.test(job.before) || !SHA.test(job.after)) throw new Error("invalid catch-up range");
    workflow = "catch-up.yml";
    inputs = { repository: job.repo, before_sha: job.before, head_sha: job.after };
  } else if (job.tag) {
    if (job.repo !== "aios-desktop-app" || !VERSION.test(job.tag) || !SHA.test(job.after)) throw new Error("invalid release");
    workflow = "release.yml";
    inputs = { tag: job.tag, expected_sha: job.after };
  } else {
    if (!SHA.test(job.sha)) throw new Error("invalid test SHA");
    workflow = "test.yml";
    inputs = { repository: job.repo, head_sha: job.sha };
  }
  return github(`repos/${BUILDER}/actions/workflows/${workflow}/dispatches`, token,
    { ref: "main", inputs }, request);
}

export class Delivery {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }

  async fetch(request) {
    // Only the bound Worker can call this object; it has no public route of its own.
    const url = new URL(request.url);
    if (request.method === "GET") {
      const state = await this.ctx.storage.get("state");
      return Response.json(state ? { accepted: true, complete: !!state.done,
        dispatched: state.index || 0, retries: state.retries || 0 } : { accepted: false });
    }
    if (request.method !== "POST" || url.pathname !== "/accept") return new Response(null, { status: 405 });
    return this.ctx.blockConcurrencyWhile(async () => {
      const event = await request.json();
      let duplicate = false;
      await this.ctx.storage.transaction(async tx => {
        const old = await tx.get("state");
        if (old) { duplicate = true; return; }
        // Acceptance and its wakeup commit together; an accepted delivery cannot be orphaned.
        await tx.put("state", { ...event, index: 0, retries: 0 });
        await tx.setAlarm(Date.now() + 1000);
      });
      return Response.json({ accepted: true, duplicate }, { status: 202 });
    });
  }

  async alarm() {
    const storage = this.ctx.storage;
    const state = await storage.get("state");
    if (!state || state.done) return;
    try {
      if (this.env.BRIDGE_ENABLED !== "true") throw new Error("bridge disabled");
      if (state.expand) {
        // The running bridge holds only a builder-only dispatch App credential.
        // Private history expansion happens in an owner-approved GitHub job.
        const token = await installationToken(this.env, "companion-builder", { actions: "write" });
        await dispatch(state, token);
        delete state.commits;
        state.done = true;
        state.index = 1;
        await storage.put("state", state);
        return;
      }
      if (!state.jobsReady) {
        for (const sha of state.commits) await storage.put(`sha:${sha}`, sha);
        delete state.commits;
        state.jobsReady = true;
        await storage.put("state", state);
      }
      const token = await installationToken(this.env, "companion-builder", { actions: "write" });
      if (state.tag) {
        await dispatch(state, token);
        state.index = 1;
      } else {
        const batch = await storage.list({ prefix: "sha:", limit: 4 });
        for (const [key, sha] of batch) {
          await dispatch({ repo: state.repo, sha }, token);
          // Checkpoint each successful dispatch before proceeding to the next commit.
          await storage.delete(key);
          state.index += 1;
          await storage.put("state", state);
        }
        if ((await storage.list({ prefix: "sha:", limit: 1 })).size) {
          await storage.setAlarm(Date.now() + 1000);
          return;
        }
      }
      state.done = true;
      state.retries = 0;
      await storage.put("state", state);
    } catch {
      // Retain the delivery and retry outages/configuration failures, without logging payloads.
      state.retries = (state.retries || 0) + 1;
      await storage.put("state", state);
      await storage.setAlarm(Date.now() + Math.min(3600000, 15000 * 2 ** Math.min(state.retries, 8)));
    }
  }
}

export default {
  async fetch(request, env) {
    if (env.BRIDGE_ENABLED !== "true") return new Response("disabled", { status: 503 });
    const url = new URL(request.url);
    if (url.pathname.startsWith("/delivery/") && request.method === "GET") {
      if (!env.BRIDGE_STATUS_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.BRIDGE_STATUS_TOKEN}`)
        return new Response(null, { status: 401 });
      const id = url.pathname.slice(10);
      if (!/^[0-9a-f-]{36}$/i.test(id)) return new Response(null, { status: 400 });
      return env.DELIVERIES.get(env.DELIVERIES.idFromName(id)).fetch("https://delivery/status");
    }
    if (url.pathname !== "/webhook" || request.method !== "POST") return new Response(null, { status: 404 });
    // Stream with a bound; Content-Length alone is not trustworthy.
    const chunks = []; let length = 0;
    for await (const chunk of request.body || []) {
      length += chunk.byteLength;
      if (length > 25 * 1024 * 1024) return new Response(null, { status: 413 });
      chunks.push(chunk);
    }
    const body = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    if (!await verifySignature(body, request.headers.get("X-Hub-Signature-256"), env.BRIDGE_WEBHOOK_SECRET))
      return new Response(null, { status: 401 });
    const id = request.headers.get("X-GitHub-Delivery");
    if (!/^[0-9a-f-]{36}$/i.test(id || "")) return new Response(null, { status: 400 });
    let accepted;
    try {
      const event = request.headers.get("X-GitHub-Event");
      if (event === "ping") return new Response(null, { status: 204 });
      accepted = envelope(event, JSON.parse(new TextDecoder().decode(body)));
      if (!accepted) return new Response(null, { status: 204 });
    } catch { return new Response(null, { status: 400 }); }
    try {
      return await env.DELIVERIES.get(env.DELIVERIES.idFromName(id)).fetch("https://delivery/accept", {
        method: "POST", body: JSON.stringify(accepted),
      });
    } catch { return new Response(null, { status: 503 }); }
  },
};
