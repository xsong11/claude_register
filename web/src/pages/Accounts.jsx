import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ChevronDown, Download, ExternalLink, FolderCog, FolderInput, KeyRound, LogIn, Mail, MessageSquare,
  MonitorPlay, Play, Plus, RefreshCw, Search, Server, ShieldCheck, Square, Trash2, Upload,
} from "lucide-react";
import { api } from "../api.js";
import { cn } from "@/lib/utils";
import { PlatformStatusBadge } from "@/components/status-badge";
import { ExportDialog } from "@/components/export-dialog";
import { ImportDialog } from "@/components/import-dialog";
import { ManualLoginDialog } from "@/components/manual-login-dialog";
import { MailDialog } from "@/components/mail-dialog";
import { GroupManageDialog } from "@/components/group-manage-dialog";
import {
  AccountCreateDialog,
  AccountFields,
  emptyAccountForm,
} from "@/components/account-create-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

const FILTERS = [
  ["all", "全部"],
  ["pending", "待注册"],
  ["phone_required", "需手机验证"],
  ["alive", "有效"],
  ["dead", "失效"],
  ["blocked", "被拦截"],
  ["unchecked", "未检测"],
  ["claimed", "已获取"],
  ["unclaimed", "未获取"],
  ["no_sk", "无 sessionKey"],
  ["oauth", "已授权 OAuth"],
  ["no_oauth", "未授权 OAuth"],
  ["chatgpt_success", "ChatGPT 已注册"],
  ["chatgpt_pending", "ChatGPT 未注册"],
  ["chatgpt_alive", "ChatGPT 有效"],
  ["chatgpt_dead", "ChatGPT 失效"],
  ["chatgpt_banned", "ChatGPT 已封禁"],
];

const ALL_GROUPS = "__all__";
const UNGROUPED = "__ungrouped__";

const hasChatGPT = (a) => !!(a.chatgpt_session_token || a.chatgpt_access_token);

function matchGroup(a, group) {
  if (group === ALL_GROUPS) return true;
  if (group === UNGROUPED) return !a.group_name;
  return a.group_name === group;
}

function matchFilter(a, filter) {
  switch (filter) {
    case "chatgpt_success":
      return a.chatgpt_status === "success";
    case "chatgpt_pending":
      return !a.chatgpt_status;
    case "chatgpt_alive":
      return a.chatgpt_check_status === "alive";
    case "chatgpt_dead":
      return a.chatgpt_check_status === "dead";
    case "chatgpt_banned":
      return a.chatgpt_check_status === "banned";
    case "pending":
    case "phone_required":
      return a.status === filter;
    case "alive":
    case "dead":
    case "blocked":
      return a.check_status === filter;
    case "unchecked":
      return !a.check_status;
    case "claimed":
      return !!a.claimed_at;
    case "unclaimed":
      return !a.claimed_at;
    case "no_sk":
      return !a.session_key;
    case "oauth":
      return !!a.access_token;
    case "no_oauth":
      return !a.access_token;
    default:
      return true;
  }
}

function matchQuery(a, q, proxyName) {
  if (!q) return true;
  return [a.email, a.display_name, a.proxy, proxyName, a.session_key, a.group_name]
    .some((v) => String(v || "").toLowerCase().includes(q));
}

function ChatGPTBadge({ acct }) {
  const live = acct.chatgpt_check_status;
  const banned = live === "banned" || acct.chatgpt_status === "banned";
  return (
    <PlatformStatusBadge
      platform="ChatGPT"
      status={banned ? "banned" : acct.chatgpt_status}
      extra={live && !banned ? LIVE_LABEL[live] || live : ""}
      className={live === "dead" || banned ? "bg-red-500/15 text-red-400" : ""}
      title={
        acct.chatgpt_status
          ? [
              acct.chatgpt_access_token ? "已有 accessToken" : "无 accessToken",
              acct.chatgpt_checked_at ? `检测时间：${acct.chatgpt_checked_at}` : "",
              acct.chatgpt_token_expires_at ? `accessToken 过期：${acct.chatgpt_token_expires_at}` : "",
            ].filter(Boolean).join("\n")
          : "尚未用此邮箱注册 ChatGPT"
      }
    />
  );
}

// sessionToken / accessToken 已在上方编辑表单里完整展示，这里不再重复
const CHATGPT_COPY_FIELDS = [
  ["chatgpt_password", "密码"],
  ["chatgpt_account_id", "账号 ID"],
];


const LIVE_LABEL = {
  alive: "有效", dead: "失效", blocked: "被拦截", error: "检测失败", banned: "已封禁",
};
const LIVE_CLS = {
  alive: "bg-emerald-500/15 text-emerald-400",
  dead: "bg-red-500/15 text-red-400",
  blocked: "bg-amber-500/15 text-amber-400",
  error: "bg-muted text-muted-foreground",
};

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 60) return "刚刚";
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}

function LiveBadge({ status, checkedAt, detail }) {
  if (!status) return null;
  return (
    <Badge
      className={cn("rounded-full border-transparent", LIVE_CLS[status] || LIVE_CLS.error)}
      title={detail || ""}
    >
      {LIVE_LABEL[status] || status}
      {checkedAt ? (
        <span className="ml-1 font-normal opacity-70">· {relTime(checkedAt)}</span>
      ) : null}
    </Badge>
  );
}

function untilTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const sec = Math.round((then - Date.now()) / 1000);
  if (sec <= 0) return "已过期";
  if (sec < 3600) return `${Math.max(1, Math.floor(sec / 60))} 分钟后过期`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时后过期`;
  return `${Math.floor(sec / 86400)} 天后过期`;
}

function OAuthBadge({ acct }) {
  if (!acct.access_token) return null;
  const expired = acct.oauth_expires_at && new Date(acct.oauth_expires_at).getTime() <= Date.now();
  return (
    <Badge
      className={cn(
        "rounded-full border-transparent",
        expired ? "bg-muted text-muted-foreground" : "bg-sky-500/15 text-sky-400",
      )}
      title={`OAuth 授权时间：${acct.oauth_at || "未知"}\naccess_token 过期：${acct.oauth_expires_at || "未知"}`}
    >
      OAuth
      {acct.oauth_expires_at ? (
        <span className="ml-1 font-normal opacity-70">· {untilTime(acct.oauth_expires_at)}</span>
      ) : null}
    </Badge>
  );
}

const OAUTH_COPY_FIELDS = [
  ["access_token", "access_token"],
  ["refresh_token", "refresh_token"],
  ["org_uuid", "组织 UUID"],
  ["account_uuid", "账号 UUID"],
];

export default function Accounts({
  attach, running, navigate, group = "", onGroupChange, onGroupsLoaded,
}) {
  const [accounts, setAccounts] = useState([]);
  const [authorizing, setAuthorizing] = useState(() => new Set());
  const [batchOAuth, setBatchOAuth] = useState(null); // {done, total, ok, failed}
  const [batchCheck, setBatchCheck] = useState(null); // {done, total, alive, dead, other}
  const [batchReg, setBatchReg] = useState(null); // {done, total, ok, failed, current}
  const stopBatchRegRef = useRef(false);
  const [takeover, setTakeover] = useState({ running: false, email: null });
  const [relogging, setRelogging] = useState(false);
  const [checking, setChecking] = useState("");
  const [exportOpen, setExportOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const busyRef = useRef(false);
  const [copiedEmail, setCopiedEmail] = useState("");
  const [editingEmail, setEditingEmail] = useState("");
  const [mailEmail, setMailEmail] = useState("");
  const [editForm, setEditForm] = useState({});
  // 待删除的邮箱列表；单个删除也走数组，空数组表示没有待确认的删除
  const [deleteTargets, setDeleteTargets] = useState([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const groupFilter = group || ALL_GROUPS;
  const setGroupFilter = (v) => onGroupChange?.(v === ALL_GROUPS ? "" : v);
  const [groups, setGroups] = useState({ groups: [], ungrouped_count: 0, loaded: false });
  const [groupManageOpen, setGroupManageOpen] = useState(false);
  const [okbMode, setOkbMode] = useState(false);
  const [savedProxies, setSavedProxies] = useState([]);
  const [okbBusy, setOkbBusy] = useState("");
  const [deleteWindows, setDeleteWindows] = useState(true);
  const [checkingChatGPT, setCheckingChatGPT] = useState("");
  const [sessionBusy, setSessionBusy] = useState("");
  const [sessionOpen, setSessionOpen] = useState("");
  const [sessionCache, setSessionCache] = useState({});
  const [batchChatGPTCheck, setBatchChatGPTCheck] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [saving, setSaving] = useState(false);

  const proxyNameByUrl = useMemo(
    () => new Map(savedProxies.map((p) => [p.url, p.name])),
    [savedProxies],
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return accounts.filter(
      (a) =>
        matchGroup(a, groupFilter)
        && matchFilter(a, filter)
        && matchQuery(a, q, proxyNameByUrl.get(a.proxy) || ""),
    );
  }, [accounts, query, filter, groupFilter, proxyNameByUrl]);
  const deleteTargetWindows = accounts.filter(
    (a) => deleteTargets.includes(a.email) && a.okbrowser_window_id,
  ).length;
  const selectedVisible = visible.filter((a) => selected.has(a.email));
  const selectedSk = selectedVisible.filter((a) => a.session_key).map((a) => a.email);
  const selectedGpt = selectedVisible.filter(hasChatGPT).map((a) => a.email);
  const allVisibleSelected = visible.length > 0 && selectedVisible.length === visible.length;

  // 列表刷新后剔除已经不存在的选中项
  useEffect(() => {
    setSelected((prev) => {
      const alive = new Set(accounts.map((a) => a.email));
      const next = new Set([...prev].filter((e) => alive.has(e)));
      return next.size === prev.size ? prev : next;
    });
  }, [accounts]);

  const toggleSelect = (email) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email);
      else next.add(email);
      return next;
    });

  const toggleSelectAll = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) visible.forEach((a) => next.delete(a.email));
      else visible.forEach((a) => next.add(a.email));
      return next;
    });

  function refreshGroups() {
    api
      .listGroups()
      .then((res) => {
        setGroups({ ...res, loaded: true });
        onGroupsLoaded?.(res);
      })
      .catch(() => {});
  }

  function refreshLists() {
    api.listAccounts().then(setAccounts).catch(() => {});
    api.takeoverStatus().then(setTakeover).catch(() => {});
    refreshGroups();
  }

  useEffect(() => {
    refreshLists();
    api
      .getConfig()
      .then((cfg) => {
        setOkbMode(cfg.browser_mode === "okbrowser");
        setSavedProxies(cfg.saved_proxies || []);
      })
      .catch(() => {});
  }, []);

  // 分组被删/改名后，当前筛选的分组不存在了就退回「全部」
  useEffect(() => {
    if (!groups.loaded || groupFilter === ALL_GROUPS || groupFilter === UNGROUPED) return;
    if (!groups.groups.some((g) => g.name === groupFilter)) setGroupFilter(ALL_GROUPS);
  }, [groups, groupFilter]);

  async function moveToGroup(emails, groupName) {
    if (!emails.length) return;
    try {
      const res = await api.accountsBatchGroup(emails, groupName);
      toast.success(
        groupName ? `已将 ${res.updated} 个账号移入「${groupName}」` : `已将 ${res.updated} 个账号移出分组`,
      );
      refreshLists();
    } catch (e) {
      if (e?.status !== 401) toast.error(e.body?.detail || `移动分组失败（${e.status || "?"}）`);
    }
  }

  function onBatchGroupSelect(value) {
    const emails = selectedVisible.map((a) => a.email);
    if (value === "__new__") {
      const name = window.prompt("新分组名称");
      if (name && name.trim()) moveToGroup(emails, name.trim());
    } else if (value === UNGROUPED) {
      moveToGroup(emails, "");
    } else if (value) {
      moveToGroup(emails, value);
    }
  }

  async function applyBatchProxy(emails, payload, label) {
    if (!emails.length) return;
    try {
      const res = await api.accountsBatchProxy(emails, payload);
      toast.success(
        label
          ? `已将 ${res.updated} 个账号的代理设为「${label}」`
          : `已清除 ${res.updated} 个账号的代理`,
      );
      refreshLists();
    } catch (e) {
      if (e?.status !== 401) toast.error(e.body?.detail || `修改代理失败（${e.status || "?"}）`);
    }
  }

  function onBatchProxySelect(value, label) {
    const emails = selectedVisible.map((a) => a.email);
    if (value === "__custom__") {
      const url = window.prompt("代理地址（如 socks5://user:pass@host:port 或 http://host:port）");
      if (url && url.trim()) applyBatchProxy(emails, { proxy: url.trim() }, url.trim());
    } else if (value) {
      applyBatchProxy(emails, value, label);
    }
  }

  const patchAccount = (email, patch) =>
    setAccounts((list) => list.map((a) => (a.email === email ? { ...a, ...patch } : a)));

  async function openWindow(a) {
    setOkbBusy(a.email);
    try {
      const res = await api.okbrowserStart(a.email);
      patchAccount(a.email, { okbrowser_window_id: res.window_id });
      toast.success(
        res.created
          ? `已为「${a.email}」新建并打开 OKBrowser 窗口 #${res.window_id}`
          : `已打开 OKBrowser 窗口 #${res.window_id}`,
      );
    } catch (e) {
      if (e?.status !== 401) toast.error(e.body?.detail || `打开窗口失败（${e.status || "?"}）`);
    } finally {
      setOkbBusy("");
    }
  }

  async function closeWindow(a) {
    setOkbBusy(a.email);
    try {
      await api.okbrowserClose(a.email);
      toast.success("已关闭 OKBrowser 窗口");
    } catch (e) {
      if (e?.status !== 401) toast.error(e.body?.detail || `关闭窗口失败（${e.status || "?"}）`);
    } finally {
      setOkbBusy("");
    }
  }

  async function removeWindow(a, unbindOnly) {
    if (!unbindOnly && !window.confirm(`从 OKBrowser 彻底删除窗口 #${a.okbrowser_window_id}？`)) return;
    setOkbBusy(a.email);
    try {
      await api.okbrowserDelete(a.email, unbindOnly);
      patchAccount(a.email, { okbrowser_window_id: "" });
      setEditForm((f) => ({ ...f, okbrowser_window_id: "" }));
      toast.success(unbindOnly ? "已解绑 OKBrowser 窗口" : "已删除 OKBrowser 窗口");
    } catch (e) {
      if (e?.status !== 401) toast.error(e.body?.detail || `操作失败（${e.status || "?"}）`);
    } finally {
      setOkbBusy("");
    }
  }

  function applyChatGPTCheck(acctEmail, res) {
    patchAccount(acctEmail, {
      chatgpt_check_status: res.status,
      chatgpt_checked_at: res.checked_at,
      chatgpt_check_detail: res.detail,
      chatgpt_token_expires_at: res.chatgpt_token_expires_at,
    });
  }

  function cacheSession(acctEmail, session) {
    const text = JSON.stringify(session, null, 2);
    setSessionCache((prev) => ({ ...prev, [acctEmail]: text }));
    setAccounts((prev) =>
      prev.map((a) => (a.email === acctEmail
        ? { ...a, chatgpt_session_json_len: JSON.stringify(session).length } : a)),
    );
    return text;
  }

  async function refreshChatGPTSession(acctEmail) {
    setSessionBusy(acctEmail);
    try {
      const res = await api.chatgptSession(acctEmail);
      applyChatGPTCheck(acctEmail, res);
      if (res.session) {
        cacheSession(acctEmail, res.session);
        setSessionOpen(acctEmail);
      }
      if (res.fresh) toast.success("已获取最新 session");
      else if (res.session) toast.warning(`刷新失败（${res.detail}），显示上次保存的 session`);
      else toast.error(`获取 session 失败：${res.detail}`);
    } catch (e) {
      toast.error(`「${acctEmail}」获取 session 失败（${e.status || "?"}）`);
    } finally {
      setSessionBusy("");
    }
  }

  async function savedSessionText(acctEmail) {
    if (sessionCache[acctEmail]) return sessionCache[acctEmail];
    setSessionBusy(acctEmail);
    try {
      const res = await api.chatgptSessionSaved(acctEmail);
      if (!res.session) {
        toast.error("没有保存的 session，请先点「获取最新」");
        return "";
      }
      return cacheSession(acctEmail, res.session);
    } catch (e) {
      toast.error(`「${acctEmail}」读取 session 失败（${e.status || "?"}）`);
      return "";
    } finally {
      setSessionBusy("");
    }
  }

  async function toggleSession(acctEmail) {
    if (sessionOpen === acctEmail) {
      setSessionOpen("");
      return;
    }
    if (await savedSessionText(acctEmail)) setSessionOpen(acctEmail);
  }

  async function copySession(acctEmail) {
    const text = await savedSessionText(acctEmail);
    if (text) copyText(text, `${acctEmail}:chatgpt_session_json`);
  }

  async function doCheckChatGPT(acctEmail) {
    setCheckingChatGPT(acctEmail);
    try {
      const res = await api.checkChatGPT(acctEmail);
      applyChatGPTCheck(acctEmail, res);
      if (res.refreshed) refreshLists();
    } catch (e) {
      toast.error(`「${acctEmail}」ChatGPT 检测失败（${e.status || "?"}）`);
    } finally {
      setCheckingChatGPT("");
    }
  }

  async function doResyncChatGPT(acctEmail) {
    setCheckingChatGPT(acctEmail);
    try {
      const res = await api.chatgptResync(acctEmail);
      if (res.mode === "synced") {
        toast.success(`「${acctEmail}」已从 OKBrowser 窗口同步到有效的 ChatGPT 凭证`);
        refreshLists();
      } else {
        attach(res.run_id);
        toast.info(`${res.reason || "窗口里没有可用会话"}，已启动重新登录任务`);
        navigate("register");
      }
    } catch (e) {
      if (e?.status === 401) return;
      toast.error(e.body?.detail || `「${acctEmail}」重新获取失败（${e.status || "?"}）`);
    } finally {
      setCheckingChatGPT("");
    }
  }

  async function runBatchChatGPTCheck(emails) {
    const queue = [...emails];
    const stat = { done: 0, total: emails.length, alive: 0, dead: 0, other: 0 };
    setBatchChatGPTCheck({ ...stat });
    const worker = async () => {
      while (queue.length) {
        const email = queue.shift();
        try {
          const res = await api.checkChatGPT(email);
          applyChatGPTCheck(email, res);
          if (res.status === "alive") stat.alive += 1;
          else if (res.status === "dead") stat.dead += 1;
          else stat.other += 1;
        } catch (e) {
          if (e?.status === 401) return;
          stat.other += 1;
        }
        stat.done += 1;
        setBatchChatGPTCheck({ ...stat });
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    setBatchChatGPTCheck(null);
    refreshLists();
    const msg = `ChatGPT 检测完成：有效 ${stat.alive} 个，失效 ${stat.dead} 个`
      + (stat.other ? `，其他/出错 ${stat.other} 个` : "");
    if (stat.dead || stat.other) toast.error(msg);
    else toast.success(msg);
  }

  // 读取接管浏览器里的 sessionKey；新值会由后端检测后新建/更新账号。
  // manual=true 时是用户点按钮，结果都要提示；轮询时只在有新进展时提示。
  async function captureOnce(manual) {
    const res = await api.takeoverCapture();
    if (res.saved && !res.unchanged) {
      const tail = res.check_status === "alive" ? "" : "（存活检测暂时失败）";
      toast.success(
        `${res.created ? "已新建账号" : "已更新 sessionKey"}：${res.email}${tail}`,
        res.note ? { description: res.note } : undefined,
      );
      refreshLists();
    } else if (res.check_status === "dead" && (manual || !res.repeat)) {
      toast.error(`取到的 sessionKey 已失效：${res.check_detail || ""}`);
    } else if (manual) {
      if (!res.found) toast.error("浏览器里还没有 sessionKey，请先在接管画面完成登录");
      else toast.success(`sessionKey 未变化，已保存在 ${res.email}`);
    }
    return res;
  }

  // 接管期间每 5 秒自动取一次 sk：手动登录成功后无需任何操作即可入库。
  useEffect(() => {
    if (!takeover.running) return undefined;
    let inflight = false;
    const timer = setInterval(async () => {
      if (inflight || busyRef.current) return;
      inflight = true;
      try {
        await captureOnce(false);
      } catch (e) {
        if (e.status === 409) {
          api.takeoverStatus().then(setTakeover).catch(() => {});
        }
      } finally {
        inflight = false;
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [takeover.running]);

  const captureNow = async () => {
    busyRef.current = true;
    setCapturing(true);
    try {
      await captureOnce(true);
    } catch (e) {
      toast.error(e.body?.detail || `提取失败（${e.status || "?"}）`);
    } finally {
      busyRef.current = false;
      setCapturing(false);
    }
  };

  async function doRerun(acctEmail, platform = "claude") {
    try {
      const res = await api.rerun(acctEmail, platform);
      attach(res.run_id);
      toast.success("已启动注册任务，正在跳转…");
      navigate("register");
    } catch (err) {
      toast.error(
        err.status === 409
          ? `「${acctEmail}」启动失败：已有任务在运行`
          : `「${acctEmail}」启动失败`,
      );
    }
  }

  async function waitForRunFinish(runId) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const detail = await api.runDetail(runId);
        if (detail && detail.status && detail.status !== "running") {
          return detail;
        }
      } catch {
        return { status: "failed" };
      }
    }
  }

  async function runBatchRegister(emails, platform = "claude") {
    if (!emails.length) return;
    stopBatchRegRef.current = false;
    const stat = { done: 0, total: emails.length, ok: 0, failed: 0, current: "", platform };
    setBatchReg({ ...stat });
    for (const email of emails) {
      if (stopBatchRegRef.current) break;
      stat.current = email;
      setBatchReg({ ...stat });
      try {
        const res = await api.rerun(email, platform);
        attach(res.run_id);
        await waitForRunFinish(res.run_id);
        const list = await api.listAccounts().catch(() => null);
        if (list) setAccounts(list);
        const updated = (list || []).find((a) => a.email === email);
        const ok = platform === "chatgpt"
          ? updated && updated.chatgpt_last_run_id === res.run_id && hasChatGPT(updated)
          : updated && updated.session_key;
        if (ok) stat.ok += 1;
        else stat.failed += 1;
      } catch (err) {
        stat.failed += 1;
        if (err?.status === 409) {
          toast.error("已有其他注册任务在运行，批量注册已中止");
          break;
        }
      }
      stat.done += 1;
      setBatchReg({ ...stat });
    }
    const stopped = stopBatchRegRef.current;
    setBatchReg(null);
    refreshLists();
    if (stopped) {
      toast.info(`已停止后续排队：已完成 ${stat.done}/${stat.total}（成功 ${stat.ok}，未成功 ${stat.failed}）`);
    } else if (stat.failed) {
      toast.error(`批量注册完成：成功 ${stat.ok} 个，未成功 ${stat.failed} 个`);
    } else {
      toast.success(`批量注册完成：全部成功（${stat.ok} 个）`);
    }
  }

  function applyCheckResult(acctEmail, res) {
    setAccounts((list) =>
      list.map((a) =>
        a.email === acctEmail
          ? {
              ...a,
              check_status: res.status,
              checked_at: res.checked_at,
              check_detail: res.detail,
              org_uuid: res.org_uuid || a.org_uuid,
            }
          : a,
      ),
    );
  }

  async function doCheck(acctEmail) {
    setChecking(acctEmail);
    try {
      applyCheckResult(acctEmail, await api.checkAccount(acctEmail));
    } catch (e) {
      toast.error(`「${acctEmail}」检测失败（${e.status || "?"}）`);
    } finally {
      setChecking("");
    }
  }

  // 批量检测：前端并发 3 路逐个调用单个检测接口，显示进度。
  async function runBatchCheck(emails) {
    const queue = [...emails];
    const stat = { done: 0, total: emails.length, alive: 0, dead: 0, other: 0 };
    setBatchCheck({ ...stat });
    const worker = async () => {
      while (queue.length) {
        const email = queue.shift();
        try {
          const res = await api.checkAccount(email);
          applyCheckResult(email, res);
          if (res.status === "alive") stat.alive += 1;
          else if (res.status === "dead") stat.dead += 1;
          else stat.other += 1;
        } catch (e) {
          if (e?.status === 401) return;
          stat.other += 1;
        }
        stat.done += 1;
        setBatchCheck({ ...stat });
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    setBatchCheck(null);
    const msg = `检测完成：有效 ${stat.alive} 个，失效 ${stat.dead} 个`
      + (stat.other ? `，其他/出错 ${stat.other} 个` : "");
    if (stat.dead || stat.other) toast.error(msg);
    else toast.success(msg);
  }

  const setAuthorizingFlag = (email, on) =>
    setAuthorizing((prev) => {
      const next = new Set(prev);
      if (on) next.add(email);
      else next.delete(email);
      return next;
    });

  // 用 sessionKey 获取 OAuth 令牌；成功返回 true。quiet 时不弹成功提示（批量用）。
  async function runOAuth(acctEmail, { quiet = false } = {}) {
    setAuthorizingFlag(acctEmail, true);
    try {
      const row = await api.accountOAuth(acctEmail);
      setAccounts((list) => list.map((a) => (a.email === acctEmail ? { ...a, ...row } : a)));
      if (!quiet) toast.success(`「${acctEmail}」已获取 OAuth 令牌`);
      return true;
    } catch (e) {
      if (e?.status === 401) return false;
      if (!quiet) {
        toast.error(e.body?.detail || `「${acctEmail}」OAuth 授权失败（${e.status || "?"}）`);
        if (e.status === 422) refreshLists(); // sessionKey 失效时后端已把检测结果记为失效
      }
      return false;
    } finally {
      setAuthorizingFlag(acctEmail, false);
    }
  }

  // 批量授权：前端并发 2 路逐个调用，避免单个长请求被反代超时，也能显示进度。
  async function runBatchOAuth(emails) {
    const queue = [...emails];
    const stat = { done: 0, total: emails.length, ok: 0, failed: 0 };
    setBatchOAuth({ ...stat });
    const worker = async () => {
      while (queue.length) {
        const email = queue.shift();
        const ok = await runOAuth(email, { quiet: true });
        stat.done += 1;
        if (ok) stat.ok += 1;
        else stat.failed += 1;
        setBatchOAuth({ ...stat });
      }
    };
    await Promise.all([worker(), worker()]);
    setBatchOAuth(null);
    refreshLists();
    if (stat.failed) toast.error(`OAuth 授权完成：成功 ${stat.ok} 个，失败 ${stat.failed} 个`);
    else toast.success(`OAuth 授权完成：成功 ${stat.ok} 个`);
  }

  async function copyLine(acct) {
    return copyText(acct.text || acct.email, acct.email);
  }

  async function copyText(text, copiedKey) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // http 面板无 clipboard API，退回 execCommand
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopiedEmail(copiedKey);
      setTimeout(() => setCopiedEmail(""), 1500);
    } catch {
      toast.error("复制失败，请手动复制");
    }
  }

  const startTakeover = async (acctEmail) => {
    try {
      await api.takeoverStart(acctEmail);
      setTakeover(await api.takeoverStatus());
    } catch (e) {
      toast.error(
        e.status === 409
          ? "已有接管会话，请先结束"
          : e.status === 400
            ? "该账号无 sessionKey"
            : e.status === 403
              ? "接管功能已禁用"
              : `启动接管失败（${e.status || "?"}）`,
      );
    }
  };

  const stopTakeover = async () => {
    busyRef.current = true;
    try {
      // 结束前最后取一次，避免刚登录完还没轮询到就把浏览器关了。
      await captureOnce(false).catch(() => {});
      await api.takeoverStop();
      setTakeover({ running: false, email: null });
    } catch {
      /* ignore */
    } finally {
      busyRef.current = false;
    }
  };

  const reloginTakeover = async () => {
    busyRef.current = true;
    setRelogging(true);
    try {
      const res = await api.takeoverRelogin();
      refreshLists();
      toast.success(
        res.check_status === "alive"
          ? `「${res.email}」已重新登录并更新 sessionKey`
          : `「${res.email}」已更新 sessionKey，但存活检测暂时失败`,
      );
    } catch (e) {
      const detail = e.body?.detail;
      toast.error(
        e.status === 409
          ? "当前没有活动的接管会话"
          : e.status === 422
            ? detail || "重新登录失败，账号可能已被封"
            : detail || `重新登录失败（${e.status || "?"}）`,
      );
    } finally {
      busyRef.current = false;
      setRelogging(false);
    }
  };

  const startEdit = (a) => {
    setEditingEmail(a.email);
    const base = emptyAccountForm();
    setEditForm(Object.fromEntries(Object.keys(base).map((k) => [k, a[k] || ""])));
  };

  const saveEdit = async () => {
    if (!String(editForm.email || "").trim()) return toast.error("邮箱不能为空");
    setSaving(true);
    try {
      const row = await api.accountUpdate(editingEmail, editForm);
      if (row.email !== editingEmail) {
        setSelected((prev) => {
          if (!prev.has(editingEmail)) return prev;
          const next = new Set(prev);
          next.delete(editingEmail);
          next.add(row.email);
          return next;
        });
      }
      setEditingEmail("");
      refreshLists();
      toast.success("已保存");
    } catch (e) {
      if (e?.status === 401) return;
      toast.error(e.body?.detail || `保存失败（${e.status || "?"}）`);
    } finally {
      setSaving(false);
    }
  };

  const toggleClaimed = async (a) => {
    const claimed = !a.claimed_at;
    try {
      const row = await api.setAccountClaimed(a.email, claimed);
      setAccounts((list) =>
        list.map((x) => (x.email === a.email ? { ...x, claimed_at: row.claimed_at } : x)),
      );
      toast.success(claimed ? "已标记为已获取" : "已取消「已获取」标记");
    } catch (e) {
      toast.error(`操作失败（${e.status || "?"}）`);
    }
  };

  const confirmDelete = async () => {
    const emails = deleteTargets;
    if (!emails.length) return;
    const withWindows = deleteTargetWindows > 0 && deleteWindows;
    setDeleteTargets([]);
    const windowNote = (res) =>
      withWindows ? `，并删除 ${res.okbrowser_deleted || 0}/${deleteTargetWindows} 个 OKBrowser 窗口` : "";
    try {
      if (emails.length === 1) {
        const res = await api.accountDelete(emails[0], withWindows);
        toast.success(`已删除 ${emails[0]}${windowNote(res)}`);
      } else {
        const res = await api.accountsBatchDelete(emails, withWindows);
        if (!res.deleted) {
          toast.error("没有删除任何账号（可能已不存在）");
        } else {
          toast.success(`已删除 ${res.deleted} 个账号${windowNote(res)}`);
        }
      }
      // 先本地去掉，避免等刷新时仍闪一下旧列表
      setAccounts((list) => list.filter((a) => !emails.includes(a.email)));
      if (emails.includes(editingEmail)) setEditingEmail("");
      setSelected((prev) => {
        const next = new Set(prev);
        emails.forEach((e) => next.delete(e));
        return next;
      });
      refreshLists();
    } catch (e) {
      if (e?.status === 401) return;
      toast.error(e.body?.detail || `删除失败（${e.status || "?"}）`);
      refreshLists();
    }
  };

  return (
    <>
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle>账号列表</CardTitle>
          <span className="flex gap-1.5">
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus /> 新增账号
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setManualOpen(true)}
              disabled={takeover.running}
              title={takeover.running ? "已有接管会话，请先结束" : ""}
            >
              <LogIn /> 手动登录…
            </Button>
            <Button variant="outline" size="sm" onClick={() => setImportOpen(true)}>
              <Upload /> 导入 SK / 邮箱…
            </Button>
            <Button
              variant="outline"
              size="sm"
              title="新建、重命名、删除分组"
              onClick={() => setGroupManageOpen(true)}
            >
              <FolderCog /> 分组管理…
            </Button>
            {accounts.length > 0 && (
              <Button variant="outline" size="sm" onClick={() => setExportOpen(true)}>
                <Download /> 导出…
              </Button>
            )}
          </span>
        </CardHeader>
        <CardContent className="space-y-3">
          {takeover.running && (
            <Alert className="border-blue-500/30 bg-blue-500/10">
              <AlertDescription className="flex w-full items-center justify-between gap-2 text-blue-400">
                <span className="min-w-0">
                  {takeover.mode === "manual" && !takeover.email
                    ? "手动登录中：在接管画面完成登录后，会自动提取 sessionKey 并新建账号"
                    : `正在接管：${takeover.email}`}
                </span>
                <span className="flex items-center gap-2">
                  <Button variant="outline" size="sm" asChild>
                    <a
                      href="/takeover.html"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      打开画面
                    </a>
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={captureNow}
                    disabled={relogging || capturing}
                  >
                    {capturing ? "提取中…" : "提取 sk"}
                  </Button>
                  {takeover.email && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={reloginTakeover}
                      disabled={relogging || capturing}
                    >
                      {relogging ? "重新登录中…" : "重新自动登录"}
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={stopTakeover}
                    disabled={relogging}
                  >
                    结束接管
                  </Button>
                </span>
              </AlertDescription>
            </Alert>
          )}
          {accounts.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-48 flex-1">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-8"
                  value={query}
                  placeholder="搜索邮箱 / 备注 / 代理 / 分组 / sessionKey"
                  spellCheck={false}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </div>
              <select
                className="h-9 max-w-44 rounded-md border bg-background px-2 text-sm"
                aria-label="按分组筛选"
                value={groupFilter}
                onChange={(e) => setGroupFilter(e.target.value)}
              >
                <option value={ALL_GROUPS}>全部分组</option>
                <option value={UNGROUPED}>未分组（{groups.ungrouped_count}）</option>
                {groups.groups.map((g) => (
                  <option key={g.name} value={g.name}>{g.name}（{g.count}）</option>
                ))}
              </select>
              <select
                className="h-9 rounded-md border bg-background px-2 text-sm"
                aria-label="按状态筛选"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                {FILTERS.map(([v, label]) => (
                  <option key={v} value={v}>{label}</option>
                ))}
              </select>
            </div>
          )}
          {accounts.length > 0 && (
            <div
              className={cn(
                "flex min-h-11 flex-wrap items-center gap-2 rounded-lg border px-3 py-1.5",
                selectedVisible.length > 0 ? "border-primary/40 bg-primary/5" : "border-dashed",
              )}
            >
              <label className="flex cursor-pointer items-center gap-2 text-sm select-none">
                <input
                  type="checkbox"
                  className="size-4 accent-primary"
                  checked={allVisibleSelected}
                  disabled={visible.length === 0}
                  onChange={toggleSelectAll}
                />
                {selectedVisible.length > 0 ? `已选 ${selectedVisible.length} 个` : "全选"}
              </label>
              <span className="text-xs text-muted-foreground">
                {visible.length === accounts.length
                  ? `共 ${accounts.length} 个`
                  : `显示 ${visible.length} / ${accounts.length} 个`}
              </span>
              <span className="flex-1" />
              {batchReg && (
                <Button
                  variant="outline"
                  size="sm"
                  title={`当前：${batchReg.current}`}
                  onClick={() => {
                    stopBatchRegRef.current = true;
                    toast.info("将在当前账号跑完后停止后续排队…");
                  }}
                >
                  <Square />
                  停止{batchReg.platform === "chatgpt" ? " ChatGPT " : ""}注册（{batchReg.done}/{batchReg.total}）
                </Button>
              )}
              {[
                [batchCheck, "Claude 检测"],
                [batchOAuth, "OAuth 授权"],
                [batchChatGPTCheck, "ChatGPT 检测"],
              ].map(([b, label]) =>
                b ? (
                  <span key={label} className="text-xs text-muted-foreground">
                    {label}中 {b.done}/{b.total}…
                  </span>
                ) : null,
              )}
              {selectedVisible.length > 0 && (
                <>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        Claude <ChevronDown />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem
                        disabled={running || !!batchReg}
                        onSelect={() => runBatchRegister(selectedVisible.map((a) => a.email))}
                      >
                        <Play /> 注册 / 重跑（{selectedVisible.length}）
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={!!batchCheck || !selectedSk.length}
                        onSelect={() => runBatchCheck(selectedSk)}
                      >
                        <ShieldCheck /> 检测 sessionKey（{selectedSk.length}）
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={!!batchOAuth || !selectedSk.length}
                        onSelect={() => runBatchOAuth(selectedSk)}
                      >
                        <KeyRound /> 获取 OAuth（{selectedSk.length}）
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        ChatGPT <ChevronDown />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem
                        disabled={running || !!batchReg}
                        onSelect={() =>
                          runBatchRegister(selectedVisible.map((a) => a.email), "chatgpt")
                        }
                      >
                        <MessageSquare /> 注册 / 重跑（{selectedVisible.length}）
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={!!batchChatGPTCheck || !selectedGpt.length}
                        onSelect={() => runBatchChatGPTCheck(selectedGpt)}
                      >
                        <ShieldCheck /> 检测会话（{selectedGpt.length}）
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        <FolderInput /> 移动分组 <ChevronDown />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="max-w-64">
                      {groups.groups.map((g) => (
                        <DropdownMenuItem key={g.name} onSelect={() => onBatchGroupSelect(g.name)}>
                          <span className="truncate">{g.name}</span>
                        </DropdownMenuItem>
                      ))}
                      {groups.groups.length > 0 && <DropdownMenuSeparator />}
                      <DropdownMenuItem onSelect={() => onBatchGroupSelect("__new__")}>
                        <Plus /> 新分组…
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => onBatchGroupSelect(UNGROUPED)}>
                        移出分组
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        <Server /> 修改代理 <ChevronDown />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="max-w-72">
                      {savedProxies.length > 0 ? (
                        savedProxies.map((p) => (
                          <DropdownMenuItem
                            key={p.id}
                            title={p.url}
                            onSelect={() => onBatchProxySelect({ proxy_id: p.id }, p.name)}
                          >
                            <span className="truncate">{p.name}</span>
                          </DropdownMenuItem>
                        ))
                      ) : (
                        <DropdownMenuItem onSelect={() => navigate?.("proxies")}>
                          去代理池添加代理…
                        </DropdownMenuItem>
                      )}
                      <DropdownMenuSeparator />
                      <DropdownMenuItem onSelect={() => onBatchProxySelect("__custom__")}>
                        <Plus /> 自定义代理…
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => onBatchProxySelect({ proxy: "" }, "")}>
                        清除代理（直连）
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <Button
                    variant="outline"
                    size="sm"
                    className="text-red-400 hover:text-red-300"
                    onClick={() => setDeleteTargets(selectedVisible.map((a) => a.email))}
                  >
                    <Trash2 /> 删除
                  </Button>
                </>
              )}
            </div>
          )}
          {accounts.length === 0 ? (
            <div className="text-sm text-muted-foreground">暂无账号，可点「新增账号」手动录入</div>
          ) : visible.length === 0 ? (
            <div className="text-sm text-muted-foreground">没有符合条件的账号</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {visible.map((a) => (
                <li key={a.email} className="flex flex-col">
                  <div
                    className={`flex items-center justify-between gap-2 rounded-lg border bg-background/50 px-3 py-2.5 text-sm ${
                      editingEmail === a.email ? "rounded-b-none border-ring" : ""
                    } ${selected.has(a.email) ? "border-primary/60" : ""}`}
                  >
                    <input
                      type="checkbox"
                      className="size-4 shrink-0 accent-primary"
                      aria-label={`选择 ${a.email}`}
                      checked={selected.has(a.email)}
                      onChange={() => toggleSelect(a.email)}
                    />
                    <span className="flex min-w-0 flex-1 flex-col gap-1 overflow-hidden">
                      <span className="truncate" title={a.email}>
                        {a.email}
                      </span>
                      <span className="flex items-center gap-2 overflow-hidden whitespace-nowrap text-xs text-muted-foreground">
                        {a.group_name ? (
                          <Badge
                            className="max-w-32 truncate rounded-full border-transparent bg-sky-500/15 text-sky-400"
                            title={`分组：${a.group_name}`}
                          >
                            {a.group_name}
                          </Badge>
                        ) : null}
                        <PlatformStatusBadge
                          platform="Claude"
                          status={a.status === "pending" ? "" : a.status}
                          title={a.status ? "" : "尚未用此邮箱注册 Claude"}
                        />
                        <LiveBadge
                          status={a.check_status}
                          checkedAt={a.checked_at}
                          detail={a.check_detail}
                        />
                        {a.claimed_at ? (
                          <Badge
                            className="rounded-full border-transparent bg-violet-500/15 text-violet-400"
                            title={`获取时间：${a.claimed_at}`}
                          >
                            已获取
                            <span className="ml-1 font-normal opacity-70">· {relTime(a.claimed_at)}</span>
                          </Badge>
                        ) : null}
                        <OAuthBadge acct={a} />
                        <ChatGPTBadge acct={a} />
                        {a.okbrowser_window_id ? (
                          <Badge
                            variant="outline"
                            className="rounded-full px-1.5 font-mono text-[11px]"
                            title="已绑定的 OKBrowser 指纹浏览器窗口"
                          >
                            OKB #{a.okbrowser_window_id}
                          </Badge>
                        ) : null}
                        {a.display_name ? <span>{a.display_name}</span> : null}
                        {a.session_key ? (
                          <span className="font-mono">
                            sk {String(a.session_key).slice(7, 17)}…
                          </span>
                        ) : null}
                        {a.mail_base_url === "outlook" ? (
                          <Badge variant="outline" className="rounded-full px-1.5 text-[11px]">Outlook</Badge>
                        ) : null}
                        {a.proxy ? (
                          <Badge
                            variant="outline"
                            className="max-w-36 truncate rounded-full px-1.5 text-[11px]"
                            title={`代理：${a.proxy}`}
                          >
                            {proxyNameByUrl.get(a.proxy)
                              ? `代理 · ${proxyNameByUrl.get(a.proxy)}`
                              : "代理"}
                          </Badge>
                        ) : null}
                      </span>
                    </span>
                    <span className="flex shrink-0 gap-1.5">
                      <Button variant="outline" size="sm" onClick={() => copyLine(a)}>
                        {copiedEmail === a.email ? "已复制" : "复制"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className={editingEmail === a.email ? "border-ring text-foreground" : ""}
                        onClick={() =>
                          editingEmail === a.email ? setEditingEmail("") : startEdit(a)
                        }
                      >
                        {editingEmail === a.email ? "收起" : "编辑"}
                      </Button>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="outline" size="sm">
                            {checking === a.email
                              ? "检测中…"
                              : authorizing.has(a.email)
                                ? "授权中…"
                                : "Claude"}
                            <ChevronDown />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem
                            disabled={running || !!batchReg}
                            onSelect={() => doRerun(a.email)}
                          >
                            <Play /> {!a.status || a.status === "pending" ? "注册" : "重跑注册"}
                          </DropdownMenuItem>
                          {a.session_key && (
                            <>
                              <DropdownMenuItem
                                disabled={checking === a.email}
                                onSelect={() => doCheck(a.email)}
                              >
                                <ShieldCheck /> 检测 sessionKey
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                disabled={authorizing.has(a.email)}
                                onSelect={() => runOAuth(a.email)}
                              >
                                <KeyRound /> {a.access_token ? "重新授权 OAuth" : "获取 OAuth"}
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                className="text-blue-400"
                                onSelect={() => startTakeover(a.email)}
                              >
                                <MonitorPlay /> 接管浏览器
                              </DropdownMenuItem>
                            </>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="outline" size="sm">
                            {checkingChatGPT === a.email ? "检测中…" : "ChatGPT"}
                            <ChevronDown />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuLabel>复用此邮箱、代理与窗口</DropdownMenuLabel>
                          <DropdownMenuItem
                            disabled={running || !!batchReg}
                            onSelect={() => doRerun(a.email, "chatgpt")}
                          >
                            <MessageSquare />
                            {a.chatgpt_status === "success" ? "重跑注册" : "注册 ChatGPT"}
                          </DropdownMenuItem>
                          {hasChatGPT(a) && (
                            <DropdownMenuItem
                              disabled={checkingChatGPT === a.email}
                              onSelect={() => doCheckChatGPT(a.email)}
                            >
                              <ShieldCheck /> 检测会话
                            </DropdownMenuItem>
                          )}
                          {a.chatgpt_status && (
                            <DropdownMenuItem
                              disabled={checkingChatGPT === a.email || running || !!batchReg}
                              title="先从绑定的 OKBrowser 窗口同步当前会话，窗口里没有可用会话时自动重新登录"
                              onSelect={() => doResyncChatGPT(a.email)}
                            >
                              <RefreshCw /> 重新获取凭证
                            </DropdownMenuItem>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                      <Button
                        variant="outline"
                        size="sm"
                        title="查询邮件"
                        aria-label={`查询 ${a.email} 的邮件`}
                        onClick={() => setMailEmail(a.email)}
                      >
                        <Mail />
                      </Button>
                      {(okbMode || a.okbrowser_window_id) && (
                        <Button
                          variant="outline"
                          size="sm"
                          title={
                            a.okbrowser_window_id
                              ? `打开 OKBrowser 窗口 #${a.okbrowser_window_id}`
                              : "新建并绑定一个 OKBrowser 窗口"
                          }
                          aria-label={`打开 ${a.email} 的 OKBrowser 窗口`}
                          onClick={() => openWindow(a)}
                          disabled={okbBusy === a.email}
                        >
                          <ExternalLink />
                        </Button>
                      )}
                      <Button
                        variant="outline"
                        size="sm"
                        className="text-red-400 hover:text-red-300"
                        title="删除账号"
                        aria-label={`删除 ${a.email}`}
                        onClick={() => setDeleteTargets([a.email])}
                      >
                        <Trash2 />
                      </Button>
                    </span>
                  </div>
                  {editingEmail === a.email && (
                    <div className="flex flex-col gap-3 rounded-b-lg border border-t-0 border-ring bg-background/50 p-3.5">
                      <AccountFields
                        form={editForm}
                        setForm={setEditForm}
                        disabled={saving}
                        idPrefix={`edit-${a.email}`}
                        groups={groups.groups.map((g) => g.name)}
                        proxies={savedProxies}
                      />
                      {hasChatGPT(a) && (
                        <div className="flex flex-col gap-1.5 rounded-md border bg-muted/30 p-2.5 text-xs">
                          <div className="text-muted-foreground">
                            ChatGPT 凭据 · accessToken 过期 {a.chatgpt_token_expires_at || "未知"}
                            {a.chatgpt_checked_at ? ` · 检测于 ${a.chatgpt_checked_at}` : ""}
                          </div>
                          {CHATGPT_COPY_FIELDS.filter(([k]) => a[k]).map(([k, label]) => (
                            <div key={k} className="flex items-center gap-2">
                              <span className="w-24 shrink-0 text-muted-foreground">{label}</span>
                              <span className="min-w-0 flex-1 truncate font-mono">
                                {k.endsWith("_token") ? `${String(a[k]).slice(0, 18)}…` : a[k]}
                              </span>
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 px-2"
                                onClick={() => copyText(String(a[k]), `${a.email}:${k}`)}
                              >
                                {copiedEmail === `${a.email}:${k}` ? "已复制" : "复制"}
                              </Button>
                            </div>
                          ))}
                          <div className="flex items-center gap-2">
                            <span className="w-24 shrink-0 text-muted-foreground">Session JSON</span>
                            <span className="min-w-0 flex-1 truncate text-muted-foreground">
                              {a.chatgpt_session_json_len
                                ? `/api/auth/session · ${a.chatgpt_session_json_len} 字符`
                                : "尚未获取"}
                            </span>
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 px-2"
                              disabled={sessionBusy === a.email}
                              title="用 sessionToken 重新请求 /api/auth/session 并保存"
                              onClick={() => refreshChatGPTSession(a.email)}
                            >
                              {sessionBusy === a.email ? "获取中…" : "获取最新"}
                            </Button>
                            {a.chatgpt_session_json_len > 0 && (
                              <>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="h-7 px-2"
                                  disabled={sessionBusy === a.email}
                                  onClick={() => toggleSession(a.email)}
                                >
                                  {sessionOpen === a.email ? "收起" : "查看"}
                                </Button>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="h-7 px-2"
                                  disabled={sessionBusy === a.email}
                                  onClick={() => copySession(a.email)}
                                >
                                  {copiedEmail === `${a.email}:chatgpt_session_json` ? "已复制" : "复制"}
                                </Button>
                              </>
                            )}
                          </div>
                          {sessionOpen === a.email && sessionCache[a.email] && (
                            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded border bg-background/60 p-2 font-mono text-[11px] leading-relaxed">
                              {sessionCache[a.email]}
                            </pre>
                          )}
                        </div>
                      )}
                      {a.okbrowser_window_id && (
                        <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 p-2.5 text-xs">
                          <span className="text-muted-foreground">
                            OKBrowser 窗口 <span className="font-mono">#{a.okbrowser_window_id}</span>
                          </span>
                          <span className="flex-1" />
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 px-2"
                            disabled={okbBusy === a.email}
                            onClick={() => openWindow(a)}
                          >
                            打开
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 px-2"
                            disabled={okbBusy === a.email}
                            onClick={() => closeWindow(a)}
                          >
                            关闭
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 px-2"
                            title="只清除绑定，不删除 OKBrowser 里的窗口"
                            disabled={okbBusy === a.email}
                            onClick={() => removeWindow(a, true)}
                          >
                            解绑
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 px-2 text-red-400 hover:text-red-300"
                            disabled={okbBusy === a.email}
                            onClick={() => removeWindow(a, false)}
                          >
                            删除窗口
                          </Button>
                        </div>
                      )}
                      {a.access_token && (
                        <div className="flex flex-col gap-1.5 rounded-md border bg-muted/30 p-2.5 text-xs">
                          <div className="flex items-center justify-between text-muted-foreground">
                            <span>
                              OAuth 令牌 · 授权于 {a.oauth_at || "未知"} · access_token 过期{" "}
                              {a.oauth_expires_at || "未知"}
                            </span>
                          </div>
                          {OAUTH_COPY_FIELDS.filter(([k]) => a[k]).map(([k, label]) => (
                            <div key={k} className="flex items-center gap-2">
                              <span className="w-24 shrink-0 text-muted-foreground">{label}</span>
                              <span className="min-w-0 flex-1 truncate font-mono">
                                {k.endsWith("_token") ? `${String(a[k]).slice(0, 18)}…` : a[k]}
                              </span>
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 px-2"
                                onClick={() => copyText(String(a[k]), `${a.email}:${k}`)}
                              >
                                {copiedEmail === `${a.email}:${k}` ? "已复制" : "复制"}
                              </Button>
                            </div>
                          ))}
                        </div>
                      )}
                      <div className="flex items-center gap-2">
                        <Button size="sm" onClick={saveEdit} disabled={saving}>
                          {saving ? "保存中…" : "保存"}
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => setEditingEmail("")}>
                          取消
                        </Button>
                        <span className="flex-1" />
                        <Button variant="outline" size="sm" onClick={() => toggleClaimed(a)}>
                          {a.claimed_at ? "取消已获取" : "标记已获取"}
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => setDeleteTargets([a.email])}
                        >
                          删除账号
                        </Button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={deleteTargets.length > 0}
        onOpenChange={(o) => !o && setDeleteTargets([])}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除账号？</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTargets.length === 1
                ? `确认删除账号 ${deleteTargets[0]}？此操作不可恢复。`
                : `确认删除所选的 ${deleteTargets.length} 个账号？此操作不可恢复。`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteTargetWindows > 0 && (
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={deleteWindows}
                onChange={(e) => setDeleteWindows(e.target.checked)}
              />
              同时删除关联的 OKBrowser 指纹浏览器窗口（{deleteTargetWindows} 个）
            </label>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={confirmDelete}
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <ExportDialog
        open={exportOpen}
        onOpenChange={setExportOpen}
        selectedEmails={selectedVisible.map((a) => a.email)}
      />
      <AccountCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={refreshLists}
        groups={groups.groups.map((g) => g.name)}
        proxies={savedProxies}
      />
      <GroupManageDialog
        open={groupManageOpen}
        onOpenChange={setGroupManageOpen}
        groups={groups}
        onChanged={refreshLists}
      />
      <MailDialog email={mailEmail} onOpenChange={(o) => !o && setMailEmail("")} />
      <ManualLoginDialog
        open={manualOpen}
        onOpenChange={setManualOpen}
        onStarted={() => api.takeoverStatus().then(setTakeover).catch(() => {})}
      />
      <ImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImported={refreshLists}
        groups={groups.groups.map((g) => g.name)}
      />
    </>
  );
}
