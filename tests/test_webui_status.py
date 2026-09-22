from flask import Flask, render_template, request, redirect, url_for, jsonify
import configparser
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePath
from urllib.parse import urlparse

import requests
from streamget.logger import logger
import status_runtime as runtime_status

app = Flask(__name__)

CONFIG_FILE = "config/config.ini"
URL_CONFIG_FILE = "config/URL_config.ini"
LOG_FILES = (
    "logs/streamget.log",
    "logs/PlayURL.log",
)
DOWNLOADS_DIR = Path(os.environ.get("DLR_DOWNLOADS_DIR", "/app/downloads"))
RECORDING_EXTENSIONS = {".mp4", ".flv", ".ts", ".mkv", ".mov", ".m4v", ".webm", ".mp3", ".m4a"}
LIVE_STATUS_STALE_SECONDS = 120
ACTIVE_RECORDING_STATUSES = {"starting", "recording", "recovering", "stopping"}

recording_process = None
recording_process_lock = threading.Lock()
runtime_status.reconcile_stale_recordings()


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
            "monitor_status": "waiting",
            "live_status": "unknown",
            "recording_status": "idle",
            "last_checked_at": None,
            "last_success_at": None,
            "live_started_at": None,
            "recording_started_at": None,
            "recording_file": None,
            "last_error": None,
        })
    return items


def classify_status(text):
    """Classify one log line without treating counters or negations as activity."""
    lower = text.lower()

    # Negative meanings must win before positive keywords.  Several of these
    # lines contain "正在录制" or "直播中" as part of a negative/statistical
    # sentence, so checking positive words first causes false activity.
    if any(k in text for k in (
        "没有正在监测和录制的直播",
        "没有正在录制的直播",
        "没有正在录制",
        "等待直播",
        "未开播",
        "未直播",
        "直播未开始",
    )):
        return "waiting", "等待直播"

    # This is a global task count, not the state of an individual anchor.
    if re.search(r"共监测\s*\d+\s*个直播中", text):
        return None

    # "准备开始录制" is preparation rather than proof that recording started.
    if "准备开始录制" in text:
        return None

    if any(k in text for k in ("正在录制中", "已经开始录制", "已开始录制", "开始录制")):
        return "recording", "正在录制"
    if any(k in text for k in ("开始直播", "已开播", "正在直播中")):
        return "live", "直播中"
    if any(k in text for k in ("获取失败", "连接失败", "错误信息", "异常")) or "error" in lower:
        return "error", "异常"
    return None


def extract_status_anchor(line):
    """Return an anchor only when the line has a real per-anchor status shape."""
    status_words = (
        r"等待直播|未开播|未直播|直播已结束|关播|"
        r"正在直播中|已开播|开始直播|"
        r"正在录制中|已经开始录制|已开始录制|开始录制"
    )
    patterns = (
        rf"主播\s*[:：]\s*([^,，|\r\n]+?)\s+(?:{status_words})",
        rf"序号\d+\s+([^|,，\r\n\[]+?)(?:\[[^\]]+\])?\s+(?:{status_words})",
        r"(?:^|\s-\s)([^|,，\r\n]+?)\[[^\]]+\]\s+正在录制中",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(1).strip()
    return ""


def status_applies_to_monitor(line, item, state):
    """Require positive states to be backed by a structured anchor log."""
    if item["url"] and item["url"] in line:
        return True

    anchor = extract_status_anchor(line)
    if anchor and item["name"] not in ("等待获取主播名", "待识别主播"):
        return anchor == item["name"]

    # A manually entered name appearing in arbitrary text is not live proof.
    # Errors may still be associated by URL above; uncertain lines are ignored.
    return False


def summarize_event(line):
    """Create a concise event only from explicit, non-statistical log lines."""
    ts = re.search(
        r"(20\d\d[-/]\d\d[-/]\d\d[ T](\d\d:\d\d:\d\d))",
        line
    )
    time_text = ts.group(2) if ts else ""
    anchor = extract_status_anchor(line)
    state = classify_status(line)

    if state and state[0] in ("live", "recording", "waiting"):
        # Global/ambiguous lines never become per-anchor recent events.
        if not anchor:
            return None
        action = {
            "live": "检测到开播",
            "recording": "开始录制",
            "waiting": "等待直播",
        }[state[0]]
        return {
            "summary": " · ".join(x for x in (time_text, anchor, action) if x),
            "key": (anchor, state[0]),
            "anchor": anchor,
            "state": state[0],
        }

    if "直播已结束" in line or "关播" in line:
        if not anchor:
            return None
        action = "直播结束"
        event_state = "waiting"
    elif "上传" in line and any(k in line for k in ("成功", "完成")):
        action, event_state = "上传完成", "upload_done"
    elif "上传" in line and any(k in line for k in ("失败", "异常")):
        action, event_state = "上传失败", "upload_failed"
    elif "推送" in line and any(k in line for k in ("成功", "完成")):
        action, event_state = "推送成功", "push_done"
    elif "推送" in line and any(k in line for k in ("失败", "异常")):
        action, event_state = "推送失败", "push_failed"
    elif "错误" in line or "异常" in line or "失败" in line:
        action, event_state = "检测异常", "error"
    elif "WebUI启动" in line:
        action, event_state = "WebUI 已启动", "webui_started"
    else:
        return None

    subject = anchor or "系统"
    return {
        "summary": " · ".join(x for x in (time_text, anchor, action) if x),
        "key": (subject, event_state),
        "anchor": subject,
        "state": event_state,
    }


def get_monitor_snapshot():
    items = parse_monitor_lines()
    state = runtime_status.load_state()
    runtime_monitors = state.get("monitors", {})
    for item in items:
        persisted = runtime_monitors.get(item["url"], {})
        for key in (
            "monitor_status", "live_status", "recording_status", "last_checked_at",
            "last_success_at", "live_started_at", "recording_started_at",
            "recording_file", "last_error", "session_id",
        ):
            if key in persisted:
                item[key] = persisted[key]
        if persisted.get("name") and item["name"] in {"待识别主播", "等待获取主播名"}:
            item["name"] = persisted["name"]
        # Display-only stale guard. A completed/manual-stop recording is not
        # evidence that the streamer went offline, so degrade to unknown only.
        if (
            item.get("live_status") == "live"
            and item.get("recording_status") not in ACTIVE_RECORDING_STATUSES
        ):
            try:
                last_success = datetime.fromisoformat(item.get("last_success_at") or "")
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_success.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                age = LIVE_STATUS_STALE_SECONDS + 1
            if age > LIVE_STATUS_STALE_SECONDS:
                item["live_status"] = "unknown"
        # Display-only normalization: offline + interrupted → idle.
        # Keeps historical interrupted events; does not mutate runtime state.
        if item.get("live_status") == "offline" and item.get("recording_status") == "interrupted":
            item["recording_status"] = "idle"

    counts = {
        "total": len(items),
        "live": sum(i["live_status"] == "live" for i in items),
        "recording": sum(i["recording_status"] == "recording" for i in items),
        "error": sum(
            i["monitor_status"] == "error" or i["recording_status"] == "error"
            for i in items
        ),
    }
    events = list(reversed(state.get("events", [])[-20:]))

    return {
        "service_running": recording_process is not None and recording_process.poll() is None,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "monitors": items,
        "events": events,
    }


def _active_recording_file_statuses() -> dict[Path, str]:
    active = {}
    for item in runtime_status.load_state().get("monitors", {}).values():
        if item.get("recording_status") not in {"starting", "recording", "recovering", "stopping"}:
            continue
        file_path = item.get("recording_file")
        if file_path:
            try:
                active[Path(file_path).resolve()] = item["recording_status"]
            except OSError:
                continue
    return active


def _active_recording_files() -> set[Path]:
    return set(_active_recording_file_statuses())


def list_recordings() -> list[dict]:
    root = DOWNLOADS_DIR.resolve()
    active = _active_recording_file_statuses()
    if not root.is_dir():
        return []
    files = []
    for path in root.rglob("*"):
        try:
            resolved = path.resolve()
            if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root):
                continue
            if path.suffix.lower() not in RECORDING_EXTENSIONS:
                continue
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(root)
        recording_status = active.get(resolved, "completed")
        files.append({
            "name": path.name,
            "path": relative.as_posix(),
            "directory": relative.parent.as_posix() if relative.parent != Path(".") else "根目录",
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "is_recording": resolved in active,
            "recording_status": recording_status,
        })
    return sorted(files, key=lambda item: item["modified_at"], reverse=True)


def _safe_recording_path(relative_path: str) -> Path:
    candidate = PurePath(relative_path)
    if not relative_path or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("录像路径不安全")
    root = DOWNLOADS_DIR.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError("只能操作 downloads 内的录像")
    return target


@app.route("/")
def index():
    return redirect(url_for("home_page"))


@app.route("/home")
def home_page():
    return render_template("index.html", active_tab="home")


@app.route("/settings")
def settings_page():
    return render_template("index.html", active_tab="settings")


@app.route("/recordings")
def recordings_page():
    return render_template("index.html", active_tab="recordings")


@app.route("/api/status")
def api_status():
    return jsonify(get_monitor_snapshot())


@app.route("/api/logs")
def api_logs():
    limit = max(20, min(request.args.get("limit", 120, type=int), 500))
    return jsonify({"lines": read_log_lines(limit)})


@app.route("/api/recordings")
def api_recordings():
    return jsonify({"recordings": list_recordings()})


@app.route("/api/recordings/delete", methods=["POST"])
def delete_recording():
    relative_path = (request.get_json(silent=True) or {}).get("path", "")
    try:
        target = _safe_recording_path(relative_path)
    except (TypeError, ValueError, OSError):
        return jsonify(success=False, message="录像路径不安全"), 400
    if not target.exists():
        return jsonify(success=False, message="录像文件不存在"), 404
    if target.is_symlink() or not target.is_file():
        return jsonify(success=False, message="只能删除录像文件，不能删除目录"), 400
    if target.resolve() in _active_recording_files():
        return jsonify(success=False, message="录像正在录制，不能删除"), 409
    try:
        target.unlink()
    except OSError as exc:
        logger.error(f"删除录像失败: {exc}")
        return jsonify(success=False, message=f"删除失败：{exc}"), 500
    return jsonify(success=True, message="录像已删除")


@app.route("/api/recordings/stop", methods=["POST"])
def stop_recording():
    url = (request.get_json(silent=True) or {}).get("url", "")
    if not isinstance(url, str) or not url.strip():
        return jsonify(success=False, message="缺少主播地址"), 400
    success, message = runtime_status.request_manual_stop(url.strip())
    return jsonify(success=success, message=message), (200 if success else 409)


def start_main_recording():
    global recording_process
    with recording_process_lock:
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

    if re.fullmatch(r"MS4wLj[A-Za-z0-9_-]+", url):
        url = f"https://www.douyin.com/user/{url}"
    elif url.isdigit():
        url = f"https://live.douyin.com/{url}"
    elif re.fullmatch(r"[A-Za-z0-9._-]{2,40}", url):
        url = f"https://www.douyin.com/user/{url}"

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
