"""CLI 入口。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .action import ActionResult, apply_dedup
from .config import (
    DEFAULT_CACHE_FILE,
    Config,
    load_config,
    save_default_config,
)
from .db import CacheDB
from .dedup import DuplicateGroup, TrackItem, group_duplicates
from .fingerprint import check_fpcalc, ensure_tool_on_path, extract_fingerprints
from .metadata import read_metadata
from .report import generate_report
from .scanner import AudioFile, scan_directories
from .scraper import Scraper
from .tagwriter import TagWriter
from .utils import human_size, logger, setup_logging

app = typer.Typer(
    name="music-dedup",
    help="Windows 音乐重复文件检测与元数据刮削工具",
    no_args_is_help=True,
)
console = Console()


def _serialize_groups(groups: list[DuplicateGroup]) -> list[dict]:
    """将分组序列化为 JSON，供 apply 命令读取。"""
    data = []
    for group in groups:
        data.append({
            "key": group.key,
            "match_type": group.match_type,
            "keep_index": group.keep_index,
            "items": [
                {
                    "path": str(item.path),
                    "size": item.size,
                    "ext": item.ext,
                }
                for item in group.items
            ],
        })
    return data


def _deserialize_groups(data: list[dict]) -> list[DuplicateGroup]:
    """从 JSON 反序列化分组（仅含路径/大小，用于 apply）。"""
    from .scanner import AudioFile as _AF

    groups = []
    for g in data:
        items = []
        for it in g["items"]:
            path = Path(it["path"])
            items.append(
                TrackItem(
                    audio_file=_AF(path=path, size=it["size"], mtime=0),
                    fingerprint=None,
                    duration=None,
                    metadata=type("M", (), {"path": path, "title": None, "artist": None})(),
                )
            )
        group = DuplicateGroup(
            key=g["key"],
            match_type=g["match_type"],
            items=items,
            keep_index=g["keep_index"],
        )
        groups.append(group)
    return groups


@app.command()
def scan(
    directories: list[str] = typer.Argument(
        None, help="要扫描的目录（可多个，支持 UNC 路径如 \\\\server\\share）"
    ),
    report: Path = typer.Option(
        Path("report.html"), "--report", "-r", help="HTML 报告输出路径"
    ),
    apply: bool = typer.Option(False, "--apply", help="扫描后直接执行去重（移到回收站）"),
    scrape: bool = typer.Option(False, "--scrape", help="扫描时同时刮削元数据"),
    log_level: str = typer.Option("INFO", "--log-level", help="日志级别"),
):
    """扫描目录，检测重复音乐文件并生成 HTML 报告。

    支持本地目录和 UNC 网络共享路径。若不指定目录，则使用配置文件中的 scan_dirs。
    """
    config = load_config()
    setup_logging(config.log_dir, getattr(logging, log_level.upper(), logging.INFO))

    # 合并命令行目录 + 配置文件目录
    dirs: list[str] = list(directories) if directories else []
    dirs.extend(config.scan.scan_dirs)

    if not dirs:
        console.print("[red]错误: 未指定扫描目录，且配置文件中未设置 scan_dirs[/]")
        console.print("[dim]用法: music-dedup scan D:\\Music \\\\192.168.1.100\\share[/]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]扫描目录 ({len(dirs)} 个):[/]")
    for d in dirs:
        console.print(f"  {d}")

    # 0. 前置检测外部工具（fpcalc 缺失会导致指纹全部降级为文件名匹配，必须提前拦截）
    ensure_tool_on_path(config.fingerprint.ffmpeg_path)
    ok, info = check_fpcalc(config.fingerprint.fpcalc_path)
    if not ok:
        console.print(f"[red]错误: fpcalc 不可用 - {info}[/]")
        console.print("[dim]指纹去重依赖 fpcalc，请安装后再试[/]")
        console.print("[dim]下载地址: https://acoustid.org/chromaprint[/]")
        console.print("[dim]或在配置文件中设置 fingerprint.fpcalc_path[/]")
        raise typer.Exit(1)
    console.print(f"[green]fpcalc 已就绪:[/] {info}")

    # 1. 扫描文件
    from pathlib import Path as _Path

    roots = [_Path(d) for d in dirs]
    files = scan_directories(roots, config.scan)
    console.print(f"[green]共找到 {len(files)} 个音频文件[/]")

    if not files:
        console.print("[yellow]未找到支持的音频文件，退出。[/]")
        return

    # 2. 提取指纹
    db = CacheDB(config.cache_file)
    try:
        fp_results = extract_fingerprints(files, config.fingerprint, db)
    finally:
        db.close()

    # 3. 读取元数据
    console.print("[cyan]读取元数据...[/]")
    items: list[TrackItem] = []
    for fpr in fp_results:
        meta = read_metadata(fpr.file.path)
        items.append(
            TrackItem(
                audio_file=fpr.file,
                fingerprint=fpr.fingerprint,
                duration=fpr.duration,
                metadata=meta,
            )
        )

    # 4. 刮削元数据（可选）
    if scrape:
        console.print("[cyan]刮削元数据...[/]")
        db2 = CacheDB(config.cache_file)
        try:
            scraper = Scraper(config.scrape, db2)
            scraped = scraper.scrape(fp_results)
        finally:
            db2.close()
        for item in items:
            if item.fingerprint and item.fingerprint in scraped:
                item.scraped = scraped[item.fingerprint]
        console.print(f"[green]刮削完成，命中 {len(scraped)} 个[/]")

    # 5. 分组
    groups = group_duplicates(items, config.dedup)
    console.print(f"[green]发现 {len(groups)} 组重复文件[/]")

    if not groups:
        console.print("[yellow]未发现重复文件。[/]")
        return

    # 6. 生成报告
    report_path = report.resolve()
    generate_report(groups, len(files), report_path)
    console.print(f"[bold green]报告已生成:[/] {report_path}")

    # 保存 JSON 数据供 apply 使用
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(_serialize_groups(groups), ensure_ascii=False, indent=2), encoding="utf-8")

    # 7. 执行去重
    if apply:
        console.print("\n[bold yellow]执行去重（移到回收站）...[/]")
        results = apply_dedup(groups, dry_run=False)
        _print_action_summary(results)
    else:
        console.print(
            f"\n[dim]确认报告无误后执行:[/] music-dedup apply \"{json_path}\""
        )


@app.command()
def apply(
    report_json: Path = typer.Argument(..., exists=True, help="scan 生成的 JSON 数据文件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预览，不实际移动"),
    include_name_matches: bool = typer.Option(
        False, "--include-name-matches",
        help="同时删除仅凭文件名匹配的组（默认跳过，避免误删）",
    ),
):
    """根据扫描结果执行去重，将重复文件移到回收站。"""
    config = load_config()
    setup_logging(config.log_dir)

    data = json.loads(report_json.read_text(encoding="utf-8"))
    groups = _deserialize_groups(data)

    if not groups:
        console.print("[yellow]无重复文件可处理。[/]")
        return

    name_groups = [g for g in groups if g.match_type == "name"]
    effective = [
        g for g in groups if include_name_matches or g.match_type != "name"
    ]
    total_duplicates = sum(len(g.duplicates) for g in effective)
    total_size = sum(g.wasted_size for g in effective)

    console.print(f"[bold]将处理 {total_duplicates} 个重复文件，可释放 {human_size(total_size)}[/]")
    if name_groups and not include_name_matches:
        console.print(
            f"[yellow]另有 {len(name_groups)} 组仅凭文件名匹配，已默认跳过"
            f"（加 --include-name-matches 可一并处理）[/]"
        )

    if dry_run:
        console.print("[yellow]DRY-RUN 模式，不会实际移动文件[/]")

    results = apply_dedup(groups, dry_run=dry_run, include_name_matches=include_name_matches)
    _print_action_summary(results)


@app.command()
def scrape(
    directories: list[str] = typer.Argument(
        None, help="要刮削的目录（可多个，支持 UNC 路径）"
    ),
    write_tags: bool = typer.Option(False, "--write-tags", help="将刮削结果写回文件标签"),
    fetch_cover: bool = typer.Option(False, "--fetch-cover", help="下载并嵌入封面"),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="预览或执行（默认预览）"),
):
    """刮削音频文件的元数据（标题/艺术家/专辑/封面等）。"""
    config = load_config()
    setup_logging(config.log_dir)

    if not config.scrape.acoustid_api_key:
        console.print("[red]错误: 未配置 AcoustID API key[/]")
        console.print(f"请在配置文件中设置: {config.cache_file.parent / 'config.toml'}")
        raise typer.Exit(1)

    # 合并命令行 + 配置目录
    dirs: list[str] = list(directories) if directories else []
    dirs.extend(config.scan.scan_dirs)
    if not dirs:
        console.print("[red]错误: 未指定目录，且配置文件中未设置 scan_dirs[/]")
        raise typer.Exit(1)

    from pathlib import Path as _Path

    ensure_tool_on_path(config.fingerprint.ffmpeg_path)
    ok, info = check_fpcalc(config.fingerprint.fpcalc_path)
    if not ok:
        console.print(f"[red]错误: fpcalc 不可用 - {info}[/]")
        console.print("[dim]刮削依赖 fpcalc 提取指纹，请安装后再试[/]")
        raise typer.Exit(1)
    console.print(f"[green]fpcalc 已就绪:[/] {info}")

    files = scan_directories([_Path(d) for d in dirs], config.scan)
    console.print(f"[green]找到 {len(files)} 个音频文件[/]")

    db = CacheDB(config.cache_file)
    try:
        fp_results = extract_fingerprints(files, config.fingerprint, db)
        scraper = Scraper(config.scrape, db)
        scraped = scraper.scrape(fp_results)
    finally:
        db.close()

    console.print(f"[green]刮削完成，命中 {len(scraped)} 个[/]")

    if write_tags:
        writer = TagWriter(config.backup_dir)
        success = 0
        failed = 0
        for fpr in fp_results:
            if not fpr.fingerprint or fpr.fingerprint not in scraped:
                continue
            ok, msg = writer.write(
                fpr.file.path, scraped[fpr.fingerprint],
                fetch_cover=fetch_cover, dry_run=dry_run,
            )
            if ok:
                success += 1
            else:
                failed += 1
            logger.info(f"[{fpr.file.name}] {msg}")

        console.print(f"[green]写入成功: {success}[/]")
        if failed:
            console.print(f"[red]写入失败: {failed}[/]")


cache_app = typer.Typer(name="cache", help="指纹缓存管理。")


@cache_app.command("stats")
def cache_stats():
    """显示缓存统计信息。"""
    config = load_config()
    db = CacheDB(config.cache_file)
    stats = db.stats()
    db.close()

    table = Table(title="缓存统计")
    table.add_column("项目")
    table.add_column("数量")
    table.add_row("指纹缓存", str(stats["fingerprints"]))
    table.add_row("元数据缓存", str(stats["metadata"]))
    console.print(table)


@cache_app.command("clear")
def cache_clear():
    """清除所有缓存。"""
    config = load_config()
    db = CacheDB(config.cache_file)
    db.clear()
    db.close()
    console.print("[green]缓存已清除[/]")


app.add_typer(cache_app)


@app.command()
def init_config():
    """生成默认配置文件。"""
    path = save_default_config()
    console.print(f"[green]配置文件已生成:[/] {path}")


def _print_action_summary(results: list[ActionResult]) -> None:
    """打印操作摘要。"""
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    freed = sum(r.freed_size for r in success)

    table = Table(title="去重结果")
    table.add_column("结果", justify="center")
    table.add_column("数量")
    table.add_column("释放空间")
    table.add_row("成功", str(len(success)), human_size(freed), style="green")
    table.add_row("失败", str(len(failed)), "", style="red")
    console.print(table)

    if failed:
        console.print("\n[red]失败的文件:[/]")
        for r in failed:
            console.print(f"  {r.path}: {r.message}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
