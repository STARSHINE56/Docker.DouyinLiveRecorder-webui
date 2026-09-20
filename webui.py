from flask import Flask, render_template, request, redirect, url_for
import configparser
import os
import subprocess

import requests

from streamget.logger import logger


app = Flask(__name__)

CONFIG_FILE = "config/config.ini"
URL_CONFIG_FILE = "config/URL_config.ini"


def read_config(file_path):
    config = configparser.ConfigParser(
        interpolation=None
    )

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8-sig"
        ) as f:
            content = f.read()

        if not content.strip().startswith(
            "["
        ):
            config.read_string(
                "[DEFAULT]\n"
                + content
            )
        else:
            config.read(
                file_path,
                encoding="utf-8-sig"
            )

    except FileNotFoundError:
        pass

    return config


def write_config(
    config,
    file_path
):
    with open(
        file_path,
        "w",
        encoding="utf-8-sig"
    ) as configfile:
        config.write(
            configfile
        )


@app.route("/")
def index():
    return redirect(
        url_for(
            "home_page"
        )
    )


@app.route(
    "/home",
    methods=["GET"]
)
def home_page():
    return render_template(
        "index.html",
        active_tab="home"
    )


recording_process = None


def start_main_recording():
    global recording_process

    if (
        recording_process is None
        or recording_process.poll()
        is not None
    ):
        try:
            logger.info(
                "WebUI启动时自动开始录制..."
            )

            recording_process = (
                subprocess.Popen(
                    [
                        "python",
                        "main.py",
                    ],
                    cwd=os.getcwd()
                )
            )

        except Exception as exc:
            logger.error(
                f"自动启动录制失败: {exc}"
            )


def run_with_auto_record():
    start_main_recording()

    app.run(
        host="0.0.0.0",
        port=5000
    )


@app.route(
    "/url_config",
    methods=[
        "GET",
        "POST",
    ]
)
def url_config_page():
    path = os.path.join(
        os.getcwd(),
        "config",
        "URL_config.ini"
    )

    if request.method == "POST":
        content = request.form[
            "url_config_content"
        ]

        with open(
            path,
            "w",
            encoding="utf-8-sig"
        ) as f:
            f.write(
                content
            )

        return redirect(
            url_for(
                "url_config_page",
                success="true"
            )
        )

    try:
        with open(
            path,
            "r",
            encoding="utf-8-sig"
        ) as f:
            content = f.read()

    except FileNotFoundError:
        content = ""

    return render_template(
        "index.html",
        url_config_content=content,
        active_tab="url_config"
    )


def config_page(
    section,
    endpoint,
    active_tab
):
    path = os.path.join(
        os.getcwd(),
        "config",
        "config.ini"
    )

    if request.method == "POST":
        config = read_config(
            path
        )

        for key, value in (
            request.form.items()
        ):
            if (
                section in config
                and key
                in config[section]
            ):
                config.set(
                    section,
                    key,
                    value
                )

        write_config(
            config,
            path
        )

        return redirect(
            url_for(
                endpoint,
                success="true"
            )
        )

    config = read_config(
        path
    )

    return render_template(
        "index.html",
        config=config,
        active_tab=active_tab,
        section=section
    )


@app.route(
    "/recording_settings",
    methods=[
        "GET",
        "POST",
    ]
)
def recording_settings_page():
    return config_page(
        "录制设置",
        "recording_settings_page",
        "recording_settings"
    )


@app.route(
    "/push_settings",
    methods=[
        "GET",
        "POST",
    ]
)
def push_settings_page():
    return config_page(
        "推送配置",
        "push_settings_page",
        "push_settings"
    )


@app.route(
    "/cookie_settings",
    methods=[
        "GET",
        "POST",
    ]
)
def cookie_settings_page():
    return config_page(
        "Cookie",
        "cookie_settings_page",
        "cookie_settings"
    )


@app.route(
    "/account_settings",
    methods=[
        "GET",
        "POST",
    ]
)
def account_settings_page():
    return config_page(
        "账号密码",
        "account_settings_page",
        "account_settings"
    )


@app.route(
    "/xiaolan_webdav",
    methods=[
        "GET",
        "POST",
    ]
)
def xiaolan_webdav_page():
    path = os.path.join(
        os.getcwd(),
        "config",
        "config.ini"
    )

    config = read_config(
        path
    )

    if "小蓝网盘" not in config:
        config.add_section(
            "小蓝网盘"
        )

    if request.method == "POST":
        keys = [
            "WebDAV地址",
            "WebDAV用户名",
            "WebDAV密码",
            "上传根目录",
            "自动上传录像",
            "按主播创建文件夹",
            "按日期创建文件夹",
            "上传成功后删除本地",
            "上传失败重试次数",
            "等待MP4最长时间(秒)",
        ]

        for key in keys:
            if key in request.form:
                config.set(
                    "小蓝网盘",
                    key,
                    request.form.get(
                        key,
                        ""
                    ).strip()
                )

        write_config(
            config,
            path
        )

        return redirect(
            url_for(
                "xiaolan_webdav_page",
                success="true"
            )
        )

    return render_template(
        "index.html",
        config=config,
        active_tab="xiaolan_webdav",
        section="小蓝网盘"
    )


@app.route(
    "/xiaolan_webdav/test",
    methods=["POST"]
)
def test_xiaolan_webdav():
    path = os.path.join(
        os.getcwd(),
        "config",
        "config.ini"
    )

    config = read_config(
        path
    )

    if "小蓝网盘" not in config:
        return {
            "success": False,
            "message":
                "请先保存 WebDAV 配置",
        }, 400

    url = config.get(
        "小蓝网盘",
        "WebDAV地址",
        fallback=""
    ).strip()

    username = config.get(
        "小蓝网盘",
        "WebDAV用户名",
        fallback=""
    ).strip()

    password = config.get(
        "小蓝网盘",
        "WebDAV密码",
        fallback=""
    )

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):
        return {
            "success": False,
            "message":
                "WebDAV 地址为空或格式错误",
        }, 400

    try:
        response = requests.request(
            "PROPFIND",
            url,
            auth=(
                username,
                password
            ),
            headers={
                "Depth": "0",
                "User-Agent":
                    "DouyinLiveRecorder-WebDAV",
            },
            timeout=15,
            allow_redirects=True
        )

        if response.status_code in (
            200,
            201,
            204,
            207,
        ):
            return {
                "success": True,
                "message":
                    "小蓝网盘 WebDAV 连接成功",
            }

        messages = {
            401:
                "认证失败，请检查用户名和密码",
            403:
                "访问被拒绝，请检查权限",
            404:
                "WebDAV 地址不存在",
        }

        return {
            "success": False,
            "message": messages.get(
                response.status_code,
                "连接失败 HTTP "
                + str(
                    response.status_code
                )
            ),
        }, 400

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "message":
                "连接超时",
        }, 400

    except requests.exceptions.RequestException as exc:
        logger.error(
            f"WebDAV连接失败: {exc}"
        )

        return {
            "success": False,
            "message":
                "无法连接 WebDAV 服务器",
        }, 400


@app.route("/log")
def get_log():
    result = []

    files = [
        os.path.join(
            os.getcwd(),
            "logs",
            "streamget.log"
        ),
        os.path.join(
            os.getcwd(),
            "logs",
            "PlayURL.log"
        ),
    ]

    for file in files:
        if not os.path.exists(
            file
        ):
            continue

        try:
            with open(
                file,
                "r",
                encoding="utf-8"
            ) as f:
                lines = f.readlines()

            result.extend(
                lines[-100:]
            )

        except Exception as exc:
            result.append(
                f"Error reading {file}: "
                f"{exc}\n"
            )

    return "".join(
        result
    )


if __name__ == "__main__":
    run_with_auto_record()
