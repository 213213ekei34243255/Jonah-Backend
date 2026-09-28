// Jonah Developer Console. Every value from the server is inserted as text (never as HTML), and there are no inline handlers,
// so the page's strict content-security-policy holds.
"use strict";
(() => {
  const root = document.getElementById("app");
  const state = { csrf: null, username: "", data: null, tab: "accounts", audit: [], timer: null, busy: false };

  // ------------------------------------------------------------------ tiny DOM helper
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const fmt = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "-");
  function ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  function toast(message, bad) {
    const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: "status", text: message });
    document.body.append(t);
    setTimeout(() => t.remove(), bad ? 6000 : 3000);
  }

  // ------------------------------------------------------------------ API
  async function api(method, path, body) {
    const res = await fetch("/admin/api" + path, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(state.csrf ? { "X-CSRF-Token": state.csrf } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let json = {};
    try { json = await res.json(); } catch { /* not JSON */ }
    if (res.status === 401 && path !== "/login") { signedOut(); throw new Error("Please sign in again."); }
    if (!res.ok) throw new Error(json.message || "Request failed (" + res.status + ")");
    return json;
  }
  function signedOut() { state.csrf = null; clearInterval(state.timer); render(); }

  // ------------------------------------------------------------------ dialogs
  function ask({ title, note, fields = [], ok = "OK", danger = false }) {
    return new Promise((resolve) => {
      const inputs = {};
      const form = h("form", { method: "dialog" },
        h("h2", { text: title }),
        note && h("p", { class: "muted", text: note }),
        fields.map((f) => {
          const input = h("input", { type: f.type || "text", name: f.name, value: f.value || "", placeholder: f.placeholder || "", autocomplete: "off", required: f.required });
          inputs[f.name] = input;
          return h("div", { class: "row" }, h("label", { text: f.label }), input);
        }),
        h("div", { class: "buttons" },
          h("button", { type: "button", text: "Cancel", onclick: () => dlg.close("cancel") }),
          h("button", { type: "submit", class: danger ? "danger" : "primary", text: ok })));
      const dlg = h("dialog", {}, form);
      dlg.addEventListener("close", () => {
        const values = dlg.returnValue === "cancel" || dlg.returnValue === "" ? null : Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value]));
        dlg.remove(); resolve(values);
      });
      form.addEventListener("submit", () => { dlg.returnValue = "ok"; });
      document.body.append(dlg);
      dlg.showModal();
      const first = Object.values(inputs)[0]; if (first) first.focus();
    });
  }

  // ------------------------------------------------------------------ data
  async function load() {
    if (!state.csrf || state.busy) return;
    try {
      state.data = await api("GET", "/overview");
      if (state.tab === "audit") state.audit = (await api("GET", "/audit?limit=200")).entries;
      render();
    } catch (e) { if (state.csrf) toast(e.message, true); }
  }
  async function act(fn, done) {
    state.busy = true;
    try { await fn(); if (done) toast(done); } catch (e) { toast(e.message, true); }
    state.busy = false;
    await load();
  }
  const action = (id, name, done) => act(() => api("POST", `/accounts/${id}/action`, { action: name }), done);

  // ------------------------------------------------------------------ views
  function renderLogin(message) {
    const user = h("input", { type: "text", autocomplete: "username", required: true });
    const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
    const err = h("p", { class: "err", role: "alert", text: message || "" });
    const form = h("form", { class: "card login" },
      h("h1", { text: "Jonah Developer Console" }),
      h("p", { class: "muted", text: "Administrator sign-in" }),
      h("div", { class: "row" }, h("label", { text: "Username" }), user),
      h("div", { class: "row" }, h("label", { text: "Password" }), pass),
      h("button", { type: "submit", class: "primary", text: "Sign in" }), err);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const r = await api("POST", "/login", { username: user.value, password: pass.value });
        state.csrf = r.csrf; state.username = r.username;
        await load();
        clearInterval(state.timer);
        state.timer = setInterval(() => { if (!document.querySelector("dialog[open]")) load(); }, 10000);
      } catch (e) { err.textContent = e.message; pass.value = ""; }
    });
    root.replaceChildren(form);
    user.focus();
  }

  function switchCard({ title, help, on, onLabel, offLabel, onToggle }) {
    return h("div", { class: "card switch" },
      h("div", { class: "text" }, h("b", { text: title }), h("span", { class: "muted", text: help }), h("div", { class: "small-text", text: on ? onLabel : offLabel })),
      h("button", { class: "toggle" + (on ? " on" : ""), role: "switch", "aria-checked": String(on), "aria-label": title, onclick: onToggle }));
  }

  function statusBadge(a) { return h("span", { class: "badge " + a.effectiveStatus, text: a.effectiveStatus[0].toUpperCase() + a.effectiveStatus.slice(1) }); }

  function accountRow(a) {
    const btn = (text, fn, cls = "", disabled = false) => h("button", { class: "small " + cls, text, onclick: fn, disabled });
    const buttons = [];
    if (a.status === "banned") buttons.push(btn("Unban", () => action(a.id, "unban", "Unbanned " + a.username)));
    else buttons.push(btn("Ban", async () => { if (confirm(`Ban ${a.username}? They lose access at their app's next check (within about a minute).`)) action(a.id, "ban", "Banned " + a.username); }, "danger"));
    if (a.status === "disabled") buttons.push(btn("Enable", () => action(a.id, "enable", "Enabled " + a.username)));
    else if (a.status === "active") buttons.push(btn("Disable", async () => { if (confirm(`Disable ${a.username}? They lose access at their app's next check.`)) action(a.id, "disable", "Disabled " + a.username); }));
    buttons.push(btn("Revoke device", async () => { if (confirm(`Revoke ${a.username}'s authorized device? They are signed out, and the account can be activated on a different Mac.`)) action(a.id, "revoke-device", "Device revoked"); }, "", !a.device));
    buttons.push(btn("Sign out now", () => action(a.id, "force-reauth", "Signed out; they must log in again"), "", !a.activeSessions));
    buttons.push(btn("Password", async () => {
      const v = await ask({ title: "Change password: " + a.username, note: "The user is signed out and must use the new password.", fields: [{ name: "password", label: "New password (8+ characters)", type: "password", required: true }], ok: "Change password" });
      if (v) act(() => api("POST", `/accounts/${a.id}/password`, { password: v.password }), "Password changed");
    }));
    buttons.push(btn("Edit", async () => {
      const local = a.expiresAt ? new Date((a.expiresAt - new Date().getTimezoneOffset() * 60) * 1000).toISOString().slice(0, 16) : "";
      const v = await ask({ title: "Edit " + a.username, note: "Changing the username or the expiry date signs the user out. Leave the expiry empty for no expiry.", fields: [
        { name: "username", label: "Username", value: a.username, required: true },
        { name: "note", label: "Note", value: a.note },
        { name: "expires", label: "Access expires (your local time)", type: "datetime-local", value: local }], ok: "Save" });
      if (v) act(() => api("PATCH", `/accounts/${a.id}`, { username: v.username, note: v.note, expiresAt: v.expires ? Math.floor(new Date(v.expires).getTime() / 1000) : null }), "Saved");
    }));
    buttons.push(btn("Delete", async () => { if (confirm(`Delete ${a.username} permanently? This cannot be undone.`)) act(() => api("DELETE", `/accounts/${a.id}`), "Deleted " + a.username); }, "danger"));

    return h("tr", {},
      h("td", {}, h("b", { text: a.username }), a.note && h("div", { class: "small-text", text: a.note })),
      h("td", {}, statusBadge(a)),
      h("td", {}, h("span", { class: "dot" + (a.online ? " on" : "") }), a.online ? "Online" : "Offline", h("div", { class: "small-text", text: "last sign-in " + ago(a.lastLoginAt) })),
      h("td", {}, a.device ? [h("div", { text: a.device.label || "(unnamed Mac)" }), h("div", { class: "small-text", text: "id " + a.device.id.slice(0, 8) + " · bound " + fmt(a.device.boundAt) })] : h("span", { class: "muted", text: "none yet" })),
      h("td", {}, a.expiresAt ? fmt(a.expiresAt) : h("span", { class: "muted", text: "no expiry" })),
      h("td", { class: "actions" }, buttons));
  }

  function renderAccounts() {
    const d = state.data;
    const u = h("input", { placeholder: "username", autocomplete: "off", required: true });
    const p = h("input", { placeholder: "password (8+ characters)", type: "password", autocomplete: "new-password", required: true });
    const e = h("input", { type: "datetime-local", "aria-label": "Expires" });
    const n = h("input", { placeholder: "note (optional)", autocomplete: "off" });
    const form = h("form", { class: "create" },
      h("div", {}, h("label", { text: "Username" }), u), h("div", {}, h("label", { text: "Password" }), p),
      h("div", {}, h("label", { text: "Expires (optional)" }), e), h("div", {}, h("label", { text: "Note" }), n),
      h("button", { type: "submit", class: "primary", text: "Create account" }));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      act(() => api("POST", "/accounts", { username: u.value, password: p.value, note: n.value, expiresAt: e.value ? Math.floor(new Date(e.value).getTime() / 1000) : null }).then(() => form.reset()), "Account created");
    });
    return [
      h("div", { class: "card" }, h("h2", { text: "Create an authorized account" }), form),
      h("div", { class: "card tablewrap" }, h("h2", { text: "Accounts" }),
        d.accounts.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Account", "Status", "Online", "Authorized device", "Expires", "Actions"].map((x) => h("th", { text: x })))), h("tbody", {}, d.accounts.map(accountRow))) : h("p", { class: "muted", text: "No accounts yet." })),
    ];
  }

  function renderAudit() {
    return h("div", { class: "card tablewrap" }, h("h2", { text: "Audit log (newest first)" }),
      h("table", {}, h("thead", {}, h("tr", {}, ["When", "Who", "What", "Account", "Detail", "IP"].map((x) => h("th", { text: x })))),
        h("tbody", {}, state.audit.map((r) => h("tr", {}, h("td", { text: fmt(r.ts) }), h("td", { text: r.actor }), h("td", { text: r.action }), h("td", { text: r.target || "" }), h("td", { text: r.detail || "" }), h("td", { text: r.ip || "" }))))));
  }

  async function changeOwnPassword() {
    const v = await ask({ title: "Change your administrator password", fields: [{ name: "current", label: "Current password", type: "password", required: true }, { name: "next", label: "New password (12+ characters)", type: "password", required: true }], ok: "Change password" });
    if (v) act(() => api("POST", "/password", v), "Password changed");
  }

  function render() {
    if (!state.csrf) return renderLogin();
    const d = state.data;
    if (!d) return root.replaceChildren(h("p", { class: "muted", text: "Loading..." }));
    const s = d.settings;
    const c = d.counts;
    root.replaceChildren(
      h("div", { class: "bar" }, h("h1", { text: "Jonah Developer Console" }), h("span", { class: "grow" }), h("span", { class: "muted", text: "signed in as " + state.username }),
        h("button", { class: "small", text: "Change password", onclick: changeOwnPassword }),
        h("button", { class: "small", text: "Sign out", onclick: async () => { try { await api("POST", "/logout", {}); } catch { /* signed out anyway */ } signedOut(); } })),
      ...(d.storage && d.storage.persistent === false
        ? [h("div", { class: "card warn", role: "alert", text: "TEMPORARY STORAGE: this server has no persistent disk, so every account, ban and device binding is LOST whenever it restarts or redeploys. Attach a disk (LICENSE_DATA_DIR) before real use." })]
        : []),
      h("div", { class: "switches" },
        switchCard({ title: "Mac app access", help: "Master switch for the whole Mac application. Off = nobody can sign in, and running apps lose access at their next check.", on: s.appActive, onLabel: "ACTIVE", offLabel: "DEACTIVATED: every user sees the access-expired message",
          onToggle: async () => { if (s.appActive && !confirm("Deactivate the entire Mac application? Every user, including everyone signed in right now, loses access.")) return; act(() => api("PATCH", "/settings", { appActive: !s.appActive }), s.appActive ? "Mac app deactivated" : "Mac app activated"); } }),
        switchCard({ title: "Unlimited-access mode", help: "Off = the unlimited version cannot be entered by anyone, and running apps lose it at their next check.", on: s.unlimitedEnabled, onLabel: "ON", offLabel: "OFF: nobody can enter the unlimited version",
          onToggle: async () => { if (s.unlimitedEnabled && !confirm("Turn unlimited-access mode off for everyone?")) return; act(() => api("PATCH", "/settings", { unlimitedEnabled: !s.unlimitedEnabled }), s.unlimitedEnabled ? "Unlimited mode off" : "Unlimited mode on"); } })),
      h("div", { class: "chips" },
        h("span", { class: "chip", text: "Online now" }, h("b", { text: c.online })), h("span", { class: "chip", text: "Active" }, h("b", { text: c.active })),
        h("span", { class: "chip", text: "Disabled" }, h("b", { text: c.disabled })), h("span", { class: "chip", text: "Banned" }, h("b", { text: c.banned })),
        h("span", { class: "chip", text: "Expired" }, h("b", { text: c.expired })),
        h("span", { class: "chip", text: "Token lifetime" }, h("b", { text: s.tokenTtlSeconds + " s" }))),
      h("div", { class: "tabs" },
        h("button", { class: state.tab === "accounts" ? "active" : "", text: "Accounts", onclick: () => { state.tab = "accounts"; load(); } }),
        h("button", { class: state.tab === "audit" ? "active" : "", text: "Audit log", onclick: () => { state.tab = "audit"; load(); } })),
      ...(state.tab === "audit" ? [renderAudit()] : renderAccounts()));
  }

  // On load: if a session cookie already exists the server tells us (the CSRF token is only handed out with it).
  (async () => {
    try {
      const me = await fetch("/admin/api/me", { credentials: "same-origin" });
      if (me.ok) {
        const j = await me.json(); state.csrf = j.csrf; state.username = j.username; await load();
        state.timer = setInterval(() => { if (!document.querySelector("dialog[open]")) load(); }, 10000);
        return;
      }
    } catch { /* fall through to the sign-in form */ }
    render();
  })();
})();
