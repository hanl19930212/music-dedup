"""HTML 报告生成模块。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Template

from .dedup import DuplicateGroup, TrackItem
from .utils import human_duration, human_size


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>音乐重复检测报告</title>
<style>
  :root {
    --bg: #1a1a2e;
    --card: #16213e;
    --card-hover: #0f3460;
    --accent: #e94560;
    --keep: #4ecca3;
    --text: #eaeaea;
    --muted: #8892b0;
    --border: #2d3748;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 20px;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  header {
    background: linear-gradient(135deg, #e94560, #0f3460);
    padding: 30px;
    border-radius: 12px;
    margin-bottom: 24px;
  }
  header h1 { font-size: 28px; margin-bottom: 8px; }
  header .subtitle { color: rgba(255,255,255,0.8); font-size: 14px; }
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat-card {
    background: var(--card);
    padding: 20px;
    border-radius: 10px;
    border-left: 4px solid var(--accent);
  }
  .stat-card.keep { border-left-color: var(--keep); }
  .stat-value { font-size: 32px; font-weight: 700; }
  .stat-label { color: var(--muted); font-size: 13px; margin-top: 4px; }
  .filter-bar {
    background: var(--card);
    padding: 16px;
    border-radius: 10px;
    margin-bottom: 20px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .filter-bar input, .filter-bar select {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 14px;
  }
  .group {
    background: var(--card);
    border-radius: 10px;
    margin-bottom: 16px;
    overflow: hidden;
    transition: background 0.2s;
  }
  .group:hover { background: var(--card-hover); }
  .group-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
  }
  .group-title { font-size: 18px; font-weight: 600; }
  .group-artist { color: var(--muted); font-size: 14px; margin-left: 8px; }
  .group-meta { font-size: 13px; color: var(--muted); }
  .match-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    margin-left: 10px;
  }
  .match-recording_id { background: #4ecca3; color: #000; }
  .match-fingerprint { background: #e94560; color: #fff; }
  .match-name { background: #f0a500; color: #000; }
  .review-tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    background: #f0a500;
    color: #000;
    font-size: 11px;
    font-weight: 700;
    margin-left: 6px;
  }
  .files { padding: 0; }
  .file-row {
    display: grid;
    grid-template-columns: 1fr 100px 120px 80px;
    gap: 12px;
    padding: 12px 20px;
    border-bottom: 1px solid rgba(45,55,72,0.5);
    align-items: center;
  }
  .file-row:last-child { border-bottom: none; }
  .file-row.keep { background: rgba(78, 204, 163, 0.08); }
  .file-name {
    font-size: 14px;
    word-break: break-all;
  }
  .file-path { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .file-ext {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    background: #2d3748;
    text-transform: uppercase;
  }
  .ext-dsf { background: #9b59b6; }
  .ext-wav { background: #3498db; }
  .ext-flac { background: #1abc9c; }
  .ext-mp3 { background: #e67e22; }
  .file-size { font-size: 13px; text-align: right; }
  .keep-tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--keep);
    color: #000;
    font-size: 11px;
    font-weight: 700;
  }
  .delete-tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--accent);
    color: #fff;
    font-size: 11px;
    font-weight: 700;
  }
  footer {
    text-align: center;
    color: var(--muted);
    font-size: 13px;
    margin-top: 30px;
    padding: 20px;
  }
  .apply-hint {
    background: var(--card);
    border: 1px dashed var(--accent);
    padding: 16px;
    border-radius: 10px;
    margin-bottom: 20px;
    font-size: 14px;
  }
  .apply-hint code {
    background: var(--bg);
    padding: 2px 8px;
    border-radius: 4px;
    color: var(--accent);
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🎵 音乐重复检测报告</h1>
    <div class="subtitle">生成时间：{{ generated_at }}</div>
  </header>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-value">{{ total_files }}</div>
      <div class="stat-label">扫描文件总数</div>
    </div>
    <div class="stat-card keep">
      <div class="stat-value">{{ duplicate_groups }}</div>
      <div class="stat-label">重复组数</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{{ total_duplicates }}</div>
      <div class="stat-label">可删除文件数</div>
    </div>
    <div class="stat-card keep">
      <div class="stat-value">{{ wasted_size }}</div>
      <div class="stat-label">可释放空间</div>
    </div>
  </div>

  <div class="apply-hint">
    确认无误后执行：<code>music-dedup apply "{{ report_path }}"</code><br>
    这会将所有标记为"待删除"的文件移入回收站（保留每组中质量最高的一个）。<br>
    <strong>注意：仅凭文件名匹配（黄色标签、带 ⚠ 标记）的组默认不会删除，需人工确认后加
    <code>--include-name-matches</code> 才会处理。</strong>
  </div>

  <div class="filter-bar">
    <input type="text" id="search" placeholder="搜索文件名/标题/艺术家...">
    <select id="matchType">
      <option value="">全部匹配类型</option>
      <option value="recording_id">Recording ID（最精确）</option>
      <option value="fingerprint">音频指纹</option>
      <option value="name">文件名</option>
    </select>
    <select id="format">
      <option value="">全部格式</option>
      <option value="dsf">DSF</option>
      <option value="wav">WAV</option>
      <option value="flac">FLAC</option>
      <option value="mp3">MP3</option>
    </select>
  </div>

  <div id="groups">
    {% for group in groups %}
    <div class="group" data-match="{{ group.match_type }}" data-formats="{{ group.formats }}">
      <div class="group-header" onclick="this.parentElement.querySelector('.files').classList.toggle('hidden')">
        <div>
          <span class="group-title">{{ group.title }}</span>
          {% if group.artist %}<span class="group-artist">{{ group.artist }}</span>{% endif %}
          <span class="match-badge match-{{ group.match_type }}">{{ group.match_label }}</span>
          {% if group.needs_review %}<span class="review-tag">⚠ 建议人工确认</span>{% endif %}
        </div>
        <div class="group-meta">{{ group.count }} 个文件 · 可释放 {{ group.wasted_size }}</div>
      </div>
      <div class="files">
        {% for file in group.files %}
        <div class="file-row {% if file.is_keep %}keep{% endif %}">
          <div>
            <div class="file-name">
              {% if file.is_keep %}<span class="keep-tag">保留</span>{% else %}<span class="delete-tag">删除</span>{% endif %}
              {{ file.name }}
            </div>
            <div class="file-path">{{ file.path }}</div>
          </div>
          <div><span class="file-ext ext-{{ file.ext }}">{{ file.ext }}</span></div>
          <div class="file-size">{{ file.size }}{% if file.duration %} · {{ file.duration }}{% endif %}</div>
          <div style="text-align:right;">{% if file.is_keep %}✓{% endif %}</div>
        </div>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </div>

  <footer>
    music-dedup · 音乐重复检测与元数据刮削工具
  </footer>
</div>
<script>
function applyFilters() {
  const search = document.getElementById('search').value.toLowerCase();
  const matchType = document.getElementById('matchType').value;
  const format = document.getElementById('format').value;
  document.querySelectorAll('.group').forEach(group => {
    const text = group.textContent.toLowerCase();
    const match = !matchType || group.dataset.match === matchType;
    const fmt = !format || group.dataset.formats.split(',').includes(format);
    const ok = (!search || text.includes(search)) && match && fmt;
    group.style.display = ok ? '' : 'none';
  });
}
document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('matchType').addEventListener('change', applyFilters);
document.getElementById('format').addEventListener('change', applyFilters);
</script>
</body>
</html>"""


def generate_report(
    groups: list[DuplicateGroup],
    total_files: int,
    report_path: Path,
) -> Path:
    """生成 HTML 重复检测报告。"""
    match_labels = {
        "recording_id": "Recording ID",
        "fingerprint": "音频指纹",
        "name": "文件名",
    }

    rendered_groups = []
    total_duplicates = 0
    total_wasted = 0

    for group in groups:
        files = []
        formats = set()
        for item in group.items:
            is_keep = item is group.keep
            files.append({
                "name": item.path.name,
                "path": str(item.path),
                "ext": item.ext,
                "size": human_size(item.size),
                "duration": human_duration(item.duration or item.metadata.duration or 0),
                "is_keep": is_keep,
            })
            formats.add(item.ext)
            if not is_keep:
                total_duplicates += 1

        total_wasted += group.wasted_size
        rendered_groups.append({
            "title": group.display_title,
            "artist": group.display_artist,
            "match_type": group.match_type,
            "match_label": match_labels.get(group.match_type, group.match_type),
            "needs_review": group.match_type == "name",
            "count": len(group.items),
            "wasted_size": human_size(group.wasted_size),
            "files": files,
            "formats": ",".join(sorted(formats)),
        })

    template = Template(HTML_TEMPLATE)
    html = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_files=total_files,
        duplicate_groups=len(groups),
        total_duplicates=total_duplicates,
        wasted_size=human_size(total_wasted),
        groups=rendered_groups,
        report_path=str(report_path),
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html, encoding="utf-8")
    return report_path
