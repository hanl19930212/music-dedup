"""删除安全链路回归测试（只读评审中实锤的 P0/P1 缺陷）。

运行方式（与 README 一致，不需要 pytest、不联网、不触碰真实音乐库）：

    python tests/test_security.py

约定：
- 所有会触发「移动文件」的路径都把 ``music_dedup.action.send2trash`` 换成只做记录的替身，
  因此本文件不会污染系统回收站，也不会真的删除任何用户文件。
- 报告的构造一律先用 ``serialize_report`` 生成合法基线，再只篡改目标字段，
  确保测的是「校验是否拦住篡改」，而不是「手工 JSON 缺字段」。
- 依赖工程师锁定的接口：``music_dedup.contract`` /
  ``ActionResult.quarantined_size`` / ``TagWriter._rollback`` 返回 bool /
  ``config._validate`` 经 ``load_config()`` 生效。
"""

from __future__ import annotations

import contextlib
import copy
import os
import shutil
import tempfile
from pathlib import Path

import music_dedup.action as action
from music_dedup.action import apply_dedup
from music_dedup.config import DedupConfig
from music_dedup.contract import (
    ReportValidationError,
    SCHEMA_VERSION,
    VALID_MATCH_TYPES,
    deserialize_report,
    serialize_report,
    verify_file_state,
)
from music_dedup.dedup import DuplicateGroup, TrackItem, group_duplicates
from music_dedup.fingerprint import decode_fingerprint, encode_fingerprint, fingerprint_similarity
from music_dedup.metadata import AudioMetadata
from music_dedup.scanner import AudioFile

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def make_item(
    path: Path,
    *,
    size: int = 100,
    mtime: float = 0.0,
    fingerprint: str | None = None,
    duration: float | None = None,
) -> TrackItem:
    """构造 TrackItem；path 只作为字符串参与报告，不要求文件一定存在。"""
    return TrackItem(
        audio_file=AudioFile(path=path, size=size, mtime=mtime),
        fingerprint=fingerprint,
        duration=duration,
        metadata=AudioMetadata(path=path),
    )


def make_group(root: Path, match_type: str = "fingerprint", count: int = 2) -> DuplicateGroup:
    """在 root 下创建真实文件，并用真实的 size/mtime 构造一个合法分组。"""
    items = []
    for index in range(count):
        path = root / f"song{index}.wav"
        path.write_bytes(b"x" * (100 + index))
        stat = path.stat()
        items.append(make_item(path, size=stat.st_size, mtime=stat.st_mtime))
    return DuplicateGroup(
        key=f"{match_type}:regression",
        match_type=match_type,
        items=items,
        keep_index=0,
    )


def valid_report(root: Path, match_type: str = "fingerprint") -> dict:
    """生成一份合法的 dict 结构报告，供各用例做「只改一个字段」的篡改基线。"""
    return serialize_report([make_group(root, match_type)], [root])


@contextlib.contextmanager
def patched_trash(calls: list[str]):
    """把 send2trash 换成只记录调用的替身，杜绝测试污染真实回收站。"""
    original = action.send2trash
    action.send2trash = lambda path: calls.append(str(path))
    try:
        yield
    finally:
        action.send2trash = original


def rejected(data: dict) -> tuple[bool, str]:
    """尝试反序列化；返回 (是否被 ReportValidationError 拒绝, 说明)。"""
    try:
        deserialize_report(data)
    except ReportValidationError as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001 - 需要把非预期异常也报成失败
        return False, f"抛出了非预期异常 {type(exc).__name__}: {exc}"
    return False, "deserialize_report 未拒绝（危险）"


def full_chain(data: dict) -> tuple[bool, str]:
    """走完整链路：contract 校验 -> （若意外放行）执行层。

    返回 (contract 是否拒绝, 说明)。调用方应在 patched_trash 上下文里使用，
    以便随后断言「零文件被移动」。
    """
    try:
        groups = deserialize_report(data)
    except ReportValidationError as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"抛出了非预期异常 {type(exc).__name__}: {exc}"
    apply_dedup(groups, dry_run=False)
    return False, "contract 未拒绝，已交由执行层处理"


# ---------------------------------------------------------------------------
# P0：删除安全（这些是评审中实测实锤过的致命缺陷）
# ---------------------------------------------------------------------------


def test_reject_legacy_list_report() -> None:
    """P0-1：顶层为 list 的旧格式必须拒绝，并提示重新执行 scan。"""
    legacy = [{
        "key": "fingerprint:old",
        "match_type": "fingerprint",
        "keep_index": 0,
        "items": [{"path": "C:/music/a.wav", "size": 100, "ext": "wav", "mtime": 0.0}],
    }]
    ok, detail = rejected(legacy)
    check("P0-1 拒绝旧版 list 结构的 report.json", ok, detail)
    hint = detail.lower()
    check("P0-1 异常提示重新执行 scan / 版本不兼容",
          any(keyword in hint for keyword in ("scan", "重新", "version", "格式", "schema")),
          detail)


def test_path_traversal_rejected() -> None:
    """P0-2：items[].path 指向扫描根之外时必须拒绝，且零文件被移动。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "music"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        victim = outside / "victim.wav"
        victim.write_bytes(b"DO NOT DELETE")

        data = valid_report(root)
        evil_paths = [
            r"C:\Windows\System32\drivers\etc\hosts",
            r"..\..\outside\victim.wav",
            r"\\other-share\private\secret.wav",
            str(outside / "victim.wav"),  # 真实存在的扫描根外诱饵
        ]
        calls: list[str] = []
        with patched_trash(calls):
            for evil in evil_paths:
                bad = copy.deepcopy(data)
                bad["groups"][0]["items"][0]["path"] = evil
                ok, detail = full_chain(bad)
                check(f"P0-2 拒绝越界路径 {evil[:40]}", ok, detail)

        check("P0-2 越界路径下零文件被移动（含扫描根外诱饵）",
              calls == [] and victim.exists() and victim.read_bytes() == b"DO NOT DELETE",
              f"send2trash 调用={calls}")


def test_keep_index_validation() -> None:
    """P0-3：keep_index 越界/类型非法时，duplicates 会把整组（含保留项）全删。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = valid_report(root)
        for bad_value in (-1, 99, None, "0", True, 2):
            bad = copy.deepcopy(data)
            bad["groups"][0]["keep_index"] = bad_value
            ok, detail = rejected(bad)
            check(f"P0-3 拒绝非法 keep_index={bad_value!r}", ok, detail)


def test_invalid_match_type_and_action_whitelist() -> None:
    """P0-4：非法 match_type 在 contract 层被拒；action 层也必须一律跳过。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = valid_report(root)
        for bad_type in ("bogus", "", "NAME", None, 123):
            bad = copy.deepcopy(data)
            bad["groups"][0]["match_type"] = bad_type
            ok, detail = rejected(bad)
            check(f"P0-4 拒绝非法 match_type={bad_type!r}", ok, detail)

        # 第二层：绕过 contract 直接构造分组，执行层也不得删除。
        for bad_type in ("bogus", ""):
            group = make_group(root, bad_type)
            calls: list[str] = []
            with patched_trash(calls):
                plans = apply_dedup([group], dry_run=True)
                apply_dedup([group], dry_run=False)
            check(f"P0-4 apply_dedup 对非法 match_type={bad_type!r} 一律跳过",
                  plans == [] and calls == [],
                  f"预览计划={len(plans)}, send2trash 调用={calls}")


def test_stale_report_detection() -> None:
    """P0-5：报告过期（同路径文件被替换）必须被 verify_file_state 检出。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = valid_report(root)
        group = deserialize_report(data)[0]
        reasons = verify_file_state(group)
        check("P0-5 文件未变化时 verify_file_state 返回空列表", reasons == [], str(reasons))

        # 场景 A：size 变化
        target = group.items[1].path
        target.write_bytes(b"y" * 999)
        reasons = verify_file_state(group)
        check("P0-5 同路径文件 size 变化被检出", len(reasons) > 0, str(reasons))

        # 场景 B：仅 mtime 变化（size 保持不变）
        fresh = deserialize_report(valid_report(root))[0]
        other = fresh.items[1].path
        stat = other.stat()
        os.utime(other, (stat.st_atime + 5, stat.st_mtime + 5))
        reasons = verify_file_state(fresh)
        check("P0-5 同路径文件 mtime 变化被检出", len(reasons) > 0, str(reasons))


def test_name_group_tampered_to_fingerprint() -> None:
    """P0-6：把 name 组篡改成 fingerprint 也不能导致删除（两层防护都测）。

    注意 ``"fingerprint"`` 本身是合法 match_type，action 白名单拦不住它，
    因此必须靠 contract 的一致性/签名校验，或执行层的额外防护。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        baseline = make_group(root, "name")
        data = serialize_report([baseline], [root])

        # 对照：合法的 name 组默认不删除
        calls: list[str] = []
        with patched_trash(calls):
            apply_dedup([baseline], dry_run=False)
        check("P0-6 对照：name 组默认跳过删除", calls == [], f"send2trash 调用={calls}")

        bad = copy.deepcopy(data)
        bad["groups"][0]["match_type"] = "fingerprint"
        calls = []
        with patched_trash(calls):
            rejected_by_contract, detail = full_chain(bad)
        check("P0-6 name→fingerprint 篡改被 contract 或执行层拦下",
              rejected_by_contract or calls == [],
              f"contract_rejected={rejected_by_contract}, {detail}")
        check("P0-6 篡改后零文件被移动",
              calls == [] and all(item.path.exists() for item in baseline.items),
              f"send2trash 调用={calls}")


# ---------------------------------------------------------------------------
# P1：标签安全、输出转义、配置与统计
# ---------------------------------------------------------------------------


def test_html_autoescape() -> None:
    """P1-7：文件名/路径里的 HTML 特殊字符必须转义，不能裸插入报告。"""
    from music_dedup.report import generate_report

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evil_name = "evil<script>alert(1)</script>.wav"
        evil_path = '"><img onerror=alert(2)>.wav'
        group = DuplicateGroup(
            key="fingerprint:evil",
            match_type="fingerprint",
            items=[make_item(root / evil_name), make_item(root / evil_path)],
            keep_index=0,
        )
        report = root / "report.html"
        generate_report([group], 2, report)
        html = report.read_text(encoding="utf-8")

        check("P1-7 文件名中的 <script> 被转义为 &lt;script&gt;", "&lt;script&gt;" in html)
        check("P1-7 裸的 evil<script> 未出现在 HTML 中", "evil<script>" not in html)
        check("P1-7 路径中的引号与 <img 被转义",
              "&quot;&gt;&lt;img" in html or "&#34;&gt;&lt;img" in html)
        check("P1-7 裸的 \"><img 未出现在 HTML 中", '"><img' not in html)


def test_backup_never_overwrites() -> None:
    """P1-8：同一文件连续两次备份必须产生两个不同文件，各自保留当时内容。"""
    from music_dedup.tagwriter import TagWriter

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_dir = root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)  # 不依赖 __init__ 是否建目录
        src = root / "song.mp3"
        src.write_bytes(b"ORIGINAL-CONTENT")
        writer = TagWriter(backup_dir)
        first = writer._backup(src)
        src.write_bytes(b"MODIFIED-CONTENT")
        second = writer._backup(src)

        check("P1-8 两次备份产生不同文件名",
              first is not None and second is not None and first != second,
              f"{first} vs {second}")
        check("P1-8 第一份备份仍是原始内容",
              first is not None and first.read_bytes() == b"ORIGINAL-CONTENT",
              str(first))
        check("P1-8 第二份备份是修改后内容",
              second is not None and second.read_bytes() == b"MODIFIED-CONTENT",
              str(second))


def test_rollback_failure_is_visible() -> None:
    """P1-9：写入失败且回滚也失败时，返回值必须显式说明，不能静默。"""
    from music_dedup.scraper import ScrapedMetadata
    from music_dedup.tagwriter import TagWriter

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_dir = root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        src = root / "song.mp3"
        src.write_bytes(b"ORIGINAL")

        writer = TagWriter(backup_dir)

        def write_boom(*_args, **_kwargs):
            raise RuntimeError("模拟标签写入失败")

        writer._write_tags = write_boom

        real_copy2 = shutil.copy2
        state = {"calls": 0}

        def fake_copy2(source, destination, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] >= 2:  # 第 1 次是 _backup，第 2 次是 _rollback
                raise PermissionError("模拟回滚失败（只读/占用）")
            return real_copy2(source, destination, *args, **kwargs)

        shutil.copy2 = fake_copy2
        try:
            ok, message = writer.write(src, ScrapedMetadata(fingerprint="x"), dry_run=False)
        finally:
            shutil.copy2 = real_copy2

        check("P1-9 写入失败时返回 False", ok is False, message)
        check("P1-9 返回消息含『回滚失败』", "回滚失败" in message, message)
        check("P1-9 返回消息含『备份保留在』", "备份保留在" in message, message)


def test_config_validation() -> None:
    """P1-10：非法配置抛带字段名的 ValueError；min_similarity=1.0 必须合法。

    通过 ``load_config()`` 触发校验（它内部依次走 ``_apply_dict`` 与 ``_validate``），
    这样不依赖私有函数名，也更贴近真实入口。
    """
    from music_dedup.config import load_config

    header = (
        'cache_file = "{cache}"\n'
        'log_dir = "{logs}"\n'
        'backup_dir = "{backups}"\n\n'
    )
    cases = [
        ("[fingerprint]\nworkers = 0\n", "workers"),
        ("[fingerprint]\nduration = 0\n", "duration"),
        ("[dedup]\nmin_similarity = 1.5\n", "min_similarity"),
        ("[scrape]\nrate_limit_delay = -1\n", "rate_limit_delay"),
        ("[scrape]\ncover_size = 0\n", "cover_size"),
    ]
    saved = os.environ.get("MUSIC_DEDUP_CONFIG")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (body, field) in enumerate(cases):
                cfg = root / f"invalid{index}.toml"
                cfg.write_text(
                    header.format(cache=(root / "cache.db").as_posix(),
                                  logs=(root / "logs").as_posix(),
                                  backups=(root / "backups").as_posix()) + body,
                    encoding="utf-8",
                )
                os.environ["MUSIC_DEDUP_CONFIG"] = str(cfg)
                try:
                    load_config()
                    ok, detail = False, "未抛 ValueError"
                except ValueError as exc:
                    ok, detail = field in str(exc), str(exc)
                check(f"P1-10 非法配置 {field} 被拒且消息含字段名", ok, detail)

            # 合法边界：1.0 用于关闭相似匹配，不能被误拒
            ok_cfg = root / "valid.toml"
            ok_cfg.write_text(
                header.format(cache=(root / "cache.db").as_posix(),
                              logs=(root / "logs").as_posix(),
                              backups=(root / "backups").as_posix())
                + "[dedup]\nmin_similarity = 1.0\n",
                encoding="utf-8",
            )
            os.environ["MUSIC_DEDUP_CONFIG"] = str(ok_cfg)
            try:
                load_config()
                ok, detail = True, "load_config 正常返回"
            except ValueError as exc:
                ok, detail = False, str(exc)
            check("P1-10 min_similarity=1.0 合法（关闭相似匹配）", ok, detail)
    finally:
        if saved is None:
            os.environ.pop("MUSIC_DEDUP_CONFIG", None)
        else:
            os.environ["MUSIC_DEDUP_CONFIG"] = saved


def test_empty_fingerprint() -> None:
    """P1-11：空/非法指纹不得被判为相似，也不得形成默认删除的精确组。"""
    check("P1-11 空指纹相似度为 0.0",
          fingerprint_similarity("", "") == 0.0,
          str(fingerprint_similarity("", "")))
    check("P1-11 非法串 vs 合法指纹为 0.0",
          fingerprint_similarity("NOT-BASE64!!", encode_fingerprint([1, 2, 3])) == 0.0)
    check("P1-11 非法串解码为空列表", decode_fingerprint("NOT-BASE64!!") == [])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        items = [
            make_item(root / "a.wav", fingerprint=""),
            make_item(root / "b.wav", fingerprint=""),
        ]
        groups = group_duplicates(items, DedupConfig(min_similarity=0.83, name_fallback=False))
        check("P1-11 两个空指纹不形成默认删除的 fingerprint 组",
              not any(g.match_type == "fingerprint" for g in groups),
              f"组类型={[g.match_type for g in groups]}")


def test_quarantine_accounting() -> None:
    """P1-12：转隔离时 freed_size 应为 0，quarantined_size 记录实际大小。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "song.wav"
        target.write_bytes(b"x" * 10)
        quarantine = root / "quarantine"

        original = action.send2trash

        def broken(path):
            raise OSError("回收站不可用（模拟 UNC 网络共享）")

        action.send2trash = broken
        try:
            result = action.move_to_trash(target, quarantine)
        finally:
            action.send2trash = original

        check("P1-12 隔离分支 quarantined 标记为真", result.quarantined is True, result.message)
        check("P1-12 隔离分支 freed_size == 0", result.freed_size == 0,
              f"freed_size={result.freed_size}")
        check("P1-12 隔离分支 quarantined_size > 0", result.quarantined_size > 0,
              f"quarantined_size={result.quarantined_size}")


# ---------------------------------------------------------------------------
# P2：轻量副作用检查
# ---------------------------------------------------------------------------


def test_tagwriter_init_no_side_effect() -> None:
    """P2-13（轻量版）：构造 TagWriter 不应创建备份目录。"""
    from music_dedup.tagwriter import TagWriter

    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp) / "backups"
        TagWriter(backup_dir)
        check("P2-13 TagWriter 构造不创建备份目录", not backup_dir.exists(), str(backup_dir))


def test_apply_dry_run_no_move() -> None:
    """P2-13（轻量版）：dry-run 只给预览计划，不调用 send2trash、不删文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        group = make_group(root, "fingerprint")
        calls: list[str] = []
        with patched_trash(calls):
            plans = apply_dedup([group], dry_run=True)
        check("P2-13 dry-run 不调用 send2trash", calls == [], f"调用={calls}")
        check("P2-13 dry-run 保留所有文件", all(item.path.exists() for item in group.items))
        check("P2-13 dry-run 仍给出预览计划", len(plans) == 1, f"计划数={len(plans)}")


def test_contract_constants() -> None:
    """附加：锁定接口常量本身也要符合约定，避免后续实现跑偏。"""
    check("contract.SCHEMA_VERSION == 1", SCHEMA_VERSION == 1, str(SCHEMA_VERSION))
    expected = {"recording_id", "fingerprint", "fingerprint_similar", "name"}
    check("contract.VALID_MATCH_TYPES 为四种合法类型", set(VALID_MATCH_TYPES) == expected,
          str(sorted(VALID_MATCH_TYPES)))
    check("ReportValidationError 是 ValueError 子类",
          issubclass(ReportValidationError, ValueError))


def main() -> int:
    test_reject_legacy_list_report()
    test_path_traversal_rejected()
    test_keep_index_validation()
    test_invalid_match_type_and_action_whitelist()
    test_stale_report_detection()
    test_name_group_tampered_to_fingerprint()
    test_html_autoescape()
    test_backup_never_overwrites()
    test_rollback_failure_is_visible()
    test_config_validation()
    test_empty_fingerprint()
    test_quarantine_accounting()
    test_tagwriter_init_no_side_effect()
    test_apply_dry_run_no_move()
    test_contract_constants()

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("未通过: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
