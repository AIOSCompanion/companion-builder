// Portable receiver: local SQLite outbox and loopback HTTP behind the host's TLS proxy.
import { DatabaseSync } from 'node:sqlite';
import { createServer } from 'node:http';
import { Readable } from 'node:stream';
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
import receiver, { Delivery } from './worker.mjs';
import { auditDispatchApp } from './audit-app.mjs';

export class DeliveryStore {
  constructor(filename) {
    this.db = new DatabaseSync(filename);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS kv(delivery TEXT, key TEXT, value TEXT,
        PRIMARY KEY(delivery,key));
      CREATE TABLE IF NOT EXISTS alarms(delivery TEXT PRIMARY KEY, due INTEGER);`);
    this.tail = Promise.resolve();
  }
  exclusive(fn) {
    const next = this.tail.then(fn);
    this.tail = next.catch(() => {});
    return next;
  }
  storage(id) {
    const db = this.db;
    const storage = {
      get(key) {
        const row = db.prepare('SELECT value FROM kv WHERE delivery=? AND key=?').get(id, key);
        return row ? JSON.parse(row.value) : undefined;
      },
      put(key, value) {
        db.prepare('INSERT OR REPLACE INTO kv VALUES(?,?,?)').run(id, key, JSON.stringify(value));
      },
      delete(key) { db.prepare('DELETE FROM kv WHERE delivery=? AND key=?').run(id, key); },
      setAlarm(due) { db.prepare('INSERT OR REPLACE INTO alarms VALUES(?,?)').run(id, due); },
      list({ prefix, limit }) {
        return new Map(db.prepare(`SELECT key,value FROM kv WHERE delivery=?
          AND substr(key,1,?)=? ORDER BY key LIMIT ?`).all(id, prefix.length, prefix, limit)
          .map(row => [row.key, JSON.parse(row.value)]));
      },
      async transaction(fn) {
        db.exec('BEGIN IMMEDIATE');
        try { const result = await fn(storage); db.exec('COMMIT'); return result; }
        catch (error) { db.exec('ROLLBACK'); throw error; }
      },
    };
    return storage;
  }
  bindings(env) {
    return { idFromName: id => id, get: id => ({
      fetch: (url, init) => this.exclusive(() => new Delivery({ storage: this.storage(id),
        blockConcurrencyWhile: fn => fn() }, env).fetch(new Request(url, init))),
    }) };
  }
  async tick(env, now = Date.now()) {
    // Serialize transactions and alarms on the single SQLite connection.
    return this.exclusive(async () => {
      for (const { delivery } of this.db.prepare('SELECT delivery FROM alarms WHERE due<=? ORDER BY due LIMIT 4').all(now)) {
        const storage = this.storage(delivery);
        // Keep the old alarm durable until completion/retry has been checkpointed.
        await new Delivery({ storage }, env).alarm();
        if (storage.get('state')?.done) {
          this.db.prepare('DELETE FROM alarms WHERE delivery=?').run(delivery);
        }
      }
    });
  }
  async close() { await this.tail; this.db.close(); }
}

export function httpServer(store, config) {
  const env = { ...config };
  env.DELIVERIES = store.bindings(env);
  const server = createServer(async (incoming, outgoing) => {
    try {
      const request = new Request(`http://localhost${incoming.url}`, {
        method: incoming.method, headers: incoming.headers,
        ...(['GET', 'HEAD'].includes(incoming.method) ? {} :
          { body: Readable.toWeb(incoming), duplex: 'half' }),
      });
      const response = await receiver.fetch(request, env);
      outgoing.writeHead(response.status, Object.fromEntries(response.headers));
      outgoing.end(Buffer.from(await response.arrayBuffer()));
    } catch { outgoing.writeHead(503); outgoing.end(); }
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 15_000;
  return { server, env };
}

async function main() {
  const env = process.env.BRIDGE_CONFIG ?
    JSON.parse(readFileSync(process.env.BRIDGE_CONFIG, 'utf8')) : process.env;
  for (const name of ['BRIDGE_APP_ID', 'BRIDGE_APP_PRIVATE_KEY', 'BRIDGE_WEBHOOK_SECRET', 'BRIDGE_STATUS_TOKEN', 'BRIDGE_DB']) {
    if (!env[name]) throw new Error('bridge configuration missing');
  }
  if (!['true', 'false'].includes(env.BRIDGE_ENABLED)) throw new Error('enable flag missing');
  await auditDispatchApp(env);
  const store = new DeliveryStore(env.BRIDGE_DB);
  const { server, env: runtime } = httpServer(store, env);
  let ticking = false;
  const timer = setInterval(() => {
    if (ticking) return;
    ticking = true;
    store.tick(runtime).catch(() => {}).finally(() => { ticking = false; });
  }, 1000);
  server.listen(Number(env.BRIDGE_PORT || 8123), '127.0.0.1');
  async function stop() {
    clearInterval(timer);
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await store.close();
  }
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Bridge startup failed; credentials and provider responses withheld'); process.exitCode = 1; });
}
