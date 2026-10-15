"""配置加载模块。

优先级：CLI 参数 > 环境变量 > 配置文件 > 默认值。
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

import tomli_w


SUPPORTED_EXTENSIONS = ("dsf", "wav", "flac", "mp3")

# 保留优先级：数值越小优先级越高
FORMAT_PRIORITY: dict[str, int] = {
    "dsf": 0,
    "wav": 1,
    "flac": 2,
    "mp3": 3,
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
    min_similarity: float = 0.95
    name_fallback: bool = True


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


def load_config() -> Config:
    """加载配置，文件不存在时返回默认配置。"""
    cfg = Config()
    path = _config_path()
    if path.exists() and tomllib is not None:
        with path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
        _apply_dict(cfg, data)
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

    if "cache_file" in data:
        cfg.cache_file = Path(data["cache_file"])
    if "log_dir" in data:
        cfg.log_dir = Path(data["log_dir"])
    if "backup_dir" in data:
        cfg.backup_dir = Path(data["backup_dir"])


def save_default_config() -> Path:
    """生成默认配置文件（仅当不存在时）。

    配置文件带注释，方便用户理解各字段含义。
    """
    path = _config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    content = '''# music-dedup 配置文件
# 扫描相关
[scan]
# 支持的音频格式
extensions = ["dsf", "wav", "flac", "mp3"]
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
min_similarity = 0.95   # 指纹相似度阈值
name_fallback = true    # 指纹失败时是否用文件名兜底

# 元数据刮削
[scrape]
acoustid_api_key = ""   # 在 https://acoustid.org/api-key 免费申请
musicbrainz_user_agent = "music-dedup/0.1 (https://github.com/music-dedup)"
fetch_cover = true
cover_size = 600
rate_limit_delay = 1.0  # MusicBrainz 请求间隔（秒）

# 缓存与日志
cache_file = "''' + str(DEFAULT_CACHE_FILE).replace("\\", "\\\\") + '''"
log_dir = "''' + str(DEFAULT_LOG_DIR).replace("\\", "\\\\") + '''"
backup_dir = "''' + str(DEFAULT_BACKUP_DIR).replace("\\", "\\\\") + '''"
'''
    path.write_text(content, encoding="utf-8")
    return path
