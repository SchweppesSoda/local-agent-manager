// The qualified T3 Effect RPC wire protocol. Credentials stay on stdin.
'use strict';
const fs = require('node:fs');
async function main() {
  const q = JSON.parse(fs.readFileSync(0, 'utf8'));
  const origin = new URL(q.origin);
  if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.pathname !== '/' || origin.search || origin.hash || origin.username || origin.password)
    throw Error('local endpoint required');
  const ticketResponse = await fetch(new URL('/api/auth/websocket-ticket', origin), {
    method: 'POST', headers: {authorization: `Bearer ${q.token}`}, redirect: 'error',
    signal: AbortSignal.timeout(10000),
  });
  if (!ticketResponse.ok) throw Error('ticket rejected');
  const ticket = await ticketResponse.json();
  if (typeof ticket.ticket !== 'string' || !ticket.ticket) throw Error('ticket missing');
  const url = new URL('/ws', origin);
  url.protocol = 'ws:';
  url.searchParams.set('orchestrationProtocol', '2');
  url.searchParams.set('wsTicket', ticket.ticket);
  const socket = new WebSocket(url);
  const pending = new Map();
  socket.addEventListener('message', event => {
    try {
      const decoded = JSON.parse(event.data);
      for (const item of Array.isArray(decoded) ? decoded : [decoded]) {
        const p = pending.get(String(item.requestId));
        if (!p || item._tag !== 'Exit') continue;
        pending.delete(String(item.requestId));
        if (item.exit?._tag === 'Success') p.resolve(item.exit.value);
        else p.reject(Error('command rejected'));
      }
    } catch { for (const p of pending.values()) p.reject(Error('invalid reply')); }
  });
  socket.addEventListener('close', () => { for (const p of pending.values()) p.reject(Error('connection closed')); });
  try {
    await new Promise((resolve,reject) => {
      socket.addEventListener('open', resolve, {once:true});
      socket.addEventListener('error', () => reject(Error('connection failed')), {once:true});
    });
    let id = 0;
    for (const command of q.commands) {
      const key = String(++id);
      await new Promise((resolve,reject) => {
        pending.set(key,{resolve,reject});
        socket.send(JSON.stringify({_tag:'Request',id:key,tag:'orchestration.dispatchCommand',payload:command,headers:[]}));
      });
    }
    process.stdout.write('true');
  } finally { socket.close(); }
}
const timer = setTimeout(() => process.exit(2), 25000);
main().then(() => clearTimeout(timer), () => { clearTimeout(timer); process.stderr.write('t3code_rpc_unverified\n'); process.exit(1); });
