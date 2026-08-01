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
