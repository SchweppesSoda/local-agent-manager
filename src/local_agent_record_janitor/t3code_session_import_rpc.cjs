// Qualified local T3 import transport. The token is read on stdin.
'use strict';
const fs = require('node:fs');

const INPUT_LIMIT = 32 * 1024;
const OUTPUT_LIMIT = 1024 * 1024;

function readInput() {
  const buffer = Buffer.alloc(INPUT_LIMIT + 1);
  let size = 0;
  while (size < buffer.length) {
    const count = fs.readSync(0, buffer, size, buffer.length - size, null);
    if (count === 0) break;
    size += count;
  }
  if (size > INPUT_LIMIT) throw Error('request limit exceeded');
  return JSON.parse(buffer.subarray(0, size).toString('utf8'));
}

function validateRequest(request) {
  const origin = new URL(request.origin);
  if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || !origin.port ||
      origin.pathname !== '/' || origin.search || origin.hash || origin.username || origin.password) {
    throw Error('local endpoint required');
  }
  if (typeof request.token !== 'string' || !request.token || request.token.length > 8192 ||
      !request.payload || typeof request.payload !== 'object' || Array.isArray(request.payload)) {
    throw Error('invalid request');
  }
  if (request.tag === 'agentSessions.scan' && Object.keys(request.payload).length === 0) return origin;
  const {projectId, expectedWorkspaceRoot} = request.payload;
  if (request.tag === 'agentSessions.import' && Object.keys(request.payload).length === 2 &&
      typeof projectId === 'string' && projectId && typeof expectedWorkspaceRoot === 'string' &&
      expectedWorkspaceRoot) return origin;
  throw Error('unsupported request');
}

async function fetchTicket(origin, token) {
  const response = await fetch(new URL('/api/auth/websocket-ticket', origin), {
    method: 'POST', headers: {authorization: `Bearer ${token}`}, redirect: 'error',
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok || !response.body) throw Error('ticket rejected');
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.byteLength;
    if (size > INPUT_LIMIT) throw Error('ticket limit exceeded');
    chunks.push(chunk);
  }
  const {ticket} = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (typeof ticket !== 'string' || !ticket || ticket.length > 8192) throw Error('ticket missing');
  return ticket;
}

function projectedReply(tag, reply) {
  if (!reply || typeof reply !== 'object') throw Error('invalid reply');
  if (tag === 'agentSessions.import') {
    return {importedCount: reply.importedCount, skippedCount: reply.skippedCount};
  }
  if (!Array.isArray(reply.candidates)) throw Error('invalid scan');
  return {
    truncated: reply.truncated,
    candidates: reply.candidates.map(({path, projectId, threadCount, sources}) =>
      ({path, projectId, threadCount, sources})),
  };
}

async function main() {
  const request = readInput();
  const origin = validateRequest(request);
  const ticket = await fetchTicket(origin, request.token);
  const url = new URL('/ws', origin);
  url.protocol = 'ws:';
  url.searchParams.set('orchestrationProtocol', '2');
  url.searchParams.set('wsTicket', ticket);
  const socket = new WebSocket(url);
  try {
    const reply = await new Promise((resolve, reject) => {
      socket.addEventListener('open', () => {
        socket.send(JSON.stringify({_tag: 'Request', id: '1', tag: request.tag,
                                   payload: request.payload, headers: []}));
      }, {once: true});
      socket.addEventListener('close', () => reject(Error('connection closed')));
      socket.addEventListener('error', () => reject(Error('connection failed')));
      socket.addEventListener('message', event => {
        try {
          if (typeof event.data !== 'string' || Buffer.byteLength(event.data) > OUTPUT_LIMIT) {
            throw Error('reply limit exceeded');
          }
          const decoded = JSON.parse(event.data);
          for (const item of Array.isArray(decoded) ? decoded : [decoded]) {
            if (String(item.requestId) !== '1' || item._tag !== 'Exit') continue;
            if (item.exit?._tag !== 'Success') throw Error('request rejected');
            resolve(projectedReply(request.tag, item.exit.value));
          }
        } catch (error) {
          reject(error);
        }
      });
    });
    const output = JSON.stringify(reply);
    if (Buffer.byteLength(output) > OUTPUT_LIMIT) throw Error('output limit exceeded');
    process.stdout.write(output);
  } finally {
    socket.close();
  }
}

const timer = setTimeout(() => process.exit(2), 30000);
main().then(() => clearTimeout(timer), () => {
  clearTimeout(timer);
  process.stderr.write('t3code_session_import_rpc_unverified\n');
  process.exitCode = 1;
});
