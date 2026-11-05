import { useCallback, useEffect, useRef, useState } from "react";
import { BookmarkMinus, BookmarkPlus, Check, ChevronDown, Clipboard, FlaskConical, FolderInput, Mail, Monitor, Play, RefreshCw, Upload } from "lucide-react";
import { toast } from "sonner";
import { api } from "../api.js";
import { MailDialog } from "@/components/mail-dialog";
import { GroupManageDialog } from "@/components/group-manage-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog as Dialog, AlertDialogContent as DialogContent, AlertDialogDescription as DialogDescription,
  AlertDialogFooter as DialogFooter, AlertDialogHeader as DialogHeader, AlertDialogTitle as DialogTitle,
  AlertDialogCancel as DialogCancel,
} from "@/components/ui/alert-dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";

export const PLATFORMS = { claude: "Claude", chatgpt: "ChatGPT", unassigned: "待归类" };
const STAGES = { register: "注册", detect: "待检测", library: "账号库" };
const MANAGEMENT = { normal: "正常", banned: "封禁", disabled: "停用", other: "其他" };
const CHECK = { unchecked: "未检测", alive: "有效", dead: "登录失效", blocked: "请求受阻", error: "检测失败", banned: "平台确认封禁", skipped: "已跳过" };
const canResync = (row) => ["dead", "blocked", "error"].includes(row.check_status);
const REGISTER = { pending: "待注册", success: "已注册", needs_manual: "待人工处理", phone_required: "待手机验证", banned: "平台确认封禁", failed: "注册失败" };
const PLANS = { free: "Free", pro: "Pro", pro_lite: "Pro Lite", plus: "Plus", go: "Go", max: "Max", max_5x: "Max 5×", max_20x: "Max 20×", team: "Team", business: "Business", enterprise: "Enterprise", edu: "Edu" };
const KYC = { not_required: "无需验证", verified: "已通过", approved: "已通过", completed: "已完成", created: "待完成", pending: "审核中", in_progress: "验证中", submitted: "已提交", needs_review: "待复核", marked_for_review: "待复核", expired: "已过期", denied: "被拒绝", declined: "被拒绝", failed: "验证失败", required: "需要验证", not_flagged: "未见限制" };
const FIELDS = { email: "邮箱", group_name: "分组", display_name: "备注", check_status: "会话检测", proxy: "代理", session_key: "sessionKey", password: "Claude 密码", access_token: "OAuth accessToken", refresh_token: "OAuth refreshToken", chatgpt_password: "ChatGPT 密码", chatgpt_session_token: "sessionToken", chatgpt_access_token: "accessToken", mail_key: "邮箱只读 Key", mail_base_url: "邮箱服务地址" };
const AUTH_FIELDS = { claude: ["session_key", "password"], chatgpt: ["chatgpt_session_token", "chatgpt_access_token", "chatgpt_password"], unassigned: [] };
const selectClass = "h-9 rounded-md border bg-background px-2.5 text-sm";
const tone = (value) => ["alive", "normal", "success"].includes(value) ? "border-emerald-500/30 text-emerald-700 dark:text-emerald-500"
  : ["banned", "dead"].includes(value) ? "border-red-500/30 text-red-700 dark:text-red-400"
    : ["blocked", "error", "other", "disabled"].includes(value) ? "border-amber-500/30 text-amber-800 dark:text-amber-500"
      : "text-muted-foreground";
const stamp = (value) => value ? new Date(value).toLocaleString() : "—";
const subscriptionExpiry = (r) => {
  const value = r.subscription_expires_at;
  if (value) return value.length === 10 ? value : stamp(value);
  if (r.plan === "free") return "不适用";
  return r.subscription_checked_at ? "平台未提供" : "未获取";
};
const accountAge = (registered, checked) => {
  const days = Math.floor((Date.parse(checked) - Date.parse(registered)) / 86400000);
  return days >= 0 ? `${days} 天` : "";
};
const displayEmail = (r) => r.email.endsWith("@sk-import.local") ? r.metadata?.observed_email || r.email : r.email;
const msg = (error) => typeof error.body?.detail === "string" ? error.body.detail : "操作失败，请重试";

function Flag({ labels, value, fallback = "—" }) {
  return <Badge variant="outline" className={tone(value)}>{labels[value] || fallback}</Badge>;
}

function StatusChoices({ value, onSelect, disabled }) {
  return <fieldset disabled={disabled} className="grid grid-cols-2 gap-2 min-[360px]:grid-cols-4">
    <legend className="sr-only">选择账号状态</legend>
    {Object.entries(MANAGEMENT).map(([status, label]) => {
      const selected = value === status;
      return <button key={status} type="button" aria-pressed={selected} onClick={() => onSelect(status)}
        className={`flex min-h-9 cursor-pointer items-center justify-center gap-1.5 rounded-md border px-2 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-60 ${selected ? `bg-accent ${tone(status)}` : "bg-background text-muted-foreground hover:bg-accent hover:text-foreground"}`}>
        {selected && <Check className="size-3.5 shrink-0" aria-hidden="true" />}{label}
      </button>;
    })}
  </fieldset>;
}

function RowAction({ icon: Icon, label, short = label, wideOnly = false, ...props }) {
  return <Button type="button" size="sm" variant="ghost" className="gap-1 px-1.5 has-[>svg]:px-1.5"
    aria-label={label} title={label} {...props}>
    <Icon aria-hidden="true" />
    <span className={wideOnly ? "hidden 2xl:inline" : "hidden xl:inline"}>{short}</span>
  </Button>;
}

export function WorkspaceHeader({ stage, platform, onPlatform }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground" aria-label="账号流程">
          {Object.entries(STAGES).map(([key, name], i) => (
            <span key={key} className="flex items-center gap-1.5">
              {i > 0 && <span aria-hidden="true">→</span>}
              <span className={stage === key ? "font-semibold text-foreground" : ""}>{name}</span>
            </span>
          ))}
        </div>
        <h1 className="text-2xl font-semibold">{stage === "register" ? "注册工作台" : STAGES[stage]}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {stage === "register" ? "导入邮箱、发起注册和处理失败任务。注册成功后进入待检测。"
            : stage === "detect" ? "检查登录是否可用、实际套餐及平台返回的限制。结果明确后自动进入账号库。"
            : "查看实际套餐、管理账号状态，或复查已有账号。"}
        </p>
      </div>
      <div className="flex gap-1 rounded-lg border bg-muted/30 p-1" role="group" aria-label="平台">
        {Object.entries(PLATFORMS).filter(([key]) => key !== "unassigned" || stage === "register").map(([key, name]) => (
          <Button key={key} size="sm" variant={platform === key ? "secondary" : "ghost"} aria-pressed={platform === key} onClick={() => onPlatform(key)}>{name}</Button>
        ))}
      </div>
    </div>
  );
}

export default function Workspace({ platform, stage, runStream, initialGroup = "", onRun }) {
  const [rows, setRows] = useState(null);
  const [loadError, setLoadError] = useState(false);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState(initialGroup);
  const [status, setStatus] = useState("");
  const [check, setCheck] = useState("");
  const [plan, setPlan] = useState("");
  const [selection, setSelection] = useState(() => new Set());
  const [detail, setDetail] = useState(null);
  const [mail, setMail] = useState("");
  const [modal, setModal] = useState(null);
  const [groupDialog, setGroupDialog] = useState(null);
  const [busy, setBusy] = useState(false);
  const [checkResults, setCheckResults] = useState(null);
  const [resyncProgress, setResyncProgress] = useState(null);
  const generation = useRef(0);
  const isRegister = stage === "register";
  const isLibrary = stage === "library";
  const assigned = platform !== "unassigned";
  async function load() {
    const id = ++generation.current;
    setLoadError(false);
    try {
      const result = await api.workspace(platform, stage);
      if (generation.current === id) setRows(result.accounts);
    } catch {
      if (generation.current === id) setLoadError(true);
    }
  }
  useEffect(() => {
    void load();
    return () => { generation.current++; };
  }, [runStream.streamEpoch, platform, stage]);
  const all = rows || [];
  const groups = [...new Set(all.map((row) => row.group_name).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const needle = query.trim().toLowerCase();
  const visible = all.filter((r) =>
    (!needle || [r.email, displayEmail(r), r.note, r.group_name, r.proxy].some((v) => v?.toLowerCase().includes(needle)))
    && (!group || (group === "__ungrouped__" ? !r.group_name : r.group_name === group))
    && (!status || r.management_status === status)
    && (!check || (check === "repair" ? canResync(r) : r.check_status === check))
    && (!plan || (plan === "paid" ? r.plan && r.plan !== "free" : plan === "unknown" ? !r.plan : r.plan === plan)));
  const selected = visible.filter((r) => selection.has(r.id));
  const ids = selected.map((r) => r.id);
  const resyncIds = selected.filter(canResync).map((r) => r.id);
  const toggle = (id) => setSelection((before) => {
    const next = new Set(before);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const filter = (set) => (event) => { set(event.target.value); setSelection(new Set()); setCheckResults(null); };
  async function manageGroups() {
    try { setGroupDialog((await api.listGroups()).groups); }
    catch (error) { toast.error(msg(error)); }
  }

  async function batch(action, targets, fields = {}) {
    if (busy) return false;
    setBusy(true);
    try {
      if (action === "export") {
        const blob = await api.workspaceExport(platform, stage, { ids: targets, ...fields });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${platform}-${stage}.csv`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast.success("已导出所选账号");
      } else if (action === "detect") {
        const response = await api.workspaceBatch(platform, stage, { ids: targets, action });
        setCheckResults({ action, items: response.results });
        const moved = response.results.filter((r) => r.stage === "library").length;
        const finished = response.results.filter((r) => r.status !== "skipped").length;
        toast.success(stage === "detect" ? `已检测 ${finished} 个，${moved} 个进入账号库` : `已检测 ${finished} 个`);
        setSelection(new Set());
        await load();
      } else if (action === "resync") {
        const items = [];
        setCheckResults(null);
        setResyncProgress({ done: 0, total: targets.length });
        for (const id of targets) {
          try {
            const response = await api.workspaceBatch(platform, stage, { ids: [id], action });
            items.push(...response.results);
          } catch (error) {
            if (error.status === 401) throw error;
            items.push({ id, email: all.find((r) => r.id === id)?.email, status: "skipped", detail: msg(error) });
          }
          setResyncProgress({ done: items.length, total: targets.length });
        }
        setCheckResults({ action, items });
        const updated = items.filter((r) => r.status === "alive").length;
        const message = `已同步 ${updated} 个${updated < items.length ? `，${items.length - updated} 个未更新` : ""}`;
        if (updated) toast.success(message);
        else toast.error(items.length === 1 ? items[0].detail : message);
        setSelection(new Set());
        await load();
      } else {
        await api.workspaceBatch(platform, stage, { ids: targets, action, ...fields });
        if (action === "delete") setDetail(null);
        if (action === "status") toast.success(`已标记为「${MANAGEMENT[fields.status]}」`);
        else toast.success(action === "claimed" ? (fields.claimed ? "已标记为已领取" : "已取消领取标记") : "已更新");
        setSelection(new Set());
        await load();
      }
      return true;
    } catch (error) { toast.error(msg(error)); return false; }
    finally { setBusy(false); setResyncProgress(null); }
  }

  async function recordAction(id, action, body) {
    setBusy(true);
    try {
      const out = await api.workspaceRecordAction(platform, id, action, body);
      if (out.run_id) { runStream.attach(out.run_id, platform); onRun?.(); }
      if (action === "assign") { setDetail(null); setSelection(new Set()); }
      toast.success(action === "register" ? "已启动注册" : action === "window" ? "已打开浏览器环境" : "已更新");
      await load();
      return true;
    } catch (error) { toast.error(msg(error)); return false; }
    finally { setBusy(false); }
  }

  return (
    <section className="min-w-0 space-y-3 rounded-xl border bg-card p-4 shadow-sm max-sm:p-3" aria-label={isRegister ? "待注册账号" : "账号列表"}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><h2 className="font-semibold">{isRegister ? (assigned ? "待注册与待处理" : "待归类邮箱") : `${PLATFORMS[platform]} 账号`}</h2>
          <p className="text-xs text-muted-foreground">{!assigned ? "这些旧记录没有明确的平台归属。指定平台后会进入对应注册列表。" : `共 ${all.length} 个账号`}</p>
        </div>
        <div className="flex gap-2">
          {isLibrary && <Button size="sm" variant="outline" onClick={manageGroups} disabled={busy}>管理分组</Button>}
          <Button size="sm" variant="outline" onClick={load} disabled={busy}><RefreshCw /> 刷新</Button>
          {assigned && <Button size="sm" variant={isRegister ? "outline" : "default"} disabled={busy} onClick={() => setModal({ action: "import" })}><Upload /> {isRegister ? "导入邮箱" : "导入凭据"}</Button>}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Input className="min-w-44 flex-1" aria-label="搜索账号" placeholder="搜索邮箱、备注、分组或代理" value={query} onChange={filter(setQuery)} />
        <select className={selectClass} aria-label="分组" value={group} onChange={filter(setGroup)}>
          <option value="">全部分组</option><option value="__ungrouped__">未分组</option>
          {[...new Set([...groups, ...(initialGroup && initialGroup !== "__ungrouped__" ? [initialGroup] : [])])].map((g) => <option key={g}>{g}</option>)}
        </select>
        {assigned && <select className={selectClass} aria-label="账号状态" value={status} onChange={filter(setStatus)}>
          <option value="">全部账号状态</option>{Object.entries(MANAGEMENT).map(([v, name]) => <option key={v} value={v}>{name}</option>)}
        </select>}
        {!isRegister && <select className={selectClass} aria-label="检测结果" value={check} onChange={filter(setCheck)}>
          <option value="">全部检测结果</option><option value="repair">需要修复</option>{Object.entries(CHECK).filter(([v]) => v !== "skipped").map(([v, name]) => <option key={v} value={v}>{name}</option>)}
        </select>}
        {isLibrary && <select className={selectClass} aria-label="套餐" value={plan} onChange={filter(setPlan)}>
          <option value="">全部套餐</option><option value="paid">已订阅</option><option value="free">免费</option><option value="unknown">套餐未知</option>
        </select>}
      </div>
      {!!ids.length && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/25 bg-primary/5 p-2 text-sm">
          <span className="mr-1">已选 {ids.length} 个</span>
          {!isRegister && <Button size="sm" disabled={busy} onClick={() => batch("detect", ids)}><FlaskConical /> 检测所选</Button>}
          {!isRegister && !!resyncIds.length && <Button size="sm" variant="outline" disabled={busy} onClick={() => batch("resync", resyncIds)}>
            <RefreshCw /> 同步 OKBrowser 会话（{resyncIds.length}）
          </Button>}
          {assigned && <Button size="sm" variant="outline" disabled={busy} onClick={() => setModal({ action: "status", ids })}>修改状态</Button>}
          <Button size="sm" variant="outline" disabled={busy} onClick={() => setModal({ action: "group", ids })}><FolderInput /> 移动分组</Button>
          {assigned && <Button size="sm" variant="outline" disabled={busy} onClick={() => setModal({ action: "proxy", ids })}>设置代理</Button>}
          <Button size="sm" variant="outline" disabled={busy} onClick={() => setModal({ action: "export", ids })}>导出</Button>
          <Button size="sm" variant="ghost" onClick={() => setSelection(new Set())}>清除</Button>
        </div>
      )}
      {resyncProgress && <p role="status" className="rounded-md border bg-muted/30 p-2 text-sm">
        正在从 OKBrowser 同步并验证会话 · {resyncProgress.done} / {resyncProgress.total}
      </p>}
      {!!checkResults?.items.length && <details open={checkResults.action === "resync" || undefined} className="rounded-md border bg-muted/30 p-2 text-xs">
        <summary className="cursor-pointer">{checkResults.action === "resync" ? "OKBrowser 同步结果" : "本批检测结果"} · {checkResults.items.length} 个</summary>
        <ul className="mt-2 space-y-1">{checkResults.items.map((r) => <li key={r.id}>{r.email || `账号 #${r.id}`} · {checkResults.action === "resync" && r.status === "skipped" ? "未更新" : CHECK[r.status]} · {r.detail}</li>)}</ul>
      </details>}
      {loadError ? <p className="py-8 text-center text-sm text-destructive">列表加载失败，请点击刷新。</p>
        : rows === null ? <p className="py-8 text-center text-sm text-muted-foreground">正在加载…</p>
          : !visible.length ? <div className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
            {all.length ? "没有符合筛选条件的账号。" : !assigned ? "没有待归类的邮箱。" : isRegister ? "还没有待处理的账号，可以导入邮箱或发起注册。" : isLibrary ? "这里会显示检测完成或直接导入的账号。" : "注册成功的账号会出现在这里。"}
          </div> : (
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-left text-sm [&_td]:px-3 [&_td]:py-3 [&_th]:px-3 [&_th]:py-2">
                <thead className="whitespace-nowrap bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th scope="col"><input type="checkbox" aria-label="全选当前可见账号" checked={ids.length === visible.length} ref={(el) => { if (el) el.indeterminate = ids.length > 0 && ids.length < visible.length; }} onChange={(e) => setSelection(new Set(e.target.checked ? visible.map((r) => r.id) : []))} /></th>
                    <th scope="col">邮箱 · 分组</th>
                    {!isRegister && <th scope="col">套餐</th>}
                    {!isRegister && <th scope="col">套餐到期时间</th>}
                    {assigned && <th scope="col">账号状态</th>}
                    {assigned && <th scope="col">{isRegister ? "注册结果" : "会话检测"}</th>}
                    <th scope="col" className="hidden lg:table-cell">代理</th>
                    <th scope="col" className="text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {visible.map((r) => (
                    <tr key={r.id} className={selection.has(r.id) ? "bg-primary/5" : "hover:bg-muted/25"}>
                      <td><input type="checkbox" aria-label={`选择 ${r.email}`} checked={selection.has(r.id)} onChange={() => toggle(r.id)} /></td>
                      <td className="min-w-36 max-w-60">
                        <div className="break-all font-medium">{displayEmail(r)}</div>
                        <div className="truncate text-xs text-muted-foreground" title={r.note}>{[r.group_name || "未分组", r.note, r.claimed_at && "已领取"].filter(Boolean).join(" · ")}</div>
                      </td>
                      {!isRegister && <td className="whitespace-nowrap" title={r.plan_checked_at ? `套餐上次确认：${stamp(r.plan_checked_at)}` : undefined}><Flag labels={PLANS} value={r.plan} fallback="未知" /></td>}
                      {!isRegister && <td className={`whitespace-nowrap text-xs ${r.subscription_expires_at ? "tabular-nums" : "text-muted-foreground"}`}
                        title={r.subscription_checked_at ? `上次查询：${stamp(r.subscription_checked_at)}` : undefined}>{subscriptionExpiry(r)}</td>}
                      {assigned && <td>
                        <button type="button" className="inline-flex items-center gap-1 rounded-md text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                          aria-label={`修改账号状态：${MANAGEMENT[r.management_status]}`} title="修改账号状态" disabled={busy}
                          onClick={() => setModal({ action: "status", ids: [r.id], row: r })}>
                          <Flag labels={MANAGEMENT} value={r.management_status} /><ChevronDown aria-hidden="true" className="size-3" />
                        </button>
                      </td>}
                      {assigned && <td className="min-w-24">
                        <Flag labels={isRegister ? REGISTER : CHECK} value={isRegister ? r.registration_status : r.check_status} fallback="待注册" />
                        {r.checked_at && <div className="mt-1 text-xs text-muted-foreground" title={r.check_detail}>{stamp(r.checked_at)}</div>}
                      </td>}
                      <td className="hidden max-w-36 truncate text-xs text-muted-foreground lg:table-cell" title={r.proxy}>{r.proxy || "—"}</td>
                      <td className="w-px"><div className="flex items-center justify-end gap-0.5 whitespace-nowrap">
                        {!isRegister && <RowAction icon={FlaskConical} label="检测账号" short="检测" disabled={busy} onClick={() => batch("detect", [r.id])} />}
                        {!isRegister && canResync(r) && <RowAction icon={RefreshCw} label="同步 OKBrowser 会话" short="同步" disabled={busy} onClick={() => batch("resync", [r.id])} />}
                        {isRegister && assigned && <RowAction icon={Play} label="开始注册 / 重试" short="注册" disabled={busy || runStream.activeStatus === "running"} onClick={() => recordAction(r.id, "register")} />}
                        {!assigned && <Button type="button" size="sm" variant="ghost" onClick={() => setModal({ action: "assign", row: r })}>指定平台</Button>}
                        <RowAction icon={Mail} label="查看邮箱" short="邮件" onClick={() => setMail(r.email)} />
                        {assigned && <RowAction icon={Monitor} label="打开 OKBrowser" short="OKBrowser" wideOnly disabled={busy} onClick={() => recordAction(r.id, "window")} />}
                        <Button type="button" size="sm" variant="ghost" className="px-2" onClick={() => setDetail(r)}>详情</Button>
                        {isLibrary && <RowAction icon={r.claimed_at ? BookmarkMinus : BookmarkPlus}
                          label={r.claimed_at ? "取消已领取" : "标记已领取"} disabled={busy}
                          onClick={() => batch("claimed", [r.id], { claimed: !r.claimed_at })} />}
                      </div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="border-t px-3 py-2 text-xs text-muted-foreground">显示 {visible.length} / {all.length} 个 · 全选仅限当前可见账号</div>
            </div>
          )}
      {detail && <Details key={detail.id} row={all.find((r) => r.id === detail.id) || detail} platform={platform} close={() => setDetail(null)} busy={busy}
        onStatus={(status) => batch("status", [detail.id], { status })}
        onDelete={() => { setDetail(null); setModal({ action: "delete", ids: [detail.id], row: detail }); }}
        onAction={(action) => recordAction(detail.id, action)} onSaved={load} />}
      {modal && <ActionDialog platform={platform} stage={stage} modal={modal} close={() => setModal(null)}
        onBatch={batch} onRecord={recordAction} onImported={load} />}
      <GroupManageDialog open={groupDialog !== null} groups={groupDialog || []} onOpenChange={(open) => !open && setGroupDialog(null)}
        onChanged={() => { setSelection(new Set()); setGroup(""); void load(); void manageGroups(); }} />
      <MailDialog email={mail} onOpenChange={(open) => !open && setMail("")} />
    </section>
  );
}

function Details({ row, platform, close, busy, onStatus, onDelete, onAction, onSaved }) {
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const request = useRef(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setLoadError("");
    try {
      const next = await api.workspaceDetail(platform, row.id, controller.signal);
      if (!controller.signal.aborted) setData(next);
    } catch (err) {
      if (!controller.signal.aborted) setLoadError(msg(err));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [platform, row.id]);
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);
  async function save(event) {
    event.preventDefault();
    setLoading(true);
    try {
      await api.workspaceEdit(platform, row.id, draft);
      setEditing(false);
      setDraft({});
      await onSaved();
      if (!request.current?.signal.aborted) await load();
      toast.success("已保存");
    } catch (err) { toast.error(msg(err)); setLoading(false); }
  }
  async function copy(value) {
    try { await navigator.clipboard.writeText(value); toast.success("已复制"); }
    catch { toast.error("复制失败"); }
  }
  const r = data || row;
  async function selectStatus(status) {
    if (status !== r.management_status && await onStatus(status) && !request.current?.signal.aborted) {
      await load();
    }
  }
  const info = [
    ["工作区", STAGES[r.stage]], ["分组", r.group_name || "未分组"], ["备注", r.note],
    ["注册时间", stamp(r.registered_at)], ["代理", row.proxy], ["共享 OKBrowser 窗口", r.window_id],
    ["最近注册任务", r.run_id ? `#${r.run_id}` : ""],
    ...(r.stage === "register" ? [] : [
      ["套餐", PLANS[r.plan] || "未知"], ["套餐确认时间", stamp(r.plan_checked_at)],
      ["套餐到期时间", subscriptionExpiry(r)], ["到期时间查询于", stamp(r.subscription_checked_at)],
      ["检测结果", CHECK[r.check_status]], ["检测时间", stamp(r.checked_at)], ["检测说明", r.check_detail],
      ...(platform === "claude" ? [
        ["账号年龄（检测时）", accountAge(r.registered_at, r.checked_at)], ["组织 UUID", r.org_uuid],
        ["组织名称", r.metadata?.org_name], ["计费类型", r.metadata?.billing_type], ["商户", r.metadata?.merchant],
        ["能力", r.metadata?.capabilities?.join("、")],
        ["订阅 KYC", KYC[r.subscription_kyc] || (r.subscription_kyc === "unknown" ? "未知" : r.subscription_kyc)],
        ["Portal KYC", KYC[r.portal_kyc] || "未知"],
      ] : []),
    ]),
  ];
  const editable = ["group_name", "note", "proxy", ...AUTH_FIELDS[platform], "mail_base_url", "mail_key"];
  const credentials = [...AUTH_FIELDS[platform], "access_token", "refresh_token", "mail_key"]
    .filter((key) => data?.credentials[key]);
  return <Sheet open onOpenChange={(open) => !open && close()}>
    <SheetContent className="w-full gap-0 sm:max-w-xl">
      <SheetHeader>
        <SheetTitle className="break-all pr-6">{displayEmail(r)}</SheetTitle>
        <SheetDescription>{PLATFORMS[platform]} · 账号详情</SheetDescription>
      </SheetHeader>
      <div className="min-h-0 space-y-5 overflow-y-auto px-4 pb-5">
        {platform !== "unassigned" && <section aria-label="修改账号状态" className="space-y-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-medium">账号状态</h3>
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">当前 <Flag labels={MANAGEMENT} value={r.management_status} /></div>
          </div>
          <StatusChoices value={r.management_status} onSelect={selectStatus} disabled={busy || loading} />
        </section>}
        <dl className="grid grid-cols-2 gap-3 rounded-lg border p-3 text-sm">
          {info.map(([title, val]) => <div key={title} className="min-w-0"><dt className="text-xs text-muted-foreground">{title}</dt><dd className="break-words">{val || "—"}</dd></div>)}
        </dl>
        {r.stage !== "register" && r.portal_kyc === "not_flagged" && <p className="text-xs text-muted-foreground">Portal 未见限制仅表示平台未报告身份验证限制。</p>}
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-medium">凭据与环境</h3>
            {data && !editing && <Button size="sm" variant="outline" disabled={loading} onClick={() => {
              setDraft(Object.fromEntries(editable.map((key) => [key, data.credentials[key] ?? data[key] ?? ""])));
              setEditing(true);
            }}>编辑资料 / 更新登录凭据</Button>}
          </div>
          {!data && loading && <p role="status" className="text-sm text-muted-foreground">正在加载凭据和状态历史…</p>}
          {loadError && <div className="flex items-center gap-2 text-sm">
            <p role="alert" className="text-destructive">详情加载失败：{loadError}</p>
            <Button size="sm" variant="outline" onClick={load}>重试</Button>
          </div>}
          {data && !editing && (credentials.length ? <dl className="divide-y rounded-lg border">
            {credentials.map((key) => {
              const value = data.credentials[key];
              const preview = key.includes("password") || value.length < 14 ? "••••••••" : `${value.slice(0, 6)}…${value.slice(-4)}`;
              return <div key={key} className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
                <dt>{FIELDS[key]}</dt>
                <dd className="flex items-center gap-2">
                  <code className="text-xs text-muted-foreground">{preview}</code>
                  <Button type="button" size="icon-xs" variant="ghost" title={`复制 ${FIELDS[key]}`} aria-label={`复制 ${FIELDS[key]}`} onClick={() => copy(value)}><Clipboard /></Button>
                </dd>
              </div>;
            })}
          </dl> : <p className="text-sm text-muted-foreground">暂无已保存的凭据。</p>)}
          {editing && <form className="space-y-2" onSubmit={save}>
            {editable.map((key) => <label className="block space-y-1 text-xs" key={key}>{FIELDS[key] || "备注"}<Input value={draft[key]} type={["group_name", "note", "mail_base_url"].includes(key) ? "text" : "password"} autoComplete="off" onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} /></label>)}
            <p className="text-xs text-muted-foreground">邮箱收件凭据和已绑定的浏览器窗口由同一邮箱共享。</p>
            <Button size="sm" disabled={loading}>保存</Button>{" "}
            <Button size="sm" type="button" variant="ghost" onClick={() => { setEditing(false); setDraft({}); }}>取消</Button>
          </form>}
        </div>
        <div className="space-y-2">
          <h3 className="font-medium">状态历史</h3>
          {data && (data.history.length ? <ul className="space-y-2 text-sm">{data.history.map((item, i) => (
            <li key={i} className="rounded border p-2"><div>{MANAGEMENT[item.old_status]} → {MANAGEMENT[item.new_status]}{item.reason && ` · ${item.reason}`}</div>
              <div className="text-xs text-muted-foreground">{stamp(item.changed_at)} · {({ manual: "人工", detection: "平台检测", migration: "旧数据迁移", registration: "注册结果" })[item.source] || item.source}</div></li>
          ))}</ul> : <p className="text-sm text-muted-foreground">暂无状态变更。</p>)}
        </div>
        <div className="flex flex-wrap gap-2 border-t pt-4">
          {platform === "claude" && r.stage === "library" && <Button variant="outline" size="sm" disabled={busy || loading} onClick={async () => { if (await onAction("oauth") && !request.current?.signal.aborted) await load(); }}>获取 Claude OAuth</Button>}
          <Button variant="ghost" className="text-destructive" size="sm" disabled={busy} onClick={onDelete}>删除账号</Button>
        </div>
      </div>
    </SheetContent>
  </Sheet>;
}

function ActionDialog({ modal, platform, stage, close, onBatch, onRecord, onImported }) {
  const { action, ids, row } = modal;
  const [group, setGroup] = useState("");
  const [proxy, setProxy] = useState("");
  const [target, setTarget] = useState(action === "assign" ? "" : stage);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState(null);
  const [fields, setFields] = useState(["email"]);
  const [saving, setSaving] = useState(false);
  const titles = { status: "修改账号状态", group: "移动分组", proxy: "设置代理", export: "导出所选账号", delete: "删除账号", import: stage === "register" ? "导入待注册邮箱" : "导入已有凭据", assign: "指定账号平台" };
  const importLabel = stage === "register" ? "每行一个邮箱，或一行 Outlook 邮箱凭据。"
    : platform === "claude" ? "每行一个 sessionKey，可以在同一行附上邮箱。"
      : "每行格式：邮箱----sessionToken；可在第三段附上 accessToken。";
  const exportFields = ["email", "group_name", "display_name", "check_status", "proxy", ...AUTH_FIELDS[platform], ...(platform === "claude" ? ["access_token", "refresh_token"] : [])];
  async function selectStatus(status) {
    setSaving(true);
    try {
      if (await onBatch("status", ids, { status })) close();
    } finally { setSaving(false); }
  }
  async function submit(event) {
    event.preventDefault();
    setSaving(true);
    try {
      if (action === "import") {
        const commit = !!preview;
        const result = await api.workspaceImport(platform, target, { text, group_name: group, proxy, commit });
        if (!commit) { setPreview(result.items); return; }
        const added = result.items.filter((r) => r.result === "added").length;
        toast.success(`已导入 ${added} 个，跳过 ${result.items.length - added} 个`);
        await onImported();
        close();
      } else if (action === "assign") {
        if (await onRecord(row.id, "assign", { platform: target })) close();
      } else {
        const body = action === "group" ? { group_name: group } : action === "proxy" ? { proxy } : action === "export" ? { fields } : {};
        if (await onBatch(action, ids, body)) close();
      }
    } catch (err) { toast.error(msg(err)); }
    finally { setSaving(false); }
  }
  const disabled = saving || (action === "export" && !fields.length)
    || (action === "assign" && !target) || (action === "import" && (!text.trim() || preview && !preview.some((r) => r.result === "ready")));
  return <Dialog open onOpenChange={(open) => !open && !saving && close()}>
    <DialogContent>
      <DialogHeader><DialogTitle>{titles[action]}</DialogTitle>
        <DialogDescription>{PLATFORMS[platform]}{ids && ` · ${ids.length} 个账号`}{row && ` · ${row.email}`}</DialogDescription>
      </DialogHeader>
      {action === "status" ? <>
        <StatusChoices value={row?.management_status} onSelect={selectStatus} disabled={saving} />
        <DialogFooter><DialogCancel type="button" disabled={saving}>取消</DialogCancel></DialogFooter>
      </> : <form onSubmit={submit} className="space-y-4">
        {action === "delete" && <p className="text-sm">删除这条 {PLATFORMS[platform]} 账号记录及其凭据和状态历史？同邮箱的另一平台账号会保留。</p>}
        {action === "assign" && <select className={`${selectClass} w-full`} aria-label="目标平台" value={target} onChange={(e) => setTarget(e.target.value)}><option value="">选择目标平台</option><option value="claude">Claude</option><option value="chatgpt">ChatGPT</option></select>}
        {(action === "group" || action === "import") && <label className="block space-y-1 text-sm">分组<Input value={group} onChange={(e) => { setGroup(e.target.value); setPreview(null); }} maxLength={64} placeholder={action === "group" ? "留空则移至未分组" : "可选"} /></label>}
        {(action === "proxy" || action === "import") && <label className="block space-y-1 text-sm">代理<Input type="password" autoComplete="off" value={proxy} onChange={(e) => { setProxy(e.target.value); setPreview(null); }} placeholder={action === "proxy" ? "代理地址，留空则清除" : "代理地址（可选）"} /></label>}
        {action === "export" && <div className="grid grid-cols-2 gap-2">
          {exportFields.map((key) => <label className="flex items-center gap-2 text-sm" key={key}><input type="checkbox" checked={fields.includes(key)} onChange={(e) => setFields(e.target.checked ? [...fields, key] : fields.filter((item) => item !== key))} />{FIELDS[key]}</label>)}
        </div>}
        {action === "import" && <>
          {stage !== "register" && <label className="block space-y-1 text-sm">导入到<select className={`${selectClass} block w-full`} value={target} onChange={(e) => { setTarget(e.target.value); setPreview(null); }}><option value="library">账号库</option><option value="detect">待检测</option></select></label>}
          <label className="block space-y-1 text-sm">{importLabel}
            <textarea className="block min-h-28 w-full rounded-md border bg-background p-2 font-mono text-sm" value={text} onChange={(e) => { setText(e.target.value); setPreview(null); }} spellCheck={false} autoComplete="off" />
          </label>
          {preview && <ul className="max-h-40 space-y-1 overflow-auto rounded border p-2 text-xs">
            {preview.map((r) => <li className="flex items-center gap-2" key={r.line}>{r.result === "ready" && <Check className="size-3 text-emerald-700 dark:text-emerald-500" />} 第 {r.line} 行 {r.email} · {r.result === "ready" ? "可导入" : r.result === "duplicate" ? "重复，将跳过" : r.detail}</li>)}
          </ul>}
        </>}
        <DialogFooter>
          <DialogCancel type="button" disabled={saving}>取消</DialogCancel>
          <Button type="submit" variant={action === "delete" ? "destructive" : "default"} disabled={disabled}>
            {saving ? "处理中…" : action === "import" ? (preview ? "确认导入" : "预览导入") : action === "delete" ? "确认删除" : "确认"}
          </Button>
        </DialogFooter>
      </form>}
    </DialogContent>
  </Dialog>;
}
