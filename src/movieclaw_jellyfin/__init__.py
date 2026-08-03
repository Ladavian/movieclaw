"""Jellyfin 兼容播放接口（协议层）。

让 Infuse / Fileball / VidHub 等播放器以 Jellyfin 服务器的身份连接 movieclaw。
设计契约见 docs/design/jellyfin-compat.md（v1.1，逐字段对照 jellyfin@33a8cdf
源码核实）——实现里凡"为什么是这个形状"的问题，一律以该文档为准。

分层纪律（设计文档 8.5）：本包只做协议翻译（GUID 编解码、PascalCase DTO、
Authorization 解析、ticks↔毫秒换算）；观看状态与取流等领域能力在
movieclaw_playback，网页端播放器将来复用领域层、不消费本包接口。
"""
