// Drives the previous Companion app's own update controls, the way a person would: "Update Now" downloads the update, and
// "Restart Now" hands off to the installer. It prints the actions it took and nothing from the page.
// Usage: node bridge_e2e_cdp.mjs <debugging port> <timeout seconds>
// Exit codes: 0 Restart Now was clicked; 3 no page target; 4 the page stopped answering before a restart; 5 timed out.

const port = Number(process.argv[2]);
const timeoutMs = Number(process.argv[3] || 600) * 1000;
const base = `http://127.0.0.1:${port}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const CLICK_SCRIPT = `(() => {
  const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
  const text = (el) => (el.innerText || el.textContent || '').trim();
  const visible = (el) => el.offsetParent !== null && !el.disabled;
  const find = (re) => buttons.find((el) => re.test(text(el)) && visible(el));
  const restart = find(/^Restart Now$/);
  if (restart) { restart.click(); return 'restart'; }
  const update = find(/^Update Now$/);
  if (update) { update.click(); return 'update'; }
  const opener = buttons.find((el) => visible(el) &&
    /update|download/i.test((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')));
  if (opener) { opener.click(); return 'open'; }
  return 'none';
})()`;

async function pageTarget() {
  const list = await (await fetch(`${base}/json/list`)).json();
  return list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl) || null;
}

function connect(url) {
  const ws = new WebSocket(url);
  const pending = new Map();
  let nextId = 0;
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  };
  const opened = new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('websocket failed'));
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    const id = ++nextId;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  return { opened, send, close: () => ws.close() };
}

async function main() {
  const deadline = Date.now() + timeoutMs;
  let target = null;
  while (Date.now() < deadline && !target) {
    try { target = await pageTarget(); } catch { target = null; }
    if (!target) await sleep(1000);
  }
  if (!target) {
    console.log('no page target');
    process.exit(3);
  }
  const cdp = connect(target.webSocketDebuggerUrl);
  await cdp.opened;
  const actions = [];
  let lastOpen = 0;
  while (Date.now() < deadline) {
    let answer;
    try {
      answer = await Promise.race([
        cdp.send('Runtime.evaluate', { expression: CLICK_SCRIPT, returnByValue: true }),
        sleep(5000).then(() => null),
      ]);
    } catch {
      answer = null;
    }
    if (!answer || answer.result === undefined || answer.result.exceptionDetails || !answer.result.result) {
      if (actions.includes('restart')) {
        console.log('the page closed after Restart Now');
        process.exit(0);
      }
      await sleep(2000);
      continue;
    }
    const value = answer.result.result.value;
    if (value === 'update') {
      actions.push('update');
      console.log('clicked Update Now');
    } else if (value === 'restart') {
      console.log('clicked Restart Now');
      process.exit(0);
    } else if (value === 'open' && Date.now() - lastOpen > 10000) {
      lastOpen = Date.now();
      console.log('opened the update popover');
    }
    await sleep(2000);
  }
  console.log(`timed out waiting for Restart Now; actions: ${actions.join(',') || 'none'}`);
  process.exit(5);
}

main().catch((error) => {
  console.log(`driver error: ${error.name}`);
  process.exit(4);
});
