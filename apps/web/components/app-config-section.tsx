"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CheckIcon, InfoIcon } from "@/components/icons";
import { Tooltip } from "@/components/tooltip";
import {
  type AppConfigView,
  getAppConfig,
  restartApp,
  saveAppConfig,
} from "@/lib/api/app";
import { getHealth } from "@/lib/api/health";

/**
 * 应用设置（设置 → 应用设置）。
 *
 * 当前阶段只有「网络」一组配置 + 「维护」里的重启入口：
 *   - 访问端口：后端监听端口，**重启后生效**（uvicorn 启动时一次性绑定，无法热切换）。
 *     Docker 部署下端口由容器钉死（APP_PORT 环境变量），输入框禁用并说明去改
 *     compose 的端口映射；该字段实际面向源码/裸机部署者。
 *   - 外部访问地址：从网络上能访问到本应用的完整地址，保存即生效（纯落库数据，
 *     供后续生成通知链接等绝对 URL 的场景使用）。
 *
 * 交互模型与「网络与代理」分区一致：输入框失焦自动落库，无「保存」按钮；
 * 端口改动落库后后端返回 restart_required，此处浮出「重启应用」横幅。
 *
 * 重启流程：调用 /app/restart → 后端优雅停机、以约定码 42 退出 → Docker 镜像
 * 的 entrypoint 重启循环原地拉起新的后端进程（前端不中断，不依赖 restart 策略；
 * 源码部署需 systemd 等守护）→ 前端轮询 /health 直到服务恢复，然后整页刷新。
 */

type RestartPhase = "idle" | "confirming" | "waiting" | "timeout";

export function AppConfigSection() {
  const [view, setView] = useState<AppConfigView | null>(null);
  const [failed, setFailed] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [portError, setPortError] = useState<string | null>(null);
  const [urlError, setUrlError] = useState<string | null>(null);
  const [restartPhase, setRestartPhase] = useState<RestartPhase>("idle");
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reload = useCallback(() => {
    setFailed(false);
    getAppConfig()
      .then(setView)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
    return () => {
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
  }, [reload]);

  /** 落库一份完整配置（失焦触发），错误信息中文回显在对应字段下方。 */
  const commit = useCallback(
    (patch: Partial<Pick<AppConfigView, "port" | "external_url">>) => {
      if (!view) return;
      setSaveState("saving");
      setSaveError(null);
      saveAppConfig({
        port: patch.port ?? view.port,
        external_url: patch.external_url ?? view.external_url,
      })
        .then((v) => {
          setView(v);
          setSaveState("saved");
          if (savedTimer.current) clearTimeout(savedTimer.current);
          savedTimer.current = setTimeout(() => setSaveState("idle"), 2000);
        })
        .catch((e) => {
          setSaveState("error");
          setSaveError((e as Error).message);
        });
    },
    [view],
  );

  const handlePortBlur = (raw: string) => {
    const text = raw.trim();
    if (!text) {
      setPortError(null);
      commit({ port: 0 });
      return;
    }
    const port = Number(text);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      setPortError("端口必须在 1024~65535 之间；留空使用默认端口");
      return;
    }
    setPortError(null);
    commit({ port });
  };

  const handleUrlBlur = (raw: string) => {
    const url = raw.trim();
    if (url && !/^https?:\/\/.+/.test(url)) {
      setUrlError("需以 http:// 或 https:// 开头的完整地址");
      return;
    }
    setUrlError(null);
    commit({ external_url: url });
  };

  /** 重启：请求后端优雅停机，然后轮询 /health 等服务恢复，恢复后整页刷新。 */
  const doRestart = async () => {
    setRestartPhase("waiting");
    try {
      await restartApp();
    } catch {
      // 请求可能因进程退出而中断，属预期，继续轮询等恢复
    }
    // 先给停机留出时间，避免轮询打到「还没退出的旧进程」造成误判
    await new Promise((r) => setTimeout(r, 4000));
    for (let i = 0; i < 45; i++) {
      try {
        await getHealth();
        window.location.reload();
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    setRestartPhase("timeout");
  };

  if (failed) {
    return (
      <div className="flex items-center gap-3">
        <p className="text-ui text-[var(--text-muted)]">应用设置加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-3 py-1.5 text-sub font-medium">
          重试
        </button>
      </div>
    );
  }
  if (!view) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载应用设置…</p>;
  }

  // 重启等待态：全区替换为状态页，避免用户在服务不可用期间继续操作表单
  if (restartPhase === "waiting" || restartPhase === "timeout") {
    return (
      <div className="css-glass !rounded-2xl px-6 py-10 text-center">
        {restartPhase === "waiting" ? (
          <>
            <p className="text-body font-medium text-[var(--text)]">正在重启应用…</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              服务恢复后页面会自动刷新，通常需要几秒到几十秒。
            </p>
          </>
        ) : (
          <>
            <p className="text-body font-medium text-[var(--text)]">等待超时，应用尚未恢复</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              Docker 部署通常几秒内自动拉起，请稍后手动刷新页面；源码部署且无
              systemd 等守护时，需要到服务器上手动启动。
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="btn-glass mt-4 px-3.5 py-1.5 text-sub font-medium"
            >
              刷新页面
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-7">
      {/* —— 网络 —— */}
      <section>
        <div className="mb-2.5 flex h-5 items-center justify-between px-1">
          <h3 className="group-label">网络</h3>
          <span className="text-sub">
            {saveState === "saving" && <span className="text-[var(--text-faint)]">保存中…</span>}
            {saveState === "saved" && (
              <span className="flex items-center gap-1 text-emerald-300/90">
                <CheckIcon className="size-3.5" />
                已保存
              </span>
            )}
            {saveState === "error" && <span className="text-red-300">保存失败：{saveError}</span>}
          </span>
        </div>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {/* 访问端口 */}
          <div className="px-5 py-4">
            <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-stretch max-md:gap-2">
              <LabelWithHelp
                label="访问端口"
                help={
                  <>
                    <p>后端 HTTP 服务的监听端口，留空使用默认（{view.default_port}）。</p>
                    <p className="mt-1.5">
                      <strong>改动需重启应用后生效</strong>——端口在服务启动时一次性绑定，无法热切换。
                    </p>
                    <p className="mt-1.5 text-[var(--text-muted)]">
                      Docker 部署下容器内端口固定，对外端口请改 docker-compose 的 ports
                      映射；此设置面向源码/裸机部署。
                    </p>
                  </>
                }
              />
              <input
                type="text"
                inputMode="numeric"
                defaultValue={view.port || ""}
                disabled={view.port_env_locked}
                onBlur={(e) => handlePortBlur(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                placeholder={`${view.default_port}（默认）`}
                className="w-[180px] rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-sub text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50 disabled:opacity-50 max-md:w-full"
              />
            </div>
            {view.port_env_locked && (
              <p className="mt-1.5 text-right text-caption text-[var(--text-faint)]">
                端口已由 APP_PORT 环境变量固定（Docker 部署由容器管理），此处不可修改
              </p>
            )}
            {portError && <p className="mt-1.5 text-right text-caption text-red-300">{portError}</p>}
          </div>

          {/* 外部访问地址 */}
          <div className="px-5 py-4">
            <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-stretch max-md:gap-2">
              <LabelWithHelp
                label="外部访问地址"
                help={
                  <>
                    <p>从网络上能访问到本应用的完整地址，保存即生效。</p>
                    <p className="mt-1.5">
                      例：<code>http://192.168.1.10:3000</code>，或经反向代理后的{" "}
                      <code>https://movie.example.com</code>。
                    </p>
                    <p className="mt-1.5 text-[var(--text-muted)]">
                      用于后续生成通知里的跳转链接、对外回调地址等需要绝对 URL 的场景。
                    </p>
                  </>
                }
              />
              <input
                type="text"
                defaultValue={view.external_url}
                onBlur={(e) => handleUrlBlur(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                placeholder="http://192.168.1.10:3000"
                className="w-[300px] max-w-[55%] rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 font-mono text-sub text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50 max-md:w-full max-md:max-w-none"
              />
            </div>
            {urlError && <p className="mt-1.5 text-right text-caption text-red-300">{urlError}</p>}
          </div>
        </div>

        {/* 端口已保存但未生效：浮出重启提示横幅 */}
        {view.restart_required && (
          <div className="mt-3 flex items-center justify-between gap-4 rounded-xl border border-amber-300/25 bg-amber-400/10 px-4 py-3">
            <p className="text-sub text-amber-200/90">
              端口修改已保存（当前监听 {view.runtime_port}），重启应用后生效。
            </p>
            <button
              type="button"
              onClick={() => setRestartPhase("confirming")}
              className="btn-glass shrink-0 px-3.5 py-1.5 text-sub font-semibold"
            >
              立即重启
            </button>
          </div>
        )}
      </section>

      {/* —— 维护 —— */}
      <section>
        <h3 className="group-label mb-2.5 px-1">维护</h3>
        <div className="css-glass !rounded-2xl">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <LabelWithHelp
              label="重启应用"
              help={
                <>
                  <p>优雅停机后重新启动后端服务，正在进行的任务会中断。</p>
                  <p className="mt-1.5">
                    Docker 部署由容器入口自动拉起新进程，通常几秒内恢复；源码部署需有
                    systemd 等守护，否则退出后要到服务器上手动启动。
                  </p>
                </>
              }
            />
            <button
              type="button"
              onClick={() => setRestartPhase("confirming")}
              className="btn-glass shrink-0 px-3.5 py-1.5 text-sub font-semibold text-red-300/90 hover:text-red-200"
            >
              重启应用
            </button>
          </div>
        </div>
      </section>

      {/* 重启二次确认 */}
      {restartPhase === "confirming" && (
        <div className="flex items-center justify-between gap-4 rounded-xl border border-red-300/25 bg-red-400/10 px-4 py-3">
          <p className="text-sub text-red-200/90">
            确认重启应用？重启期间服务短暂不可用，正在进行的下载投递/整理任务会中断。
          </p>
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => void doRestart()}
              className="btn-accent rounded-full px-3.5 py-1.5 text-sub font-semibold"
            >
              确认重启
            </button>
            <button
              type="button"
              onClick={() => setRestartPhase("idle")}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              取消
            </button>
          </span>
        </div>
      )}
    </div>
  );
}

/** 字段名 + ⓘ 帮助（与「网络与代理」分区同款：说明收进 tooltip，页面只留字段）。 */
function LabelWithHelp({ label, help }: { label: string; help: React.ReactNode }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <span className="text-body font-medium text-[var(--text)]">{label}</span>
      <Tooltip content={help} placement="top" maxWidth={340}>
        <button
          type="button"
          aria-label="说明"
          className="flex text-[var(--text-faint)] transition-colors hover:text-[var(--text-muted)] focus-visible:text-[var(--text-muted)]"
        >
          <InfoIcon className="size-[15px]" />
        </button>
      </Tooltip>
    </span>
  );
}
