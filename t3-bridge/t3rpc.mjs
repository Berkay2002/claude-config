#!/usr/bin/env node
// Small WebSocket RPC client for a T3 Code server (Effect RPC, JSON frames, no dependencies).
//
//   t3rpc.mjs --url http://127.0.0.1:PORT --pair-url '<pairing URL>' [--session-file F] <command> ...
//   commands: smoke | items | respond | send | call (see usage() below)
//
// Protocol, confirmed from the T3 source (packages/contracts, apps/server/src/ws.ts):
//  1. POST /api/auth/browser-session {credential: <pairing token>} -> Set-Cookie session
//  2. POST /api/auth/websocket-ticket/rpc (cookie) -> {ticket}
//  3. WebSocket /ws?orchestrationProtocol=2&wsTicket=<ticket>; one JSON text frame per message:
//     out {_tag:"Request",id,tag,payload,headers:[]} | {_tag:"Ack",requestId} | {_tag:"Interrupt",requestId,interruptors:[]} | {_tag:"Pong"}
//     in  {_tag:"Chunk",requestId,values:[..]} | {_tag:"Exit",requestId,exit:{_tag:"Success",value}|{_tag:"Failure",cause}} | {_tag:"Ping"}
// A pairing token is single use, so --session-file keeps the session cookie for later invocations.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PROTOCOL = 2; // ORCHESTRATION_PROTOCOL_VERSION, packages/contracts/src/environment.ts
const TERMINAL = new Set(["completed", "interrupted", "failed", "cancelled", "rolled_back"]);
const id = () => randomUUID();

export function pairingToken(pairUrl) {
  try {
    const u = new URL(pairUrl);
    const t = new URLSearchParams(u.hash.replace(/^#/, "")).get("token") ?? u.searchParams.get("token");
    if (t) return t;
  } catch {}
  return pairUrl; // a bare token
}

async function post(url, body, cookie) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", ...(cookie ? { cookie } : {}) },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error(`POST ${new URL(url).pathname} -> ${res.status} ${(await res.text()).slice(0, 300)}`);
  return res;
}

/** Returns the one-time WebSocket ticket; reads or writes the session cookie in sessionFile when given. */
export async function authenticate({ url, pairUrl, sessionFile }) {
  let cookie = sessionFile && fs.existsSync(sessionFile) ? fs.readFileSync(sessionFile, "utf8").trim() : "";
  if (!cookie) {
    if (!pairUrl) throw new Error("need --pair-url (no saved session)");
    const res = await post(`${url}/api/auth/browser-session`, { credential: pairingToken(pairUrl) });
    cookie = res.headers.getSetCookie().map((c) => c.split(";")[0]).join("; ");
    if (!cookie) throw new Error("browser-session returned no cookie");
    if (sessionFile) fs.writeFileSync(sessionFile, cookie + "\n", { mode: 0o600 });
  }
  const res = await post(`${url}/api/auth/websocket-ticket/rpc`, {}, cookie);
  return (await res.json()).ticket;
}

/** Opens the RPC socket. call() resolves with the Exit value; stream() resolves when `onChunk` returns true. */
export async function connect(opts) {
  const ticket = await authenticate(opts);
  const wsUrl = new URL(opts.url);
  wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
  wsUrl.pathname = "/ws";
  wsUrl.search = new URLSearchParams({ orchestrationProtocol: String(PROTOCOL), wsTicket: ticket }).toString();
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error(`websocket connect failed: ${wsUrl.origin}/ws`)), { once: true });
  });
  const send = (m) => ws.send(JSON.stringify(m));
  const reqs = new Map();
  let n = 0;
  ws.addEventListener("message", (ev) => {
    for (const m of [].concat(JSON.parse(ev.data))) {
      if (m._tag === "Ping") send({ _tag: "Pong" });
      const r = reqs.get(m.requestId);
      if (!r) continue;
      if (m._tag === "Chunk") {
        send({ _tag: "Ack", requestId: m.requestId });
        if (r.onChunk?.(m.values)) {
          reqs.delete(m.requestId);
          send({ _tag: "Interrupt", requestId: m.requestId, interruptors: [] });
          r.resolve(undefined);
        }
      } else if (m._tag === "Exit") {
        reqs.delete(m.requestId);
        if (m.exit._tag === "Success") r.resolve(m.exit.value);
        else r.reject(new Error(`${r.tag} failed: ${JSON.stringify(m.exit.cause).slice(0, 600)}`));
      }
    }
  });
  ws.addEventListener("close", () => {
    for (const r of reqs.values()) r.reject(new Error("websocket closed"));
    reqs.clear();
  });
  const call = (tag, payload = {}, onChunk) =>
    new Promise((resolve, reject) => {
      const rid = String(++n);
      reqs.set(rid, { tag, resolve, reject, onChunk });
      send({ _tag: "Request", id: rid, tag, payload, headers: [] });
    });
  return { call, stream: call, close: () => ws.close() };
}

// ---- commands (names and payloads from packages/contracts/src/orchestrationV2.ts and project.ts) ----

const dispatch = (c, command) => c.call("orchestration.dispatchCommand", command);
const projection = (c, threadId) => c.call("orchestration.getThreadProjection", { threadId });

async function findOrCreateProject(c, dir) {
  const root = path.resolve(dir);
  let projects = [];
  await c.stream("orchestration.subscribeShell", {}, (vs) => {
    const snap = vs.find((v) => v.kind === "snapshot");
    if (snap) projects = snap.snapshot.projects;
    return Boolean(snap);
  });
  const hit = projects.find((p) => p.workspaceRoot === root && !p.archivedAt);
  if (hit) return hit.id;
  const projectId = id();
  await c.call("projects.mutate", {
    type: "project.create", commandId: id(), projectId,
    title: path.basename(root) || root, workspaceRoot: root, createWorkspaceRootIfMissing: true,
  });
  return projectId;
}

const lastRun = (p) => [...p.runs].sort((a, b) => a.ordinal - b.ordinal).at(-1);
const isRunActive = (p) => p.runs.some((r) => !TERMINAL.has(r.status));
const pending = (p) => p.runtimeRequests.filter((r) => r.status === "pending");

/** Polls until `minRuns` runs exist and all are terminal; returns the final projection (with .timedOut). */
async function waitIdle(c, threadId, minRuns, timeoutS) {
  const end = Date.now() + timeoutS * 1000;
  for (;;) {
    const p = await projection(c, threadId);
    if (p.runs.length >= minRuns && !isRunActive(p)) return p;
    if (Date.now() > end) return Object.assign(p, { timedOut: true });
    await new Promise((r) => setTimeout(r, 1000));
  }
}

const itemLine = (i) => {
  const o = { type: i.type, status: i.status, id: i.id, title: i.title };
  for (const k of ["text", "prompt", "result", "input", "output", "fileName", "requestId", "requestKind", "childThreadId", "subagentId", "toolName"])
    if (i[k] !== undefined && i[k] !== null) o[k] = i[k];
  if (i.questions) o.questions = i.questions;
  return o;
};
const printItems = (p) => {
  for (const i of p.turnItems) console.log(JSON.stringify(itemLine(i)));
  for (const r of pending(p)) console.log(JSON.stringify({ type: "pending_request", id: r.id, kind: r.kind, status: r.status, responseCapability: r.responseCapability?.type }));
};
const summary = (p, threadId) => ({
  threadId,
  itemTypes: [...new Set(p.turnItems.map((i) => i.type))],
  runStatus: lastRun(p)?.status ?? null,
  pendingRequests: pending(p).map((r) => ({ id: r.id, kind: r.kind })),
  ...(p.timedOut ? { timedOut: true } : {}),
});

async function main(argv) {
  const [cmd, ...rest] = argv._;
  const a = argv;
  const need = (k) => { if (a[k] === undefined || a[k] === true) throw new Error(`missing --${k}`); return a[k]; };
  const c = await connect({
    url: (a.url ?? process.env.T3RPC_URL ?? "").replace(/\/$/, "") || need("url"),
    pairUrl: a["pair-url"] ?? process.env.T3RPC_PAIR_URL,
    sessionFile: a["session-file"] ?? process.env.T3RPC_SESSION_FILE,
  });
  try {
    if (cmd === "smoke") {
      const dir = need("project-dir");
      const projectId = await findOrCreateProject(c, dir);
      const threadId = id();
      await c.call("orchestration.launchThread", {
        commandId: id(), creationSource: "web", threadId, projectId,
        title: "t3rpc smoke",
        modelSelection: { instanceId: need("instance"), model: need("model") },
        runtimeMode: a["runtime-mode"] ?? "full-access", interactionMode: "default",
        workspaceStrategy: { type: "root" },
        initialMessage: { text: need("prompt"), attachments: [] },
      });
      if (a["no-wait"]) { console.log(JSON.stringify({ threadId })); return 0; }
      const p = await waitIdle(c, threadId, 1, Number(a.timeout ?? 120));
      const expect = need("expect");
      const ok = p.messages.some((m) => m.role === "assistant" && m.text.includes(expect));
      console.log(JSON.stringify({ ...summary(p, threadId), ok }));
      return ok ? 0 : 1;
    }
    if (cmd === "items") {
      printItems(await projection(c, need("thread")));
      return 0;
    }
    if (cmd === "respond") {
      const threadId = need("thread");
      const body = a.approve ? { decision: "accept" } : a.decline ? { decision: "decline" }
        : a.answers ? { answers: JSON.parse(a.answers) } : null;
      if (!body) throw new Error("need --approve, --decline or --answers JSON");
      const r = await dispatch(c, { type: "runtime-request.respond", commandId: id(), threadId, requestId: need("request"), ...body });
      console.log(JSON.stringify(r));
      return 0;
    }
    if (cmd === "send") {
      const threadId = need("thread");
      const before = await projection(c, threadId);
      const r = await dispatch(c, {
        type: "message.dispatch", commandId: id(), threadId, messageId: id(), text: need("text"), attachments: [],
        createdBy: "user", creationSource: "web",
        dispatchMode: { type: isRunActive(before) ? "queue_after_active" : "start_immediately" },
      });
      if (!a.wait) { console.log(JSON.stringify(r)); return 0; }
      const p = await waitIdle(c, threadId, before.runs.length + 1, Number(a.timeout ?? 120));
      console.log(JSON.stringify(summary(p, threadId)));
      return 0;
    }
    if (cmd === "call") { // debugging: call METHOD [JSON payload]
      console.log(JSON.stringify(await c.call(rest[0], rest[1] ? JSON.parse(rest[1]) : {})));
      return 0;
    }
    usage();
    return 2;
  } finally {
    c.close();
  }
}

function usage() {
  console.error(`usage: t3rpc.mjs --url URL --pair-url PAIRING_URL [--session-file F] <command>
  smoke --instance ID --model SLUG --project-dir DIR --prompt TEXT (--expect TEXT | --no-wait) [--timeout S] [--runtime-mode M]
  items --thread ID
  respond --thread ID --request ID (--approve | --decline | --answers JSON)
  send --thread ID --text TEXT [--wait] [--timeout S]
  call METHOD [JSON]
env defaults: T3RPC_URL, T3RPC_PAIR_URL, T3RPC_SESSION_FILE`);
}

export function parseArgs(args) {
  const out = { _: [] };
  for (let i = 0; i < args.length; i++) {
    if (!args[i].startsWith("--")) { out._.push(args[i]); continue; }
    const k = args[i].slice(2);
    out[k] = args[i + 1] === undefined || args[i + 1].startsWith("--") ? true : args[++i];
  }
  return out;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main(parseArgs(process.argv.slice(2))).then(
    (code) => process.exit(code),
    (e) => { console.error(`t3rpc: ${e.message}`); process.exit(1); },
  );
}
