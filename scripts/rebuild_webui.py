from pathlib import Path

WEBUI = r"""from flask import Flask, render_template, request, redirect, url_for, jsonify
import configparser
import os
import re
import subprocess
from datetime import datetime
from urllib.parse import urlparse

import requests
from streamget.logger import logger

app = Flask(__name__)

CONFIG_FILE = "config/config.ini"
URL_CONFIG_FILE = "config/URL_config.ini"
LOG_FILES = (
    "logs/streamget.log",
    "logs/PlayURL.log",
)

recording_process = None


def read_config(file_path):
    config = configparser.ConfigParser(interpolation=None)
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
        if content.strip() and not content.strip().startswith("["):
            config.read_string("[DEFAULT]\\n" + content)
        else:
            config.read(file_path, encoding="utf-8-sig")
    except FileNotFoundError:
        pass
    return config


def write_config(config, file_path):
    with open(file_path, "w", encoding="utf-8-sig") as f:
        config.write(f)


def read_url_config():
    try:
        return Path(URL_CONFIG_FILE).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


def read_log_lines(limit=300):
    lines = []
    for name in LOG_FILES:
        path = Path(name)
        if not path.exists():
            continue
        try:
            part = path.read_text(encoding="utf-8", errors="replace").splitlines()
            lines.extend(part[-limit:])
        except OSError:
            continue
    return lines[-limit:]


def platform_from_url(url):
    host = urlparse(url).netloc.lower()
    if "douyin.com" in host:
        return "抖音"
    if "tiktok.com" in host:
        return "TikTok"
    if "kuaishou.com" in host:
        return "快手"
    if "huya.com" in host:
        return "虎牙"
    if "douyu.com" in host:
        return "斗鱼"
    if "bilibili.com" in host or "b23.tv" in host:
        return "B站"
    return "直播"


def parse_monitor_lines():
    items = []
    for raw in read_url_config().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        name_match = re.search(r"主播\s*[:：]\s*([^,，|]+)", line)
        name = name_match.group(1).strip() if name_match else "等待获取主播名"

        main_part = re.split(r"[,，]\s*主播\s*[:：]", line, maxsplit=1)[0].strip()
        url = main_part.split("|", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            continue

        items.append({
            "name": name,
            "url": url,
            "platform": platform_from_url(url),
            "status": "waiting",
            "status_text": "等待直播",
            "updated_at": "--",
        })
    return items


def classify_status(text):
    lower = text.lower()
    if any(k in text for k in ("正在录制", "开始录制", "录制中")):
        return "recording", "正在录制"
    if any(k in text for k in ("检测到", "开始直播", "已开播", "直播中")):
        return "live", "直播中"
    if any(k in text for k in ("等待直播", "未开播", "未直播")):
        return "waiting", "等待直播"
    if any(k in text for k in ("获取失败", "连接失败", "错误信息", "异常")) or "error" in lower:
        return "error", "异常"
    return None


def get_monitor_snapshot():
    items = parse_monitor_lines()
    logs = read_log_lines(500)

    for item in items:
        keys = [item["name"], item["url"]]
        for line in reversed(logs):
            if not any(k and k != "等待获取主播名" and k in line for k in keys):
                continue
            state = classify_status(line)
            if state:
                item["status"], item["status_text"] = state
                ts = re.search(r"(20\\d\\d[-/]\\d\\d[-/]\\d\\d[ T]\\d\\d:\\d\\d:\\d\\d)", line)
                if ts:
                    item["updated_at"] = ts.group(1).replace("/", "-")
                break

    counts = {
        "total": len(items),
        "waiting": sum(i["status"] == "waiting" for i in items),
        "live": sum(i["status"] == "live" for i in items),
        "recording": sum(i["status"] == "recording" for i in items),
        "error": sum(i["status"] == "error" for i in items),
    }

    event_words = ("直播", "录制", "推送", "上传", "失败", "异常", "WebUI")
    events = [x for x in logs if any(k in x for k in event_words)][-8:]
    events.reverse()

    return {
        "service_running": recording_process is not None and recording_process.poll() is None,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "monitors": items,
        "events": events,
    }


@app.route("/")
def index():
    return redirect(url_for("home_page"))


@app.route("/home")
def home_page():
    return render_template("index.html", active_tab="home")


@app.route("/settings")
def settings_page():
    return render_template("index.html", active_tab="settings")


@app.route("/api/status")
def api_status():
    return jsonify(get_monitor_snapshot())


def start_main_recording():
    global recording_process
    if recording_process is None or recording_process.poll() is not None:
        try:
            logger.info("WebUI启动时自动开始录制...")
            recording_process = subprocess.Popen(["python", "main.py"], cwd=os.getcwd())
        except Exception as exc:
            logger.error(f"自动启动录制失败: {exc}")


def run_with_auto_record():
    start_main_recording()
    app.run(host="0.0.0.0", port=5000)


@app.route("/url_config", methods=["GET", "POST"])
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


def config_page(section, endpoint, active_tab):
    config = read_config(CONFIG_FILE)
    if request.method == "POST":
        if section not in config:
            config.add_section(section)
        for key, value in request.form.items():
            if key == "button_clicked":
                continue
            config.set(section, key, value)
        write_config(config, CONFIG_FILE)
        return redirect(url_for(endpoint, success="true"))
    return render_template(
        "index.html",
        config=config,
        active_tab=active_tab,
        section=section,
    )


@app.route("/recording_settings", methods=["GET", "POST"])
def recording_settings_page():
    return config_page("录制设置", "recording_settings_page", "recording_settings")


@app.route("/push_settings", methods=["GET", "POST"])
def push_settings_page():
    return config_page("推送配置", "push_settings_page", "push_settings")


@app.route("/cookie_settings", methods=["GET", "POST"])
def cookie_settings_page():
    return config_page("Cookie", "cookie_settings_page", "cookie_settings")


@app.route("/account_settings", methods=["GET", "POST"])
def account_settings_page():
    return config_page("账号密码", "account_settings_page", "account_settings")


@app.route("/xiaolan_webdav", methods=["GET", "POST"])
def xiaolan_webdav_page():
    return config_page("小蓝网盘", "xiaolan_webdav_page", "xiaolan_webdav")


@app.route("/xiaolan_webdav/test", methods=["POST"])
def test_xiaolan_webdav():
    config = read_config(CONFIG_FILE)
    if "小蓝网盘" not in config:
        return jsonify(success=False, message="请先保存 WebDAV 配置"), 400

    url = config.get("小蓝网盘", "WebDAV地址", fallback="").strip()
    username = config.get("小蓝网盘", "WebDAV用户名", fallback="").strip()
    password = config.get("小蓝网盘", "WebDAV密码", fallback="")

    if not url.startswith(("http://", "https://")):
        return jsonify(success=False, message="WebDAV 地址为空或格式错误"), 400

    try:
        response = requests.request(
            "PROPFIND",
            url,
            auth=(username, password),
            headers={"Depth": "0", "User-Agent": "DouyinLiveRecorder-WebDAV"},
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code in (200, 201, 204, 207):
            return jsonify(success=True, message="小蓝网盘 WebDAV 连接成功")

        messages = {
            401: "认证失败，请检查用户名和密码",
            403: "访问被拒绝，请检查权限",
            404: "WebDAV 地址不存在",
        }
        message = messages.get(response.status_code, f"连接失败 HTTP {response.status_code}")
        return jsonify(success=False, message=message), 400
    except requests.exceptions.Timeout:
        return jsonify(success=False, message="连接超时"), 400
    except requests.exceptions.RequestException as exc:
        logger.error(f"WebDAV连接失败: {exc}")
        return jsonify(success=False, message="无法连接 WebDAV 服务器"), 400


@app.route("/log")
def get_log():
    return "\\n".join(read_log_lines(100))


if __name__ == "__main__":
    run_with_auto_record()
"""

INDEX = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DouyinLiveRecorder</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--text:#172033;--muted:#718096;--line:#e7ecf3;--primary:#2563eb;--green:#16a34a;--orange:#f59e0b;--red:#dc2626;--purple:#7c3aed;--shadow:0 8px 24px rgba(15,23,42,.06)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{text-decoration:none;color:inherit}.shell{max-width:1180px;margin:auto;padding:18px 16px 92px}.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.brand h1{font-size:23px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:13px}.service{font-size:13px;color:var(--muted)}
.mainnav{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;background:#edf2f8;padding:6px;border-radius:14px;margin-bottom:16px}.mainnav a{padding:10px 8px;text-align:center;border-radius:10px;font-weight:650;color:#52627a}.mainnav a.active{background:#fff;color:var(--primary);box-shadow:0 2px 8px rgba(15,23,42,.06)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:18px;margin-bottom:14px}.card h2{font-size:18px;margin:0 0 14px}.section-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.refresh{border:0;background:#edf4ff;color:var(--primary);padding:8px 11px;border-radius:10px}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px}.stat{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px}.stat b{display:block;font-size:24px}.stat span{color:var(--muted);font-size:12px}
.monitor-list{display:grid;gap:10px}.monitor{border:1px solid var(--line);border-radius:14px;padding:14px;display:grid;grid-template-columns:1fr auto;gap:8px}.monitor-name{font-weight:750;font-size:16px}.meta{color:var(--muted);font-size:13px;overflow-wrap:anywhere}.badge{align-self:start;padding:5px 9px;border-radius:999px;font-size:12px;font-weight:700;background:#f1f5f9}.badge.waiting{color:#a16207;background:#fef3c7}.badge.live{color:#166534;background:#dcfce7}.badge.recording{color:#6d28d9;background:#ede9fe}.badge.error{color:#b91c1c;background:#fee2e2}
.events{display:grid;gap:8px}.event{padding:10px 0;border-bottom:1px solid var(--line);font-size:13px;color:#4b5563;overflow-wrap:anywhere}.event:last-child{border-bottom:0}
.settings-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.settings-grid a{border:1px solid var(--line);border-radius:14px;padding:16px;background:#fff}.settings-grid b{display:block;margin-bottom:3px}.settings-grid span{font-size:13px;color:var(--muted)}
form{margin:0}.form-group{margin-bottom:14px}.form-group label{display:block;font-weight:650;margin-bottom:6px}.form-group input,.form-group textarea,.form-group select{width:100%;min-height:44px;border:1px solid #cfd8e6;border-radius:10px;padding:10px 12px;background:#fff;font:inherit}.form-group textarea{min-height:220px;resize:vertical}.btn{width:100%;min-height:46px;border:0;border-radius:11px;background:var(--primary);color:#fff;font-weight:750;padding:10px 14px}.btn.secondary{background:#eef4ff;color:var(--primary);margin-top:9px}
.hint{font-size:13px;color:var(--muted);margin:0 0 12px}.empty{color:var(--muted);text-align:center;padding:28px 10px}
.subnav{display:flex;gap:8px;overflow:auto;margin:-2px 0 14px;padding-bottom:2px}.subnav a{white-space:nowrap;background:#eef2f7;padding:8px 11px;border-radius:10px;color:#52627a}.subnav a.active{background:#dbeafe;color:#1d4ed8}
.toast{position:fixed;top:16px;left:50%;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 14px;border-radius:10px;z-index:99;box-shadow:var(--shadow)}
@media(max-width:760px){.shell{padding:14px 12px 86px}.stats{grid-template-columns:repeat(2,1fr)}.stats .stat:first-child{grid-column:span 2}.mainnav{position:fixed;z-index:50;left:10px;right:10px;bottom:10px;margin:0;box-shadow:0 10px 30px rgba(15,23,42,.18)}.settings-grid{grid-template-columns:1fr}.card{padding:15px}.brand h1{font-size:20px}}
@media(min-width:900px){.monitor-list{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand"><h1>DouyinLiveRecorder</h1><p>直播监控控制台</p></div>
    <div id="service" class="service">正在读取状态…</div>
  </header>

  <nav class="mainnav">
    <a href="{{ url_for('home_page') }}" class="{{ 'active' if active_tab=='home' else '' }}">首页</a>
    <a href="{{ url_for('url_config_page') }}" class="{{ 'active' if active_tab=='url_config' else '' }}">主播</a>
    <a href="{{ url_for('recording_settings_page') }}" class="{{ 'active' if active_tab=='recording_settings' else '' }}">录制</a>
    <a href="{{ url_for('settings_page') }}" class="{{ 'active' if active_tab in ['settings','push_settings','cookie_settings','account_settings','xiaolan_webdav'] else '' }}">设置</a>
  </nav>

  {% if active_tab == 'home' %}
  <div class="stats">
    <div class="stat"><b id="s-total">0</b><span>监控总数</span></div>
    <div class="stat"><b id="s-waiting">0</b><span>等待直播</span></div>
    <div class="stat"><b id="s-live">0</b><span>直播中</span></div>
    <div class="stat"><b id="s-recording">0</b><span>正在录制</span></div>
    <div class="stat"><b id="s-error">0</b><span>异常</span></div>
  </div>

  <section class="card">
    <div class="section-head"><h2>主播监控</h2><button class="refresh" onclick="loadStatus()">刷新</button></div>
    <div id="monitor-list" class="monitor-list"><div class="empty">正在加载主播状态…</div></div>
  </section>

  <section class="card"><h2>最近事件</h2><div id="events" class="events"><div class="empty">暂无事件</div></div></section>

  {% elif active_tab == 'url_config' %}
  <section class="card">
    <h2>主播与直播地址</h2>
    <p class="hint">每行一个地址。当前核心已支持抖音直播间链接、分享短链，以及可被 streamget 解析的主播主页链接。保存后主程序会继续循环监测。</p>
    <form method="post">
      <div class="form-group"><textarea name="url_config_content" placeholder="每行一个直播地址">{{ url_config_content }}</textarea></div>
      <button class="btn" type="submit">保存主播列表</button>
    </form>
  </section>

  {% elif active_tab == 'settings' %}
  <section class="card">
    <h2>设置</h2>
    <div class="settings-grid">
      <a href="{{ url_for('push_settings_page') }}"><b>推送设置</b><span>开播、关播与通知渠道</span></a>
      <a href="{{ url_for('cookie_settings_page') }}"><b>Cookie</b><span>各平台登录 Cookie</span></a>
      <a href="{{ url_for('account_settings_page') }}"><b>账号密码</b><span>平台账号相关配置</span></a>
      <a href="{{ url_for('xiaolan_webdav_page') }}"><b>小蓝网盘</b><span>WebDAV 自动上传配置</span></a>
    </div>
  </section>

  {% else %}
  <div class="subnav">
    <a href="{{ url_for('recording_settings_page') }}" class="{{ 'active' if active_tab=='recording_settings' else '' }}">录制设置</a>
    <a href="{{ url_for('push_settings_page') }}" class="{{ 'active' if active_tab=='push_settings' else '' }}">推送</a>
    <a href="{{ url_for('cookie_settings_page') }}" class="{{ 'active' if active_tab=='cookie_settings' else '' }}">Cookie</a>
    <a href="{{ url_for('account_settings_page') }}" class="{{ 'active' if active_tab=='account_settings' else '' }}">账号密码</a>
    <a href="{{ url_for('xiaolan_webdav_page') }}" class="{{ 'active' if active_tab=='xiaolan_webdav' else '' }}">小蓝网盘</a>
  </div>
  <section class="card">
    <h2>{{ section }}</h2>
    {% if active_tab == 'xiaolan_webdav' %}<p class="hint">录像转为 MP4 后可上传；建议先测试连接，再开启自动上传。</p>{% endif %}
    <form method="post">
      {% for key, value in config.items(section) %}
      <div class="form-group">
        <label>{{ key }}</label>
        {% if value in ['是','否'] %}
          <select name="{{ key }}"><option value="是" {{ 'selected' if value=='是' else '' }}>是</option><option value="否" {{ 'selected' if value=='否' else '' }}>否</option></select>
        {% elif '密码' in key or 'token' in key.lower() or '令牌' in key %}
          <input type="password" name="{{ key }}" value="{{ value }}">
        {% else %}
          <input type="text" name="{{ key }}" value="{{ value }}">
        {% endif %}
      </div>
      {% endfor %}
      <button class="btn" type="submit">保存设置</button>
    </form>
    {% if active_tab == 'xiaolan_webdav' %}
      <button class="btn secondary" id="webdav-test" type="button">测试 WebDAV 连接</button>
      <div id="webdav-result" class="hint"></div>
    {% endif %}
  </section>
  {% endif %}
</div>

<script>
function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML}
async function loadStatus(){
  if(!document.getElementById('monitor-list')) return;
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    const d=await r.json();
    document.getElementById('service').textContent=(d.service_running?'● 录制服务运行中':'○ WebUI 已启动')+' · '+d.updated_at.slice(11);
    for(const k of ['total','waiting','live','recording','error']){
      const e=document.getElementById('s-'+k); if(e)e.textContent=d.counts[k]??0;
    }
    const box=document.getElementById('monitor-list');
    box.innerHTML=d.monitors.length?d.monitors.map(x=>`<div class="monitor"><div><div class="monitor-name">${esc(x.name)}</div><div class="meta">${esc(x.platform)} · ${esc(x.url)}</div><div class="meta">最近状态：${esc(x.updated_at)}</div></div><span class="badge ${esc(x.status)}">${esc(x.status_text)}</span></div>`).join(''):'<div class="empty">还没有添加主播，请到“主播”页面添加地址。</div>';
    const ev=document.getElementById('events');
    ev.innerHTML=d.events.length?d.events.map(x=>`<div class="event">${esc(x)}</div>`).join(''):'<div class="empty">暂无事件</div>';
  }catch(e){document.getElementById('service').textContent='状态读取失败'}
}
loadStatus();setInterval(loadStatus,5000);

const testBtn=document.getElementById('webdav-test');
if(testBtn)testBtn.addEventListener('click',async()=>{
  const result=document.getElementById('webdav-result');
  testBtn.disabled=true;testBtn.textContent='正在测试…';result.textContent='';
  try{
    const r=await fetch('/xiaolan_webdav/test',{method:'POST'});
    const d=await r.json();result.textContent=d.message||'测试完成';
  }catch(e){result.textContent='测试请求失败'}
  finally{testBtn.disabled=false;testBtn.textContent='测试 WebDAV 连接'}
});

const p=new URLSearchParams(location.search);
if(p.get('success')==='true'){
  const t=document.createElement('div');t.className='toast';t.textContent='保存成功';document.body.appendChild(t);
  setTimeout(()=>t.remove(),2200);history.replaceState({},'',location.pathname);
}
</script>
</body>
</html>
"""


def main():
    root = Path(".")
    if not (root / "main.py").exists():
        raise SystemExit("错误：请在项目仓库根目录运行此脚本。")

    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "webui.py").write_text(WEBUI, encoding="utf-8")
    (root / "templates" / "index.html").write_text(INDEX, encoding="utf-8")

    print("已重构：webui.py")
    print("已重构：templates/index.html")
    print("保留：main.py / streamget / 小蓝网盘上传脚本")
    print("新增：首页统计、主播状态、最近事件、四栏导航、移动端响应式 UI、/api/status")


if __name__ == "__main__":
    main()
