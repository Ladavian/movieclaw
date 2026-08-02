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

/**
 * 监听导入规则（媒体库之上的独立功能）：监听一个源目录，目录里下载完成
 * 的内容自动识别并按规范命名搬进目标库主根（硬链接/复制）。
 */
export interface ImportWatchRule {
  id: number;
  /** 源目录（绝对路径，不得与任何库根路径重叠） */
  source_path: string;
  /** 搬运策略：硬链接（零占用需与目标库主根同盘）/ 复制（可跨盘） */
  strategy: "hardlink" | "copy";
  /** 目标库；null=自动路由或自定义目录 */
  library_id: number | null;
  library_name: string | null;
  /** 自定义目录目标（其余目标为 null）：识别改名后落该目录、不进入任何媒体库 */
  target_path: string | null;
  /** 自动路由/自定义目录的媒体类型（指定库时为 null） */
  kind: "movie" | "tv" | null;
  /** 目标展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」 */
  target_label: string;
  created_at: string;
}

/**
 * 创建/更新规则的请求体。目标三态：library_id 指定库；target_path 自定义
 * 目录（与 library_id 互斥）；两者皆空即自动路由。后两态 kind 必填。
 */
export interface ImportWatchPayload {
  source_path: string;
  strategy: "hardlink" | "copy";
  library_id: number | null;
  kind?: "movie" | "tv" | null;
  target_path?: string | null;
}

/** 列出全部监听导入规则。 */
export function listImportWatchRules(): Promise<ImportWatchRule[]> {
  return unwrap(request<ApiEnvelope<ImportWatchRule[]>>("/import-watch"));
}

/** 创建规则（硬链接策略保存即做同盘检测，跨盘会被拒绝并提示改复制）。 */
export function createImportWatchRule(payload: ImportWatchPayload): Promise<ImportWatchRule> {
  return unwrap(
    request<ApiEnvelope<ImportWatchRule>>("/import-watch", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 更新规则。 */
export function updateImportWatchRule(
  id: number,
  payload: ImportWatchPayload,
): Promise<ImportWatchRule> {
  return unwrap(
    request<ApiEnvelope<ImportWatchRule>>(`/import-watch/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 删除规则（不动磁盘，仅停止监听）。 */
export function deleteImportWatchRule(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/import-watch/${id}`, { method: "DELETE" }),
  );
}
