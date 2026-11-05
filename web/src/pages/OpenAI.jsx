import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ChevronDown, ChevronRight, Copy, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../api.js";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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

const STATUS = {
  ok: ["正常", "bg-emerald-500/15 text-emerald-400"],
  invalid: ["Key 无效", "bg-red-500/15 text-red-400"],
  error: ["刷新失败", "bg-amber-500/15 text-amber-400"],
};

const usd = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

function fmtTime(value) {
  if (value == null || value === "") return "";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

// OpenAI 的 redacted_value 中间是上百个 *，只保留前缀和末 4 位
const shortRedacted = (v) => (v ? v.replace(/\*{4,}/, "…") : "");

const errText = (e, fallback) => e.body?.detail || `${fallback}（${e.status || "?"}）`;

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success("已复制");
  } catch {
    toast.error("复制失败，请手动选择复制");
  }
}

function Field({ id, label, children }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Label htmlFor={id} className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </Label>
      {children}
    </div>
  );
}

function KeyDialog({ open, onOpenChange, editing, onSaved }) {
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setForm({
      name: editing?.name || "",
      admin_key: "",
      credit_usd: editing?.credit_usd ?? "",
      credit_since: editing?.credit_since || "",
    });
  }, [open, editing]);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  async function save() {
    if (!form.name.trim()) return toast.error("请填写名称");
    if (!editing && !form.admin_key.trim()) return toast.error("请填写 Admin Key");
    setBusy(true);
    try {
      const body = { ...form, credit_usd: form.credit_usd === "" ? null : form.credit_usd };
      const row = editing
        ? await api.openaiKeyUpdate(editing.id, body)
        : await api.openaiKeyCreate(body);
      toast.success(editing ? "已保存" : `已添加「${row.name}」`);
      onOpenChange(false);
      onSaved(row);
    } catch (e) {
      if (e?.status !== 401) toast.error(errText(e, "保存失败"), { duration: 8000 });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <AlertDialogContent className="max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle>{editing ? "编辑 Admin Key" : "新增 Admin Key"}</AlertDialogTitle>
          <AlertDialogDescription>
            在 platform.openai.com → Settings → Admin keys 创建（需要组织 Owner）。
            剩余额度 = 额度 − 起算日至今的费用。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="grid grid-cols-2 gap-2.5">
          <div className="col-span-2">
            <Field id="oa-name" label="名称">
              <Input id="oa-name" value={form.name ?? ""} onChange={set("name")}
                placeholder="如：主组织" disabled={busy} />
            </Field>
          </div>
          <div className="col-span-2">
            <Field id="oa-key" label="Admin Key">
              <Input id="oa-key" className="font-mono text-xs" value={form.admin_key ?? ""}
                onChange={set("admin_key")} spellCheck={false} disabled={busy}
                placeholder={editing ? `${editing.key_masked}（留空不修改）` : "sk-admin-…"} />
            </Field>
          </div>
          <Field id="oa-credit" label="额度（USD，可空）">
            <Input id="oa-credit" type="number" min="0" step="0.01" value={form.credit_usd ?? ""}
              onChange={set("credit_usd")} placeholder="如：50" disabled={busy} />
          </Field>
          <Field id="oa-since" label="起算日期（UTC）">
            <Input id="oa-since" type="date" value={form.credit_since ?? ""}
              onChange={set("credit_since")} disabled={busy} />
          </Field>
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
          <Button onClick={save} disabled={busy}>{busy ? "校验中…" : "保存"}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ConfirmDialog({ confirm, onClose }) {
  return (
    <AlertDialog open={!!confirm} onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent className="max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle>{confirm?.title}</AlertDialogTitle>
          <AlertDialogDescription>{confirm?.description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>取消</AlertDialogCancel>
          <AlertDialogAction
            className="bg-red-600 text-white hover:bg-red-600/90"
            onClick={() => confirm?.onConfirm()}
          >
            删除
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function SecretDialog({ secret, onClose }) {
  return (
    <AlertDialog open={!!secret} onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent className="max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>已新建 Key「{secret?.name}」</AlertDialogTitle>
          <AlertDialogDescription>
            请立即复制保存：关闭后无法再查看，面板也不会保存这个 Key。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="break-all rounded-md border bg-muted/40 p-2.5 font-mono text-xs select-all">
          {secret?.value}
        </div>
        <AlertDialogFooter>
          <Button variant="outline" onClick={() => copyText(secret?.value || "")}>
            <Copy /> 复制
          </Button>
          <AlertDialogAction>我已保存</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ProjectKeys({ adminKey, onConfirm }) {
  const [data, setData] = useState(null); // {projects, api_keys}
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState(null);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const res = await api.openaiProjectKeys(adminKey.id);
      setData(res);
      setProjectId((p) => p || res.projects[0]?.id || "");
    } catch (e) {
      if (e?.status !== 401) setError(errText(e, "加载失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [adminKey.id]);

  async function create() {
    if (!projectId || !name.trim()) return toast.error("请选择项目并填写 Key 名称");
    setCreating(true);
    try {
      const res = await api.openaiProjectKeyCreate(adminKey.id, projectId, name.trim());
      setSecret(res);
      setName("");
      load();
    } catch (e) {
      if (e?.status !== 401) toast.error(errText(e, "新建失败"), { duration: 8000 });
    } finally {
      setCreating(false);
    }
  }

  function remove(k) {
    onConfirm({
      title: `删除 Key「${k.name || k.id}」？`,
      description:
        k.owner_type === "service_account"
          ? "这是服务账号的 Key，会连同服务账号一起删除，使用它的程序将立即失效。"
          : "删除后使用它的程序将立即失效，无法恢复。",
      onConfirm: async () => {
        try {
          await api.openaiProjectKeyDelete(adminKey.id, k.project_id, k.id);
          toast.success("已删除");
          load();
        } catch (e) {
          if (e?.status !== 401) toast.error(errText(e, "删除失败"), { duration: 8000 });
        }
      },
    });
  }

  return (
    <div className="mt-2 space-y-2 border-t pt-2.5">
      <SecretDialog secret={secret} onClose={() => setSecret(null)} />
      {error && <div className="text-xs text-red-400">{error}</div>}
      {data && (
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="h-8 rounded-md border bg-background px-2 text-xs"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={creating}
          >
            {data.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name || p.id}</option>
            ))}
          </select>
          <Input
            className="h-8 w-48 text-xs"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="新 Key 名称"
            disabled={creating}
          />
          <Button size="sm" variant="outline" onClick={create} disabled={creating || !data.projects.length}>
            <Plus /> {creating ? "创建中…" : "新建 Key"}
          </Button>
          <Button size="sm" variant="ghost" onClick={load} disabled={loading}>
            <RefreshCw className={cn(loading && "animate-spin")} />
          </Button>
        </div>
      )}
      {loading && !data ? (
        <div className="text-xs text-muted-foreground">加载中…</div>
      ) : data && data.api_keys.length === 0 ? (
        <div className="text-xs text-muted-foreground">该组织下还没有项目 Key</div>
      ) : data ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 font-normal">项目</th>
                <th className="py-1 pr-3 font-normal">名称</th>
                <th className="py-1 pr-3 font-normal">Key</th>
                <th className="py-1 pr-3 font-normal">类型</th>
                <th className="py-1 pr-3 font-normal">创建时间</th>
                <th className="py-1 pr-3 font-normal">最近使用</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.api_keys.map((k) => (
                <tr key={k.id} className="border-t">
                  <td className="py-1.5 pr-3">{k.project_name}</td>
                  <td className="py-1.5 pr-3">{k.name}</td>
                  <td className="py-1.5 pr-3 font-mono">{shortRedacted(k.redacted_value)}</td>
                  <td className="py-1.5 pr-3">
                    {k.owner_type === "service_account" ? "服务账号" : `用户 ${k.owner_name}`}
                  </td>
                  <td className="py-1.5 pr-3">{fmtTime(k.created_at)}</td>
                  <td className="py-1.5 pr-3">{fmtTime(k.last_used_at) || "从未"}</td>
                  <td className="py-1.5 text-right">
                    <Button size="icon-sm" variant="ghost" title="删除" onClick={() => remove(k)}>
                      <Trash2 className="text-red-400" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function Remaining({ k }) {
  if (k.remaining_usd == null) return <span className="text-muted-foreground">未设额度</span>;
  const ratio = k.credit_usd > 0 ? Math.max(0, Math.min(1, k.remaining_usd / k.credit_usd)) : 0;
  const low = ratio < 0.1;
  return (
    <span className="flex items-center gap-2">
      <span className={cn("font-medium", low && "text-red-400")}>
        {usd(k.remaining_usd)} / {usd(k.credit_usd)}
      </span>
      <span className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <span
          className={cn("block h-full", low ? "bg-red-500" : "bg-emerald-500")}
          style={{ width: `${ratio * 100}%` }}
        />
      </span>
    </span>
  );
}

export default function OpenAI() {
  const [keys, setKeys] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dialog, setDialog] = useState(null); // {editing}
  const [confirm, setConfirm] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());
  const [busy, setBusy] = useState(() => new Set());

  async function load() {
    setLoading(true);
    try {
      setKeys((await api.openaiKeys()).keys);
    } catch (e) {
      if (e?.status !== 401) toast.error(errText(e, "加载失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const upsert = (row) =>
    setKeys((list) =>
      list.some((k) => k.id === row.id)
        ? list.map((k) => (k.id === row.id ? row : k))
        : [...list, row],
    );

  const flag = (setter, id, on) =>
    setter((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  async function refresh(k) {
    flag(setBusy, k.id, true);
    try {
      const row = await api.openaiKeyRefresh(k.id);
      upsert(row);
      if (row.status === "ok") toast.success(`「${k.name}」已刷新`);
      else toast.error(`「${k.name}」${row.last_error}`, { duration: 8000 });
    } catch (e) {
      if (e?.status !== 401) toast.error(errText(e, "刷新失败"));
    } finally {
      flag(setBusy, k.id, false);
    }
  }

  async function refreshAll() {
    for (const k of keys || []) await refresh(k);
  }

  function remove(k) {
    setConfirm({
      title: `删除「${k.name}」？`,
      description: "只删除面板里的记录，不会撤销 OpenAI 那边的 Admin Key。",
      onConfirm: async () => {
        try {
          await api.openaiKeyDelete(k.id);
          setKeys((list) => list.filter((x) => x.id !== k.id));
          toast.success("已删除");
        } catch (e) {
          if (e?.status !== 401) toast.error(errText(e, "删除失败"));
        }
      },
    });
  }

  const anyBusy = busy.size > 0;

  return (
    <Card>
      <KeyDialog
        open={!!dialog}
        onOpenChange={(o) => !o && setDialog(null)}
        editing={dialog?.editing}
        onSaved={upsert}
      />
      <ConfirmDialog confirm={confirm} onClose={() => setConfirm(null)} />
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>OpenAI Admin Key</CardTitle>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refreshAll}
            disabled={loading || anyBusy || !keys?.length}>
            <RefreshCw className={cn(anyBusy && "animate-spin")} /> 全部刷新
          </Button>
          <Button size="sm" onClick={() => setDialog({ editing: null })}>
            <Plus /> 新增 Admin Key
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          OpenAI 不提供余额接口，剩余额度 = 你填的额度 − 起算日至今的费用。费用来自 OpenAI
          Costs API，按 UTC 日统计，有数小时延迟，以 OpenAI 后台账单为准。
        </p>
        {loading && !keys ? (
          <div className="text-sm text-muted-foreground">加载中…</div>
        ) : keys && keys.length === 0 ? (
          <div className="text-sm text-muted-foreground">还没有添加 Admin Key</div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {(keys || []).map((k) => {
              const st = STATUS[k.status] || ["未检测", "bg-muted text-muted-foreground"];
              const open = expanded.has(k.id);
              return (
                <li key={k.id} className="rounded-lg border bg-background/50 px-3 py-2.5 text-sm">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="shrink-0 text-muted-foreground hover:text-foreground"
                      title={open ? "收起项目 Key" : "展开项目 Key"}
                      onClick={() => flag(setExpanded, k.id, !open)}
                    >
                      {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                    </button>
                    <span className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{k.name}</span>
                        <Badge className={cn("rounded-full border-transparent", st[1])}>{st[0]}</Badge>
                        <span className="font-mono text-xs text-muted-foreground">{k.key_masked}</span>
                        {k.org_id && (
                          <span className="font-mono text-xs text-muted-foreground">{k.org_id}</span>
                        )}
                      </span>
                      <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                        <span>本月费用 <span className="text-foreground">{usd(k.cost_month_usd)}</span></span>
                        <span>
                          {k.credit_since || "—"} 起费用{" "}
                          <span className="text-foreground">{usd(k.cost_since_usd)}</span>
                        </span>
                        <span className="flex items-center gap-1">剩余 <Remaining k={k} /></span>
                        {k.refreshed_at && <span>刷新于 {fmtTime(k.refreshed_at)}</span>}
                      </span>
                      {k.status !== "ok" && k.last_error && (
                        <span className="text-xs text-red-400/90">{k.last_error}</span>
                      )}
                    </span>
                    <span className="flex shrink-0 gap-1">
                      <Button variant="outline" size="sm" disabled={busy.has(k.id)} onClick={() => refresh(k)}>
                        {busy.has(k.id) ? "刷新中…" : "刷新"}
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => setDialog({ editing: k })}>
                        编辑
                      </Button>
                      <Button variant="ghost" size="icon-sm" title="删除" onClick={() => remove(k)}>
                        <Trash2 className="text-red-400" />
                      </Button>
                    </span>
                  </div>
                  {open && <ProjectKeys adminKey={k} onConfirm={setConfirm} />}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
