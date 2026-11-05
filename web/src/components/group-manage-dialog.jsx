import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

function errText(e, fallback) {
  return e?.body?.detail || `${fallback}（${e?.status || "?"}）`;
}

function GroupRow({ group, busy, onRename, onDelete }) {
  const [name, setName] = useState(group.name);
  useEffect(() => setName(group.name), [group.name]);
  const dirty = name.trim() && name.trim() !== group.name;
  return (
    <li className="flex items-center gap-2">
      <Input
        className="h-8 flex-1"
        value={name}
        disabled={busy}
        spellCheck={false}
        aria-label={`分组 ${group.name} 名称`}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && dirty && onRename(group.name, name.trim())}
      />
      <span className="w-14 shrink-0 text-right text-xs text-muted-foreground">
        {group.count} 个
      </span>
      <Button
        variant="outline"
        size="sm"
        className="h-8"
        disabled={busy || !dirty}
        onClick={() => onRename(group.name, name.trim())}
      >
        重命名
      </Button>
      <Button
        variant="outline"
        size="sm"
        className="h-8 text-red-700 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
        aria-label={`删除分组 ${group.name}`}
        disabled={busy}
        onClick={() => onDelete(group)}
      >
        <Trash2 />
      </Button>
    </li>
  );
}

export function GroupManageDialog({ open, onOpenChange, groups, onChanged }) {
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) setNewName("");
  }, [open]);

  async function act(fn, ok, fail) {
    setBusy(true);
    try {
      await fn();
      toast.success(ok);
      onChanged?.();
      return true;
    } catch (e) {
      if (e?.status !== 401) toast.error(errText(e, fail));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const name = newName.trim();
    if (!name) return;
    if (await act(() => api.createGroup(name), `已新建分组「${name}」`, "新建分组失败")) {
      setNewName("");
    }
  }

  const rename = (oldName, name) =>
    act(() => api.renameGroup(oldName, name), `已将「${oldName}」重命名为「${name}」`, "重命名失败");

  function remove(group) {
    const tip = group.count
      ? `删除分组「${group.name}」？其下 ${group.count} 个账号会变为未分组（账号本身不删除）。`
      : `删除分组「${group.name}」？`;
    if (!window.confirm(tip)) return;
    act(() => api.deleteGroup(group.name), `已删除分组「${group.name}」`, "删除分组失败");
  }

  const list = groups?.groups || [];

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-h-[85vh] overflow-y-auto">
        <AlertDialogHeader>
          <AlertDialogTitle>分组管理</AlertDialogTitle>
          <AlertDialogDescription>
            每个账号最多属于一个分组；未分组账号 {groups?.ungrouped_count ?? 0} 个。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="flex items-center gap-2">
          <Input
            className="h-8 flex-1"
            value={newName}
            placeholder="新分组名称"
            maxLength={64}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
          />
          <Button size="sm" className="h-8" disabled={busy || !newName.trim()} onClick={create}>
            <Plus /> 新建
          </Button>
        </div>
        {list.length === 0 ? (
          <div className="text-sm text-muted-foreground">暂无分组</div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {list.map((g) => (
              <GroupRow key={g.name} group={g} busy={busy} onRename={rename} onDelete={remove} />
            ))}
          </ul>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel>关闭</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
