// JY Fund Signal — 관리자 Worker
// - POST /login  : {password} → 성공 시 서명된 세션 토큰 반환
// - PUT  /groups : groups.json 콘텐츠 → GitHub API로 커밋
// Secrets (wrangler secret put): ADMIN_PW, GITHUB_PAT, SESSION_SECRET

const ALLOWED_ORIGINS = new Set([
  "https://thehfk.github.io",
  "http://localhost:8000",   // 로컬 개발용
  "http://127.0.0.1:8000",
]);
const REPO = "thehfk/jyfund-signal";
const GROUPS_PATH = "data/groups.json";
const SESSION_MAX_AGE_MS = 30 * 24 * 3600 * 1000; // 30일
const LOGIN_FAIL_DELAY_MS = 500;                   // 브루트포스 억제
const MAX_BODY_BYTES = 200 * 1024;                 // 200KB (groups.json 상한)

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") {
      return cors(new Response(null, { status: 204 }), origin);
    }
    const url = new URL(request.url);
    try {
      if (url.pathname === "/login" && request.method === "POST") {
        return cors(await handleLogin(request, env), origin);
      }
      if (url.pathname === "/groups" && request.method === "PUT") {
        return cors(await handleGroupsPut(request, env), origin);
      }
      if (url.pathname === "/health" && request.method === "GET") {
        return cors(json({ ok: true, ts: Date.now() }), origin);
      }
      return cors(new Response("Not found", { status: 404 }), origin);
    } catch (e) {
      console.error("worker error:", e);
      return cors(json({ ok: false, error: String(e).slice(0, 200) }, 500), origin);
    }
  },
};

// ────────── handlers ──────────

async function handleLogin(request, env) {
  requireEnv(env, ["ADMIN_PW", "SESSION_SECRET"]);
  let body;
  try {
    body = await readJson(request);
  } catch {
    return json({ ok: false, error: "invalid json" }, 400);
  }
  const pw = (body && typeof body.password === "string") ? body.password : "";
  if (!pw || pw.length > 500) {
    return json({ ok: false, error: "bad request" }, 400);
  }
  if (!constTimeEq(pw, env.ADMIN_PW)) {
    // 브루트포스 억제: 실패 시 500ms 지연
    await sleep(LOGIN_FAIL_DELAY_MS);
    return json({ ok: false, error: "invalid password" }, 401);
  }
  const token = await mintSession(env.SESSION_SECRET);
  return json({ ok: true, token, expiresInMs: SESSION_MAX_AGE_MS });
}

async function handleGroupsPut(request, env) {
  requireEnv(env, ["GITHUB_PAT", "SESSION_SECRET"]);
  const auth = request.headers.get("Authorization") || "";
  const token = auth.replace(/^Bearer\s+/i, "").trim();
  if (!(await verifySession(token, env.SESSION_SECRET))) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  let payload;
  try {
    payload = await readJson(request);
  } catch {
    return json({ ok: false, error: "invalid json" }, 400);
  }
  // groups.json 최소 스키마 검증
  if (
    !payload || typeof payload !== "object" ||
    !Array.isArray(payload.groups) ||
    payload.groups.some(g => !g || typeof g.id !== "string" || typeof g.name !== "string" || !Array.isArray(g.tickers))
  ) {
    return json({ ok: false, error: "schema invalid" }, 422);
  }

  // 현재 sha 조회
  const cur = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${GROUPS_PATH}?ref=main&t=${Date.now()}`,
    { headers: ghHeaders(env), cf: { cacheTtl: 0 } }
  );
  let sha = null;
  if (cur.ok) {
    const cj = await cur.json();
    sha = cj.sha;
  } else if (cur.status !== 404) {
    const t = await cur.text();
    return json({ ok: false, error: `github get: ${cur.status} ${t.slice(0, 200)}` }, 502);
  }

  const content = JSON.stringify(payload, null, 2) + "\n";
  const putBody = {
    message: `groups: sync via worker ${new Date().toISOString()}`,
    content: b64EncodeUnicode(content),
    branch: "main",
  };
  if (sha) putBody.sha = sha;

  const put = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${GROUPS_PATH}`,
    { method: "PUT", headers: { ...ghHeaders(env), "Content-Type": "application/json" }, body: JSON.stringify(putBody) }
  );
  if (!put.ok) {
    // sha 충돌 시 최신 sha로 1회 재시도
    if (put.status === 409 || put.status === 422) {
      const fresh = await fetch(
        `https://api.github.com/repos/${REPO}/contents/${GROUPS_PATH}?ref=main&t=${Date.now()}`,
        { headers: ghHeaders(env), cf: { cacheTtl: 0 } }
      );
      if (fresh.ok) {
        putBody.sha = (await fresh.json()).sha;
        const retry = await fetch(
          `https://api.github.com/repos/${REPO}/contents/${GROUPS_PATH}`,
          { method: "PUT", headers: { ...ghHeaders(env), "Content-Type": "application/json" }, body: JSON.stringify(putBody) }
        );
        if (!retry.ok) {
          const t = await retry.text();
          return json({ ok: false, error: `github put retry: ${retry.status} ${t.slice(0, 200)}` }, 502);
        }
        const rj = await retry.json();
        return json({ ok: true, sha: rj.content?.sha });
      }
    }
    const t = await put.text();
    return json({ ok: false, error: `github put: ${put.status} ${t.slice(0, 200)}` }, 502);
  }
  const rj = await put.json();
  return json({ ok: true, sha: rj.content?.sha });
}

// ────────── helpers ──────────

function requireEnv(env, keys) {
  for (const k of keys) {
    if (!env[k]) throw new Error(`missing env: ${k}`);
  }
}

function ghHeaders(env) {
  return {
    "Authorization": `Bearer ${env.GITHUB_PAT}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "jyfund-signal-worker",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function readJson(request) {
  const cl = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (cl > MAX_BODY_BYTES) throw new Error("body too large");
  return await request.json();
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function cors(res, origin) {
  const allow = ALLOWED_ORIGINS.has(origin) ? origin : "https://thehfk.github.io";
  const h = new Headers(res.headers);
  h.set("Access-Control-Allow-Origin", allow);
  h.set("Vary", "Origin");
  h.set("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS");
  h.set("Access-Control-Allow-Headers", "Content-Type, Authorization");
  h.set("Access-Control-Max-Age", "86400");
  return new Response(res.body, { status: res.status, headers: h });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function b64EncodeUnicode(s) {
  // UTF-8 안전한 base64 인코딩
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

function constTimeEq(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// ────────── session (HMAC-SHA256) ──────────

async function hmacSha256Hex(secret, msg) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function mintSession(secret) {
  const issuedAt = Date.now();
  const nonce = crypto.getRandomValues(new Uint8Array(8));
  const nonceHex = [...nonce].map(b => b.toString(16).padStart(2, "0")).join("");
  const payload = `${issuedAt}.${nonceHex}`;
  const sig = await hmacSha256Hex(secret, payload);
  return `${payload}.${sig}`;
}

async function verifySession(token, secret) {
  if (!token) return false;
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  const [issuedAtStr, nonceHex, sig] = parts;
  const payload = `${issuedAtStr}.${nonceHex}`;
  const expected = await hmacSha256Hex(secret, payload);
  if (!constTimeEq(sig, expected)) return false;
  const issuedAt = parseInt(issuedAtStr, 10);
  if (!issuedAt) return false;
  if (Date.now() - issuedAt > SESSION_MAX_AGE_MS) return false;
  return true;
}
