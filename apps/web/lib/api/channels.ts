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

/** 已绑定的微信账号（见 schemas.channels.WeixinAccountView）。 */
export interface WeixinAccount {
  account_id: string;
  /** 扫码绑定人的微信用户 id（白名单：只有此人能对话） */
  bound_user_id: string | null;
  /** active=正常；stale=凭据失效需重新扫码 */
  status: "active" | "stale";
  /** 当前进程内收发循环是否在运行 */
  running: boolean;
  last_error: string | null;
  bound_at: string;
}

/** 绑定流程状态（见 schemas.channels.WeixinBindingStatusView 的注释）。 */
export type WeixinBindingStatus =
  | "pending"
  | "scanned"
  | "need_verify_code"
  | "confirmed"
  | "already_bound"
  | "expired"
  | "failed";

export interface WeixinBindingStart {
  challenge_id: string;
  qrcode_url: string;
  /** 服务端渲染好的二维码 SVG data URL，<img> 直接显示 */
  qrcode_image: string;
  message: string;
}

export interface WeixinBindingSnapshot {
  challenge_id: string;
  status: WeixinBindingStatus;
  message: string;
  qrcode_url: string;
  qrcode_image: string;
  account: WeixinAccount | null;
}

export function listWeixinAccounts(init?: RequestInit): Promise<WeixinAccount[]> {
  return unwrap(request<ApiEnvelope<WeixinAccount[]>>("/channels/weixin/accounts", init));
}

export function startWeixinBinding(): Promise<WeixinBindingStart> {
  return unwrap(
    request<ApiEnvelope<WeixinBindingStart>>("/channels/weixin/bindings", { method: "POST" }),
  );
}

export function getWeixinBindingStatus(challengeId: string): Promise<WeixinBindingSnapshot> {
  return unwrap(
    request<ApiEnvelope<WeixinBindingSnapshot>>(
      `/channels/weixin/bindings/${encodeURIComponent(challengeId)}`,
    ),
  );
}

export function submitWeixinVerifyCode(
  challengeId: string,
  code: string,
): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/channels/weixin/bindings/${encodeURIComponent(challengeId)}/verify-code`,
      { method: "POST", body: JSON.stringify({ code }) },
    ),
  );
}

export function unbindWeixinAccount(accountId: string): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(
      `/channels/weixin/accounts/${encodeURIComponent(accountId)}`,
      { method: "DELETE" },
    ),
  );
}
