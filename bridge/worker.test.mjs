import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, createVerify } from "node:crypto";
import worker, { envelope, verifySignature, appJWT, dispatch, Delivery } from "./worker.mjs";
import { auditDispatchApp } from "./audit-app.mjs";

const a = "a".repeat(40), b = "b".repeat(40), zero = "0".repeat(40);
const repository = { owner: { login: "AIOSCompanion" }, name: "aios-desktop-app",
  full_name: "AIOSCompanion/aios-desktop-app", private: true };
const push = { repository, ref: "refs/heads/main", after: b, before: a, commits: [{ id: a }, { id: b }] };
const delivery = "11111111-2222-3333-4444-555555555555";
const secret = "synthetic-webhook-secret-only";

test("deploy scope audit rejects a private-source-capable or broadly installed bridge App", async () => {
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const env = { BRIDGE_APP_ID: "12345", BRIDGE_APP_PRIVATE_KEY: privateKey.export({ type: "pkcs1", format: "pem" }) };
  let extraPermission = false, extraRepo = false;
  const request = async url => {
    if (url.endsWith("/app")) return Response.json({ permissions: { actions: "write", ...(extraPermission ? { contents: "read" } : {}) } });
    if (url.includes("/app/installations?")) return Response.json([{ id: 987, account: { login: "AIOSCompanion" }, repository_selection: "selected" }]);
    if (url.endsWith("/access_tokens")) return Response.json({ token: "synthetic-token" });
    if (url.includes("/installation/repositories?")) return Response.json({ total_count: extraRepo ? 2 : 1,
      repositories: [{ full_name: "AIOSCompanion/companion-builder" }] });
    throw new Error("unexpected request");
  };
  await auditDispatchApp(env, request);
  extraPermission = true;
  await assert.rejects(auditDispatchApp(env, request));
  extraPermission = false; extraRepo = true;
  await assert.rejects(auditDispatchApp(env, request));
});

test("HMAC validation uses the exact body; rejects tampering and absent signatures", async () => {
  const body = new TextEncoder().encode('synthetic ü');
  const signature = `sha256=${createHmac("sha256", secret).update(body).digest("hex")}`;
  assert.equal(await verifySignature(body, signature, secret), true);
  assert.equal(await verifySignature(new TextEncoder().encode("altered"), signature, secret), false);
  assert.equal(await verifySignature(body, "", secret), false);
  assert.equal(await verifySignature(body, signature, ""), false);
});

test("branch and main pushes retain every commit; only immutable values leave the payload", () => {
  for (const ref of ["refs/heads/main", "refs/heads/feature/test"]) {
    const accepted = envelope("push", { ...push, ref });
    assert.deepEqual(accepted.commits, [a, b]);
    assert.equal("ref" in accepted, false);
    assert.equal("repository" in accepted, false);
  }
  assert.equal(envelope("push", { ...push, deleted: true }), null);
  assert.throws(() => envelope("push", { ...push, after: "main" }));
  assert.equal(envelope("push", { ...push, repository: { ...repository, private: false } }), null);
});

test("only new desktop version tags start releases; moved tags fail closed", () => {
  const tag = { ...push, before: zero, ref: "refs/tags/v1.2.3" };
  assert.equal(envelope("push", tag).tag, "v1.2.3");
  assert.throws(() => envelope("push", { ...tag, before: a }));
  assert.throws(() => envelope("push", { ...tag, forced: true }));
  assert.equal(envelope("push", { ...tag, ref: "refs/tags/main" }), null);
});

test("PR heads run only for the same private repo; forks and closed PRs do not run", () => {
  const pr = { repository, action: "synchronize", pull_request: { head: { sha: a, repo: repository } } };
  assert.deepEqual(envelope("pull_request", pr).commits, [a]);
  assert.equal(envelope("pull_request", { ...pr, action: "closed" }), null);
  assert.equal(envelope("pull_request", { ...pr, pull_request: { head: { sha: a, repo: { full_name: "other/fork" } } } }), null);
});

test("Go commits use public tests for branch and PR events and ignore release tags", async () => {
  const goRepo = { ...repository, name: "companion-engine", full_name: "AIOSCompanion/companion-engine" };
  for (const ref of ["refs/heads/main", "refs/heads/feature/go"]) {
    assert.deepEqual(envelope("push", { ...push, repository: goRepo, ref }).commits, [a, b]);
  }
  assert.equal(envelope("push", { ...push, repository: goRepo, ref: "refs/tags/v1.2.3", before: zero }), null);
  const pr = { repository: goRepo, action: "opened", pull_request: { head: { sha: a, repo: goRepo } } };
  assert.deepEqual(envelope("pull_request", pr).commits, [a]);
  await dispatch({ repo: goRepo.name, sha: b }, "synthetic-token", async (url, options) => {
    assert.match(url, /\/companion-builder\/actions\/workflows\/test\.yml\/dispatches$/);
    assert.deepEqual(JSON.parse(options.body), { ref: "main", inputs: { repository: goRepo.name, head_sha: b } });
    return new Response(null, { status: 204 });
  });
});

test("truncated push payloads require paginated expansion", () => {
  assert.equal(envelope("push", { ...push, commits: Array(2048).fill({ id: a }) }).expand, true);
});

test("GitHub App JWT handles real PKCS1 and PKCS8 keys with short lifetime", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  for (const type of ["pkcs1", "pkcs8"]) {
    const token = await appJWT("12345", privateKey.export({ type, format: "pem" }), 1700000000000);
    const [header, payload, signature] = token.split(".");
    const claims = JSON.parse(Buffer.from(payload, "base64url"));
    assert.equal(claims.iss, "12345");
    assert.equal(claims.exp - claims.iat, 600);
    const verifier = createVerify("RSA-SHA256");
    verifier.update(`${header}.${payload}`);
    assert.equal(verifier.verify(publicKey, Buffer.from(signature, "base64url")), true);
  }
});

test("dispatch targets only the public builder main branch; release carries the event SHA", async () => {
  const calls = [];
  const request = async (url, options) => { calls.push([url, JSON.parse(options.body)]); return new Response(null, { status: 204 }); };
  await dispatch({ repo: repository.name, sha: a }, "synthetic-token", request);
  await dispatch({ repo: repository.name, tag: "v1.2.3", after: b }, "synthetic-token", request);
  assert.ok(calls.every(([url, body]) => url.startsWith("https://api.github.com/repos/AIOSCompanion/companion-builder/") && body.ref === "main"));
  assert.deepEqual(calls[1][1].inputs, { tag: "v1.2.3", expected_sha: b });
  await assert.rejects(dispatch({ repo: "other", sha: a }, "synthetic-token", request));
});

class Storage {
  data = new Map(); alarm = null;
  async get(k) { return structuredClone(this.data.get(k)); }
  async put(k, value) { this.data.set(k, structuredClone(value)); }
  async delete(k) { this.data.delete(k); }
  async setAlarm(t) { this.alarm = t; }
  async transaction(fn) {
    const snapshot = structuredClone(this.data), alarm = this.alarm;
    try { return await fn(this); }
    catch (error) { this.data = snapshot; this.alarm = alarm; throw error; }
  }
  async list({ prefix, limit }) { return new Map([...this.data].filter(([k]) => k.startsWith(prefix)).slice(0, limit)); }
}

test("signed HTTP delivery persists minimal data; replay does not reset completed state", async () => {
  const storage = new Storage();
  const object = new Delivery({ storage, blockConcurrencyWhile: fn => fn() }, {});
  const env = { BRIDGE_ENABLED: "true", BRIDGE_WEBHOOK_SECRET: secret,
    DELIVERIES: { idFromName: id => id, get: () => ({ fetch: (url, init) => object.fetch(new Request(url, init)) }) } };
  const body = JSON.stringify({ ...push, secret_metadata: "must not persist" });
  const headers = { "X-GitHub-Delivery": delivery, "X-GitHub-Event": "push",
    "X-Hub-Signature-256": `sha256=${createHmac("sha256", secret).update(body).digest("hex")}` };
  const send = () => worker.fetch(new Request("https://bridge/webhook", { method: "POST", body, headers }), env);
  assert.equal((await send()).status, 202);
  assert.ok(storage.alarm > 0);
  assert.equal(JSON.stringify([...storage.data]).includes("must not persist"), false);
  await storage.put("state", { done: true, index: 2 });
  assert.equal((await (await send()).json()).duplicate, true);
  assert.equal((await storage.get("state")).done, true);
  assert.equal((await worker.fetch(new Request("https://bridge/webhook", { method: "POST", body }), env)).status, 401);
});

test("disabled bridge cannot accept events or expose status", async () => {
  assert.equal((await worker.fetch(new Request("https://bridge/webhook", { method: "POST" }), {})).status, 503);
});

test("configuration outages retain accepted work and schedule a retry", async () => {
  const storage = new Storage();
  await storage.put("state", { repo: repository.name, commits: [a], index: 0 });
  const object = new Delivery({ storage }, { BRIDGE_ENABLED: "false" });
  await object.alarm();
  assert.equal((await storage.get("state")).retries, 1);
  assert.deepEqual((await storage.get("state")).commits, [a]);
  assert.ok(storage.alarm > Date.now());
});

test("alarm dispatches every commit and resumes after GitHub failure without losing accepted work", async t => {
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const env = { BRIDGE_ENABLED: "true", BRIDGE_APP_ID: "12345",
    BRIDGE_APP_PRIVATE_KEY: privateKey.export({ type: "pkcs1", format: "pem" }) };
  const storage = new Storage();
  const object = new Delivery({ storage, blockConcurrencyWhile: fn => fn() }, env);
  const dispatched = []; let failOnce = true;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.endsWith("/installation")) return Response.json({ id: 987 });
    if (url.endsWith("/access_tokens")) {
      assert.deepEqual(JSON.parse(options.body), { repositories: ["companion-builder"], permissions: { actions: "write" } });
      return Response.json({ token: "synthetic-installation-token" });
    }
    if (url.endsWith("/dispatches")) {
      const sha = JSON.parse(options.body).inputs.head_sha;
      if (sha === b && failOnce) { failOnce = false; return new Response(null, { status: 503 }); }
      dispatched.push(sha);
      return new Response(null, { status: 204 });
    }
    throw new Error("unexpected GitHub API request");
  });
  await object.fetch(new Request("https://delivery/accept", { method: "POST", body: JSON.stringify(envelope("push", push)) }));
  await object.alarm();
  assert.deepEqual(dispatched, [a]);
  assert.equal((await storage.get("state")).done, undefined);
  await object.alarm();
  assert.deepEqual(dispatched, [a, b]);
  assert.equal((await storage.get("state")).done, true);
  await object.alarm();
  assert.deepEqual(dispatched, [a, b]);
});

test("large pushes delegate private history expansion to an approved builder job", async t => {
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const storage = new Storage();
  const env = { BRIDGE_ENABLED: "true", BRIDGE_APP_ID: "12345",
    BRIDGE_APP_PRIVATE_KEY: privateKey.export({ type: "pkcs8", format: "pem" }) };
  const object = new Delivery({ storage }, env);
  const seen = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.endsWith("/installation")) {
      assert.ok(url.includes("/companion-builder/"));
      return Response.json({ id: 987 });
    }
    if (url.endsWith("/access_tokens")) return Response.json({ token: "synthetic-token" });
    if (url.endsWith("/dispatches")) {
      assert.ok(url.endsWith("/catch-up.yml/dispatches"));
      seen.push(JSON.parse(options.body));
      return new Response(null, { status: 204 });
    }
    throw new Error("unexpected request");
  });
  await storage.put("state", { repo: repository.name, commits: [a, b], before: a, after: b,
    expand: true, page: 2, index: 0 });
  await object.alarm();
  assert.deepEqual(seen, [{ ref: "main", inputs: { repository: repository.name, before_sha: a, head_sha: b } }]);
  assert.equal((await storage.get("state")).done, true);
  await object.alarm();
  assert.equal(seen.length, 1);
});
