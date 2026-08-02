import type { Route } from "next";
import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";

import { SettingsPanel } from "@/components/settings-view";
import { settingsSections } from "@/lib/mock-data";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ section: string }>;
}): Promise<Metadata> {
  const { section } = await params;
  const label = settingsSections.find((s) => s.id === section)?.label;
  return { title: label ? `${label} · 设置` : "设置" };
}

/** 设置分区（/settings/[section]）：个人信息 / 外观 / 站点 / 下载器 / 订阅 等。 */
export default async function SettingsSectionPage({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  // 旧「搜索」分区已并入「资源站点」，老书签/历史链接重定向过去，不要 404
  if (section === "search") redirect("/settings/sites" as Route);
  // 旧「关于与更新」分区已并入「应用」，同样重定向兜底
  if (section === "about") redirect("/settings/app" as Route);
  if (!settingsSections.some((s) => s.id === section)) notFound();
  return <SettingsPanel active={section} />;
}
