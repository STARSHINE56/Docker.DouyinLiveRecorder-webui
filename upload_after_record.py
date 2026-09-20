import argparse
import configparser
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests


CONFIG_FILE = Path("config/config.ini")


def log(message):
    print(
        f"[XiaolanUpload] {message}",
        flush=True
    )


def yes(value):
    return str(value).strip().lower() in {
        "是",
        "yes",
        "true",
        "1",
        "on",
    }


def safe_name(value):
    value = str(
        value or ""
    ).strip()

    value = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip(" ._")

    return value or "未知主播"


def load_config():
    config = configparser.ConfigParser(
        interpolation=None
    )

    config.read(
        CONFIG_FILE,
        encoding="utf-8-sig"
    )

    if "小蓝网盘" not in config:
        raise RuntimeError(
            "缺少 [小蓝网盘]"
        )

    return config["小蓝网盘"]


def join_url(base, *parts):
    result = base.rstrip("/")

    for part in parts:
        part = str(
            part or ""
        ).strip("/")

        if not part:
            continue

        result += (
            "/"
            + urllib.parse.quote(
                part,
                safe=""
            )
        )

    return result


def ensure_collection(
    session,
    url,
    auth
):
    response = session.request(
        "PROPFIND",
        url,
        auth=auth,
        headers={
            "Depth": "0",
            "User-Agent":
                "DouyinLiveRecorder-WebDAV",
        },
        timeout=30,
        allow_redirects=True
    )

    if response.status_code in (
        200,
        207,
    ):
        return

    if response.status_code == 404:
        response = session.request(
            "MKCOL",
            url,
            auth=auth,
            headers={
                "User-Agent":
                    "DouyinLiveRecorder-WebDAV",
            },
            timeout=30,
            allow_redirects=True
        )

        if response.status_code in (
            200,
            201,
            204,
            405,
        ):
            return

        raise RuntimeError(
            "创建目录失败 HTTP "
            + str(
                response.status_code
            )
        )

    if response.status_code == 405:
        return

    raise RuntimeError(
        "检查目录失败 HTTP "
        + str(
            response.status_code
        )
    )


def ensure_remote_path(
    session,
    base_url,
    root_dir,
    anchor_dir,
    date_dir,
    auth
):
    current = base_url.rstrip("/")

    components = []

    for source in (
        root_dir,
        anchor_dir,
        date_dir,
    ):
        if not source:
            continue

        for part in str(
            source
        ).split("/"):
            part = part.strip()

            if part:
                components.append(
                    part
                )

    for part in components:
        current = join_url(
            current,
            part
        )

        ensure_collection(
            session,
            current,
            auth
        )

    return current


def get_mp4_candidates(
    save_file_path
):
    source = Path(
        save_file_path
    )

    folder = source.parent
    stem = source.stem

    result = []

    exact = folder / (
        stem + ".mp4"
    )

    if exact.exists():
        result.append(
            exact
        )

    prefix = (
        stem.rsplit("_", 1)[0]
        if "_" in stem
        else stem
    )

    for file in folder.glob(
        "*.mp4"
    ):
        if file in result:
            continue

        if (
            file.stem == stem
            or file.stem.startswith(
                prefix + "_"
            )
        ):
            result.append(
                file
            )

    return sorted(
        result,
        key=lambda p:
            p.stat().st_mtime
            if p.exists()
            else 0
    )


def file_is_stable(
    path
):
    if not path.exists():
        return False

    first = path.stat().st_size

    if first <= 0:
        return False

    time.sleep(5)

    if not path.exists():
        return False

    second = path.stat().st_size

    return (
        first == second
        and second > 0
    )


def wait_for_mp4(
    save_file_path,
    timeout
):
    deadline = (
        time.time()
        + timeout
    )

    log(
        "等待 MP4 转换完成..."
    )

    while time.time() < deadline:
        candidates = (
            get_mp4_candidates(
                save_file_path
            )
        )

        ready = []

        for file in candidates:
            if file_is_stable(
                file
            ):
                ready.append(
                    file
                )

        if ready:
            return ready

        time.sleep(5)

    return []


def upload(
    session,
    local_file,
    remote_folder,
    auth,
    retries
):
    remote_url = join_url(
        remote_folder,
        local_file.name
    )

    for attempt in range(
        1,
        retries + 1
    ):
        try:
            size = (
                local_file
                .stat()
                .st_size
            )

            log(
                f"上传 {local_file.name} "
                f"{size / 1024 / 1024:.1f}MB "
                f"({attempt}/{retries})"
            )

            with local_file.open(
                "rb"
            ) as fp:
                response = (
                    session.put(
                        remote_url,
                        data=fp,
                        auth=auth,
                        headers={
                            "Content-Type":
                                "video/mp4",
                            "User-Agent":
                                "DouyinLiveRecorder-WebDAV",
                        },
                        timeout=(
                            30,
                            3600,
                        ),
                        allow_redirects=True
                    )
                )

            if response.status_code in (
                200,
                201,
                204,
            ):
                verify = (
                    session.request(
                        "PROPFIND",
                        remote_url,
                        auth=auth,
                        headers={
                            "Depth": "0",
                        },
                        timeout=30,
                        allow_redirects=True
                    )
                )

                if verify.status_code in (
                    200,
                    207,
                ):
                    log(
                        "上传成功并通过校验: "
                        f"{local_file.name}"
                    )

                    return True

                log(
                    "远端校验失败 HTTP "
                    + str(
                        verify.status_code
                    )
                )

            else:
                log(
                    "上传失败 HTTP "
                    + str(
                        response.status_code
                    )
                )

        except Exception as exc:
            log(
                f"上传异常: {exc}"
            )

        if attempt < retries:
            delay = min(
                60,
                attempt * 10
            )

            log(
                f"{delay}秒后重试"
            )

            time.sleep(
                delay
            )

    return False


def main():
    parser = argparse.ArgumentParser(
        add_help=False
    )

    parser.add_argument(
        "--record_name",
        default=""
    )

    parser.add_argument(
        "--save_file_path",
        default=""
    )

    args, _ = (
        parser.parse_known_args()
    )

    if not args.save_file_path:
        log(
            "没有收到录像文件路径"
        )

        return 0

    cfg = load_config()

    if not yes(
        cfg.get(
            "自动上传录像",
            "否"
        )
    ):
        log(
            "自动上传未开启"
        )

        return 0

    url = cfg.get(
        "WebDAV地址",
        ""
    ).strip()

    username = cfg.get(
        "WebDAV用户名",
        ""
    ).strip()

    password = cfg.get(
        "WebDAV密码",
        ""
    )

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):
        log(
            "WebDAV 地址错误"
        )

        return 1

    root_dir = cfg.get(
        "上传根目录",
        "/直播录像/"
    )

    by_anchor = yes(
        cfg.get(
            "按主播创建文件夹",
            "是"
        )
    )

    by_date = yes(
        cfg.get(
            "按日期创建文件夹",
            "否"
        )
    )

    delete_local = yes(
        cfg.get(
            "上传成功后删除本地",
            "否"
        )
    )

    try:
        retries = max(
            1,
            int(
                cfg.get(
                    "上传失败重试次数",
                    "3"
                )
            )
        )
    except ValueError:
        retries = 3

    try:
        wait_seconds = max(
            30,
            int(
                cfg.get(
                    "等待MP4最长时间(秒)",
                    "1800"
                )
            )
        )
    except ValueError:
        wait_seconds = 1800

    anchor = safe_name(
        args.record_name
    )

    if " " in anchor:
        anchor = safe_name(
            anchor.split(
                " ",
                1
            )[-1]
        )

    anchor_folder = (
        anchor
        if by_anchor
        else ""
    )

    date_folder = (
        datetime.now().strftime(
            "%Y-%m-%d"
        )
        if by_date
        else ""
    )

    mp4_files = wait_for_mp4(
        args.save_file_path,
        wait_seconds
    )

    if not mp4_files:
        log(
            "没有找到转换完成的 MP4；"
            "不会删除任何文件"
        )

        return 1

    session = requests.Session()

    auth = (
        username,
        password
    )

    try:
        remote_folder = (
            ensure_remote_path(
                session,
                url,
                root_dir,
                anchor_folder,
                date_folder,
                auth
            )
        )
    except Exception as exc:
        log(
            f"创建网盘目录失败: {exc}"
        )

        return 1

    all_ok = True

    for file in mp4_files:
        ok = upload(
            session,
            file,
            remote_folder,
            auth,
            retries
        )

        if not ok:
            all_ok = False

            log(
                "上传失败，保留本地: "
                f"{file}"
            )

            continue

        if delete_local:
            try:
                file.unlink()

                log(
                    "上传校验成功，"
                    "删除本地 MP4: "
                    f"{file.name}"
                )

            except Exception as exc:
                log(
                    "上传成功，但删除"
                    f"本地失败: {exc}"
                )

    return (
        0
        if all_ok
        else 1
    )


if __name__ == "__main__":
    sys.exit(
        main()
    )
