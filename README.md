# music-dedup

Windows 音乐重复文件检测与元数据刮削工具。

基于 **音频指纹**（Chromaprint / fpcalc）对本地目录与**局域网共享**中的音乐文件进行精确去重，
并可通过 AcoustID + MusicBrainz + iTunes 刮削标题、艺术家、专辑、流派、年份与封面，
将结果写回文件标签（含备份与自动回滚）。

- 支持格式：`dsf` / `dff` / `wav` / `aif` / `aiff` / `flac` / `wma` / `mp3`
- 支持路径：本地目录、映射网络盘、UNC 网络共享（`\\server\share`）
- 删除行为：**默认移入回收站**，可恢复，不做永久删除
- 交付形态：Python 安装包 + 单文件 `music-dedup.exe`（内置 ffmpeg / fpcalc，免环境）

---

## 目录

- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令详解](#命令详解)
- [配置文件](#配置文件)
- [打包为单文件 exe](#打包为单文件-exe)
- [项目结构](#项目结构)
- [安全设计](#安全设计)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 核心特性

### 分层去重（精度由高到低）

去重算法按四层优先级分组，一层命中后文件即被"占用"，不会被更低精度层重复处理：

| 层级 | 匹配类型 | 依据 | 说明 |
| --- | --- | --- | --- |
| 1 | `recording_id` | MusicBrainz Recording ID | 最精确，经刮削后得到 |
| 2 | `fingerprint` | fpcalc 指纹完全相同 | 内容级精确匹配 |
| 3 | `fingerprint_similar` | 指纹相似度 ≥ `min_similarity` | 同一录音的不同编码/不同发行版本，**默认不删除** |
| 4 | `name` | 文件名规范化匹配 | **兜底**，可能误判，默认不删除 |

> 层级 3、4 都可能误判（同一首歌的重制版、现场版），`apply` 默认跳过，
> 需要人工复核后显式加 `--include-similar-matches` / `--include-name-matches`。
>
> 阈值 `min_similarity` 的取值依据（本地 514 个文件 + NAS 1634 个文件的实测）：
> 同一录音的不同文件 0.83~1.00，同一首歌的现场版 0.50~0.67，完全不同的歌 0.55。
> 默认取 0.83，正好落在"同一录音"与"不同录音"之间的空档里。

> `fingerprint_duration_tolerance` 只是候选粗筛：同一录音的不同发行版本
> 常因前后静音不同差出十几秒（实测最大 26 秒），真正把关的是相似度阈值。

### 智能保留策略

每组重复文件自动推荐一个保留项，排序键为：

```
格式优先级（dff > dsf > wav > aif/aiff > flac > wma > mp3） > 文件体积（大者优先） > 修改时间（新者优先）
```

### 网络共享支持

- 自动将 `/` 归一为 `\`，压缩多余分隔符，校验 UNC 结构（必须含 server 与 share）
- 跨多个入口扫描时按绝对路径去重，避免同一文件被重复计入
- 网络不可达时给出可操作的排查提示（主机不可达 / 共享名错误 / 权限不足 / SMB 未启用）

### 元数据刮削

- **AcoustID**：指纹 → MusicBrainz Recording ID（支持置信度）
- **MusicBrainz**：Recording ID → 标题 / 艺术家 / 专辑 / 流派 / 年份（遵循 1 req/s 速率限制）
- **iTunes Search API**：按标题 + 艺术家获取封面，并替换为指定尺寸
- 指纹与元数据结果均走 **SQLite 缓存**，二次扫描秒级完成

### 指纹是怎么比对的

fpcalc 有两种输出：

- 默认输出是**压缩格式**（base64 编码的变长字节流，开头是 1 字节版本号 + 3 字节项数），
  这是 AcoustID 提交用的格式，**不能**按 4 字节切开当 32 位整数来比较。
- `fpcalc -raw` 输出的是**未压缩格式**：每个采样点一个 32 位整数，这才是能逐项比较的。

本项目去重时用 `-raw`，AcoustID 查询时另外按需取一份压缩格式，两者不互相依赖。
旧版本缓存里存的是压缩格式指纹，升级后会自动清空并重算。

### 标签写回与安全

- 按格式分发写入：mp3(ID3) / flac(Vorbis + Picture) / wav(ID3) / dsf(ID3) / wma(ASF) / aiff / dff
- 写入前**自动备份**原文件到 `backup_dir`；写入失败**自动回滚**
- 支持 `--dry-run` 先预览再执行

---

## 工作原理

```
扫描目录 ──► 提取指纹(fpcalc + 缓存) ──► [可选]刮削元数据 ──► 分层分组 ──► HTML 报告 ──► (可选)移入回收站
```

1. **scan**：递归扫描所有目标目录，过滤扩展名与排除目录，收集文件。
2. **指纹**：对未命中缓存的文件并发调用 fpcalc 提取指纹，结果写入缓存。
3. **刮削（可选）**：经 AcoustID → MusicBrainz → iTunes 补全元数据。
4. **分组**：按 recording_id → fingerprint → fingerprint_similar → name 四层归组，选出保留项。
5. **报告**：生成自包含的 HTML 报告（含过滤、搜索、统计）与配套 JSON。
6. **去重**：读取 JSON，将每组中非保留文件移入回收站。

---

## 安装

### 前置：外部工具

去重依赖 **fpcalc**（Chromaprint CLI），fpcalc 解码音频依赖 **ffmpeg**。

1. **fpcalc**：从 <https://acoustid.org/chromaprint> 下载对应平台的可执行文件。
2. **ffmpeg**：从 <https://www.gyan.dev/ffmpeg/builds/> 下载，取其中 `ffmpeg.exe`。

将二者放入项目根的 `bin/` 目录（或加入系统 `PATH`），随后在配置文件中按需指定路径。

> 官方发布的 Windows 版 fpcalc 已内置 FFmpeg 解码库，单独也能解 wma / flac / dsf 等格式；
> 只有自编译或精简版才需要额外准备 `ffmpeg.exe`。

### 从源码安装

需要 Python **3.11+**。

```bash
git clone <your-repo-url> music-dedup
cd music-dedup
pip install -e .
```

安装完成后即可使用 `music-dedup` 命令。

---

## 快速开始

```bash
# 1. 生成默认配置文件（位于 ~/.music-dedup/config.toml）
music-dedup init-config

# 2. 扫描本地目录 + 局域网共享，生成 HTML 报告
music-dedup scan "D:\Music" "\\192.168.1.100\share\music"

# 3. 预览报告后执行去重（移入回收站）
music-dedup apply report.json --dry-run     # 先预览
music-dedup apply report.json               # 确认无误后执行
```

扫描与刮削一体化（需要 AcoustID API key）：

```bash
music-dedup scan "D:\Music" --scrape --report report.html
```

---

## 命令详解

### `music-dedup scan`

扫描目录，检测重复文件并生成 HTML 报告。

```
music-dedup scan [DIRECTORIES...] [OPTIONS]

参数:
  DIRECTORIES                要扫描的目录（可多个，支持 UNC 路径）

选项:
  -r, --report PATH          HTML 报告输出路径（默认 report.html）
      --apply                扫描后直接执行去重（移入回收站）
      --scrape               扫描时同时刮削元数据
      --log-level TEXT       日志级别（默认 INFO）
```

- 未指定目录时，回退到配置文件中的 `scan.scan_dirs`。
- 扫描前会**前置检测 fpcalc**，不可用则直接报错退出（避免指纹全部降级为文件名匹配）。
- 会在报告同路径生成同名 `.json`，供 `apply` 命令读取。

### `music-dedup apply`

根据扫描结果执行去重。

```
music-dedup apply REPORT_JSON [OPTIONS]

参数:
  REPORT_JSON                scan 生成的 JSON 数据文件（必填，需存在）

选项:
    --dry-run              仅预览，不实际移动
    --include-name-matches 同时删除仅凭文件名匹配的组（默认跳过，避免误删）
    --include-similar-matches 同时删除仅凭指纹相似匹配的组（默认跳过，避免误删重制版）
```

默认**跳过** `match_type == "name"` 和 `match_type == "fingerprint_similar"` 的组，
并在结果中提示跳过数量。

### `music-dedup scrape`

单独刮削元数据（标题 / 艺术家 / 专辑 / 封面等）。

```
music-dedup scrape [DIRECTORIES...] [OPTIONS]

选项:
      --write-tags           将刮削结果写回文件标签
      --fetch-cover          下载并嵌入封面
      --dry-run / --execute  预览或执行（默认预览）
```

需要先在配置文件中填写 `scrape.acoustid_api_key`。

### `music-dedup cache`

指纹 / 元数据缓存管理。

```bash
music-dedup cache stats     # 查看缓存条目数
music-dedup cache clear     # 清空缓存
```

### `music-dedup init-config`

生成带注释的默认配置文件，仅当文件不存在时创建。

---

## 配置文件

默认路径：`~/.music-dedup/config.toml`，可用环境变量 `MUSIC_DEDUP_CONFIG` 覆盖。

```toml
# 缓存与日志（必须写在所有 [段] 之前，否则会被当成段内的键而读不到）
cache_file = "~/.music-dedup/cache.db"
log_dir     = "~/.music-dedup/logs"
backup_dir  = "~/.music-dedup/backups"

# 扫描相关
[scan]
extensions = ["dsf", "dff", "wav", "aif", "aiff", "flac", "wma", "mp3"]  # 支持的音频格式
exclude_dirs = ["@eaDir", ".Trash", "System Volume Information", "$RECYCLE.BIN"]
scan_dirs = []                    # 预定义扫描目录，支持本地与 UNC 路径

# 音频指纹
[fingerprint]
duration = 120                    # 指纹采样时长（秒）
workers = 4                       # 并发进程数
fpcalc_path = "fpcalc"            # fpcalc 可执行文件路径
ffmpeg_path = ""                  # ffmpeg 路径（或所在目录），留空则用系统 PATH

# 去重
[dedup]
min_similarity = 0.83             # 指纹相似度阈值（>= 1.0 表示关闭相似匹配）
name_fallback = true              # 指纹失败时是否用文件名兜底
fingerprint_duration_tolerance = 30.0  # 指纹相似比较允许的时长差（秒），仅作候选粗筛
quarantine_dir = "_music_duplicates_quarantine"  # 回收站不可用时的兜底隔离目录
fallback_to_quarantine = true     # 移入回收站失败时改用隔离目录

# 元数据刮削
[scrape]
acoustid_api_key = ""             # https://acoustid.org/api-key 免费申请
musicbrainz_user_agent = "music-dedup/0.1 (https://github.com/music-dedup)"
fetch_cover = true
cover_size = 600
rate_limit_delay = 1.0            # MusicBrainz 请求间隔（秒）
```

配置优先级：**CLI 参数 > 环境变量 > 配置文件 > 默认值**。

---

## 打包为单文件 exe

目标：产出一个自带 ffmpeg / fpcalc 的 `music-dedup.exe`，目标机器**无需安装 Python 或任何外部工具**。

### 步骤

1. 将 `ffmpeg.exe` 与 `fpcalc.exe` 放入项目根的 `bin/` 目录。
2. 在 Windows 上执行一键脚本：

```bat
build_windows.bat
```

脚本会依次安装 `pyinstaller` 依赖、以 editable 模式安装本项目、然后按
[packaging/music-dedup.spec](packaging/music-dedup.spec) 打包。

3. 产物位于 `dist/music-dedup.exe`，直接运行即可。

打包运行时，[config.py](src/music_dedup/config.py) 的 `_resolve_frozen_paths` 会自动定位解包
目录中的 `bin/`，将其注入 `PATH` 并设置 `fpcalc_path` / `ffmpeg_path`。

> 打包默认关闭 UPX（`upx=False`）以降低杀毒软件误报概率。

---

## 项目结构

```
music-dedup/
├── build_windows.bat            # Windows 一键打包脚本
├── pyproject.toml               # 项目元数据与依赖
├── README.md
├── packaging/
│   └── music-dedup.spec         # PyInstaller 打包配置
└── src/music_dedup/
    ├── __init__.py
    ├── cli.py                   # CLI 入口（scan / apply / scrape / cache / init-config）
    ├── config.py                # 配置加载、PyInstaller 路径解析
    ├── scanner.py               # 目录扫描、UNC 网络路径处理
    ├── fingerprint.py           # fpcalc 指纹提取、前置检测、缓存
    ├── metadata.py              # mutagen 读取标签
    ├── scraper.py               # AcoustID + MusicBrainz + iTunes 刮削
    ├── cover.py                 # 封面下载与 MIME 识别
    ├── tagwriter.py             # 标签写回（备份 / 回滚）
    ├── dedup.py                 # 分层分组算法
    ├── action.py                # 移入回收站
    ├── report.py                # HTML 报告生成
    ├── db.py                    # SQLite 缓存
    ├── utils.py                 # 文件名规范化、日志、格式化
└── tests/
│   ├── test_e2e.py              # 端到端冒烟测试（用 fpcalc 桩，不联网）
│   ├── test_units.py            # 单元测试（归一化、分组、相似度、缓存等）
│   └── stub/                    # fpcalc 桩：-version / -raw -json 确定性指纹
```

### 运行测试

```bash
pip install -e .
python tests/test_e2e.py
python tests/test_units.py
```

测试用 `tests/stub/fpcalc.bat` 代替真实的 fpcalc，不联网、不触碰真实音乐库，
覆盖扫描 → 指纹 → 分组 → 报告 → 预览 → 移入回收站 → 缓存命中的完整链路。
`test_units.py` 覆盖文件名归一化、分层分组、指纹相似度（含起点错位）、
占位标签与现场版标记、旧缓存格式迁移、回收站兜底等单元。

---

## 安全设计

- **移入回收站，而非永久删除**：所有删除通过 `send2trash` 完成，可在系统回收站恢复。
- **回收站不可用时改隔离**：UNC 网络共享上 Windows 回收站 API 不可用，此时自动改为
  把文件移入 `quarantine_dir`，并在结果里单独标出，不会静默丢失文件。
- **把握度不足的匹配默认不删除**：`match_type` 为 `name`（可能同名不同曲）或
  `fingerprint_similar`（可能是重制版、现场版）的组，`apply` 默认跳过，
  需人工确认后再加 `--include-name-matches` / `--include-similar-matches`。
- **fpcalc 前置检测**：扫描前校验 fpcalc 可用性，避免指纹全部失败后静默降级为文件名匹配（进而误删）。
- **写入即备份、失败即回滚**：标签写回前备份原文件，异常时自动还原。
- **干跑模式**：`--dry-run` 可在实际改动前完整预览。
- **凭据不入库**：`.gitignore` 排除 `bin/*`、`*.local.toml`、日志与缓存，避免 API key 与二进制被提交。

---

## 常见问题

**Q：报错"fpcalc 不可用"？**
确认已下载 fpcalc 并放入 `bin/` 或 `PATH`，也可在配置 `fingerprint.fpcalc_path` 指定完整路径。

**Q：fpcalc 能运行但提取指纹失败？**
多为缺少 ffmpeg 解码器。设置 `fingerprint.ffmpeg_path`，或将 `ffmpeg.exe` 与 `fpcalc.exe` 放在同一目录。
个别格式（如 `dff`）fpcalc 本身就解不了，这类文件会退回到文件名匹配，扫描日志里会逐个告警。

**Q：升级后第一次扫描很慢？**
旧版本缓存里存的是 fpcalc 的压缩格式指纹，无法逐项比较，升级后会自动清空重算，
之后就走缓存了。

**Q：扫描 UNC 共享时报"无法访问网络共享"？**
依次排查：目标主机是否在线、共享名是否正确、凭据是否有读取权限、目标 Windows 是否开启"文件和打印机共享"。

**Q：为什么有些组显示建议人工确认？**
靠文件名兜底匹配的组（`match_type == "name"`，如同名不同曲）以及仅靠指纹相似匹配的组
（`match_type == "fingerprint_similar"`，如重制版）都只是提示，默认不会删除，
确认无误后再加对应开关执行。

**Q：扫描很慢？**
指纹结果与元数据都会写 SQLite 缓存，二次运行仅处理新增/变更文件。可适当调大 `fingerprint.workers`。

**Q：MusicBrainz 返回 403 / 被限流？**
请保持 `rate_limit_delay >= 1.0`，并将 `musicbrainz_user_agent` 修改为包含你联系方式的自定义 UA。

---

## 许可证

本项目未附带许可证文件。在发布或分发前，请根据你的需求添加合适的开源许可证（如 MIT）。
