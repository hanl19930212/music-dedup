"""元数据刮削模块：AcoustID + MusicBrainz + iTunes。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .config import ScrapeConfig
from .db import CacheDB
from .fingerprint import FingerprintResult
from .utils import logger


@dataclass
class ScrapedMetadata:
    """刮削到的元数据。"""

    fingerprint: str
    recording_id: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[str] = None
    cover_url: Optional[str] = None
    confidence: float = 0.0
    needs_confirmation: bool = False


ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/{rid}"
ITUNES_URL = "https://itunes.apple.com/search"


class Scraper:
    """元数据刮削器。"""

    def __init__(self, config: ScrapeConfig, db: CacheDB) -> None:
        self.config = config
        self.db = db
        self._session = requests.Session()
        self._last_mb_request = 0.0

    def scrape(self, fp_results: list[FingerprintResult]) -> dict[str, ScrapedMetadata]:
        """批量刮削元数据，返回 fingerprint -> ScrapedMetadata 映射。"""
        results: dict[str, ScrapedMetadata] = {}

        if not self.config.acoustid_api_key:
            logger.warning("未配置 AcoustID API key，跳过刮削")
            return results

        for fp_result in fp_results:
            if not fp_result.fingerprint:
                continue

            # 查缓存
            cached = self.db.get_metadata(fp_result.fingerprint)
            if cached and cached.get("recording_id"):
                results[fp_result.fingerprint] = ScrapedMetadata(
                    fingerprint=fp_result.fingerprint,
                    recording_id=cached.get("recording_id"),
                    title=cached.get("title"),
                    artist=cached.get("artist"),
                    album=cached.get("album"),
                    genre=cached.get("genre"),
                    year=cached.get("year"),
                    cover_url=cached.get("cover_url"),
                    confidence=cached.get("confidence") or 0.0,
                )
                continue

            # 实际刮削
            scraped = self._scrape_one(fp_result.fingerprint, fp_result.duration)
            if scraped:
                results[fp_result.fingerprint] = scraped
                self.db.set_metadata(fp_result.fingerprint, scraped.__dict__)

        return results

    def _scrape_one(self, fingerprint: str, duration: Optional[float]) -> Optional[ScrapedMetadata]:
        """刮削单个指纹的元数据。"""
        meta = ScrapedMetadata(fingerprint=fingerprint)

        # 1. AcoustID: 指纹 -> recording ID
        recording_id, confidence = self._lookup_acoustid(fingerprint, duration)
        if not recording_id:
            return None

        meta.recording_id = recording_id
        meta.confidence = confidence

        # 2. MusicBrainz: recording ID -> 标题/艺术家/专辑
        mb_data = self._lookup_musicbrainz(recording_id)
        if mb_data:
            meta.title = mb_data.get("title")
            meta.artist = mb_data.get("artist")
            meta.album = mb_data.get("album")
            meta.genre = mb_data.get("genre")
            meta.year = mb_data.get("year")

        # 3. iTunes: 封面
        if self.config.fetch_cover and meta.title and meta.artist:
            cover = self._lookup_itunes_cover(meta.title, meta.artist)
            if cover:
                meta.cover_url = cover

        return meta

    def _lookup_acoustid(
        self, fingerprint: str, duration: Optional[float]
    ) -> tuple[Optional[str], float]:
        """通过 AcoustID 查找 MusicBrainz recording ID。"""
        params = {
            "client": self.config.acoustid_api_key,
            "fingerprint": fingerprint,
            "meta": "recordingids",
        }
        if duration:
            params["duration"] = int(duration)

        try:
            resp = self._session.get(ACOUSTID_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"AcoustID 请求失败: {e}")
            return None, 0.0

        results = data.get("results", [])
        if not results:
            return None, 0.0

        # 取置信度最高的结果
        best = max(results, key=lambda r: r.get("score", 0))
        score = best.get("score", 0)
        recordings = best.get("recordings", [])

        if not recordings:
            return None, 0.0

        # 多个候选且分数接近时标记需确认
        needs_confirm = len(results) > 1 and (results[0].get("score", 0) - results[1].get("score", 0)) < 0.1

        recording_id = recordings[0].get("id")
        confidence = score / 100.0

        return recording_id, confidence

    def _lookup_musicbrainz(self, recording_id: str) -> Optional[dict[str, Any]]:
        """通过 MusicBrainz API 获取录音详情。"""
        self._rate_limit_mb()

        headers = {"User-Agent": self.config.musicbrainz_user_agent}
        params = {"inc": "artists+releases+genres", "fmt": "json"}
        url = MUSICBRAINZ_URL.format(rid=recording_id)

        try:
            resp = self._session.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"MusicBrainz 请求失败: {e}")
            return None

        result: dict[str, Any] = {}
        result["title"] = data.get("title")

        # 艺术家
        artist_credits = data.get("artist-credit", [])
        if artist_credits:
            names = []
            for ac in artist_credits:
                if isinstance(ac, dict):
                    names.append(ac.get("name", ""))
                else:
                    names.append(str(ac))
            result["artist"] = "".join(names)

        # 专辑（取第一个发行）
        releases = data.get("releases", [])
        if releases:
            result["album"] = releases[0].get("title")
            date = releases[0].get("date")
            if date:
                result["year"] = date[:4]

        # 流派（取第一个）
        genres = data.get("genres", [])
        if genres:
            result["genre"] = genres[0].get("name")

        return result

    def _lookup_itunes_cover(self, title: str, artist: str) -> Optional[str]:
        """通过 iTunes Search API 获取封面 URL。"""
        params = {
            "term": f"{artist} {title}",
            "media": "music",
            "limit": 1,
        }
        try:
            resp = self._session.get(ITUNES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"iTunes 封面查询失败: {e}")
            return None

        results = data.get("results", [])
        if not results:
            return None

        artwork = results[0].get("artworkUrl100")
        if not artwork:
            return None

        # 将 100x100 替换为所需尺寸
        size = self.config.cover_size
        return artwork.replace("100x100bb", f"{size}x{size}bb")

    def _rate_limit_mb(self) -> None:
        """MusicBrainz 速率限制：1 次/秒。"""
        elapsed = time.time() - self._last_mb_request
        if elapsed < self.config.rate_limit_delay:
            time.sleep(self.config.rate_limit_delay - elapsed)
        self._last_mb_request = time.time()
