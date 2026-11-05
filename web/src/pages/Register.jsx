import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { api } from "../api.js";
import { StatusBadge } from "@/components/status-badge";
import { GroupDatalist } from "@/components/account-create-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";

const PLATFORMS = [
  ["claude", "Claude"],
  ["chatgpt", "ChatGPT"],
  ["both", "Claude + ChatGPT"],
];
const PLATFORM_LABEL = Object.fromEntries(PLATFORMS);

export function PlatformBadge({ platform }) {
  const p = platform || "claude";
  return (
    <Badge
      variant="outline"
      className={`rounded-full px-1.5 text-[11px] ${
        p === "chatgpt" ? "border-teal-500/40 text-teal-700 dark:text-teal-400" : p === "both" ? "border-violet-500/40 text-violet-700 dark:text-violet-400" : ""
      }`}
    >
      {PLATFORM_LABEL[p] || p}
    </Badge>
  );
}

export default function Register({ runStream, platform = "claude" }) {
  const { activeRunId, activeStatus, activePlatform, logLines, attach, streamEpoch } = runStream;
  const [proxies, setProxies] = useState([]);
  const [proxyId, setProxyId] = useState("");
  const [groupName, setGroupName] = useState("");
  const [groups, setGroups] = useState([]);
  useEffect(() => {
    api.getConfig().then((cfg) => setProxies(cfg.saved_proxies || []))
      .catch(() => toast.error("代理列表加载失败，请刷新页面"));
    api.listGroups().then((res) => setGroups(res.groups || [])).catch(() => {});
  }, []);
  const [domain, setDomain] = useState("");
  const [email, setEmail] = useState("");
  const [starting, setStarting] = useState(false);
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [previewIndex, setPreviewIndex] = useState(null);
  const logPanelRef = useRef(null);

  const previewShots = selectedRun?.screenshots || [];
  const previewCount = previewShots.length;
  const previewOpen = previewIndex !== null && previewIndex < previewCount;
  const previewName = previewOpen ? previewShots[previewIndex] : null;
  const previewSrc = previewOpen ? `/runs/${selectedRun.id}/${previewName}` : null;
  const stepPreview = (delta) =>
    setPreviewIndex((i) => (i === null ? i : (i + delta + previewCount) % previewCount));

  useEffect(() => {
    setPreviewIndex(null);
  }, [selectedRun?.id]);

  useEffect(() => {
    if (!previewOpen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setPreviewIndex(null);
      else if (e.key === "ArrowLeft") stepPreview(-1);
      else if (e.key === "ArrowRight") stepPreview(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewOpen, previewCount]);

  useEffect(() => {
    let current = true;
    api.listRuns(platform).then((items) => current && setRuns(items)).catch(() => {});
    return () => { current = false; };
  }, [streamEpoch, platform]);

  useEffect(() => {
    const vp = logPanelRef.current?.closest("[data-slot='scroll-area-viewport']");
    if (vp) vp.scrollTop = vp.scrollHeight;
  }, [logLines]);

  async function startRun() {
    setStarting(true);
    try {
      const res = await api.startRun(
        email || undefined, domain || undefined, proxyId || undefined, platform, groupName.trim(),
      );
      attach(res.run_id, platform);
    } catch (err) {
      toast.error(err.status === 409 ? "已有任务在运行" : err.body?.detail || "启动失败，请重试");
    } finally {
      setStarting(false);
    }
  }

  async function openRun(id) {
    try {
      setSelectedRun(await api.runDetail(id));
    } catch {
      setSelectedRun(null);
    }
  }

  return (
    <>
      <div className="grid items-start gap-5 min-[1200px]:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>注册 {PLATFORM_LABEL[platform]}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Input
              placeholder="邮箱后缀 / domain（可选）"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            />
            <Input
              placeholder="已有邮箱（可选，用于重跑该账号）"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <div className="space-y-1.5">
              <label htmlFor="registration-proxy" className="text-sm font-medium">注册代理</label>
              <select id="registration-proxy" value={proxyId} onChange={(e) => setProxyId(e.target.value)}
                className="h-10 w-full rounded-md border bg-background px-3 text-sm">
                <option value="">使用默认配置（3x-ui / 固定代理 / 直连）</option>
                {proxies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <p className="text-xs text-muted-foreground">在<a className="underline" href="#/proxies">代理池</a>保存代理后，可在此选择用于本次注册。</p>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="registration-group" className="text-sm font-medium">归属分组</label>
              <GroupDatalist id="registration-group-list" groups={groups} />
              <Input
                id="registration-group"
                list="registration-group-list"
                placeholder="可选；不填则保持账号原分组"
                value={groupName}
                onChange={(e) => setGroupName(e.target.value)}
              />
            </div>
            <Button
              onClick={startRun}
              disabled={starting || activeStatus === "running"}
            >
              {starting ? "启动中…" : "开始注册"}
            </Button>

            {activeStatus === "running" && activePlatform && activePlatform !== platform && (
              <p className="text-xs text-muted-foreground">{PLATFORM_LABEL[activePlatform]} 正在注册，完成后可启动下一条任务。</p>
            )}
            {activeRunId && (!activePlatform || activePlatform === platform) && (
              <div className="pt-2">
                <div className="mb-2 flex items-center justify-between text-sm text-muted-foreground">
                  <span>
                    运行 <code className="font-mono text-xs">{activeRunId}</code>
                  </span>
                  <StatusBadge status={activeStatus} />
                </div>
                <ScrollArea className="log-panel h-64 rounded-lg border bg-muted text-muted-foreground dark:bg-black/40">
                  <div className="p-3" ref={logPanelRef}>
                    {logLines.length === 0 ? (
                      <div className="text-muted-foreground/60">等待日志输出…</div>
                    ) : (
                      logLines.map((line, i) => <div key={i}>{line}</div>)
                    )}
                  </div>
                </ScrollArea>
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>运行历史</CardTitle>
          </CardHeader>
          <CardContent>
            {runs.length === 0 ? (
              <div className="text-sm text-muted-foreground">暂无运行记录</div>
            ) : (
              <ScrollArea className="[&>[data-slot=scroll-area-viewport]]:max-h-80">
                <ul className="flex flex-col gap-1.5 pr-3">
                {runs.map((r) => (
                  <li
                    key={r.id}
                    onClick={() => openRun(r.id)}
                    className="flex cursor-pointer items-center justify-between gap-2 rounded-lg border bg-background/50 px-3 py-2.5 text-sm transition-colors hover:border-ring"
                  >
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="flex items-center gap-1.5">
                        <span className="font-mono text-xs">{r.id}</span>
                        <PlatformBadge platform={r.platform} />
                        {r.group_name ? (
                          <span className="truncate text-[11px] text-muted-foreground">· {r.group_name}</span>
                        ) : null}
                      </span>
                      <span className="truncate text-xs text-muted-foreground">
                        {r.email || "（未指定邮箱）"} {r.domain ? `@${r.domain}` : ""}
                      </span>
                    </span>
                    <StatusBadge status={r.status} />
                  </li>
                ))}
                </ul>
              </ScrollArea>
            )}

            {selectedRun && (
              <div className="mt-4 border-t pt-4">
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">
                    运行详情 <span className="font-mono text-xs">{selectedRun.id}</span>
                  </h3>
                  <Button variant="ghost" size="sm" onClick={() => setSelectedRun(null)}>
                    关闭
                  </Button>
                </div>
                <StatusBadge status={selectedRun.status} />
                <ScrollArea className="log-panel mt-2 rounded-lg border bg-muted text-muted-foreground dark:bg-black/40 [&>[data-slot=scroll-area-viewport]]:max-h-80">
                  <pre className="p-3 whitespace-pre-wrap break-all">{selectedRun.log || "（无日志）"}</pre>
                </ScrollArea>
                {selectedRun.screenshots && selectedRun.screenshots.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {selectedRun.screenshots.map((name, idx) => {
                      const src = `/runs/${selectedRun.id}/${name}`;
                      return (
                        <button
                          key={name}
                          type="button"
                          title={name}
                          className="cursor-zoom-in rounded-md border p-0 transition-opacity hover:opacity-90"
                          onClick={() => setPreviewIndex(idx)}
                        >
                          <img
                            className="block h-[90px] w-[140px] rounded-md object-cover"
                            src={src}
                            alt={name}
                          />
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {previewSrc && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          onClick={() => setPreviewIndex(null)}
          role="dialog"
          aria-modal="true"
          aria-label="截图预览"
        >
          <button
            type="button"
            className="absolute top-4 right-4 flex size-9 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
            onClick={() => setPreviewIndex(null)}
            aria-label="关闭预览"
          >
            <X className="size-5" />
          </button>
          {previewCount > 1 && (
            <>
              <button
                type="button"
                className="absolute top-1/2 left-4 flex size-11 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
                onClick={(e) => {
                  e.stopPropagation();
                  stepPreview(-1);
                }}
                aria-label="上一张"
              >
                <ChevronLeft className="size-6" />
              </button>
              <button
                type="button"
                className="absolute top-1/2 right-4 flex size-11 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
                onClick={(e) => {
                  e.stopPropagation();
                  stepPreview(1);
                }}
                aria-label="下一张"
              >
                <ChevronRight className="size-6" />
              </button>
            </>
          )}
          <img
            className="max-h-[85vh] max-w-[85vw] rounded-md object-contain shadow-lg"
            src={previewSrc}
            alt={previewName}
            onClick={(e) => e.stopPropagation()}
          />
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-white/10 px-3 py-1 text-xs text-white">
            {previewIndex + 1} / {previewCount} · {previewName}
          </div>
        </div>
      )}
    </>
  );
}
