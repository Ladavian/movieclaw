import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** 保存请求体（见 routes/app_config.AppConfigPayload）。 */
export interface AppConfigPayload {
  /** 后端监听端口：0 = 未设置（跟随默认），否则 1024~65535；重启后生效 */
  port: number;
  /** 网络可访问到本应用的完整地址（http/https）；空 = 未配置 */
  external_url: string;
}

/** 读取响应：配置本体 + 端口生效状态。 */
export interface AppConfigView extends AppConfigPayload {
  /** 端口未设置时的生效默认值（APP_PORT 环境变量或内置 8000） */
  default_port: number;
  /** 当前进程实际监听的端口 */
  runtime_port: number;
  /** 端口已被 APP_PORT 环境变量钉死（Docker 部署即如此），设置页端口不生效 */
  port_env_locked: boolean;
  /** 已保存端口与当前监听端口不一致，需重启生效 */
  restart_required: boolean;
}

/** 读取应用设置。 */
export function getAppConfig(): Promise<AppConfigView> {
  return unwrap(request<ApiEnvelope<AppConfigView>>("/app/config"));
}

/** 保存应用设置（外部地址即时生效；端口改动看返回的 restart_required）。 */
export function saveAppConfig(payload: AppConfigPayload): Promise<AppConfigView> {
  return unwrap(
    request<ApiEnvelope<AppConfigView>>("/app/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 请求重启应用：后端优雅停机后由 Docker 等进程守护拉起。 */
export function restartApp(): Promise<null> {
  return unwrap(request<ApiEnvelope<null>>("/app/restart", { method: "POST" }));
}

// ---------------------------------------------------------------------------
// 应用内更新（设置 → 关于与更新，见 routes/app_update）
// ---------------------------------------------------------------------------

/** 版本与更新能力状态（GET /app/update/status）。 */
export interface UpdateStatusView {
  /** 当前运行的应用版本 */
  current_version: string;
  /** 代码来源：baseline（镜像内置）/ overlay（应用内更新版本）/ dev（源码部署） */
  code_source: "baseline" | "overlay" | "dev" | string;
  /** overlay 生效时的版本号 */
  overlay_version: string | null;
  /** 镜像运行时版本（依赖集合代号）；非 Docker 部署为 null */
  runtime_version: number | null;
  /** 是否支持应用内更新（仅 Docker 部署） */
  can_update: boolean;
  /** 是否存在可回退的上一版本 */
  has_previous: boolean;
  /** 曾连续启动失败被自动回落的坏版本列表 */
  bad_versions: string[];
  /** 当前生效的 NER 模型 Release tag（如 torrent-ner-v1）；无法识别时为 null */
  model_tag: string | null;
  /** 盘上已安装但未在运行的 overlay 版本（runtime 不匹配/坏版本回落/等待重启） */
  inactive_overlay_version: string | null;
  /** 上述版本未生效的中文原因 */
  inactive_overlay_reason: string | null;
}

/** 检查更新的结果（POST /app/update/check）。 */
export interface UpdateCheckView {
  current_version: string;
  latest_version: string;
  update_available: boolean;
  /** false = 新版本包含依赖变化，需升级 Docker 镜像 */
  compatible: boolean;
  requires_runtime: number;
  /** GitHub Release 的更新说明（Markdown 文本） */
  changelog: string;
  published_at: string;
  /** 最新版曾在本机连续启动失败被自动回落（重装会清除标记再试一次） */
  latest_known_bad: boolean;
}

/** 更新执行进度（GET /app/update/progress）。 */
export interface UpdateProgressView {
  phase:
    | "idle"
    | "checking"
    | "downloading"
    | "verifying"
    | "applying"
    | "restarting"
    | "failed"
    | string;
  detail: string;
  percent: number | null;
  error: string | null;
  target_version: string | null;
}

export function getUpdateStatus(): Promise<UpdateStatusView> {
  return unwrap(request<ApiEnvelope<UpdateStatusView>>("/app/update/status"));
}

export function checkUpdate(): Promise<UpdateCheckView> {
  return unwrap(request<ApiEnvelope<UpdateCheckView>>("/app/update/check", { method: "POST" }));
}

export function applyUpdate(): Promise<UpdateProgressView> {
  return unwrap(request<ApiEnvelope<UpdateProgressView>>("/app/update/apply", { method: "POST" }));
}

export function getUpdateProgress(): Promise<UpdateProgressView> {
  return unwrap(request<ApiEnvelope<UpdateProgressView>>("/app/update/progress"));
}

/** 回退到上一版本；返回后端的中文结果说明（信封 message）。 */
export async function rollbackUpdate(): Promise<string> {
  const envelope = await request<ApiEnvelope<null>>("/app/update/rollback", { method: "POST" });
  return envelope.message;
}

/** 检查 NER 模型更新的结果（POST /app/update/model/check）。 */
export interface ModelUpdateCheckView {
  /** 当前生效的模型 Release tag；老镜像无记录时为 null（显示「无法识别」） */
  current_tag: string | null;
  latest_tag: string;
  update_available: boolean;
  /** 最新模型发布是否携带更新清单（没带则无法应用内安装） */
  installable: boolean;
  published_at: string;
}

export function checkModelUpdate(): Promise<ModelUpdateCheckView> {
  return unwrap(
    request<ApiEnvelope<ModelUpdateCheckView>>("/app/update/model/check", { method: "POST" }),
  );
}

export function applyModelUpdate(): Promise<UpdateProgressView> {
  return unwrap(
    request<ApiEnvelope<UpdateProgressView>>("/app/update/model/apply", { method: "POST" }),
  );
}
