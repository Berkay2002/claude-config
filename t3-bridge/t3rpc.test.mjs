// Run: node --test t3-bridge/t3rpc.test.mjs
// A fake T3 server (HTTP auth routes + a hand-rolled WebSocket) checks the auth exchange and the Effect RPC framing.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { connect, pairingToken, parseArgs } from "./t3rpc.mjs";

const wsFrame = (obj) => {
  const b = Buffer.from(JSON.stringify(obj));
  const head = b.length < 126 ? Buffer.from([0x81, b.length]) : Buffer.from([0x81, 126, b.length >> 8, b.length & 255]);
  return Buffer.concat([head, b]);
};
const wsParse = (buf) => { // masked client text frames (< 64 KiB), possibly several per chunk
  const out = [];
  for (let at = 0; at < buf.length; ) {
    let len = buf[at + 1] & 127, off = at + 2;
    if (len === 126) { len = buf.readUInt16BE(at + 2); off = at + 4; }
    const mask = buf.subarray(off, off + 4), data = buf.subarray(off + 4, off + 4 + len);
    out.push(JSON.parse(Buffer.from(data.map((x, i) => x ^ mask[i % 4])).toString()));
    at = off + 4 + len;
  }
  return out;
};

async function fakeServer() {
  const log = { exchanges: 0, ticketCookies: [], wsUrl: null, frames: [] };
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      if (req.url === "/api/auth/browser-session") {
        log.exchanges++;
        log.credential = JSON.parse(body).credential;
        res.setHeader("set-cookie", ["t3_session=abc123; HttpOnly; Path=/"]);
        res.end(JSON.stringify({ authenticated: true }));
      } else if (req.url === "/api/auth/websocket-ticket/rpc") {
        log.ticketCookies.push(req.headers.cookie);
        res.end(JSON.stringify({ ticket: "tick-" + log.ticketCookies.length }));
      } else res.writeHead(404).end();
    });
  });
  const sockets = new Set();
  server.on("upgrade", (req, socket) => {
    sockets.add(socket);
    log.wsUrl = req.url;
    const accept = createHash("sha1").update(req.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    socket.on("data", (buf) => {
      if ((buf[0] & 15) === 8) return socket.end();
      for (const m of wsParse(buf)) {
      log.frames.push(m);
      if (m._tag !== "Request") continue;
      if (m.tag === "echo") socket.write(wsFrame({ _tag: "Exit", requestId: m.id, exit: { _tag: "Success", value: m.payload } }));
      else if (m.tag === "boom") socket.write(wsFrame({ _tag: "Exit", requestId: m.id, exit: { _tag: "Failure", cause: [{ _tag: "Fail", error: "nope" }] } }));
      else if (m.tag === "feed") socket.write(wsFrame({ _tag: "Chunk", requestId: m.id, values: [{ n: 1 }, { n: 2 }] }));
      }
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { log, url: `http://127.0.0.1:${server.address().port}`, close: () => { for (const k of sockets) k.destroy(); server.closeAllConnections(); server.close(); } };
}

test("pairingToken reads the URL fragment, the query or a bare token", () => {
  assert.equal(pairingToken("http://localhost:3797/pair#token=AB12"), "AB12");
  assert.equal(pairingToken("http://localhost:3797/pair?token=CD34"), "CD34");
  assert.equal(pairingToken("EF56"), "EF56");
});

test("parseArgs", () => {
  assert.deepEqual(parseArgs(["smoke", "--url", "u", "--wait", "--n", "1"]), { _: ["smoke"], url: "u", wait: true, n: "1" });
});

test("auth exchange, session reuse, request framing, failures and streams", async () => {
  const s = await fakeServer();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "t3rpc-"));
  const sessionFile = path.join(dir, "session");
  try {
    const c = await connect({ url: s.url, pairUrl: "http://x/pair#token=TOK", sessionFile });
    assert.equal(s.log.credential, "TOK");
    assert.deepEqual(s.log.ticketCookies, ["t3_session=abc123"]);
    assert.equal(s.log.wsUrl, "/ws?orchestrationProtocol=2&wsTicket=tick-1");

    assert.deepEqual(await c.call("echo", { a: 1 }), { a: 1 });
    assert.deepEqual(s.log.frames[0], { _tag: "Request", id: "1", tag: "echo", payload: { a: 1 }, headers: [] });
    await assert.rejects(c.call("boom"), /boom failed.*nope/);

    const seen = [];
    await c.stream("feed", {}, (vs) => (seen.push(...vs), true));
    assert.deepEqual(seen, [{ n: 1 }, { n: 2 }]);
    await new Promise((r) => setTimeout(r, 50));
    const tags = s.log.frames.map((f) => f._tag);
    assert.deepEqual(tags.slice(-2), ["Ack", "Interrupt"]);
    c.close();

    // The pairing token is single use: the second connection reuses the saved cookie.
    const c2 = await connect({ url: s.url, sessionFile });
    assert.equal(s.log.exchanges, 1);
    assert.equal(s.log.ticketCookies[1], "t3_session=abc123");
    c2.close();
  } finally {
    s.close();
    fs.rmSync(dir, { recursive: true });
  }
});
