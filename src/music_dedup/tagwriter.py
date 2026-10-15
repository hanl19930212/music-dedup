"""标签写回模块：将刮削的元数据写入文件，支持备份与回滚。"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import APIC, ID3, TALB, TCON, TIT2, TPE1, TDRC, TRCK
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture
from mutagen.wave import WAVE
from mutagen.dsf import DSF

from .cover import detect_image_mime, download_cover
from .scraper import ScrapedMetadata
from .utils import get_file_ext, logger


class TagWriter:
    """将刮削元数据写回音频文件。"""

    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        path: Path,
        meta: ScrapedMetadata,
        fetch_cover: bool = False,
        dry_run: bool = True,
    ) -> tuple[bool, str]:
        """将元数据写入文件。

        返回 (success, message)。
        dry_run=True 时只返回将执行的操作，不实际写入。
        """
        ext = get_file_ext(path)

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
            return True, f"[DRY-RUN] 将写入: {', '.join(actions)}"

        # 备份原文件
        backup_path = self._backup(path)
        if backup_path is None:
            return False, "备份失败，已跳过"

        try:
            self._write_tags(path, meta, fetch_cover)
            logger.info(f"标签写入成功: {path.name}")
            return True, "写入成功"
        except Exception as e:
            # 回滚
            self._rollback(path, backup_path)
            logger.error(f"标签写入失败，已回滚 [{path.name}]: {e}")
            return False, f"写入失败: {e}"

    def _backup(self, path: Path) -> Optional[Path]:
        """备份原文件到备份目录。"""
        import hashlib

        file_hash = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]
        backup_path = self.backup_dir / f"{file_hash}{path.suffix}"
        try:
            shutil.copy2(path, backup_path)
            return backup_path
        except Exception as e:
            logger.error(f"备份失败 [{path.name}]: {e}")
            return None

    def _rollback(self, path: Path, backup_path: Path) -> None:
        """从备份恢复原文件。"""
        try:
            shutil.copy2(backup_path, path)
            backup_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"回滚失败 [{path.name}]: {e}，备份保留在: {backup_path}")

    def _write_tags(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        """实际写入标签（按格式分发）。"""
        ext = get_file_ext(path)

        if ext == "mp3":
            self._write_mp3(path, meta, fetch_cover)
        elif ext == "flac":
            self._write_flac(path, meta, fetch_cover)
        elif ext == "wav":
            self._write_wav(path, meta, fetch_cover)
        elif ext == "dsf":
            self._write_dsf(path, meta, fetch_cover)
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
            if cover_data:
                mime = detect_image_mime(cover_data)
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
            if cover_data:
                mime = detect_image_mime(cover_data)
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
            if cover_data:
                mime = detect_image_mime(cover_data)
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3, mime=mime, type=3, desc="Cover", data=cover_data
                )

        audio.save()

    def _write_dsf(self, path: Path, meta: ScrapedMetadata, fetch_cover: bool) -> None:
        audio = DSF(str(path))
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
            if cover_data:
                mime = detect_image_mime(cover_data)
                tags.delall("APIC")
                tags["APIC"] = APIC(
                    encoding=3, mime=mime, type=3, desc="Cover", data=cover_data
                )

        audio.save()
