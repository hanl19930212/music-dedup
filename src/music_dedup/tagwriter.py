"""标签写回模块：将刮削的元数据写入文件，支持备份与回滚。"""

from __future__ import annotations

import os
import shutil
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from .cover import detect_image_mime, download_cover
from .scraper import ScrapedMetadata
from .utils import get_file_ext, logger

# 支持写回标签的扩展名，与 _write_tags 的分发表保持一致
SUPPORTED_TAG_FORMATS = frozenset(
    {"mp3", "flac", "wav", "dsf", "dff", "aif", "aiff", "wma"}
)


class TagWriter:
    """将刮削元数据写回音频文件。"""

    def __init__(self, backup_dir: Path) -> None:
        # 不在这里创建目录：dry-run 不应留下任何文件系统副作用，
        # 备份目录只在真正要写标签时由 _backup 创建。
        self.backup_dir = backup_dir

    def write(
        self,
        path: Path,
        meta: ScrapedMetadata,
        fetch_cover: bool = False,
        dry_run: bool = True,
    ) -> tuple[bool, str]:
        """将元数据写入文件。

        返回 (success, message)。
        dry_run=True 时只做只读校验并返回将执行的操作，不实际写入。
        """
        if dry_run:
            actions = []
            if meta.title:
                actions.append(f"title={meta.title}")
            if meta.artist:
                actions.append(f"artist={meta.artist}")
            if meta.album:
                actions.append(f"album={meta.album}")
            if meta.genre:
                actions.append(f"genre={meta.genre}")
            if meta.year:
                actions.append(f"year={meta.year}")
            if fetch_cover and meta.cover_url:
                actions.append("cover")
            plan = f"[DRY-RUN] 将写入: {', '.join(actions)}"

            # 只读校验：让预览结果与实际执行一致，不创建/修改任何文件
            issues: list[str] = []
            if not path.exists():
                issues.append("文件不存在")
            ext = get_file_ext(path)
            if ext not in SUPPORTED_TAG_FORMATS:
                issues.append(f"不支持写入标签的格式: {ext}")
            if self.backup_dir.exists() and not os.access(self.backup_dir, os.W_OK):
                issues.append(f"备份目录不可写: {self.backup_dir}")
            if issues:
                return False, f"{plan}；但无法执行: {'; '.join(issues)}"
            return True, plan

        # 备份原文件
        backup_path = self._backup(path)
        if backup_path is None:
            return False, "备份失败，已跳过"

        try:
            self._write_tags(path, meta, fetch_cover)
            logger.info(f"标签写入成功: {path.name}")
            return True, "写入成功"
        except Exception as e:
            restored = self._rollback(path, backup_path)
            msg = f"写入失败: {e}"
            if not restored:
                msg += f"；【回滚失败】原文件可能已损坏，备份保留在: {backup_path}"
                logger.critical(msg)
            else:
                logger.error(f"标签写入失败，已回滚 [{path.name}]: {e}")
            return False, msg

    def _backup(self, path: Path) -> Optional[Path]:
        """备份原文件到备份目录；带时间戳，避免二次写入覆盖最初的备份。"""
        import hashlib

        file_hash = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        backup_path = self.backup_dir / f"{file_hash}_{timestamp}{path.suffix}"
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
            return backup_path
        except Exception as e:
            logger.error(f"备份失败 [{path.name}]: {e}")
            return None

    def _rollback(self, path: Path, backup_path: Path) -> bool:
        """从备份恢复原文件，返回是否成功；失败时保留备份供人工处理。"""
        try:
            shutil.copy2(backup_path, path)
            backup_path.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.error(f"回滚失败 [{path.name}]: {e}，备份保留在: {backup_path}")
            return False

    def _write_tags(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        """实际写入标签（按格式分发）。"""
        ext = get_file_ext(path)

        if ext == "mp3":
            self._write_mp3(path, meta, fetch_cover)
        elif ext == "flac":
            self._write_flac(path, meta, fetch_cover)
        elif ext == "wav":
            self._write_wav(path, meta, fetch_cover)
        elif ext in ("dsf", "dff"):
            self._write_id3_container(path, meta, fetch_cover)
        elif ext in ("aif", "aiff"):
            self._write_aiff(path, meta, fetch_cover)
        elif ext == "wma":
            self._write_wma(path, meta, fetch_cover)
        else:
            raise ValueError(f"不支持的格式: {ext}")

    def _write_mp3(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = MP3(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if meta.title:
            tags["TIT2"] = TIT2(encoding=3, text=meta.title)
        if meta.artist:
            tags["TPE1"] = TPE1(encoding=3, text=meta.artist)
        if meta.album:
            tags["TALB"] = TALB(encoding=3, text=meta.album)
        if meta.genre:
            tags["TCON"] = TCON(encoding=3, text=meta.genre)
        if meta.year:
            tags["TDRC"] = TDRC(encoding=3, text=meta.year)

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3,
                    mime=mime,
                    type=3,
                    desc="Cover",
                    data=cover_data,
                )

        audio.save()

    def _write_flac(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = FLAC(str(path))
        if meta.title:
            audio["title"] = meta.title
        if meta.artist:
            audio["artist"] = meta.artist
        if meta.album:
            audio["album"] = meta.album
        if meta.genre:
            audio["genre"] = meta.genre
        if meta.year:
            audio["date"] = meta.year

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                audio.clear_pictures()
                pic = Picture()
                pic.data = cover_data
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                audio.add_picture(pic)

        audio.save()

    def _write_wav(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = WAVE(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if meta.title:
            tags["TIT2"] = TIT2(encoding=3, text=meta.title)
        if meta.artist:
            tags["TPE1"] = TPE1(encoding=3, text=meta.artist)
        if meta.album:
            tags["TALB"] = TALB(encoding=3, text=meta.album)
        if meta.genre:
            tags["TCON"] = TCON(encoding=3, text=meta.genre)
        if meta.year:
            tags["TDRC"] = TDRC(encoding=3, text=meta.year)

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3, mime=mime, type=3, desc="Cover", data=cover_data
                )

        audio.save()

    def _write_id3_container(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        """写 DSF / DFF（容器内嵌 ID3 标签）。"""
        from mutagen.dsdiff import DSDIFF

        audio = DSF(str(path)) if get_file_ext(path) == "dsf" else DSDIFF(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if meta.title:
            tags["TIT2"] = TIT2(encoding=3, text=meta.title)
        if meta.artist:
            tags["TPE1"] = TPE1(encoding=3, text=meta.artist)
        if meta.album:
            tags["TALB"] = TALB(encoding=3, text=meta.album)
        if meta.genre:
            tags["TCON"] = TCON(encoding=3, text=meta.genre)
        if meta.year:
            tags["TDRC"] = TDRC(encoding=3, text=meta.year)

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3, mime=mime, type=3, desc="Cover", data=cover_data
                )

        audio.save()

    def _write_aiff(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = AIFF(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if meta.title:
            tags["TIT2"] = TIT2(encoding=3, text=meta.title)
        if meta.artist:
            tags["TPE1"] = TPE1(encoding=3, text=meta.artist)
        if meta.album:
            tags["TALB"] = TALB(encoding=3, text=meta.album)
        if meta.genre:
            tags["TCON"] = TCON(encoding=3, text=meta.genre)
        if meta.year:
            tags["TDRC"] = TDRC(encoding=3, text=meta.year)

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3, mime=mime, type=3, desc="Cover", data=cover_data
                )

        audio.save()

    def _write_wma(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = ASF(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if meta.title:
            tags["Title"] = meta.title
        if meta.artist:
            tags["Author"] = meta.artist
        if meta.album:
            tags["WM/AlbumTitle"] = meta.album
        if meta.genre:
            tags["WM/Genre"] = meta.genre
        if meta.year:
            tags["WM/Year"] = meta.year

        if fetch_cover and meta.cover_url:
            cover_data = download_cover(meta.cover_url)
            mime = detect_image_mime(cover_data) if cover_data else None
            if mime:
                for key in [key for key in tags if str(key).startswith("WM/Picture")]:
                    del tags[key]
                tags["WM/Picture"] = _asf_picture(cover_data, mime)

        audio.save()


def _asf_picture(data: bytes, mime: str, description: str = "Cover") -> bytes:
    """按 ASF 规范拼出 WM/Picture 的二进制负载（mutagen 没有现成的类）。

    结构：图片类型(1) + 数据长度(4) + MIME(UTF-16LE) + 描述(UTF-16LE) + 图片数据
    """
    mime_bytes = mime.encode("utf-16-le") + b"\x00\x00"
    desc_bytes = description.encode("utf-16-le") + b"\x00\x00"
    return b"\x03" + struct.pack("<I", len(data)) + mime_bytes + desc_bytes + data
