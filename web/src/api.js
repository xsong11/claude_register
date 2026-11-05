// 会话失效（401）全局广播：App 监听后踢回登录页，
// 避免密码改了/cookie 过期后面板还停在空壳界面上。
export const SESSION_EXPIRED_EVENT = "cr:session-expired";

// 登录请求自身的 401 是「密码错」，不是会话失效，不广播。
const AUTH_FREE = new Set(["/api/login", "/api/logout"]);

const j = async (r) => {
  if (!r.ok) {
    if (r.status === 401 && !AUTH_FREE.has(new URL(r.url, location.origin).pathname)) {
      window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT));
    }
    const err = new Error(`http ${r.status}`);
    err.status = r.status;
    try {
      err.body = await r.json();
    } catch {
      /* no json body */
    }
    throw err;
  }
  return r.json();
};

export const api = {
  login: (password) =>
    fetch("/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(j),

  getConfig: () => fetch("/api/config").then(j),

  logout: () => fetch("/api/logout", { method: "POST" }).then(j),

  putConfig: (body) =>
    fetch("/api/config", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  startRun: (email, domain, proxyId, platform, groupName) =>
    fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        email, domain, proxy_id: proxyId, platform, group_name: groupName || undefined,
      }),
    }).then(j),

  listGroups: () => fetch("/api/groups").then(j),

  createGroup: (name) =>
    fetch("/api/groups", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(j),

  renameGroup: (name, newName) =>
    fetch(`/api/groups/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ new_name: newName }),
    }).then(j),

  deleteGroup: (name) =>
    fetch(`/api/groups/${encodeURIComponent(name)}`, { method: "DELETE" }).then(j),

  accountsBatchGroup: (emails, groupName) =>
    fetch("/api/accounts/batch-group", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ emails, group_name: groupName }),
    }).then(j),

  accountsBatchProxy: (emails, body) =>
    fetch("/api/accounts/batch-proxy", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ emails, ...body }),
    }).then(j),

  okbrowserStart: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/okbrowser/start`, {
      method: "POST",
    }).then(j),

  okbrowserClose: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/okbrowser/close`, {
      method: "POST",
    }).then(j),

  okbrowserDelete: (email, unbindOnly) =>
    fetch(
      `/api/accounts/${encodeURIComponent(email)}/okbrowser${unbindOnly ? "?unbind_only=true" : ""}`,
      { method: "DELETE" },
    ).then(j),

  checkChatGPT: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/check-chatgpt`, {
      method: "POST",
    }).then(j),
  chatgptResync: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/chatgpt-resync`, {
      method: "POST",
    }).then(j),
  chatgptSession: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/chatgpt-session`, {
      method: "POST",
    }).then(j),
  chatgptSessionSaved: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/chatgpt-session`).then(j),

  accountMails: (email, limit) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/mails?limit=${limit || 20}`, {
      cache: "no-store",
    }).then(j),

  listRuns: () => fetch("/api/runs").then(j),

  runDetail: (id) => fetch(`/api/runs/${id}`).then(j),

  listAccounts: () => fetch("/api/accounts").then(j),

  importAccounts: (body) =>
    fetch("/api/accounts/import", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  exportAccountsText: (params) =>
    fetch("/api/accounts/export", {
      method: "POST",
      cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(params || {}),
    }).then((r) => {
      if (!r.ok) {
        if (r.status === 401) window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT));
        const err = new Error(`http ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return r.text();
    }),

  exportFields: () => fetch("/api/export/fields").then(j),

  rotateApiKey: () => fetch("/api/api-key", { method: "POST" }).then(j),

  accountCreate: (fields) =>
    fetch("/api/accounts", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(fields),
    }).then(j),

  accountsBatchDelete: (emails, deleteOkbrowser) =>
    fetch("/api/accounts/batch-delete", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ emails, delete_okbrowser: !!deleteOkbrowser }),
    }).then(j),

  accountUpdate: (email, fields) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(fields),
    }).then(j),

  accountDelete: (email, deleteOkbrowser) =>
    fetch(
      `/api/accounts/${encodeURIComponent(email)}${deleteOkbrowser ? "?delete_okbrowser=true" : ""}`,
      { method: "DELETE" },
    ).then(j),

  rerun: (email, platform) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/rerun`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ platform: platform || "claude" }),
    }).then(j),

  setAccountClaimed: (email, claimed) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/claimed`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ claimed }),
    }).then(j),

  checkAccount: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/check`, {
      method: "POST",
    }).then(j),

  accountOAuth: (email) =>
    fetch(`/api/accounts/${encodeURIComponent(email)}/oauth`, {
      method: "POST",
    }).then(j),

  sub2apiAccounts: () => fetch("/api/sub2api/accounts").then(j),

  sub2apiReauth: (id) =>
    fetch(`/api/sub2api/accounts/${encodeURIComponent(id)}/reauth`, {
      method: "POST",
    }).then(j),

  openaiKeys: () => fetch("/api/openai/keys").then(j),

  openaiKeyCreate: (body) =>
    fetch("/api/openai/keys", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  openaiKeyUpdate: (id, body) =>
    fetch(`/api/openai/keys/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  openaiKeyDelete: (id) => fetch(`/api/openai/keys/${id}`, { method: "DELETE" }).then(j),

  openaiKeyRefresh: (id) => fetch(`/api/openai/keys/${id}/refresh`, { method: "POST" }).then(j),

  openaiProjectKeys: (id) => fetch(`/api/openai/keys/${id}/api-keys`).then(j),

  openaiProjectKeyCreate: (id, projectId, name) =>
    fetch(`/api/openai/keys/${id}/api-keys`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ project_id: projectId, name }),
    }).then(j),

  openaiProjectKeyDelete: (id, projectId, keyId) =>
    fetch(
      `/api/openai/keys/${id}/api-keys/${encodeURIComponent(keyId)}?project_id=${encodeURIComponent(projectId)}`,
      { method: "DELETE" },
    ).then(j),

  xuiTest: (node) =>
    fetch("/api/xui/test", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(node),
    }).then(j),

  xuiCleanup: () =>
    fetch("/api/xui/cleanup", { method: "POST" }).then(j),

  proxyTest: (body) =>
    fetch("/api/proxies/test", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  takeoverStart: (email) =>
    fetch("/api/takeover/start", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email }),
    }).then(j),

  takeoverManual: (body) =>
    fetch("/api/takeover/manual", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  takeoverCapture: () =>
    fetch("/api/takeover/capture", { method: "POST" }).then(j),

  takeoverStop: () =>
    fetch("/api/takeover/stop", { method: "POST" }).then(j),

  takeoverRelogin: () =>
    fetch("/api/takeover/relogin", { method: "POST" }).then(j),

  takeoverStatus: () => fetch("/api/takeover").then(j),
};
