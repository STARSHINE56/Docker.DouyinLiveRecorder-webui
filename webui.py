from flask import Flask, render_template, request, redirect, url_for, jsonify
import configparser
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
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

        main_part = re.split(r"[,，]\s*主播\s*[:：]", line, maxsplit=1)[0].strip()
        url = main_part.split("|", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            continue

        name = name_match.group(1).strip() if name_match else ""
        if not name:
            name = extract_anchor_name_from_logs(url) or "待识别主播"

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
            if not any(k and k not in ("等待获取主播名", "待识别主播") and k in line for k in keys):
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

    def summarize_event(line):
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
