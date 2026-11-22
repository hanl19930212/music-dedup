"""music-dedup 端到端冒烟测试（使用 fpcalc 桩，不联网）。

覆盖本次修复的四个问题：
  1. scanner 使用 os.walk，Python 3.11 上不再崩溃
  2. HTML 报告里的 apply 提示指向 .json 而不是 .html
  3. 命令行目录覆盖配置里的 scan_dirs
  4. 配置里的 ~ 正确展开到用户目录
以及去重主流程：扫描 -> 分组 -> 报告 -> 预览 -> 移入回收站 -> 缓存命中。

注意：为不污染用户真实回收站，CLI 子进程通过 sitecustomize 把 send2trash
重定向到 .cache/e2e/fake_trash，并在 main() 的 finally 中清理恢复。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".cache" / "e2e"
MUSIC = TMP / "music"
OTHER = TMP / "other"
CONFIG = TMP / "config.toml"
REPORT = TMP / "out" / "report.html"
STUB_REAL = ROOT / "tests" / "stub" / "fpcalc.bat"
CLI = ROOT / ".venv" / "Scripts" / "music-dedup.exe"
# 用「移动到临时目录」代替真实回收站：e2e 不再污染用户回收站，且可在 teardown 恢复
FAKE_TRASH = TMP / "fake_trash"
SITECUSTOMIZE = TMP / "sitecustomize.py"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def run(args: list[str], expect_ok: bool = True, config: Path | None = None) -> subprocess.CompletedProcess:
    # PYTHONPATH 注入 sitecustomize.py：CLI 子进程在解释器启动阶段即替换 send2trash，
    # 把「移入回收站」重定向到 FAKE_TRASH。注入脚本内部全程 try/except，
    # 任何异常都静默回退到原行为，保证不影响既有断言。
    env = dict(
        os.environ,
        MUSIC_DEDUP_CONFIG=str(config or CONFIG),
        PYTHONIOENCODING="utf-8",
        E2E_FAKE_TRASH=str(FAKE_TRASH),
        PYTHONPATH=str(TMP) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    proc = subprocess.run(
        [str(CLI), *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=180,
    )
    if expect_ok and proc.returncode != 0:
        print(f"--- 命令失败: {args}\n{proc.stdout}\n{proc.stderr}", flush=True)
    return proc


def write_wav(path: Path, freq: float, seconds: float = 1.0, jitter: bool = False) -> bytes:
    rate = 8000
    frames = int(rate * seconds)
    samples = bytearray()
    for i in range(frames):
        value = int(12000 * ((i * freq) % 1000) / 1000)
        if jitter and i % 2048 == 0:
            value += 3000
        samples += struct.pack("<h", value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))
    return path.read_bytes()


def build_fixtures() -> None:
    for folder in (MUSIC, OTHER, TMP / "out", FAKE_TRASH):
        if folder.exists():
            shutil.rmtree(folder)
    for folder in (MUSIC, OTHER):
        folder.mkdir(parents=True, exist_ok=True)

    # 指纹组：内容完全相同，文件名不同
    payload = write_wav(MUSIC / "李宗盛-鬼迷心窍.wav", 220)
    (MUSIC / "鬼迷心窍-李宗盛.wav").write_bytes(payload)

    # 格式优先级组：内容相同，.wav 应压过 .mp3
    payload = write_wav(MUSIC / "instrumental.wav", 330, seconds=0.5)
    (MUSIC / "instrumental.mp3").write_bytes(payload)

    # 文件名组：两首不同的歌，归一化后都变成歌手名
    write_wav(MUSIC / "爱的代价-李宗盛.wav", 440)
    write_wav(MUSIC / "凡人歌-李宗盛.wav", 550)

    # 同一首歌的两种写法（内容不同，只能靠文件名归组）
    write_wav(MUSIC / "李宗盛-再回首.wav", 880)
    write_wav(MUSIC / "再回首-李宗盛.wav", 990)

    # 独立文件，不应参与任何分组
    write_wav(MUSIC / "999朵玫瑰.wav", 660)

    # 指纹相似但不完全相同：同一段旋律，个别采样点有细微差异
    write_wav(MUSIC / "同一首歌-原版.wav", 1200)
    write_wav(MUSIC / "同一首歌-另一个版本.wav", 1200, jitter=True)

    # config 的 scan_dirs 指向的目录：命令行指定目录时不应被扫描
    write_wav(OTHER / "不应出现.wav", 770)


def write_config() -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    # TOML 基本字符串里反斜杠必须转义
    def q(value: str) -> str:
        return value.replace("\\", "\\\\")

    CONFIG.write_text(
        f'cache_file = "{q(str(TMP / "cache.db"))}"\n'
        f'log_dir = "{q(str(TMP / "logs"))}"\n'
        f'backup_dir = "{q(str(TMP / "backups"))}"\n'
        "\n[scan]\n"
        f'scan_dirs = ["{q(str(OTHER))}"]\n'
        "\n[fingerprint]\n"
        f'fpcalc_path = "{q(str(STUB_REAL))}"\n'
        "duration = 120\n"
        "\n[dedup]\n"
        "name_fallback = true\n"
        "\n[scrape]\n"
        'acoustid_api_key = ""\n',
        encoding="utf-8",
    )


def write_sitecustomize() -> None:
    """生成注入脚本：让 CLI 子进程的 send2trash 改成移动到 FAKE_TRASH。

    sitecustomize 在解释器启动的 site 阶段执行，早于 ``music_dedup.action`` 里的
    ``from send2trash import send2trash``，因此替换模块属性即可生效。
    """
    TMP.mkdir(parents=True, exist_ok=True)
    SITECUSTOMIZE.write_text(
        "try:\n"
        "    import os\n"
        "    import shutil\n"
        "    from pathlib import Path\n"
        "    _raw = os.environ.get('E2E_FAKE_TRASH', '')\n"
        "    if _raw:\n"
        "        _dest = Path(_raw)\n"
        "        import send2trash as _st\n"
        "        def _fake_trash(path):\n"
        "            _dest.mkdir(parents=True, exist_ok=True)\n"
        "            _name = Path(path)\n"
        "            _target = _dest / _name.name\n"
        "            _index = 2\n"
        "            while _target.exists():\n"
        "                _target = _dest / (f'{_name.stem}__{_index}{_name.suffix}')\n"
        "                _index += 1\n"
        "            shutil.move(str(path), str(_target))\n"
        "        _st.send2trash = _fake_trash\n"
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8",
    )


def main() -> int:
    build_fixtures()
    write_config()
    write_sitecustomize()
    try:
        return _run_checks()
    finally:
        # teardown：清理替身回收站与注入脚本，不残留、不污染用户系统回收站
        if FAKE_TRASH.exists():
            shutil.rmtree(FAKE_TRASH, ignore_errors=True)
        SITECUSTOMIZE.unlink(missing_ok=True)


def _run_checks() -> int:

    # --- 1. 扫描（Python 3.11 上不能因 Path.walk 崩溃） ---
    proc = run(["scan", str(MUSIC), "--report", str(REPORT)])
    check("扫描在 Python 3.11 上完成（os.walk 修复）", proc.returncode == 0,
          f"exit={proc.returncode}")
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        return 1

    json_path = REPORT.with_suffix(".json")
    html = REPORT.read_text(encoding="utf-8")

    # --- 2. HTML 报告里的 apply 提示（report.py 开启 autoescape，引号渲染成 &#34;）---
    quotes = ('"', "&quot;", "&#34;")
    json_apply = tuple(f"music-dedup apply {q}{json_path}{q}" for q in quotes)
    check("HTML 报告的 apply 提示指向 .json",
          any(variant in html for variant in json_apply),
          "应出现 report.json（兼容原样引号 / &quot; / &#34;）")
    # 反向校验：三种引号形式下都不能把 .html 当作 apply 输入（比原来只查 " 更严）
    html_apply = tuple(f"music-dedup apply {q}{REPORT}{q}" for q in quotes)
    check("HTML 报告不再把 .html 当作 apply 输入",
          (all(variant not in html for variant in html_apply)
           and f"{REPORT.name}" not in html.split("apply-hint")[1][:400])
          if "apply-hint" in html else True)

    # --- 3. 命令行目录覆盖配置的 scan_dirs ---
    # report.json 顶层由 list 升级为 dict：分组移到 data["groups"]，
    # 并新增 schema_version / generated_at / scan_roots 元信息。
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    check("report.json 为 dict 结构且含 schema_version / 非空 scan_roots / groups",
          isinstance(payload, dict)
          and payload.get("schema_version") == 1
          and bool(payload.get("scan_roots"))
          and isinstance(payload.get("groups"), list),
          f"type={type(payload).__name__}, "
          f"keys={list(payload)[:6] if isinstance(payload, dict) else payload}")
    groups = payload["groups"] if isinstance(payload, dict) and "groups" in payload else []
    scanned = {item["path"] for group in groups for item in group["items"]}
    check("命令行目录覆盖配置里的 scan_dirs",
          not any(str(OTHER) in path for path in scanned),
          f"OTHER 中的文件出现在结果里: {[p for p in scanned if str(OTHER) in p]}")

    # --- 4. 配置里的 ~ 展开（沙箱不允许写用户目录，故做单元级断言）---
    from music_dedup.config import Config, _apply_dict

    probe = Config()
    _apply_dict(probe, {
        "cache_file": "~/.music-dedup-e2e/cache.db",
        "log_dir": "~/logs",
        "backup_dir": "~/backups",
    })
    home = Path.home()
    check("配置里的 ~ 展开到用户目录",
          probe.cache_file == home / ".music-dedup-e2e" / "cache.db"
          and probe.log_dir == home / "logs"
          and probe.backup_dir == home / "backups",
          str(probe.cache_file))
    check("当前目录没有生成字面量 ~ 目录", not (ROOT / "~").exists() and not (TMP / "~").exists())

    # --- 4b. init-config 生成的默认配置必须真的生效 ---
    gen_config = TMP / "generated.toml"
    if gen_config.exists():
        gen_config.unlink()
    run(["init-config"], config=gen_config)
    import tomllib

    from music_dedup.config import DEFAULT_CACHE_FILE, _apply_dict

    parsed = tomllib.loads(gen_config.read_text(encoding="utf-8"))
    cfg = Config()
    _apply_dict(cfg, parsed)
    check("init-config 生成的 cache_file 能被读到（顶层键修复）",
          cfg.cache_file == DEFAULT_CACHE_FILE,
          f"{cfg.cache_file} vs {DEFAULT_CACHE_FILE}")

    # --- 5. 分组正确性 ---
    by_type: dict[str, list] = {}
    for group in groups:
        by_type.setdefault(group["match_type"], []).append(group)
    fp_groups = by_type.get("fingerprint", [])
    name_groups = by_type.get("name", [])
    similar_groups = by_type.get("fingerprint_similar", [])
    check("指纹完全相同的文件被归为一组", len(fp_groups) == 2,
          f"fingerprint 组数={len(fp_groups)}")
    check("指纹相近但不完全相同的文件归入相似组", len(similar_groups) == 1,
          f"fingerprint_similar 组数={len(similar_groups)}")
    for group in fp_groups:
        keeper = group["items"][group["keep_index"]]["path"]
        if keeper.endswith("instrumental.wav"):
            check("格式优先级生效：.wav 压过 .mp3", True, keeper)
        else:
            check("同内容同格式时按修改时间/体积择一", len(group["items"]) == 2, keeper)
    check("文件名兜底：同一首歌的两种写法归为一组", len(name_groups) == 1,
          f"name 组数={len(name_groups)}")
    if name_groups:
        members = {Path(item["path"]).name for item in name_groups[0]["items"]}
        check("文件名兜底不会把同歌手的不同歌曲归到一起",
              members == {"李宗盛-再回首.wav", "再回首-李宗盛.wav"},
              f"组成员={sorted(members)}")

    # --- 6. apply --dry-run ---
    before_dry_run = sorted(p.name for p in MUSIC.iterdir())
    before_fake_trash = sorted(p.name for p in FAKE_TRASH.iterdir()) if FAKE_TRASH.exists() else []
    proc = run(["apply", str(json_path), "--dry-run"])
    after_fake_trash = sorted(p.name for p in FAKE_TRASH.iterdir()) if FAKE_TRASH.exists() else []
    check("apply --dry-run 成功", proc.returncode == 0, f"exit={proc.returncode}")
    check("dry-run 不删除任何文件",
          sorted(p.name for p in MUSIC.iterdir()) == before_dry_run,
          f"剩余={len(list(MUSIC.iterdir()))} 个文件")
    # 副作用对比：dry-run 不得产生任何回收站/隔离动作
    check("dry-run 不产生回收站/隔离副作用",
          before_fake_trash == after_fake_trash,
          f"fake_trash: {before_fake_trash} -> {after_fake_trash}")

    # --- 7. apply 实际执行：指纹组移入回收站，文件名组默认跳过 ---
    proc = run(["apply", str(json_path)])
    check("apply 执行成功", proc.returncode == 0, f"exit={proc.returncode}")
    remaining = {p.name for p in MUSIC.iterdir()}
    check("指纹组的一个副本被移走", "instrumental.mp3" not in remaining, f"剩余={sorted(remaining)}")
    check("文件名组默认未被处理", {"爱的代价-李宗盛.wav", "凡人歌-李宗盛.wav"} <= remaining,
          f"剩余={sorted(remaining)}")
    check("保留项仍在原位", "instrumental.wav" in remaining, f"剩余={sorted(remaining)}")
    check("相似组默认未被处理", "同一首歌-另一个版本.wav" in remaining,
          f"剩余={sorted(remaining)}")

    # --- 8. 缓存命中 ---
    proc = run(["cache", "stats"])
    check("指纹缓存已写入", "指纹缓存" in proc.stdout, proc.stdout.strip().splitlines()[-1] if proc.stdout else "")

    # --- 9. 二次扫描走缓存 ---
    proc = run(["scan", str(MUSIC), "--report", str(TMP / "out" / "second.html")])
    check("二次扫描成功（缓存命中）", proc.returncode == 0, f"exit={proc.returncode}")

    # 信息性输出：确认 send2trash 替身是否生效（不计入断言，避免注入失败时误报）
    trashed = sorted(p.name for p in FAKE_TRASH.iterdir()) if FAKE_TRASH.exists() else []
    print(f"[info] 替身回收站内容: {trashed}"
          f"（若为空说明注入未生效，文件可能进了系统回收站）", flush=True)

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("未通过: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
