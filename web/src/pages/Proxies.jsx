import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";

const newId = () =>
  globalThis.crypto?.randomUUID?.() || `proxy-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const blank = () => ({ id: newId(), name: "", url: "" });

export default function Proxies() {
  const [items, setItems] = useState([]);
  const [form, setForm] = useState(blank);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState({}); // { [id | "__form__"]: true }
  const [results, setResults] = useState({}); // { [id]: { ok, ip, country, city, timezone, latency_ms, detail } }

  useEffect(() => {
    api
      .getConfig()
      .then((c) => setItems(c.saved_proxies || []))
      .catch((e) => {
        if (e?.status !== 401) toast.error("加载失败");
      })
      .finally(() => setLoading(false));
  }, []);

  const testOne = async (key, payload, label) => {
    setTesting((t) => ({ ...t, [key]: true }));
    try {
      const res = await api.proxyTest(payload);
      if (key !== "__form__") {
        setResults((r) => ({ ...r, [key]: res }));
      }
      if (res.ok) {
        const loc = [res.country, res.city].filter(Boolean).join(" ");
        toast.success(
          `${label ? `「${label}」` : "代理"}连通：${res.ip || "未知 IP"}${loc ? `（${loc}）` : ""} · ${res.timezone} · ${res.latency_ms}ms`,
        );
      } else {
        toast.error(`${label ? `「${label}」` : "代理"}不可用：${res.detail || "连接失败"}`);
      }
      return res;
    } catch (e) {
      const detail = e.body?.detail || `测试失败（${e.status || "?"}）`;
      if (key !== "__form__") {
        setResults((r) => ({ ...r, [key]: { ok: false, detail } }));
      }
      if (e?.status !== 401) toast.error(detail);
      return null;
    } finally {
      setTesting((t) => {
        const next = { ...t };
        delete next[key];
        return next;
      });
    }
  };

  const testAll = async () => {
    for (const p of items) {
      await testOne(p.id, { proxy_id: p.id }, p.name);
    }
  };

  const save = async (e) => {
    e.preventDefault();
    if (!form.name.trim() || !form.url.trim()) return toast.error("请填写名称和地址");
    const entry = { ...form, name: form.name.trim(), url: form.url.trim() };
    const next = [...items.filter((x) => x.id !== entry.id), entry];
    try {
      await api.putConfig({ saved_proxies: next });
      setItems(next);
      setForm(blank());
      toast.success("已保存，正在测试连通性…");
      testOne(entry.id, { proxy_id: entry.id }, entry.name);
    } catch (e) {
      toast.error(e.body?.detail || "代理地址无效");
    }
  };

  const remove = async (id) => {
    const next = items.filter((x) => x.id !== id);
    try {
      await api.putConfig({ saved_proxies: next });
      setItems(next);
    } catch {
      toast.error("删除失败");
    }
  };

  const editing = items.some((x) => x.id === form.id);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>代理池</CardTitle>
        {items.length > 0 && (
          <Button
            size="sm"
            variant="outline"
            disabled={Object.keys(testing).length > 0}
            onClick={testAll}
          >
            测试全部
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        <form onSubmit={save} className="grid gap-2 sm:grid-cols-[1fr_2fr_auto_auto]">
          <Input
            placeholder="名称"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <Input
            placeholder="http://user:pass@host:port 或 socks5://host:port"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
          />
          <Button
            type="button"
            variant="outline"
            disabled={!form.url.trim() || !!testing.__form__}
            onClick={() => testOne("__form__", { url: form.url.trim() }, form.name.trim())}
          >
            {testing.__form__ ? "测试中…" : "测试"}
          </Button>
          <span className="flex gap-2">
            <Button type="submit">{editing ? "更新" : "添加"}</Button>
            {editing && (
              <Button type="button" variant="ghost" onClick={() => setForm(blank())}>
                取消
              </Button>
            )}
          </span>
        </form>
        {loading ? (
          <p>加载中…</p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无代理</p>
        ) : (
          <ul className="space-y-2">
            {items.map((p) => {
              const r = results[p.id];
              const busy = !!testing[p.id];
              return (
                <li
                  key={p.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded border p-3"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <b>{p.name}</b>
                      {r && (
                        r.ok ? (
                          <Badge className="rounded-full border-transparent bg-emerald-500/15 text-emerald-800 dark:text-emerald-400">
                            连通 · {r.ip}
                            {r.country ? ` (${[r.country, r.city].filter(Boolean).join(" ")})` : ""}
                            {r.timezone ? ` · ${r.timezone}` : ""}
                            {r.latency_ms ? ` · ${r.latency_ms}ms` : ""}
                          </Badge>
                        ) : (
                          <Badge
                            className="rounded-full border-transparent bg-red-500/15 text-red-700 dark:text-red-400"
                            title={r.detail || ""}
                          >
                            不通 · {r.detail || "连接失败"}
                          </Badge>
                        )
                      )}
                    </div>
                    <code className="block truncate text-xs text-muted-foreground">{p.url}</code>
                  </div>
                  <span className="flex shrink-0 gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => testOne(p.id, { proxy_id: p.id }, p.name)}
                    >
                      {busy ? "测试中…" : "测试"}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setForm(p)}>
                      编辑
                    </Button>
                    <Button size="sm" variant="destructive" onClick={() => remove(p.id)}>
                      删除
                    </Button>
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
