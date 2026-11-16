// Real workerd and SQLite Durable Object, with GitHub isolated behind a synthetic API.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync } from "node:crypto";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

test("real Worker accepts a signed push and its durable alarm dispatches both commits", { timeout: 45000 }, async () => {
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const dispatched = [];
  const secret = "synthetic-webhook-key";
  const statusToken = "synthetic-status-token";
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    scriptPath: new URL("./worker.mjs", import.meta.url).pathname.replace(/^\/([A-Za-z]:\/)/, "$1"),
    compatibilityDate: "2026-11-16",
    bindings: { BRIDGE_ENABLED: "true", BRIDGE_APP_ID: "12345",
      BRIDGE_APP_PRIVATE_KEY: privateKey.export({ type: "pkcs1", format: "pem" }),
      BRIDGE_WEBHOOK_SECRET: secret, BRIDGE_STATUS_TOKEN: statusToken },
    durableObjects: { DELIVERIES: { className: "Delivery", useSQLite: true } },
    outboundService: async request => {
      const path = new URL(request.url).pathname;
      if (path.endsWith("/installation")) return Response.json({ id: 987 });
      if (path.endsWith("/access_tokens")) {
        assert.deepEqual(await request.json(), { repositories: ["companion-builder"], permissions: { actions: "write" } });
        return Response.json({ token: "synthetic-token" });
      }
      if (path === "/repos/AIOSCompanion/companion-builder/actions/workflows/test.yml/dispatches") {
        dispatched.push(await request.json());
        return new Response(null, { status: 204 });
      }
      throw new Error("Unexpected outbound request; no real GitHub access allowed");
    },
  }));
  try {
    const a = "a".repeat(40), b = "b".repeat(40);
    const id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
    const body = JSON.stringify({ repository: { owner: { login: "AIOSCompanion" },
      name: "aios-desktop-app", full_name: "AIOSCompanion/aios-desktop-app", private: true },
      before: "0".repeat(40), after: b, ref: "refs/heads/main", commits: [{ id: a }, { id: b }] });
    const headers = { "X-GitHub-Delivery": id, "X-GitHub-Event": "push",
      "X-Hub-Signature-256": `sha256=${createHmac("sha256", secret).update(body).digest("hex")}` };
    const response = await mf.dispatchFetch("https://bridge/webhook", { method: "POST", body, headers });
    assert.equal(response.status, 202);
    let complete = false;
    const end = Date.now() + 30000;
    while (Date.now() < end) {
      const status = await mf.dispatchFetch(`https://bridge/delivery/${id}`, {
        headers: { Authorization: `Bearer ${statusToken}` },
      });
      const state = await status.json();
      if (state.complete) { complete = true; assert.equal(state.dispatched, 2); break; }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert.equal(complete, true, "durable alarm did not complete");
    assert.deepEqual(dispatched.map(d => d.inputs.head_sha).sort(), [a, b]);
    assert.ok(dispatched.every(d => d.ref === "main"));
    const replay = await mf.dispatchFetch("https://bridge/webhook", { method: "POST", body, headers });
    assert.equal((await replay.json()).duplicate, true);
    assert.equal(dispatched.length, 2);
    const denied = await mf.dispatchFetch("https://bridge/webhook", { method: "POST", body });
    assert.equal(denied.status, 401);
  } finally { await mf.dispose(); }
});
