// Зависшая вкладка (рендерер не отвечает) блокирует connectOverCDP навсегда: Playwright при подключении
// подписывается на все цели и ждёт каждую. Перед подключением пингуем вкладки по их websocket
// (Runtime.evaluate) и закрываем те, что молчат дольше ms. Живые вкладки и полузаполненные формы не трогаем.

function ping(wsUrl, ms) {
  return new Promise((resolve) => {
    let ws;
    const done = (ok) => { clearTimeout(t); try { ws?.close(); } catch {} resolve(ok); };
    const t = setTimeout(() => done(false), ms);
    try { ws = new WebSocket(wsUrl); } catch { return done(false); }
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: '1' } }));
    ws.onmessage = (e) => { try { if (JSON.parse(e.data).id === 1) done(true); } catch {} };
    ws.onerror = () => done(false);
  });
}

// -> список закрытых URL
export async function closeHungTabs(port, ms = 4000) {
  const base = `http://127.0.0.1:${port}`;
  let list;
  try {
    list = await (await fetch(`${base}/json/list`, { signal: AbortSignal.timeout(5000) })).json();
  } catch { return []; }
  const pages = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  const alive = await Promise.all(pages.map((t) => ping(t.webSocketDebuggerUrl, ms)));
  const closed = [];
  for (const [i, t] of pages.entries()) {
    if (alive[i]) continue;
    try { await fetch(`${base}/json/close/${t.id}`, { signal: AbortSignal.timeout(5000) }); closed.push(t.url); } catch {}
  }
  if (closed.length) console.error(`WARN: закрыл зависшие вкладки: ${closed.map((u) => u.slice(0, 80)).join(', ')}`);
  return closed;
}
