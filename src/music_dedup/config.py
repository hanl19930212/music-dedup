"""配置加载模块。

优先级：CLI 参数 > 配置文件 > 默认值。
环境变量目前仅 MUSIC_DEDUP_CONFIG 生效，用于指定配置文件路径。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback
    tomllib = None  # type: ignore


from .utils import logger

SUPPORTED_EXTENSIONS = ("dsf", "dff", "wav", "flac", "aif", "aiff", "wma", "mp3")

# 保留优先级：数值越小优先级越高
FORMAT_PRIORITY: dict[str, int] = {
    "dff": 0,
    "dsf": 1,
    "wav": 2,
    "aif": 3,
    "aiff": 3,
    "flac": 4,
    "wma": 5,
    "mp3": 6,
}

DEFAULT_CONFIG_DIR = Path.home() / ".music-dedup"
DEFAULT_CACHE_FILE = DEFAULT_CONFIG_DIR / "cache.db"
DEFAULT_LOG_DIR = DEFAULT_CONFIG_DIR / "logs"
DEFAULT_BACKUP_DIR = DEFAULT_CONFIG_DIR / "backups"


@dataclass
class ScanConfig:
    extensions: list[str] = field(default_factory=lambda: list(SUPPORTED_EXTENSIONS))
    exclude_dirs: list[str] = field(
        default_factory=lambda: ["@eaDir", ".Trash", "System Volume Information", "$RECYCLE.BIN"]
    )
    scan_dirs: list[str] = field(default_factory=list)  # 预定义扫描目录（支持 UNC 路径）


@dataclass
class FingerprintConfig:
    duration: int = 120
    workers: int = 4
    fpcalc_path: str = "fpcalc"
    ffmpeg_path: str = ""  # ffmpeg 可执行文件（或所在目录）路径，fpcalc 依赖它解码


@dataclass
class DedupConfig:
    # 指纹相似阈值：实测同一录音的不同编码约 0.87~1.0，
    # 现场版与录音室版约 0.56，完全不同的歌约 0.55
    min_similarity: float = 0.83
    name_fallback: bool = True
    # 指纹相似比较允许的时长差（秒）。这只是候选粗筛：
    # 同一录音的不同发行版本常因前后静音不同差出十几秒，最终仍由相似度阈值把关
    fingerprint_duration_tolerance: float = 30.0
    # 回收站不可用时（例如 UNC 网络共享）的兜底隔离目录
    quarantine_dir: str = "_music_duplicates_quarantine"
    fallback_to_quarantine: bool = True


@dataclass
class ScrapeConfig:
    acoustid_api_key: str = ""
    musicbrainz_user_agent: str = "music-dedup/0.1 (https://github.com/music-dedup)"
    fetch_cover: bool = True
    cover_size: int = 600
    rate_limit_delay: float = 1.0
    write_tags: bool = False


@dataclass
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    cache_file: Path = DEFAULT_CACHE_FILE
    log_dir: Path = DEFAULT_LOG_DIR
    backup_dir: Path = DEFAULT_BACKUP_DIR


def _config_path() -> Path:
    env = os.environ.get("MUSIC_DEDUP_CONFIG")
    if env:
        return Path(env)
    return DEFAULT_CONFIG_DIR / "config.toml"


def _ensure_dirs(cfg: Config) -> None:
    cfg.cache_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)


def _validate(cfg: Config) -> None:
    """校验配置取值范围，避免非法值在运行期才以晦涩错误暴露。

    min_similarity 允许 1.0：这是 README 里「关闭相似匹配」的既定用法。
    """
    def _check(condition: bool, name: str, value: object) -> None:
        if not condition:
            raise ValueError(f"配置项 {name} 取值非法: {value!r}")

    _check(cfg.fingerprint.workers >= 1, "fingerprint.workers", cfg.fingerprint.workers)
    _check(cfg.fingerprint.duration > 0, "fingerprint.duration", cfg.fingerprint.duration)
    _check(
        0.0 <= cfg.dedup.min_similarity <= 1.0,
        "dedup.min_similarity",
        cfg.dedup.min_similarity,
    )
    _check(
        cfg.dedup.fingerprint_duration_tolerance >= 0,
        "dedup.fingerprint_duration_tolerance",
        cfg.dedup.fingerprint_duration_tolerance,
    )
    _check(
        cfg.scrape.rate_limit_delay >= 0,
        "scrape.rate_limit_delay",
        cfg.scrape.rate_limit_delay,
    )
    _check(cfg.scrape.cover_size > 0, "scrape.cover_size", cfg.scrape.cover_size)
    _check(
        bool(cfg.dedup.quarantine_dir.strip()),
        "dedup.quarantine_dir",
        cfg.dedup.quarantine_dir,
    )


def load_config() -> Config:
    """加载配置，文件不存在时返回默认配置。"""
    cfg = Config()
    path = _config_path()
    if path.exists() and tomllib is not None:
        with path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
        _apply_dict(cfg, data)
    _validate(cfg)
    _resolve_frozen_paths(cfg)
    _ensure_dirs(cfg)
    return cfg


def _resolve_frozen_paths(cfg: Config) -> None:
    """PyInstaller 打包运行时，将自带的 bin 目录注入 PATH 并设置工具路径。

    打包后 ffmpeg.exe / fpcalc.exe 位于解包目录 _MEIPASS/bin 下，
    若不处理，fpcalc 会因找不到工具而失败。
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    bin_dir = Path(base) / "bin"
    if not bin_dir.exists():
        return

    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")

    fpcalc = bin_dir / "fpcalc.exe"
    if fpcalc.exists() and cfg.fingerprint.fpcalc_path == "fpcalc":
        cfg.fingerprint.fpcalc_path = str(fpcalc)

    if not cfg.fingerprint.ffmpeg_path and (bin_dir / "ffmpeg.exe").exists():
        cfg.fingerprint.ffmpeg_path = str(bin_dir)


def _apply_dict(cfg: Config, data: dict[str, Any]) -> None:
    scan = data.get("scan", {})
    if "extensions" in scan:
        cfg.scan.extensions = [str(e).lower().lstrip(".") for e in scan["extensions"]]
    if "exclude_dirs" in scan:
        cfg.scan.exclude_dirs = list(scan["exclude_dirs"])
    if "scan_dirs" in scan:
        cfg.scan.scan_dirs = [str(d) for d in scan["scan_dirs"]]

    fp = data.get("fingerprint", {})
    if "duration" in fp:
        cfg.fingerprint.duration = int(fp["duration"])
    if "workers" in fp:
        cfg.fingerprint.workers = int(fp["workers"])
    if "fpcalc_path" in fp:
        cfg.fingerprint.fpcalc_path = str(fp["fpcalc_path"])
    if "ffmpeg_path" in fp:
        cfg.fingerprint.ffmpeg_path = str(fp["ffmpeg_path"])

    dd = data.get("dedup", {})
    if "min_similarity" in dd:
        cfg.dedup.min_similarity = float(dd["min_similarity"])
    if "name_fallback" in dd:
        cfg.dedup.name_fallback = bool(dd["name_fallback"])
    if "fingerprint_duration_tolerance" in dd:
        cfg.dedup.fingerprint_duration_tolerance = float(dd["fingerprint_duration_tolerance"])
    if "quarantine_dir" in dd:
        cfg.dedup.quarantine_dir = str(dd["quarantine_dir"])
    if "fallback_to_quarantine" in dd:
        cfg.dedup.fallback_to_quarantine = bool(dd["fallback_to_quarantine"])

    sc = data.get("scrape", {})
    if "acoustid_api_key" in sc:
        cfg.scrape.acoustid_api_key = str(sc["acoustid_api_key"])
    if "musicbrainz_user_agent" in sc:
        cfg.scrape.musicbrainz_user_agent = str(sc["musicbrainz_user_agent"])
    if "fetch_cover" in sc:
        cfg.scrape.fetch_cover = bool(sc["fetch_cover"])
    if "cover_size" in sc:
        cfg.scrape.cover_size = int(sc["cover_size"])
    if "rate_limit_delay" in sc:
        cfg.scrape.rate_limit_delay = float(sc["rate_limit_delay"])

    # 缓存与日志路径必须写在所有 [段] 之前才是顶层键。
    # 历史上（以及 README 的示例里）它们被放在 [scrape] 段末尾，
    # TOML 会把它们解析成 scrape 的子键而读不到，这里做兼容。
    for key in ("cache_file", "log_dir", "backup_dir"):
        value = _top_level_path(data, key)
        if value is None:
            continue
        expanded = Path(value).expanduser()
        if key == "cache_file":
            cfg.cache_file = expanded
        elif key == "log_dir":
            cfg.log_dir = expanded
        else:
            cfg.backup_dir = expanded


def _top_level_path(data: dict[str, Any], key: str) -> str | None:
    """读取顶层路径配置；兼容被误放进某个 [段] 里的情况。"""
    if key in data:
        return str(data[key])
    for section, table in data.items():
        if isinstance(table, dict) and key in table:
            logger.warning(
                f"配置项 {key} 写在了 [{section}] 段内，应移到文件顶层（所有 [段] 之前），已按兼容方式读取"
            )
            return str(table[key])
    return None


def save_default_config() -> Path:
    """生成默认配置文件（仅当不存在时）。

    配置文件带注释，方便用户理解各字段含义。
    """
    path = _config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    content = '''# music-dedup 配置文件

# 缓存与日志（必须写在所有 [段] 之前，否则会被当成段内的键而读不到）
cache_file = "''' + str(DEFAULT_CACHE_FILE).replace("\\", "\\\\") + '''"
log_dir = "''' + str(DEFAULT_LOG_DIR).replace("\\", "\\\\") + '''"
backup_dir = "''' + str(DEFAULT_BACKUP_DIR).replace("\\", "\\\\") + '''"

# 扫描相关
[scan]
# 支持的音频格式
extensions = ["dsf", "dff", "wav", "flac", "aif", "aiff", "wma", "mp3"]
# 扫描时跳过的目录名
exclude_dirs = ["@eaDir", ".Trash", "System Volume Information", "$RECYCLE.BIN"]
# 预定义扫描目录，可包含本地路径和 UNC 网络共享路径
# 例如: ["D:\\\\Music", "\\\\\\\\192.168.1.100\\\\share\\\\music"]
scan_dirs = []

# 音频指纹
[fingerprint]
duration = 120                  # 指纹采样时长（秒）
workers = 4                     # 并发进程数
fpcalc_path = "fpcalc"          # fpcalc 可执行文件路径（或完整路径）
ffmpeg_path = ""                # ffmpeg 路径（或所在目录）；fpcalc 依赖 ffmpeg 解码，留空则用系统 PATH

# 去重
[dedup]
min_similarity = 0.83   # 指纹相似度阈值
name_fallback = true    # 指纹失败时是否用文件名兜底
fingerprint_duration_tolerance = 30.0  # 指纹相似比较允许的时长差（秒），仅作候选粗筛
quarantine_dir = "_music_duplicates_quarantine"  # 回收站不可用时的兜底隔离目录
fallback_to_quarantine = true   # 移入回收站失败时改用隔离目录

# 元数据刮削
[scrape]
acoustid_api_key = ""   # 在 https://acoustid.org/api-key 免费申请
musicbrainz_user_agent = "music-dedup/0.1 (https://github.com/music-dedup)"
fetch_cover = true
cover_size = 600
rate_limit_delay = 1.0  # MusicBrainz 请求间隔（秒）
'''
    path.write_text(content, encoding="utf-8")
    return path
