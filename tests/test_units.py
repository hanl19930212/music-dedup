"""music-dedup 单元测试（纯 Python，不依赖 pytest、不联网）。

覆盖：文件名归一化、分层分组、指纹相似度、回收站兜底、缓存键、报告统计。
"""

from __future__ import annotations

import random
import sqlite3
import tempfile
from pathlib import Path

from music_dedup.action import resolve_quarantine_dir
from music_dedup.config import DedupConfig
from music_dedup.db import CacheDB
from music_dedup.dedup import TrackItem, group_duplicates
from music_dedup.fingerprint import (
    decode_fingerprint,
    encode_fingerprint,
    fingerprint_similarity,
)
from music_dedup.metadata import AudioMetadata
from music_dedup.scanner import AudioFile
from music_dedup.utils import filename_segments, is_placeholder_tag, normalize_filename

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def make_item(path: Path, fingerprint: str | None = None, duration: float | None = None,
              size: int = 1000, title: str | None = None, artist: str | None = None) -> TrackItem:
    meta = AudioMetadata(path=path, title=title, artist=artist)
    return TrackItem(
        audio_file=AudioFile(path=path, size=size, mtime=0.0),
        fingerprint=fingerprint,
        duration=duration,
        metadata=meta,
    )


def test_segments() -> None:
    cases = {
        "李宗盛-鬼迷心窍.wav": ["李宗盛", "鬼迷心窍"],
        "鬼迷心窍-李宗盛.wav": ["鬼迷心 chuck", "李宗盛"],
        "0003-999朵玫瑰.wav": ["999朵玫瑰"],
        "01_歌曲.flac": ["歌曲"],
        "10. 爱的代价.mp3": ["爱的代价"],
        "1-06 鬼迷心窍.flac": ["鬼迷心窍"],
        "2002年的第一场雪-刀郎.wav": ["2002年的第一场雪", "刀郎"],
    }
    for name, expected in cases.items():
        if name == "鬼迷心窍-李宗盛.wav":
            expected = ["鬼迷心窍", "李宗盛"]
        got = filename_segments(name)
        check(f"文件名切分 {name}", got == expected, f"{got} != {expected}")

    check("归一化与分隔顺序无关",
          normalize_filename("李宗盛-鬼迷心窍.wav") == normalize_filename("鬼迷心窍-李宗盛.wav"),
          normalize_filename("李宗盛-鬼迷心窍.wav"))
    check("同歌手的不同歌曲归一化后不同",
          normalize_filename("李宗盛-鬼迷心窍.wav") != normalize_filename("李宗盛-爱的代价.wav"))


def test_name_grouping() -> None:
    root = Path("C:/music")
    items = [
        make_item(root / "李宗盛-再回首.wav"),
        make_item(root / "再回首-李宗盛.wav"),
        make_item(root / "李宗盛-鬼迷心窍.wav"),
        make_item(root / "李宗盛-爱的代价.wav"),
    ]
    groups = group_duplicates(items, DedupConfig(min_similarity=0.0))
    name_groups = [g for g in groups if g.match_type == "name"]
    check("同一首歌的两种写法归为一组", len(name_groups) == 1, f"name 组数={len(name_groups)}")
    if name_groups:
        members = {item.path.name for item in name_groups[0].items}
        check("同歌手的不同歌曲不会被归到一起",
              members == {"李宗盛-再回首.wav", "再回首-李宗盛.wav"}, f"{sorted(members)}")

    # 标签里的歌名/歌手也参与匹配
    tagged = [
        make_item(root / "a.wav", title="再回首", artist="李宗盛"),
        make_item(root / "b.wav", title="再回首", artist="李宗盛"),
    ]
    groups = group_duplicates(tagged, DedupConfig(min_similarity=0.0))
    check("标签相同的文件归为一组",
          any(g.match_type == "name" for g in groups), f"{[g.match_type for g in groups]}")


def test_fingerprint_similarity() -> None:
    random.seed(20261026)
    original = [random.getrandbits(32) for _ in range(200)]
    encoded = encode_fingerprint(original)
    check("指纹解码长度正确", len(decode_fingerprint(encoded)) == 200)
    check("相同指纹相似度为 1", fingerprint_similarity(encoded, encoded) > 0.999)

    slightly = list(original)
    for index in (10, 50, 90, 130, 170):
        slightly[index] ^= 0x0000FFFF
    score = fingerprint_similarity(encoded, encode_fingerprint(slightly))
    check("少量比特差异仍判为同一录音", score >= 0.95, f"similarity={score:.4f}")

    unrelated = encode_fingerprint([random.getrandbits(32) for _ in range(200)])
    score = fingerprint_similarity(encoded, unrelated)
    check("无关指纹相似度低", score < 0.95, f"similarity={score:.4f}")


def test_fingerprint_offset_tolerance() -> None:
    """两份同一录音的指纹起点差几秒时，仍要能认出来。"""
    random.seed(11)
    original = [random.getrandbits(32) for _ in range(300)]
    shifted = [0] * 24 + original
    score = fingerprint_similarity(encode_fingerprint(original), encode_fingerprint(shifted))
    check("起点错开约 3 秒仍判为同一录音", score > 0.9, f"similarity={score:.4f}")


def test_placeholder_and_live_tags() -> None:
    """占位标签不能参与匹配；现场版标记必须保留。"""
    check("下载器占位标签被识别", is_placeholder_tag("kuwo") and is_placeholder_tag("酷我"))
    check("正常歌名不算占位", not is_placeholder_tag("再回首"))
    check("现场版标记保留在片段里",
          filename_segments("爱在西元前(Live).wma") != filename_segments("爱在西元前.wma"),
          str(filename_segments("爱在西元前(Live).wma")))

    root = Path("C:/music")
    items = [
        make_item(root / "爱在西元前.wav", title="kuwo", artist="kuwo"),
        make_item(root / "再回首.wav", title="kuwo", artist="kuwo"),
    ]
    groups = group_duplicates(items, DedupConfig(min_similarity=0.0))
    check("占位标签不会把不同歌归成一组",
          not any(g.match_type == "name" for g in groups),
          f"{[g.match_type for g in groups]}")

    live_items = [
        make_item(root / "爱在西元前.wav"),
        make_item(root / "爱在西元前(Live).wma"),
    ]
    groups = group_duplicates(live_items, DedupConfig(min_similarity=0.0))
    check("现场版与录音室版不会被文件名归到一起",
          not any(g.match_type == "name" for g in groups),
          f"{[g.match_type for g in groups]}")


def test_cache_format_migration() -> None:
    """旧版本缓存的压缩格式指纹必须清掉重算。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cache.db"
        target = Path(tmp) / "song.wav"
        target.write_bytes(b"x")

        db = CacheDB(db_path)
        db.set_fingerprint(target, 1, 2.0, "COMPRESSED-OLD-FORMAT", 3.0)
        db.close()

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE fingerprints SET fp_format = 0")
        conn.commit()
        conn.close()

        db = CacheDB(db_path)
        try:
            check("升级后旧格式指纹被清掉",
                  db.get_fingerprint(target, 1, 2.0) is None)
        finally:
            db.close()


def test_similar_fingerprint_grouping() -> None:
    random.seed(7)
    original = [random.getrandbits(32) for _ in range(200)]
    variant = list(original)
    for index in (20, 80, 140):
        variant[index] ^= 0x0000FFFF

    root = Path("C:/music")
    items = [
        make_item(root / "song.flac", fingerprint=encode_fingerprint(original), duration=180.0, size=2000),
        make_item(root / "song.dsf", fingerprint=encode_fingerprint(variant), duration=180.0, size=9000),
    ]
    groups = group_duplicates(items, DedupConfig(min_similarity=0.95))
    similar = [g for g in groups if g.match_type == "fingerprint_similar"]
    check("指纹相似但不完全相同的文件归为一组", len(similar) == 1,
          f"组类型={[g.match_type for g in groups]}")
    if similar:
        check("相似组里优先保留格式优先级更高的（dsf > flac）",
              similar[0].keep.path.name == "song.dsf", similar[0].keep.path.name)

    # 时长差太多时不归组
    items[1].duration = 240.0
    groups = group_duplicates(items, DedupConfig(min_similarity=0.95))
    check("时长差超过容差的不归组",
          not any(g.match_type == "fingerprint_similar" for g in groups),
          f"{[g.match_type for g in groups]}")


def test_trash_fallback() -> None:
    import music_dedup.action as action

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "song.wav"
        target.write_bytes(b"x" * 10)
        quarantine = Path(tmp) / "quarantine"

        original = action.send2trash

        def broken(path: str) -> None:
            raise OSError("回收站不可用（模拟 UNC 网络共享）")

        action.send2trash = broken
        try:
            result = action.move_to_trash(target, quarantine)
        finally:
            action.send2trash = original

        check("回收站失败时退回到隔离目录",
              result.success and result.quarantined and not target.exists(),
              result.message)
        check("隔离目录里确实有文件", any(quarantine.glob("*.wav")))

        # 没有兜底目录时应当如实报告失败
        target2 = Path(tmp) / "song2.wav"
        target2.write_bytes(b"x" * 10)
        action.send2trash = broken
        try:
            result = action.move_to_trash(target2, None)
        finally:
            action.send2trash = original
        check("没有兜底目录时如实报告失败",
              not result.success and target2.exists(), result.message)


def test_quarantine_dir_resolution() -> None:
    paths = [Path("D:/Music/A/1.flac"), Path("D:/Music/B/2.flac")]
    resolved = resolve_quarantine_dir(paths, "_quarantine")
    check("隔离目录取公共父目录", resolved == Path("D:/Music/_quarantine"), str(resolved))


def test_cache_key_normalization() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "song.wav"
        target.write_bytes(b"x")
        db = CacheDB(Path(tmp) / "cache.db")
        try:
            db.set_fingerprint(target, 1, 2.0, "FP", 3.0)
            hit = db.get_fingerprint(target, 1, 2.0)
            check("缓存命中", hit is not None and hit[0] == "FP", str(hit))

            # 同一个文件换个写法（带 ./ 前缀）应当命中同一条缓存
            alias = Path(tmp) / "." / "song.wav"
            hit2 = db.get_fingerprint(alias, 1, 2.0)
            check("缓存键对路径写法不敏感", hit2 is not None, str(hit2))
        finally:
            db.close()


def test_report_stats() -> None:
    from music_dedup.report import generate_report

    root = Path("C:/music")
    items = [
        make_item(root / "keep.wav", fingerprint="A" * 20, duration=10.0, size=100),
        make_item(root / "dup.wav", fingerprint="A" * 20, duration=10.0, size=100),
        make_item(root / "x.wav", size=100),
        make_item(root / "y.wav", size=100),
    ]
    groups = group_duplicates(items, DedupConfig(min_similarity=0.0))
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.html"
        generate_report(groups, len(items), report)
        html = report.read_text(encoding="utf-8")
        check("报告把待人工确认的组单独计数", "待人工确认的组数" in html)
        json_hint = report.with_suffix('.json')
        # report.py 开启 autoescape 后，模板里的双引号会被渲染成 &#34;；
        # 历史上也出现过 &quot;，这里同时兼容三种形式，不降低校验强度。
        apply_hints = tuple(f"apply {quote}{json_hint}{quote}"
                            for quote in ('"', "&quot;", "&#34;"))
        check("报告的 apply 提示指向 json", any(hint in html for hint in apply_hints),
              f"兼容形式: {apply_hints}")


def test_scrape_negative_caching() -> None:
    from music_dedup.config import ScrapeConfig
    from music_dedup.fingerprint import FingerprintResult
    from music_dedup.scanner import AudioFile
    from music_dedup.scraper import Scraper

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs) -> FakeResponse:
            self.calls += 1
            return FakeResponse({"results": []})  # 库里没有收录

    with tempfile.TemporaryDirectory() as tmp:
        db = CacheDB(Path(tmp) / "cache.db")
        try:
            config = ScrapeConfig(acoustid_api_key="test-key")
            scraper = Scraper(config, db)
            scraper._session = FakeSession()

            target = Path(tmp) / "song.wav"
            target.write_bytes(b"x")
            result = FingerprintResult(
                file=AudioFile(path=target, size=1, mtime=0.0),
                fingerprint="FINGERPRINT",
                duration=10.0,
            )
            scraper.acoustid_fingerprint = lambda path: "COMPRESSED-FINGERPRINT"

            first = scraper.scrape([result])
            check("未收录时没有刮削结果", first == {}, str(first))
            check("未收录时会发一次请求", scraper._session.calls == 1, f"calls={scraper._session.calls}")
            check("负结果已写入缓存", db.get_metadata("FINGERPRINT") is not None)

            second = scraper.scrape([result])
            check("二次刮削直接命中缓存不再请求", scraper._session.calls == 1,
                  f"calls={scraper._session.calls}")
            check("二次刮削结果一致", second == first)
        finally:
            db.close()


def test_acoustid_lookup_selection() -> None:
    from music_dedup.config import ScrapeConfig
    from music_dedup.scraper import Scraper

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def get(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse(self._payload)

    payload = {
        "status": "ok",
        "results": [
            # 最高分候选没有关联 MusicBrainz 录音（真实库里的《白狐》就是这样）
            {"id": "track-without-recordings", "score": 0.9988},
            {"id": "track-with-recordings", "score": 0.8488,
             "recordings": [{"id": "recording-123"}]},
        ],
    }

    with tempfile.TemporaryDirectory() as tmp:
        db = CacheDB(Path(tmp) / "cache.db")
        try:
            scraper = Scraper(ScrapeConfig(acoustid_api_key="test-key"), db)
            scraper._session = FakeSession(payload)

            recording_id, confidence, definitive = scraper._lookup_acoustid("FP", 306.0)
            check("跳过没有 recordings 的最高分候选", recording_id == "recording-123",
                  f"recording_id={recording_id}")
            check("confidence 直接取 0~1 的 score（不再除以 100）",
                  abs(confidence - 0.8488) < 1e-6, f"confidence={confidence}")
            check("有匹配时查询成功标记 definitive", definitive is True)

            scraper._session = FakeSession({"status": "ok", "results": []})
            recording_id, confidence, definitive = scraper._lookup_acoustid("FP", 306.0)
            check("无候选时返回空但查询成功",
                  recording_id is None and confidence == 0.0 and definitive is True,
                  f"recording_id={recording_id} confidence={confidence} definitive={definitive}")
        finally:
            db.close()


def main() -> int:
    test_segments()
    test_name_grouping()
    test_fingerprint_similarity()
    test_fingerprint_offset_tolerance()
    test_placeholder_and_live_tags()
    test_cache_format_migration()
    test_similar_fingerprint_grouping()
    test_trash_fallback()
    test_quarantine_dir_resolution()
    test_cache_key_normalization()
    test_report_stats()
    test_scrape_negative_caching()
    test_acoustid_lookup_selection()

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("未通过: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
