import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATUS = {
  pending: { label: "待注册", cls: "bg-zinc-500/15 text-zinc-700 dark:text-zinc-400" },
  running: { label: "运行中", cls: "bg-blue-500/15 text-blue-700 dark:text-blue-400" },
  success: { label: "成功", cls: "bg-emerald-500/15 text-emerald-800 dark:text-emerald-400" },
  failed: { label: "失败", cls: "bg-red-500/15 text-red-700 dark:text-red-400" },
  needs_manual: { label: "需人工介入", cls: "bg-amber-500/15 text-amber-800 dark:text-amber-400" },
  phone_required: { label: "需手机验证", cls: "bg-orange-500/15 text-orange-800 dark:text-orange-400" },
  banned: { label: "已封禁", cls: "bg-red-500/15 text-red-700 dark:text-red-400" },
};

// 平台注册状态：空状态视为「未注册」，success 显示为「已注册」
export function PlatformStatusBadge({ platform, status, extra, title, className }) {
  const s = !status
    ? { label: "未注册", cls: "border-dashed border-border bg-transparent text-muted-foreground" }
    : status === "success"
      ? { label: "已注册", cls: STATUS.success.cls }
      : STATUS[status] || { label: status, cls: "bg-muted text-muted-foreground" };
  return (
    <Badge
      className={cn("gap-1 rounded-full border-transparent", s.cls, className)}
      title={title}
    >
      <span className="font-semibold">{platform}</span>
      <span className={cn(status === "running" && "animate-pulse")}>{s.label}</span>
      {extra ? <span className="font-normal opacity-80">· {extra}</span> : null}
    </Badge>
  );
}

export function StatusBadge({ status }) {
  const s = STATUS[status] || { label: status || "未知", cls: "bg-muted text-muted-foreground" };
  return (
    <Badge className={cn("gap-1.5 rounded-full border-transparent", s.cls)}>
      <span
        className={cn(
          "size-1.5 rounded-full bg-current",
          status === "running" && "animate-pulse",
        )}
      />
      {s.label}
    </Badge>
  );
}
