"""CLI 入口。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .action import ActionResult, apply_dedup, resolve_quarantine_dir
from .config import Config, load_config, save_default_config
from .contract import (
    ReportValidationError,
    deserialize_report,
    serialize_report,
    verify_file_state,
)
from .db import CacheDB
from .dedup import DuplicateGroup, TrackItem, group_duplicates
from .fingerprint import (
    check_fpcalc,
    ensure_tools_on_path,
    extract_acoustid_fingerprint,
    extract_fingerprints,
)
from .metadata import read_metadata
from .report import generate_report
from .scanner import scan_directories
from .scraper import Scraper
from .tagwriter import TagWriter
from .utils import human_size, logger, setup_logging

app = typer.Typer(
    name="music-dedup",
    help="Windows 音乐重复文件检测与元数据刮削工具",
    no_args_is_help=True,
)
console = Console()


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
    dry_run: bool = typer.Option(False, "--dry-run", help="仅预览，不实际移动"),
    include_name_matches: bool = typer.Option(
        False, "--include-name-matches",
        help="同时删除仅凭文件名匹配的组（默认跳过，避免误删）",
    ),
    include_similar_matches: bool = typer.Option(
        False, "--include-similar-matches",
        help="同时删除仅凭指纹相似匹配的组（默认跳过，避免误删重制版）",
    ),
):
    """扫描目录，检测重复音乐文件并生成 HTML 报告。

    支持本地目录和 UNC 网络共享路径。若不指定目录，则使用配置文件中的 scan_dirs。
    """
    config = load_config()
    setup_logging(config.log_dir, getattr(logging, log_level.upper(), logging.INFO))

    # 命令行目录优先；未指定时才回退到配置文件中的 scan_dirs
    dirs: list[str] = (
        [d for d in directories if d and d.strip()]
        if directories
        else [d for d in config.scan.scan_dirs if d and d.strip()]
    )

    if not dirs:
        console.print("[red]错误: 未指定扫描目录，且配置文件中未设置 scan_dirs[/]")
        console.print("[dim]用法: music-dedup scan D:\\Music \\\\192.168.1.100\\share[/]")
        raise typer.Exit(1)

    # 隔离目录可能就落在扫描根下，二次扫描必须排除它，否则隔离文件会被重新计入（Q-09）
    config.scan.exclude_dirs = list(
        dict.fromkeys([*config.scan.exclude_dirs, config.dedup.quarantine_dir])
    )

    console.print(f"[bold cyan]扫描目录 ({len(dirs)} 个):[/]")
    for d in dirs:
        console.print(f"  {d}")

    # 0. 前置检测外部工具（fpcalc 缺失会导致指纹全部降级为文件名匹配，必须提前拦截）
    ensure_tools_on_path(config.fingerprint.ffmpeg_path, config.fingerprint.fpcalc_path)
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

    # 2b. 失败率防线：大面积失败时若继续，会全部退化成文件名兜底组（Q-05）
    failed = sum(1 for result in fp_results if not result.fingerprint)
    if fp_results and failed / len(fp_results) > 0.8:
        console.print(
            f"[red]错误: 指纹提取失败率过高（{failed}/{len(fp_results)}），已中止扫描[/]"
        )
        console.print(
            "[dim]常见原因：缺少 ffmpeg 解码器、fpcalc 不支持该格式（如 dff）、网络共享读取失败[/]"
        )
        console.print(
            "[dim]为避免全部降级为文件名匹配造成误删，本次不生成报告，请修复后重试[/]"
        )
        raise typer.Exit(1)

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
            scraper = Scraper(
                config.scrape,
                db2,
                acoustid_fingerprint=
                lambda path: extract_acoustid_fingerprint(path, config.fingerprint)
            )
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

    # 6. 生成报告（apply 命令读的是同目录下的 JSON）
    report_path = report.resolve()
    json_path = report_path.with_suffix(".json")
    generate_report(groups, len(files), report_path, json_path=json_path)
    console.print(f"[bold green]报告已生成:[/] {report_path}")

    # 保存 JSON 数据供 apply 使用（带版本契约，apply 会逐项校验）
    json_path.write_text(
        json.dumps(serialize_report(groups, roots), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 7. 执行去重（与 apply 命令共用同一入口，保证预览与跳过规则一致）
    if apply:
        console.print("\n[bold yellow]执行去重（移到回收站）...[/]")
        _execute_dedup(
            groups,
            config,
            dry_run=dry_run,
            include_name_matches=include_name_matches,
            include_similar_matches=include_similar_matches,
        )
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
    include_similar_matches: bool = typer.Option(
        False, "--include-similar-matches",
        help="同时删除仅凭指纹相似匹配的组（默认跳过，避免误删重制版）",
    ),
):
    """根据扫描结果执行去重，将重复文件移到回收站。"""
    config = load_config()
    setup_logging(config.log_dir)

    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        console.print(f"[red]报告 JSON 解析失败: {e}[/]")
        raise typer.Exit(1) from e
    except OSError as e:
        console.print(f"[red]报告读取失败: {e}[/]")
        raise typer.Exit(1) from e

    try:
        groups = deserialize_report(data)
    except ReportValidationError as e:
        console.print(f"[red]报告校验失败: {e}[/]")
        raise typer.Exit(1) from e

    # 报告生成后文件可能已被替换/移动，宁可拒绝也不误删（dry-run 同样校验）
    problems: list[str] = []
    for group in groups:
        problems.extend(verify_file_state(group))
    if problems:
        console.print("[red]报告与当前文件状态不一致，已中止（请重新执行 scan 生成报告）:[/]")
        for problem in problems:
            console.print(f"  {problem}")
        raise typer.Exit(1)

    if not groups:
        console.print("[yellow]无重复文件可处理。[/]")
        return

    _execute_dedup(
        groups,
        config,
        dry_run=dry_run,
        include_name_matches=include_name_matches,
        include_similar_matches=include_similar_matches,
    )


def _execute_dedup(
    groups: list[DuplicateGroup],
    config: Config,
    dry_run: bool,
    include_name_matches: bool,
    include_similar_matches: bool,
) -> None:
    """scan --apply 与 apply 共用的去重执行入口，避免两套跳过/统计规则漂移。"""
    name_groups = [g for g in groups if g.match_type == "name"]
    similar_groups = [g for g in groups if g.match_type == "fingerprint_similar"]
    effective = [
        g for g in groups
        if (include_name_matches or g.match_type != "name")
        and (include_similar_matches or g.match_type != "fingerprint_similar")
    ]
    total_duplicates = sum(len(g.duplicates) for g in effective)
    total_size = sum(g.wasted_size for g in effective)

    console.print(
        f"[bold]{'计划处理' if dry_run else '将处理'} {total_duplicates} 个重复文件，"
        f"可释放 {human_size(total_size)}[/]"
    )
    if name_groups and not include_name_matches:
        console.print(
            f"[yellow]另有 {len(name_groups)} 组仅凭文件名匹配，已默认跳过"
            f"（加 --include-name-matches 可一并处理）[/]"
        )
    if similar_groups and not include_similar_matches:
        console.print(
            f"[yellow]另有 {len(similar_groups)} 组仅凭指纹相似匹配，已默认跳过"
            f"（加 --include-similar-matches 可一并处理）[/]"
        )

    if dry_run:
        console.print("[yellow]DRY-RUN 模式，不会实际移动文件[/]")

    quarantine_dir = resolve_quarantine_dir(
        [item.path for group in groups for item in group.items],
        config.dedup.quarantine_dir,
    )
    results = apply_dedup(
        groups,
        dry_run=dry_run,
        include_name_matches=include_name_matches,
        include_similar_matches=include_similar_matches,
        quarantine_dir=quarantine_dir if config.dedup.fallback_to_quarantine else None,
    )
    _print_action_summary(results, dry_run=dry_run)


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

    # 命令行目录优先；未指定时才回退到配置文件中的 scan_dirs
    dirs: list[str] = (
        [d for d in directories if d and d.strip()]
        if directories
        else [d for d in config.scan.scan_dirs if d and d.strip()]
    )
    if not dirs:
        console.print("[red]错误: 未指定目录，且配置文件中未设置 scan_dirs[/]")
        raise typer.Exit(1)

    from pathlib import Path as _Path

    ensure_tools_on_path(config.fingerprint.ffmpeg_path, config.fingerprint.fpcalc_path)
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
        scraper = Scraper(
            config.scrape,
            db,
            acoustid_fingerprint=
            lambda path: extract_acoustid_fingerprint(path, config.fingerprint)
        )
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
                # 失败原因（尤其「回滚失败/文件可能已损坏」）必须上屏，不能只进日志
                console.print(f"[red]写入失败 [{escape(fpr.file.name)}]: {escape(msg)}[/]")
            logger.info("[%s] %s", fpr.file.name, msg)

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


def _print_action_summary(results: list[ActionResult], dry_run: bool = False) -> None:
    """打印操作摘要；dry-run 下用「计划处理」措辞，避免被误读为已删除。"""
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    quarantined = [r for r in success if r.quarantined]
    freed = sum(r.freed_size for r in success)
    quarantined_size = sum(r.quarantined_size for r in quarantined)

    table = Table(title="去重结果（DRY-RUN 预览）" if dry_run else "去重结果")
    table.add_column("结果", justify="center")
    table.add_column("数量")
    table.add_column("预计释放空间" if dry_run else "释放空间")
    table.add_row(
        "计划处理" if dry_run else "成功",
        str(len(success)),
        human_size(freed),
        style="green",
    )
    table.add_row("失败", str(len(failed)), "", style="red")
    if quarantined:
        # 隔离是同盘移动，并未释放空间；单独标出占用量，避免统计虚高
        table.add_row(
            "其中转入隔离目录",
            str(len(quarantined)),
            f"未释放 {human_size(quarantined_size)}",
            style="yellow",
        )
    console.print(table)

    if failed:
        console.print("\n[red]失败的文件:[/]")
        for r in failed:
            console.print(f"  {r.path}: {r.message}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
