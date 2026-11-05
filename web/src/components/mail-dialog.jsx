import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Copy, RefreshCw } from "lucide-react";
import { api } from "../api.js";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

const LIMITS = [10, 20, 50];

function fmtDate(raw) {
  if (!raw) return "时间未知";
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString();
}

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`已复制${label}`);
  } catch {
    toast.error("复制失败");
  }
}

function MailItem({ mail }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-lg border bg-muted/20 p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium" title={mail.subject}>
            {mail.subject || "（无主题）"}
          </div>
          <div className="truncate text-xs text-muted-foreground" title={mail.from}>
            {mail.from || "发件人未知"}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1 text-xs text-muted-foreground">
          <span>{fmtDate(mail.date)}</span>
          {mail.folder === "Junk" ? (
            <Badge className="rounded-full border-transparent bg-amber-500/15 text-amber-800 dark:text-amber-400">
              垃圾邮件
            </Badge>
          ) : null}
        </div>
      </div>
      {(mail.code || mail.link) && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {mail.code ? (
            <Button variant="outline" size="sm" className="font-mono" onClick={() => copy(mail.code, "验证码")}>
              {mail.code} <Copy />
            </Button>
          ) : null}
          {mail.link ? (
            <Button variant="outline" size="sm" onClick={() => copy(mail.link, "登录链接")}>
              复制登录链接 <Copy />
            </Button>
          ) : null}
        </div>
      )}
      {mail.text ? (
        <div className="mt-2">
          <p
            className={`whitespace-pre-wrap break-all text-xs text-muted-foreground ${open ? "" : "line-clamp-3"}`}
          >
            {mail.text}
            {open && mail.truncated ? "\n…（正文过长已截断）" : ""}
          </p>
          <button
            type="button"
            className="mt-1 text-xs text-primary hover:underline"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "收起正文" : "展开正文"}
          </button>
        </div>
      ) : null}
    </li>
  );
}

export function MailDialog({ email, onOpenChange }) {
  const [limit, setLimit] = useState(20);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!email) return;
    setLoading(true);
    setError("");
    try {
      setData(await api.accountMails(email, limit));
    } catch (e) {
      if (e?.status === 401) return;
      setError(e.body?.detail || `查询失败（${e.status || "?"}）`);
    } finally {
      setLoading(false);
    }
  }, [email, limit]);

  useEffect(() => {
    if (email) load();
    else {
      setData(null);
      setError("");
    }
  }, [email, load]);

  const mails = data?.mails || [];
  return (
    <Sheet open={!!email} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 sm:max-w-xl">
        <SheetHeader className="border-b">
          <SheetTitle>邮件</SheetTitle>
          <SheetDescription className="truncate" title={email}>
            {email}
            {data ? ` · ${data.source === "outlook" ? "Outlook（收件箱 + 垃圾邮件）" : "AnyMail（近 30 天）"}` : ""}
          </SheetDescription>
          <div className="flex items-center gap-2 pt-1">
            <select
              className="h-8 rounded-md border bg-background px-2 text-sm"
              value={limit}
              disabled={loading}
              aria-label="显示数量"
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              {LIMITS.map((n) => <option key={n} value={n}>最近 {n} 封</option>)}
            </select>
            <Button variant="outline" size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={loading ? "animate-spin" : ""} /> {loading ? "读取中…" : "刷新"}
            </Button>
          </div>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : loading && !data ? (
            <p className="text-sm text-muted-foreground">正在读取邮件…</p>
          ) : data && mails.length === 0 ? (
            <p className="text-sm text-muted-foreground">没有邮件。</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {mails.map((m, i) => <MailItem key={`${m.date}-${i}`} mail={m} />)}
            </ul>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
