"use strict";
// One click: read the signed-in DeepSeek session (the login token in localStorage, which is NOT a cookie,
// plus every deepseek.com cookie, httpOnly ones included) and hand it to the harness that is listening
// after the person typed /connect. The only address this extension may contact is http://127.0.0.1:47123.
// The harness accepts requests only from this extension's pinned ID, so no other page or add-on can.

const HARNESS = "http://127.0.0.1:47123";
const $ = (id) => document.getElementById(id);
const status = (text, kind) => { const el = $("status"); el.textContent = text; el.className = kind || ""; };
let needsCode = false;

function setState(kind, text) {
  $("dot").className = kind;
  $("stateText").textContent = text;
}

async function ping() {
  try {
    const response = await fetch(HARNESS + "/connect/ping", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const body = await response.json();
    needsCode = !!body.needs_code;
    $("codeRow").hidden = !needsCode;
    setState("on", needsCode ? "Harness is listening - enter the pairing code" : "Harness is listening");
    return true;
  } catch (e) {
    setState("off", "Harness is not listening - type /connect in it");
    return false;
  }
}

async function chatTab() {
  const tabs = await chrome.tabs.query({ url: "https://chat.deepseek.com/*" });
  return tabs.find((t) => t.active) || tabs[0] || null;
}

async function collect() {
  const tab = await chatTab();
  if (!tab) throw new Error("Open https://chat.deepseek.com in this browser and sign in first.");
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => { try { return localStorage.getItem("userToken"); } catch (e) { return null; } },
  });
  const rawToken = result && result.result;
  if (!rawToken) throw new Error("No login token found on that page. Sign in, reload the DeepSeek tab and try again.");
  const cookies = await chrome.cookies.getAll({ domain: "deepseek.com" });
  return {
    format: "deepseek-harness-session",
    version: 2,
    exportedAt: new Date().toISOString(),
    userToken: rawToken,            // raw localStorage value; the harness unwraps {"value": ...}
    cookies: cookies.map((c) => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure,
      httpOnly: c.httpOnly, session: c.session, expirationDate: c.expirationDate,
    })),
  };
}

async function connect() {
  status("Reading your DeepSeek session...");
  const payload = await collect();
  const headers = { "Content-Type": "application/json" };
  if (needsCode) {
    const code = $("code").value.trim();
    if (!/^\d{6}$/.test(code)) throw new Error("Enter the 6-digit pairing code shown in the harness.");
    headers["X-Connect-Code"] = code;
  }
  status("Connecting...");
  let response;
  try {
    response = await fetch(HARNESS + "/connect/session", { method: "POST", headers, body: JSON.stringify(payload), cache: "no-store" });
  } catch (e) {
    setState("off", "Harness is not listening - type /connect in it");
    throw new Error("Could not reach the harness. Type /connect in it, then press Connect again.");
  }
  let body = {};
  try { body = await response.json(); } catch (e) { /* not JSON */ }
  if (response.ok && body.ok) {
    setState("on", "Connected");
    return status(`Connected. ${body.cookies} cookies imported, login check: ${body.verified}. You can close this popup.`, "ok");
  }
  const why = body.error || (body.messages || []).join(" ") || body.detail || ("HTTP " + response.status);
  status("Not connected: " + why, "bad");
}

async function save() {
  status("Reading your DeepSeek session...");
  const payload = await collect();
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "deepseek-session.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
  status("Saved deepseek-session.json to Downloads. Move it into the harness cookies/ folder, then type /refresh. Delete it afterwards: it is a login.", "ok");
}

function guarded(fn) {
  return async () => {
    $("connect").disabled = true;
    try { await fn(); } catch (e) { status(e.message || String(e), "bad"); }
    finally { $("connect").disabled = false; }
  };
}

$("connect").addEventListener("click", guarded(connect));
$("save").addEventListener("click", guarded(save));
ping();
