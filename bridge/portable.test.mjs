import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac, generateKeyPairSync } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { DeliveryStore, httpServer } from './server.mjs';

test('real HTTP and SQLite survive restart, retry a partial failure and deduplicate concurrent deliveries', async t => {
  const folder = await mkdtemp(join(tmpdir(), 'builder-bridge-'));
  const filename = join(folder, 'outbox.sqlite');
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const config = { BRIDGE_ENABLED: 'true', BRIDGE_APP_ID: '12345',
    BRIDGE_APP_PRIVATE_KEY: privateKey.export({ type: 'pkcs1', format: 'pem' }),
    BRIDGE_WEBHOOK_SECRET: 'synthetic-webhook-key', BRIDGE_STATUS_TOKEN: 'synthetic-status-key' };
  const a = 'a'.repeat(40), b = 'b'.repeat(40), id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  const body = JSON.stringify({ repository: { owner: { login: 'AIOSCompanion' },
    name: 'aios-desktop-app', full_name: 'AIOSCompanion/aios-desktop-app', private: true },
    before: '0'.repeat(40), after: b, ref: 'refs/heads/main', commits: [{ id: a }, { id: b }],
    private_message: 'must-not-be-stored' });
  const headers = { 'X-GitHub-Delivery': id, 'X-GitHub-Event': 'push',
    'X-Hub-Signature-256': `sha256=${createHmac('sha256', config.BRIDGE_WEBHOOK_SECRET).update(body).digest('hex')}` };
  const realFetch = globalThis.fetch;
  const seen = []; let failSecond = true;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (!String(url).startsWith('https://api.github.com/')) return realFetch(url, options);
    if (url.endsWith('/installation')) return Response.json({ id: 987 });
    if (url.endsWith('/access_tokens')) {
      assert.deepEqual(JSON.parse(options.body), { repositories: ['companion-builder'], permissions: { actions: 'write' } });
      return Response.json({ token: 'synthetic-token' });
    }
    if (url.endsWith('/test.yml/dispatches')) {
      const payload = JSON.parse(options.body);
      assert.equal(payload.ref, 'main');
      if (payload.inputs.head_sha === b && failSecond) { failSecond = false; return new Response(null, { status: 503 }); }
      seen.push(payload.inputs.head_sha);
      return new Response(null, { status: 204 });
    }
    throw new Error('Unexpected outbound request; real GitHub access forbidden');
  });
  let store = new DeliveryStore(filename), server;
  async function listen() {
    const runtime = httpServer(store, config);
    server = runtime.server;
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    return { url: `http://127.0.0.1:${server.address().port}`, env: runtime.env };
  }
  async function close() {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await store.close();
  }
  try {
    let runtime = await listen();
    const post = () => realFetch(`${runtime.url}/webhook`, { method: 'POST', body, headers });
    const replies = await Promise.all([post(), post()]);
    assert.ok(replies.every(reply => reply.status === 202));
    assert.deepEqual((await Promise.all(replies.map(reply => reply.json()))).map(x => x.duplicate).sort(), [false, true]);
    assert.equal(JSON.stringify(store.storage(id).get('state')).includes('must-not-be-stored'), false);
    await store.tick(runtime.env, Date.now() + 2000);
    assert.deepEqual(seen, [a]);
    assert.equal(store.storage(id).get('state').retries, 1);
    await close();
    store = new DeliveryStore(filename);
    runtime = await listen();
    await store.tick(runtime.env, Date.now() + 3600000);
    assert.deepEqual(seen, [a, b]);
    const status = await realFetch(`${runtime.url}/delivery/${id}`, {
      headers: { Authorization: `Bearer ${config.BRIDGE_STATUS_TOKEN}` },
    });
    assert.equal((await status.json()).complete, true);
    assert.equal((await realFetch(`${runtime.url}/delivery/${id}`)).status, 401);
    const replay = await realFetch(`${runtime.url}/webhook`, { method: 'POST', body, headers });
    assert.equal((await replay.json()).duplicate, true);
    await store.tick(runtime.env, Date.now() + 7200000);
    assert.deepEqual(seen, [a, b]);
    assert.equal((await realFetch(`${runtime.url}/webhook`, { method: 'POST', body })).status, 401);
  } finally { await close(); await rm(folder, { recursive: true, force: true }); }
});

test('SQLite acceptance and alarm roll back together on failure', async () => {
  const store = new DeliveryStore(':memory:');
  const storage = store.storage('synthetic');
  try {
    await assert.rejects(store.exclusive(() => storage.transaction(async tx => {
      tx.put('state', { commits: ['a'.repeat(40)] });
      tx.setAlarm(Date.now());
      throw new Error('synthetic transaction failure');
    })));
    assert.equal(storage.get('state'), undefined);
    assert.equal(store.db.prepare('SELECT count(*) AS n FROM alarms').get().n, 0);
  } finally { await store.close(); }
});
