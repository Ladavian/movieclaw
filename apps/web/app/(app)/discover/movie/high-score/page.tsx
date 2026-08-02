import type { Metadata } from "next";

import { CollectionGridView } from "@/components/collection-grid-view";

export const metadata: Metadata = { title: "豆瓣高分电影" };

/** 豆瓣高分电影完整榜单（500 部）：网格浏览、片名搜索与分批加载。 */
export default function HighScorePage() {
  return (
    <div className="flex h-full flex-col">
      <CollectionGridView collectionId="movie_high_score" title="豆瓣高分电影" />
    </div>
  );
}
