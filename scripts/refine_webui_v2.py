from pathlib import Path
import re

WEBUI_PATH = Path("webui.py")
INDEX_PATH = Path("templates/index.html")


def require(text, needle, label):
    if needle not in text:
        raise RuntimeError(f"未找到预期内容：{label}")
    return text


def patch_webui():
    text = WEBUI_PATH.read_text(encoding="utf-8")

    marker = '''def read_url_config():
    try:
        return Path(URL_CONFIG_FILE).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
'''
    require(text, marker, "read_url_config")

    replacement = marker + r'''

def write_url_config(content):
    path = Path(URL_CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        content.rstrip() + ("\n" if content.strip() else ""),
        encoding="utf-8-sig"
    )


def monitor_lines_raw():
    rows = []
    for raw in read_url_config().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        rows.append(line)
    return rows


def extract_anchor_name_from_logs(url):
    short = url.rstrip("/").rsplit("/", 1)[-1]

    patterns = (
        re.compile(r"主播\s*[:：]\s*([^,，|]+)"),
        re.compile(r"序号\d+\s+([^\s|]+)\s+(?:等待直播|正在录制|直播中)"),
    )

    for line in reversed(read_log_lines(800)):
        if url not in line and short not in line:
            continue

        for pattern in patterns:
            match = pattern.search(line)
            if match:
                name = match.group(1).strip()
                if name:
                    return name

    return ""
'''
    text = text.replace(marker, replacement, 1)

    old = '''        name_match = re.search(r"主播\\s*[:：]\\s*([^,，|]+)", line)
        name = name_match.group(1).strip() if name_match else "等待获取主播名"

        main_part = re.split(r"[,，]\\s*主播\\s*[:：]", line, maxsplit=1)[0].strip()
        url = main_part.split("|", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            continue

        items.append({
            "name": name,
'''

    new = '''        name_match = re.search(r"主播\\s*[:：]\\s*([^,，|]+)", line)

        main_part = re.split(r"[,，]\\s*主播\\s*[:：]", line, maxsplit=1)[0].strip()
        url = main_part.split("|", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            continue

        name = name_match.group(1).strip() if name_match else ""
        if not name:
            name = extract_anchor_name_from_logs(url) or "待识别主播"

        items.append({
            "name": name,
'''
    require(text, old, "parse_monitor_lines 名称解析")
    text = text.replace(old, new, 1)

    old_match = 'if not any(k and k != "等待获取主播名" and k in line for k in keys):'
    new_match = 'if not any(k and k not in ("等待获取主播名", "待识别主播") and k in line for k in keys):'
    require(text, old_match, "状态匹配条件")
    text = text.replace(old_match, new_match, 1)

    old_events = '''    event_words = ("直播", "录制", "推送", "上传", "失败", "异常", "WebUI")
    events = [x for x in logs if any(k in x for k in event_words)][-8:]
    events.reverse()
'''

    new_events = r'''    def summarize_event(line):
        ts = re.search(
            r"(20\d\d[-/]\d\d[-/]\d\d[ T](\d\d:\d\d:\d\d))",
            line
        )
        time_text = ts.group(2) if ts else ""

        anchor = ""
        anchor_match = re.search(
            r"(?:主播\s*[:：]\s*|序号\d+\s+)([^|,，\s]+)",
            line
        )
        if anchor_match:
            anchor = anchor_match.group(1).strip()

        if "正在录制" in line or "开始录制" in line:
            action = "开始录制"
        elif "直播已结束" in line or "关播" in line:
            action = "直播结束"
        elif "等待直播" in line:
            action = "等待直播"
        elif "上传" in line and any(k in line for k in ("成功", "完成")):
            action = "上传完成"
        elif "上传" in line and any(k in line for k in ("失败", "异常")):
            action = "上传失败"
        elif "推送" in line and any(k in line for k in ("成功", "完成")):
            action = "推送成功"
        elif "推送" in line and any(k in line for k in ("失败", "异常")):
            action = "推送失败"
        elif "直播中" in line or "已开播" in line:
            action = "检测到开播"
        elif "错误" in line or "异常" in line or "失败" in line:
            action = "检测异常"
        elif "WebUI启动" in line:
            action = "WebUI 已启动"
        else:
            return ""

        parts = [x for x in (time_text, anchor, action) if x]
        return " · ".join(parts)

    events = []
    seen = set()

    for line in reversed(logs):
        summary = summarize_event(line)

        if not summary or summary in seen:
            continue

        seen.add(summary)
        events.append(summary)

        if len(events) >= 8:
            break
'''
    require(text, old_events, "最近事件")
    text = text.replace(old_events, new_events, 1)

    old_route = '''@app.route("/url_config", methods=["GET", "POST"])
def url_config_page():
    path = Path(URL_CONFIG_FILE)
    if request.method == "POST":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.form.get("url_config_content", ""), encoding="utf-8-sig")
        return redirect(url_for("url_config_page", success="true"))
    return render_template(
        "index.html",
        active_tab="url_config",
        url_config_content=read_url_config(),
    )
'''

    new_route = r'''@app.route("/url_config", methods=["GET", "POST"])
def url_config_page():
    if request.method == "POST":
        write_url_config(
            request.form.get("url_config_content", "")
        )
        return redirect(
            url_for("url_config_page", success="true")
        )

    return render_template(
        "index.html",
        active_tab="url_config",
        url_config_content=read_url_config(),
        monitor_items=parse_monitor_lines(),
    )


@app.route("/anchors/add", methods=["POST"])
def add_anchor():
    url = request.form.get("anchor_url", "").strip()
    name = request.form.get("anchor_name", "").strip()

    if not url.startswith(("http://", "https://")):
        return redirect(
            url_for("url_config_page", error="invalid_url")
        )

    lines = monitor_lines_raw()
    existing_urls = []

    for line in lines:
        main_part = re.split(
            r"[,，]\s*主播\s*[:：]",
            line,
            maxsplit=1
        )[0].strip()

        existing_urls.append(
            main_part.split("|", 1)[0].strip()
        )

    if url not in existing_urls:
        line = url
        if name:
            line += f",主播: {name}"

        lines.append(line)
        write_url_config("\n".join(lines))

    return redirect(
        url_for("url_config_page", success="true")
    )


@app.route("/anchors/delete/<int:index>", methods=["POST"])
def delete_anchor(index):
    lines = monitor_lines_raw()

    if 0 <= index < len(lines):
        del lines[index]
        write_url_config("\n".join(lines))

    return redirect(
        url_for("url_config_page", success="true")
    )
'''
    require(text, old_route, "url_config route")
    text = text.replace(old_route, new_route, 1)

    WEBUI_PATH.write_text(text, encoding="utf-8")


def patch_index():
    text = INDEX_PATH.read_text(encoding="utf-8")

    text = re.sub(
        r'body\{margin:0;background:var\(--bg\);color:var\(--text\);font:[^}]+\}',
        'body{margin:0;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5}',
        text,
        count=1,
    )

    old_css = '@media(max-width:760px){.shell{padding:14px 12px 86px}'
    new_css = '@media(max-width:760px){body{padding-bottom:env(safe-area-inset-bottom)}.shell{padding:12px 12px 132px}'
    require(text, old_css, "移动端 CSS")
    text = text.replace(old_css, new_css, 1)

    text = text.replace(
        '.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:18px;margin-bottom:14px}',
        '.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:16px;margin-bottom:12px}',
        1,
    )

    text = text.replace(
        '.monitor{border:1px solid var(--line);border-radius:14px;padding:14px;display:grid;grid-template-columns:1fr auto;gap:8px}',
        '.monitor{border:1px solid var(--line);border-radius:14px;padding:12px;display:grid;grid-template-columns:1fr auto;gap:6px}',
        1,
    )

    text = text.replace(
        '.event{padding:10px 0;border-bottom:1px solid var(--line);font-size:13px;color:#4b5563;overflow-wrap:anywhere}',
        '.event{padding:8px 0;border-bottom:1px solid var(--line);font-size:13px;color:#4b5563;overflow-wrap:anywhere}',
        1,
    )

    style_marker = '.settings-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}'
    require(text, style_marker, "settings-grid CSS")

    extra_css = r'''.anchor-add{display:grid;grid-template-columns:1fr 160px auto;gap:8px;margin-bottom:14px}.anchor-add input{width:100%;min-height:44px;border:1px solid #cfd8e6;border-radius:10px;padding:10px 12px;background:#fff}.anchor-add button{border:0;border-radius:10px;background:var(--primary);color:#fff;padding:0 16px;font-weight:700}.anchor-row{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}.anchor-row:last-child{border-bottom:0}.anchor-row strong{display:block}.anchor-row .small{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.delete-btn{border:0;background:#fff1f2;color:#be123c;border-radius:9px;padding:8px 10px}.advanced{margin-top:14px}.advanced summary{cursor:pointer;color:var(--primary);font-weight:650}.advanced textarea{margin-top:10px}@media(max-width:760px){.anchor-add{grid-template-columns:1fr}.anchor-add button{min-height:44px}}'''

    text = text.replace(
        style_marker,
        extra_css + style_marker,
        1
    )

    old_section = '''  {% elif active_tab == 'url_config' %}
  <section class="card">
    <h2>主播与直播地址</h2>
    <p class="hint">每行一个地址。当前核心已支持抖音直播间链接、分享短链，以及可被 streamget 解析的主播主页链接。保存后主程序会继续循环监测。</p>
    <form method="post">
      <div class="form-group"><textarea name="url_config_content" placeholder="每行一个直播地址">{{ url_config_content }}</textarea></div>
      <button class="btn" type="submit">保存主播列表</button>
    </form>
  </section>
'''

    new_section = r'''  {% elif active_tab == 'url_config' %}
  <section class="card">
    <h2>添加主播</h2>
    <p class="hint">支持直播间链接、抖音分享短链，以及当前核心可解析的主播主页链接。主播名可以留空。</p>

    <form
      class="anchor-add"
      method="post"
      action="{{ url_for('add_anchor') }}"
    >
      <input
        type="text"
        name="anchor_url"
        placeholder="粘贴主播主页 / 分享短链 / 直播间链接"
        required
      >
      <input
        type="text"
        name="anchor_name"
        placeholder="主播名（可选）"
      >
      <button type="submit">添加</button>
    </form>
  </section>

  <section class="card">
    <h2>已添加主播</h2>

    {% if monitor_items %}
      {% for item in monitor_items %}
      <div class="anchor-row">
        <div>
          <strong>{{ item.name }}</strong>
          <div class="small">{{ item.platform }} · {{ item.url }}</div>
        </div>

        <form
          method="post"
          action="{{ url_for('delete_anchor', index=loop.index0) }}"
          onsubmit="return confirm('确定删除这个主播吗？')"
        >
          <button class="delete-btn" type="submit">删除</button>
        </form>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">还没有添加主播</div>
    {% endif %}

    <details class="advanced">
      <summary>高级编辑原始配置</summary>

      <form method="post">
        <div class="form-group">
          <textarea name="url_config_content">{{ url_config_content }}</textarea>
        </div>
        <button class="btn" type="submit">保存原始配置</button>
      </form>
    </details>
  </section>
'''
    require(text, old_section, "主播页面")
    text = text.replace(old_section, new_section, 1)

    old_js = '''box.innerHTML=d.monitors.length?d.monitors.map(x=>`<div class="monitor"><div><div class="monitor-name">${esc(x.name)}</div><div class="meta">${esc(x.platform)} · ${esc(x.url)}</div><div class="meta">最近状态：${esc(x.updated_at)}</div></div><span class="badge ${esc(x.status)}">${esc(x.status_text)}</span></div>`).join(''):'<div class="empty">还没有添加主播，请到“主播”页面添加地址。</div>';'''

    new_js = '''box.innerHTML=d.monitors.length?d.monitors.map(x=>`<div class="monitor"><div><div class="monitor-name">${esc(x.name)}</div><div class="meta">${esc(x.platform)} · ${esc(x.url)}</div>${x.updated_at&&x.updated_at!=='--'?`<div class="meta">更新：${esc(x.updated_at.slice(11))}</div>`:''}</div><span class="badge ${esc(x.status)}">${esc(x.status_text)}</span></div>`).join(''):'<div class="empty">还没有添加主播，请到“主播”页面添加地址。</div>';'''

    require(text, old_js, "首页主播卡片 JS")
    text = text.replace(old_js, new_js, 1)

    INDEX_PATH.write_text(text, encoding="utf-8")


def main():
    if not WEBUI_PATH.exists() or not INDEX_PATH.exists():
        raise SystemExit("请在项目仓库根目录运行。")

    patch_webui()
    patch_index()

    print("WebUI V2 精修完成：")
    print("- 修复移动端底部导航遮挡")
    print("- 压缩卡片与事件列表")
    print("- 最近事件改为摘要")
    print("- 主播名从日志自动尝试补全")
    print("- 主播页改为添加 + 列表 + 删除")
    print("- 保留高级原始配置编辑")
    print("- 未修改 main.py 和录制核心")
    print("- 未强制指定字体，继续跟随设备字体")


if __name__ == "__main__":
    main()
