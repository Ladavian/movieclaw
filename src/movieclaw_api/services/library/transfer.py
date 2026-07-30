"""条目转移：把一部作品连同**整个磁盘目录**从一个库搬到另一个库。

为什么需要它：入库时的选库要么靠收藏范围路由（library_routing）、要么靠
用户在订阅弹窗里手选，两条路都可能选错——最典型的是韩剧被路由进了
「大陆华语剧」（元数据里 origin_countries 缺失或不准）。此前对这种错分
**没有任何补救手段**：改库的收藏范围只影响后续入库，重新识别只改身份锚
不动归属，唯一能"挪窝"的办法是手工移动目录再两边重扫——对普通用户
不可接受。本模块补上这条通道。

语义定线（三条，与删除/整理同一套克制哲学）：

- **搬的是目录不是台账**：转移 = 磁盘上的条目目录整体搬到目标库主根下 +
  台账行随迁（library_id 与 file_path 一起改）。只改台账不动文件会立刻
  被下一次扫描打回原形（文件还在旧根下，旧库重新入账、新库标缺失）；
- **目录名原样保留**：搬过去仍叫 ``风筝 (2017)``，不趁机规范化命名——
  规范化是「整理文件名」的职责，一次操作只做一件事，用户才说得清
  "刚才那一下到底改了什么"；
- **绝不覆盖、绝不合并**：目标已存在同名目录即判为冲突并中止该单元
  （多半是目标库里已经有这部片子），宁可让用户自己决断。

跨设备（源根与目标根不在同一块盘）是本模块唯一的重活：``os.rename`` 会以
EXDEV 失败，此时退化为"完整复制 → 复制成功才删源"。两个后果必须在预览
里说清楚：耗时按体积走（几十 GB 以分钟计），且**与做种目录的硬链接关系
会断开**（复制产生新 inode，下载器仍在对旧文件做种，磁盘占用翻倍）。

并发：转移期间在**源库与目标库两侧**都占住库级任务位（TaskState），扫描、
整理、重识别、改库配置一律挡下——搬运中途被扫描介入会把搬走的旧路径标
missing、把新路径当新文件重走识别链，人工认领会丢。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, ConflictException
from movieclaw_api.services.library.layout import entry_dir_of

# 复用整理器的"只清理自己搬空的目录"实现（非空即停、绝不删文件）——同一
# 语义两处各写一份迟早分叉，而这段克制正是搬运类操作的安全底线
from movieclaw_api.services.library.organize import _prune_emptied_dirs
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library, LibraryFile, MediaItem, Subscription, utcnow
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

logger = logging.getLogger("movieclaw_api.library_transfer")

# 跟随主文件一起搬的附属文件后缀判定：同目录、文件名以"主文件名."开头
# （foo.zh.srt / foo.nfo）。同名不同容器的视频是独立版本不是附属，排除。
# 与 library_organize 同一套约定
_SIDECAR_SKIP_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".iso", ".wmv", ".mov", ".flv"}


@dataclass
class TransferState:
    """进行中转移的实时状态（前端进度条 + 冲突提示的数据源）。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str
    processed: int = 0
    total: int = 0


# 每库单飞：源库与目标库**两个键**都登记同一个 state 对象，任何一侧的
# 扫描/整理/编辑都能据此挡下（值同一个对象，进度更新一处生效两处可见）
_transfer_tasks: TaskState[TransferState] = TaskState()
# 后台任务的存活引用：create_task 不持引用可能被 GC 中途取消
_running: set[asyncio.Task] = set()


def is_transferring(library_id: int) -> bool:
    """该库是否正被某次条目转移占用（作为源或目标皆算）。"""
    return _transfer_tasks.running(library_id)


def transfer_state(library_id: int) -> TransferState | None:
    """进行中转移的实时状态；没在转移返回 None。"""
    return _transfer_tasks.state_of(library_id)


def last_transfer(library_id: int) -> tuple | None:
    """最近一次转移的 (完成时间, TransferSummary)；从未转移过则为 None。"""
    return _transfer_tasks.last(library_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 计划：纯计算（只读磁盘与台账），预览接口与执行共用
# ---------------------------------------------------------------------------


@dataclass
class TransferMove:
    """一个搬运单元：整个条目目录，或（目录里混着别的条目时）单个文件。"""

    source_path: str
    target_path: str
    is_dir: bool
    size_bytes: int  # 本单元涉及的台账体积（展示用）
    file_ids: list[int] = field(default_factory=list)  # 随迁的台账行
    # 单文件模式下跟随主文件搬的字幕/NFO 等（(源, 目标) 对）
    sidecars: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TransferSkip:
    """不参与转移的路径与中文原因（预览逐条展示，用户心里有数）。"""

    file_path: str
    reason: str


@dataclass
class TransferPlan:
    """一次转移的完整计划。``blocked`` 非空时执行接口直接拒绝。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str
    moves: list[TransferMove] = field(default_factory=list)
    skips: list[TransferSkip] = field(default_factory=list)
    # 逻辑随迁的缺失台账行（磁盘上没有实体，只改 library_id 与路径投影）
    missing_file_ids: list[int] = field(default_factory=list)
    total_bytes: int = 0
    # 目标主根与源目录不在同一块盘 → 退化为完整复制（耗时 + 断硬链）
    cross_device: bool = False
    # 阻断性问题（目标已存在同名目录等）：有值就不给执行
    blocked: list[str] = field(default_factory=list)


async def build_transfer_plan(
    session,
    source: Library,
    target: Library,
    item: MediaItem,
    files: list[LibraryFile],
) -> TransferPlan:
    """计算转移计划。只读磁盘与台账，不做任何写入。

    调用前的合法性校验（同类型、非同库、目标有主根）由 ``assert_transferable``
    统一负责——预览与执行都要走那一道，不在这里重复。
    """
    assert source.id is not None and target.id is not None and item.id is not None
    # 源库里**其他条目**占用的路径：条目目录里混着别人时不能整目录搬
    foreign = list(
        (
            await session.execute(
                select(LibraryFile.file_path).where(
                    LibraryFile.library_id == source.id,
                    LibraryFile.media_item_id != item.id,
                )
            )
        )
        .scalars()
        .all()
    )
    roots = [Path(p) for p in source.root_paths]
    target_root = Path(target.primary_root or "")
    # 磁盘检查（exists/stat/附属文件枚举）放线程池：网络挂载上一次 stat 也要毫秒级
    return await asyncio.to_thread(
        _build_plan_sync,
        source.id,
        target.id,
        item.id,
        item.title,
        roots,
        target_root,
        files,
        [Path(p) for p in foreign],
    )


def _build_plan_sync(
    source_library_id: int,
    target_library_id: int,
    media_item_id: int,
    title: str,
    roots: list[Path],
    target_root: Path,
    files: list[LibraryFile],
    foreign_paths: list[Path],
) -> TransferPlan:
    plan = TransferPlan(
        source_library_id=source_library_id,
        target_library_id=target_library_id,
        media_item_id=media_item_id,
        title=title,
    )
    if not target_root.is_dir():
        plan.blocked.append(f"目标库的主根路径不可访问：{target_root}（盘未挂载？）")
        return plan

    # 条目目录 → 该目录下本条目的台账行；保序（第一个目录决定跨设备判定）
    by_entry: dict[Path, list[LibraryFile]] = {}
    loose: list[LibraryFile] = []  # 找不到条目目录、直接躺在库根下的裸文件

    for row in files:
        path = Path(row.file_path)
        if row.missing_since is not None:
            # 磁盘上没有实体，无从搬运——只做逻辑随迁（下面统一处理）
            assert row.id is not None
            plan.missing_file_ids.append(row.id)
            continue
        entry = entry_dir_of(roots, path)
        if entry is None and row.container in ("bluray", "dvd"):
            entry = path  # 直接躺在根下的原盘目录：目录本身就是条目
        if entry is not None and _inside_roots(entry, roots):
            by_entry.setdefault(entry, []).append(row)
        elif _inside_roots(path, roots):
            loose.append(row)
        else:
            plan.skips.append(
                TransferSkip(row.file_path, "不在源库的根路径之内（根路径可能已变更），已跳过")
            )

    taken: set[str] = set()  # 本次计划已占用的目标路径（同名条目目录撞车检测）

    for entry, rows in by_entry.items():
        dst = target_root / entry.name
        if str(dst) in taken or dst.exists():
            plan.blocked.append(
                f"目标库里已存在同名目录「{dst}」，为避免覆盖/合并已中止；"
                "请先处理目标库里的同名内容，或改用「重新识别」修正身份"
            )
            continue
        mixed = [p for p in foreign_paths if p == entry or entry in p.parents]
        if mixed:
            # 目录里混着其他条目：退化为逐文件搬（保留相对结构，Season 层跟着走）
            for row in rows:
                src = Path(row.file_path)
                rel = src.relative_to(entry)
                _add_file_move(plan, row, src, dst / rel, taken)
            plan.skips.append(
                TransferSkip(
                    str(entry),
                    f"目录里还有其他条目的 {len(mixed)} 个文件，未整目录搬运——"
                    "只搬本片的文件及其字幕/NFO",
                )
            )
            continue
        size = sum(r.size_bytes for r in rows)
        taken.add(str(dst))
        plan.moves.append(
            TransferMove(
                source_path=str(entry),
                target_path=str(dst),
                is_dir=True,
                size_bytes=size,
                file_ids=[r.id for r in rows if r.id is not None],
            )
        )

    for row in loose:
        src = Path(row.file_path)
        _add_file_move(plan, row, src, target_root / src.name, taken)

    plan.total_bytes = sum(m.size_bytes for m in plan.moves)
    if plan.moves:
        plan.cross_device = _cross_device(Path(plan.moves[0].source_path), target_root)
    return plan


def _add_file_move(
    plan: TransferPlan, row: LibraryFile, src: Path, dst: Path, taken: set[str]
) -> None:
    """登记一个单文件搬运单元（含字幕/NFO 等附属文件）。"""
    if str(dst) in taken or dst.exists():
        plan.skips.append(TransferSkip(str(src), f"目标路径已存在同名文件，跳过以免覆盖：{dst}"))
        return
    taken.add(str(dst))
    assert row.id is not None
    plan.moves.append(
        TransferMove(
            source_path=str(src),
            target_path=str(dst),
            is_dir=src.is_dir(),
            size_bytes=row.size_bytes,
            file_ids=[row.id],
            sidecars=_find_sidecars(src, dst),
        )
    )


def _find_sidecars(src: Path, dst: Path) -> list[tuple[str, str]]:
    """主文件的附属文件（同目录、以"主文件名."开头的字幕/NFO/图片）。"""
    if src.is_dir() or not src.suffix:
        return []
    try:
        entries = sorted(src.parent.iterdir())
    except OSError:
        return []
    prefix = src.stem + "."
    moves = []
    for entry in entries:
        if not entry.is_file() or entry == src or not entry.name.startswith(prefix):
            continue
        if entry.suffix.lower() in _SIDECAR_SKIP_EXTS:
            continue
        tail = entry.name[len(src.stem) :]  # 含开头的 "."，如 ".zh.srt"
        moves.append((str(entry), str(dst.parent / (dst.stem + tail))))
    return moves


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    """路径必须严格位于某个库根之内（不等于根本身）——搬运的硬边界。"""
    return any(root in path.parents for root in roots)


def _cross_device(source: Path, target_root: Path) -> bool:
    """源与目标是否分处两块盘（决定 rename 还是完整复制）。探测失败保守判 True。"""
    try:
        return os.stat(source).st_dev != os.stat(target_root).st_dev
    except OSError:
        return True


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass
class TransferSummary:
    """一次转移的结论（日志与接口响应共用）。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str = ""
    target_library_name: str = ""
    moved_paths: list[str] = field(default_factory=list)  # 实际搬到的目标路径
    files_relocated: int = 0  # 随迁的台账行数
    bytes_moved: int = 0
    removed_dirs: int = 0  # 搬空后清理掉的源目录数
    subscription_moved: bool = False  # 该片的订阅是否一并改挂目标库
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def assert_transferable(source: Library, target: Library) -> None:
    """转移前的合法性校验（预览与执行共用；不合法抛中文报错）。"""
    if source.id == target.id:
        raise BadRequestException("目标库与当前库相同，无需转移")
    if source.kind != target.kind:
        raise BadRequestException(
            "只能转移到同类型的媒体库（电影 ↔ 电影、剧集 ↔ 剧集）；"
            "如果是类型判错，请用「重新识别」或在待识别清单里人工认领"
        )
    if not target.primary_root:
        raise BadRequestException(f"目标库「{target.name}」没有配置根路径，无法接收转移")
    for library in (source, target):
        assert library.id is not None
        _assert_idle(library)


def _assert_idle(library: Library) -> None:
    """库必须空闲：扫描/重识别/整理/另一次转移在跑时一律不给转。"""
    from movieclaw_api.services.library.organize import is_organizing
    from movieclaw_api.services.library.scan import PHASE_LABELS, busy_phase

    assert library.id is not None
    phase = busy_phase(library.id)
    if phase is not None:
        raise ConflictException(
            f"「{library.name}」{PHASE_LABELS[phase]}，请等当前任务完成后再转移"
        )
    if is_organizing(library.id):
        raise ConflictException(f"「{library.name}」正在整理文件名，请等整理完成后再转移")
    state = _transfer_tasks.state_of(library.id)
    if state is not None:
        raise ConflictException(f"「{library.name}」正在转移「{state.title}」，请等待完成")


def start_transfer(plan: TransferPlan) -> TransferState:
    """占住两侧库级任务位并在后台执行转移；同步返回初始状态。

    锁在**返回响应前**就立起来（而不是等后台任务跑起来才立），前端紧接着
    的第一次进度轮询必然看得到"正在转移"——否则会闪一下"没有任务在跑"，
    看起来像点了没反应。
    """
    state = TransferState(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
        total=len(plan.moves),
    )
    _transfer_tasks.try_start(plan.source_library_id, state)
    _transfer_tasks.try_start(plan.target_library_id, state)

    task = asyncio.create_task(_run(plan, state))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return state


async def _run(plan: TransferPlan, state: TransferState) -> None:
    summary = TransferSummary(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
    )
    try:
        await _transfer(plan, state, summary)
    except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
        logger.exception(
            "条目 #%s 从库 #%s 转移到库 #%s 时发生未知错误",
            plan.media_item_id,
            plan.source_library_id,
            plan.target_library_id,
        )
        summary.errors.append("转移中断：发生未知错误（详见后端日志）")
    finally:
        finished = (utcnow(), summary)
        _transfer_tasks.finish(plan.source_library_id, result=finished)
        _transfer_tasks.finish(plan.target_library_id, result=finished)


async def _transfer(plan: TransferPlan, state: TransferState, summary: TransferSummary) -> None:
    db = get_database()
    async with db.session() as session:
        target = await session.get(Library, plan.target_library_id)
        if target is None:
            summary.errors.append("目标媒体库不存在（可能已被删除）")
            return
        summary.target_library_name = target.name
        repo = LibraryFileRepository(session)
        dirty_parents: set[Path] = set()

        for done, move in enumerate(plan.moves, start=1):
            src, dst = Path(move.source_path), Path(move.target_path)
            try:
                await asyncio.to_thread(_move, src, dst, plan.cross_device)
            except _MoveError as exc:
                summary.errors.append(str(exc))
                state.processed = done
                continue
            summary.moved_paths.append(move.target_path)
            summary.bytes_moved += move.size_bytes
            dirty_parents.add(src.parent)

            for sidecar_src, sidecar_dst in move.sidecars:
                try:
                    await asyncio.to_thread(_move, Path(sidecar_src), Path(sidecar_dst), False)
                except _MoveError as exc:
                    summary.errors.append(f"附属文件搬运失败：{exc}")

            # 搬运成功立即随迁台账：中途失败不会留下"账在新库、文件还在旧库"
            for file_id in move.file_ids:
                row = await session.get(LibraryFile, file_id)
                if row is None:
                    continue
                new_path = (
                    str(dst / Path(row.file_path).relative_to(src)) if move.is_dir else str(dst)
                )
                await repo.relocate_to_library(
                    file_id, library_id=plan.target_library_id, file_path=new_path
                )
                summary.files_relocated += 1
            state.processed = done

        # 缺失行没有磁盘实体，只改归属：留在旧库会变成"旧库里一个永远补不回来
        # 的缺失条目"，跟着条目走才对得上用户认知
        for file_id in plan.missing_file_ids:
            row = await session.get(LibraryFile, file_id)
            if row is None:
                continue
            await repo.relocate_to_library(
                file_id,
                library_id=plan.target_library_id,
                file_path=row.file_path,
                keep_missing=True,
            )
            summary.files_relocated += 1

        # 只清理被本次转移搬空的源目录（及其变空的祖先）：非空即停、绝不删文件
        source = await session.get(Library, plan.source_library_id)
        if source is not None and dirty_parents:
            summary.removed_dirs = await asyncio.to_thread(
                _prune_emptied_dirs, dirty_parents, [r.rstrip("/") for r in source.root_paths]
            )

        # 订阅一并改挂目标库：不然下一集下载完又按旧库投递，用户刚搬完就被打回原形
        if summary.files_relocated:
            subscription = (
                await session.execute(
                    select(Subscription).where(Subscription.media_item_id == plan.media_item_id)
                )
            ).scalar_one_or_none()
            if subscription is not None and subscription.library_id != plan.target_library_id:
                subscription.library_id = plan.target_library_id
                subscription.updated_at = utcnow()
                await session.commit()
                summary.subscription_moved = True

    from movieclaw_api.services.media_server_notify import notify_media_server_refresh

    try:
        await notify_media_server_refresh()
    except Exception:  # noqa: BLE001 -- 下游刷新失败不该影响转移结论
        logger.warning("转移完成后通知媒体服务器刷新失败（不影响本地库存）", exc_info=True)

    logger.info(
        "条目「%s」已从库 #%s 转移到「%s」：搬运 %d 个路径、随迁 %d 条台账（约 %.1f GB），"
        "清理空目录 %d，问题 %d",
        summary.title,
        summary.source_library_id,
        summary.target_library_name,
        len(summary.moved_paths),
        summary.files_relocated,
        summary.bytes_moved / 1024**3,
        summary.removed_dirs,
        len(summary.errors),
    )


class _MoveError(Exception):
    """单个单元搬运失败。message 是完整中文句子，直接进 errors。"""


def _move(src: Path, dst: Path, cross_device: bool) -> None:
    """把一个文件或目录搬到新位置（同步，放线程池）。

    同盘走 ``os.rename``（瞬时、保留硬链接与 inode）；跨盘 rename 会以
    EXDEV 失败，退化为"完整复制 → 复制成功才删源"。复制中途失败会清掉
    半成品目标再报错，绝不留下一个看起来像搬完了的残缺目录。

    ``cross_device`` 只是计划阶段的**预判**（用于文案），真正的分支仍以
    rename 的 errno 为准——预判失误不会导致错误行为。
    """
    if not src.exists():
        raise _MoveError(f"源路径已不在原位，跳过：{src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _MoveError(f"创建目标目录失败（{exc.strerror}）：{dst.parent}") from exc
    if dst.exists():
        raise _MoveError(f"目标路径已被占用，跳过以免覆盖：{dst}")
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise _MoveError(f"搬运失败（{exc.strerror}）：{src} → {dst}") from exc
    # 跨设备：复制成功才删源
    try:
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
        raise _MoveError(f"跨盘复制失败（{exc}）：{src} → {dst}") from exc
    try:
        shutil.rmtree(src) if src.is_dir() else src.unlink()
    except OSError as exc:
        raise _MoveError(
            f"已复制到目标，但删除源路径失败（{exc.strerror}）：{src}——"
            "请手工确认后删除，否则会占用双份空间"
        ) from exc
