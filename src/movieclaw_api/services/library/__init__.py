"""媒体库域子包：扫描识别、监听导入、整理、详情装配、认领、路由、配置。

原先 11 个 ``library_*`` 平级模块交叉引用密集，这里补上目录边界：

- ``config``    库的 CRUD 与保存路径推导（derive_save_path）
- ``scan``      存量扫描：遍历 → 识别 → 探测 → 资产补齐（含 6 小时对账任务）
- ``ingest``    监听导入：下载完成内容按 info_hash 认领/识别后硬链、复制进库
- ``resolve``   识别底座：TMDB 保守收敛与验证（scan/ingest 共用）
- ``items``     条目详情装配、海报墙聚合、真实删除
- ``claim``     人工认领与身份复核拍板
- ``organize``  存量文件名规范化（批量改名归位）
- ``routing``   收藏范围路由与投递目录三级兜底（resolve_save_path 唯一实现）
- ``nfo``       NFO 读写（Kodi/Emby/Jellyfin 规范）
- ``layout``    条目目录/命名约定的纯函数（原 library_import）
- ``watch``     watchdog 实时监控（去抖批处理）

包内互引密集（scan↔items、scan↔organize、config↔ingest 等既有环由函数内
延迟导入消解），外部消费方直接按子模块路径导入。
"""
