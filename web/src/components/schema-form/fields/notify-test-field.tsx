import { Alert, Button } from "@mantine/core";
import { useState } from "react";
import type { FieldProps } from "../schema";
import { FieldChrome } from "./field-chrome";

/**
 * 「测试发送」按钮 (本 fork 新增, 由后端 schema 里的 ``x-widget: "NotifyTest"`` 标记).
 *
 * 直接读取**界面上正在编辑**的渠道对象 (不要求先保存), POST 给
 * ``/api/config/notify/test``: 后端用示例数据真实发送一次, 把对方的响应码或错误正文返回,
 * 于是"配置对不对"在界面上就能立刻看到.
 */

/** 去掉路径最后一段: ``notify.channels[0].test_send`` → ``notify.channels[0]``. */
function parentPath(name: string): string {
  return name.replace(/(\[[^\]]*\])$|(\.[^.[\]]+)$/, "");
}

/** 按路径取值, 同时兼容 ``a.b[0]`` 与 ``a.b.0`` 两种写法. */
function readPath(root: unknown, path: string): unknown {
  if (!path) {
    return root;
  }
  const parts = path.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
  let node: unknown = root;
  for (const part of parts) {
    if (node === null || typeof node !== "object") {
      return undefined;
    }
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

/** 表单实例的形态在不同版本里可能是 `state.values` 或 `store.state.values`, 两种都认. */
interface LooseForm {
  state?: { values?: unknown };
  store?: { state?: { values?: unknown } };
}

export function NotifyTestField({ name, label, description, form, variant }: FieldProps) {
  const [state, setState] = useState<{ kind: "idle" | "busy" | "ok" | "error"; text: string }>({
    kind: "idle",
    text: "",
  });

  const run = async () => {
    const formAny = form as unknown as LooseForm;
    const values = formAny.state?.values ?? formAny.store?.state?.values ?? {};
    const channel = readPath(values, parentPath(name));
    if (channel === undefined || channel === null || typeof channel !== "object") {
      setState({ kind: "error", text: "读不到当前渠道内容, 请先保存一次再测试" });
      return;
    }
    setState({ kind: "busy", text: "" });
    try {
      const response = await fetch("/api/config/notify/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(channel),
      });
      const data = (await response.json().catch(() => ({}))) as {
        ok?: boolean;
        message?: string;
        detail?: string;
      };
      if (!response.ok) {
        setState({ kind: "error", text: data.detail ?? `HTTP ${response.status}` });
      } else if (data.ok) {
        setState({ kind: "ok", text: `发送成功 (${data.message ?? ""})` });
      } else {
        setState({ kind: "error", text: data.message ?? "发送失败" });
      }
    } catch (error) {
      setState({ kind: "error", text: String(error) });
    }
  };

  const color = state.kind === "ok" ? "teal" : state.kind === "error" ? "red" : "gray";

  return (
    <FieldChrome variant={variant} label={label} description={description}>
      <Button size="xs" variant="light" loading={state.kind === "busy"} onClick={() => void run()}>
        {label || "测试发送"}
      </Button>
      {state.text ? (
        <Alert mt="xs" color={color} variant="light">
          {state.text}
        </Alert>
      ) : null}
    </FieldChrome>
  );
}
