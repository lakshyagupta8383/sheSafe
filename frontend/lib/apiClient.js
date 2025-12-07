// ALWAYS hit backend correctly
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function readSafe(res) {
  try { return await res.text(); } catch { return ""; }
}

async function parse(res, url) {
  const txt = await readSafe(res);
  let data;
  try { data = txt ? JSON.parse(txt) : {}; } catch { data = { raw: txt }; }

  if (!res.ok) {
    const msg = data?.detail || data?.reason || data?.message || `HTTP ${res.status}`;
    const err = new Error(`${msg} — ${url}`);
    err.status = res.status;
    err.body = data;
    throw err;
  }

  return data;
}

export async function get(path) {
  const url = `${API_BASE}/api/${path.replace(/^\/+/, '')}`;
  console.log("GET →", url);
  const res = await fetch(url);
  return parse(res, url);
}

export async function post(path, body) {
  const url = `${API_BASE}/api/${path.replace(/^\/+/, '')}`;
  console.log("POST →", url);
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse(res, url);
}
