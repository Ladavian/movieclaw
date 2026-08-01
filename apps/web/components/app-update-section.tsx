"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { RefreshIcon } from "@/components/icons";
import {
  type ModelUpdateCheckView,
  type UpdateCheckView,
  type UpdateProgressView,
  type UpdateStatusView,
  applyModelUpdate,
  applyUpdate,
  checkModelUpdate,
  checkUpdate,
  getUpdateProgress,
  getUpdateStatus,
  rollbackUpdate,
} from "@/lib/api/app";
import { getHealth } from "@/lib/api/health";

/**
 * 关于与更新（设置 → 关于与更新）。
 *
 * 应用内更新的用户界面（机制见 docs/design/in-app-update.md）：
 *   - 状态区：当前版本 + 代码来源（镜像内置 / 应用内更新 vX / 源码部署）；
 *     曾启动失败被自动回落的版本在此外显。
 *   - 检查更新：比对 GitHub 最新 Release。依赖没变（runtime 匹配）就能
 *     一键更新；依赖变了明确提示需升级 Docker 镜像。
 *   - 更新执行：后端后台下载校验，前端 1s 轮询进度；进入 restarting 后
 *     改为轮询 /health 等服务恢复（前后端全量重启），恢复即整页刷新。
 *   - 回退：切回上一版本（可再次回退撤销）；无上一版本时回落镜像内置版本。
 */

type RestartWait = "idle" | "waiting" | "timeout";

export function AppUpdateSection() {
  const [status, setStatus] = useState<UpdateStatusView | null>(null);
  const [failed, setFailed] = useState(false);
  const [check, setCheck] = useState<UpdateCheckView | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [progress, setProgress] = useState<UpdateProgressView | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [modelCheck, setModelCheck] = useState<ModelUpdateCheckView | null>(null);
  const [modelChecking, setModelChecking] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  const [restartWait, setRestartWait] = useState<RestartWait>("idle");
  const [confirmRollback, setConfirmRollback] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const reload = useCallback(() => {
    setFailed(false);
    getUpdateStatus()
      .then(setStatus)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, [reload]);

  /** 全量重启的等待：前后端都会退出，稍等后轮询 /health，恢复即整页刷新。 */
  const waitForRestart = useCallback(async () => {
    setRestartWait("waiting");
    await new Promise((r) => setTimeout(r, 5000));
    for (let i = 0; i < 60; i++) {
      try {
        await getHealth();
        window.location.reload();
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    setRestartWait("timeout");
  }, []);

  /** 更新执行中：轮询进度直到失败或进入重启阶段。 */
  const pollProgress = useCallback(() => {
    if (pollTimer.current) clearInterval(pollTimer.current);
    pollTimer.current = setInterval(async () => {
      try {
        const p = await getUpdateProgress();
        setProgress(p);
        if (p.phase === "failed") {
          if (pollTimer.current) clearInterval(pollTimer.current);
        } else if (p.phase === "restarting") {
          if (pollTimer.current) clearInterval(pollTimer.current);
          void waitForRestart();
        }
      } catch {
        // 后端可能已进入重启：转入健康轮询
        if (pollTimer.current) clearInterval(pollTimer.current);
        void waitForRestart();
      }
    }, 1000);
  }, [waitForRestart]);

  const doCheck = async () => {
    setChecking(true);
    setCheckError(null);
    setCheck(null);
    try {
      setCheck(await checkUpdate());
    } catch (e) {
      setCheckError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  const doApply = async () => {
    setActionError(null);
    try {
      setProgress(await applyUpdate());
      pollProgress();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const doModelCheck = async () => {
    setModelChecking(true);
    setModelError(null);
    setModelCheck(null);
    try {
      setModelCheck(await checkModelUpdate());
    } catch (e) {
      setModelError((e as Error).message);
    } finally {
      setModelChecking(false);
    }
  };

  const doModelApply = async () => {
    setModelError(null);
    try {
      setProgress(await applyModelUpdate());
      pollProgress();
    } catch (e) {
      setModelError((e as Error).message);
    }
  };

  const doRollback = async () => {
    setConfirmRollback(false);
    setActionError(null);
    try {
      await rollbackUpdate();
      void waitForRestart();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  if (failed) {
    return (
      <div className="flex items-center gap-3">
        <p className="text-ui text-[var(--text-muted)]">版本信息加载失败</p>
        <button type="button" onClick={reload} className="btn-glass px-3 py-1.5 text-sub font-medium">
          重试
        </button>
      </div>
    );
  }
  if (!status) {
    return <p className="text-ui text-[var(--text-muted)]">正在加载版本信息…</p>;
  }

  // 重启等待态：全区替换为状态页（与应用设置的重启流程同款体验）
  if (restartWait !== "idle") {
    return (
      <div className="css-glass !rounded-2xl px-6 py-10 text-center">
        {restartWait === "waiting" ? (
          <>
            <p className="text-body font-medium text-[var(--text)]">正在重启并切换版本…</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              前后端会一起重启，服务恢复后页面自动刷新，通常需要几十秒。
            </p>
          </>
        ) : (
          <>
            <p className="text-body font-medium text-[var(--text)]">等待超时，应用尚未恢复</p>
            <p className="mt-2 text-sub text-[var(--text-muted)]">
              请稍后手动刷新页面。若反复无法恢复，容器会自动回落到更新前的版本，
              数据不受影响。
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

  const updating =
    progress != null && !["idle", "failed"].includes(progress.phase);
  const sourceLabel =
    status.code_source === "overlay"
      ? `应用内更新版本${status.overlay_version ? ` v${status.overlay_version}` : ""}`
      : status.code_source === "baseline"
        ? "Docker 镜像内置"
        : "源码部署";

  return (
    <div className="space-y-7">
      {/* —— 版本 —— */}
      <section>
        <h3 className="group-label mb-2.5 px-1">版本</h3>
        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div>
              <p className="text-ui font-medium text-[var(--text)]">当前版本</p>
              <p className="mt-0.5 text-sub text-[var(--text-muted)]">来源：{sourceLabel}</p>
            </div>
            <span className="font-mono text-body text-[var(--text)]">v{status.current_version}</span>
          </div>
          {status.bad_versions.length > 0 && (
            <div className="px-5 py-3.5">
              <p className="text-sub text-amber-300/90">
                版本 {status.bad_versions.map((v) => `v${v}`).join("、")} 曾连续启动失败，
                已被自动回落保护。可在新版本发布后重新更新。
              </p>
            </div>
          )}
        </div>
      </section>

      {/* —— 更新 —— */}
      <section>
        <h3 className="group-label mb-2.5 px-1">更新</h3>
        <div className="css-glass !rounded-2xl px-5 py-4">
          {!status.can_update ? (
            <p className="text-sub text-[var(--text-muted)]">
              仅 Docker 镜像部署支持应用内更新；源码部署请用 git pull 更新。
            </p>
          ) : updating && progress ? (
            <div>
              <p className="text-ui font-medium text-[var(--text)]">
                {progress.detail || "正在更新…"}
              </p>
              {progress.phase === "downloading" && progress.percent != null && (
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
                  <div
                    className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500"
                    style={{ width: `${progress.percent}%` }}
                  />
                </div>
              )}
              <p className="mt-2 text-sub text-[var(--text-muted)]">
                更新在后台执行，完成后会自动重启并刷新页面。
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={doCheck}
                  disabled={checking}
                  className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
                >
                  <RefreshIcon className={`size-4 ${checking ? "animate-spin" : ""}`} />
                  <span>{checking ? "正在检查…" : "检查更新"}</span>
                </button>
                {check && !check.update_available && (
                  <span className="text-sub text-emerald-300/90">
                    已是最新版本（v{check.current_version}）
                  </span>
                )}
                {checkError && <span className="text-sub text-red-300">{checkError}</span>}
              </div>

              {check?.update_available && (
                <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] px-4 py-3.5">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <p className="text-ui font-medium text-[var(--text)]">
                      发现新版本 v{check.latest_version}
                    </p>
                    {check.compatible ? (
                      <button
                        type="button"
                        onClick={doApply}
                        className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-[var(--accent)]"
                      >
                        立即更新
                      </button>
                    ) : (
                      <span className="text-sub text-amber-300/90">
                        本次更新包含依赖变化，需拉取新的 Docker 镜像升级
                      </span>
                    )}
                  </div>
                  {check.changelog && (
                    <pre className="scroll-thin mt-3 max-h-56 overflow-y-auto whitespace-pre-wrap break-words font-sans text-sub leading-relaxed text-[var(--text-muted)]">
                      {check.changelog}
                    </pre>
                  )}
                </div>
              )}
              {actionError && <p className="text-sub text-red-300">{actionError}</p>}
              {progress?.phase === "failed" && progress.error && (
                <p className="text-sub text-red-300">更新失败：{progress.error}</p>
              )}
            </div>
          )}
        </div>
      </section>

      {/* —— NER 模型 ——（独立于代码更新，更新只重启后端、页面不中断） */}
      {status.can_update && (
        <section>
          <h3 className="group-label mb-2.5 px-1">NER 识别模型</h3>
          <div className="css-glass !rounded-2xl px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-ui font-medium text-[var(--text)]">
                  当前模型：{status.model_tag ?? "无法识别（较早的镜像）"}
                </p>
                <p className="mt-0.5 text-sub text-[var(--text-muted)]">
                  种子名识别（NER）模型独立更新，无需升级镜像；更新后仅重启后端，页面不中断。
                </p>
              </div>
              {!updating && (
                <button
                  type="button"
                  onClick={doModelCheck}
                  disabled={modelChecking}
                  className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
                >
                  {modelChecking ? "正在检查…" : "检查模型更新"}
                </button>
              )}
            </div>
            {modelCheck && (
              <div className="mt-3 flex flex-wrap items-center gap-3">
                {!modelCheck.update_available ? (
                  <span className="text-sub text-emerald-300/90">
                    模型已是最新（{modelCheck.latest_tag}）
                  </span>
                ) : !modelCheck.installable ? (
                  <span className="text-sub text-amber-300/90">
                    发现新模型 {modelCheck.latest_tag}，但该发布未携带更新清单，暂无法应用内安装
                  </span>
                ) : (
                  <>
                    <span className="text-sub text-[var(--text)]">
                      发现新模型 {modelCheck.latest_tag}
                    </span>
                    <button
                      type="button"
                      onClick={doModelApply}
                      disabled={updating}
                      className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-[var(--accent)] disabled:opacity-50"
                    >
                      更新模型
                    </button>
                  </>
                )}
              </div>
            )}
            {modelError && <p className="mt-2 text-sub text-red-300">{modelError}</p>}
          </div>
        </section>
      )}

      {/* —— 回退 ——（只在有更新历史时出现） */}
      {status.can_update && (status.has_previous || status.code_source === "overlay") && (
        <section>
          <h3 className="group-label mb-2.5 px-1">回退</h3>
          <div className="css-glass flex flex-wrap items-center justify-between gap-3 !rounded-2xl px-5 py-4">
            <p className="text-sub text-[var(--text-muted)]">
              更新后遇到问题时，可切回{status.has_previous ? "上一版本" : "镜像内置版本"}
              （数据不受影响；跨过数据库升级的回退可从自动备份恢复）。
            </p>
            {confirmRollback ? (
              <span className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={doRollback}
                  className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-red-300"
                >
                  确认回退并重启
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmRollback(false)}
                  className="btn-glass px-3.5 py-1.5 text-sub font-medium"
                >
                  取消
                </button>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmRollback(true)}
                disabled={updating}
                className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-50"
              >
                回退版本
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
