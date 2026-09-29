import argparse
import configparser
import json
import re
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
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

    if "云盘配置" in config:
        return config["云盘配置"]
    if "小蓝网盘" in config:
        return config["小蓝网盘"]
    raise RuntimeError("缺少 [云盘配置] 或 [小蓝网盘]")


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

    if "%03d" in stem:
        pattern = stem.replace("%03d", "[0-9][0-9][0-9]") + ".mp4"
        result.extend(file for file in folder.glob(pattern) if file.is_file())
    else:
        exact = folder / (stem + ".mp4")
        if exact.exists():
            result.append(exact)

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


def file_is_open(path):
    """Refuse cleanup/conversion while a local process still has the source open."""
    if not Path('/proc').is_dir():
        return True  # cannot establish that a recording has stopped
    source = Path(path).stat()
    for proc in Path('/proc').glob('[0-9]*/fd'):
        try:
            for fd in proc.iterdir():
                try:
                    target = fd.stat()
                    if (target.st_dev, target.st_ino) == (source.st_dev, source.st_ino):
                        return True
                except FileNotFoundError:
                    continue
        except (PermissionError, OSError):
            # Some hosts expose unrelated PIDs but hide their descriptors.
            # The recorder and this uploader share the container's visible PID namespace.
            continue
    return False


def probe_media_file(path, require_video=True, require_audio=True, runner=subprocess.run):
    """Validate a finished media file before it is eligible for upload."""
    path = Path(path)
    if not path.exists() or path.stat().st_size <= 0:
        return False
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type", "-of", "json", str(path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            log(f"ffprobe 失败，保留本地: {path.name}")
            return False
        payload = json.loads(result.stdout or "{}")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        stream_types = {item.get("codec_type") for item in payload.get("streams", [])}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log(f"ffprobe 校验异常，保留本地: {path.name}: {exc}")
        return False
    if duration <= 0 or (require_video and "video" not in stream_types):
        log(f"MP4 无有效视频或时长，拒绝上传并保留本地: {path.name}")
        return False
    if require_audio and "audio" not in stream_types:
        log(f"MP4 未检测到音轨，拒绝上传并保留本地: {path.name}")
        return False
    return True


def wait_for_mp4(
    save_file_path,
    timeout,
    require_video=True,
    require_audio=True,
    poll_interval=5,
):
    deadline = (
        time.time()
        + timeout
    )

    log(
        "等待 MP4 转换完成..."
    )

    previous = None
    while time.time() < deadline:
        candidates = (
            get_mp4_candidates(
                save_file_path
            )
        )

        signature = tuple((str(file), file.stat().st_size) for file in candidates if file.exists())
        if candidates and signature == previous:
            source = Path(save_file_path)
            if source.is_file() and (file_is_open(source) or not file_is_stable(source)):
                log(f"源文件仍在写入，拒绝上传: {source.name}")
                return []
            if source.is_file() and any(file.stat().st_mtime < source.stat().st_mtime for file in candidates):
                log(f"MP4 早于对应 TS，拒绝上传旧文件: {source.name}")
                return []
            invalid = [file for file in candidates if not probe_media_file(
                file, require_video=require_video, require_audio=require_audio
            )]
            if invalid:
                return []
            return candidates
        previous = signature
        time.sleep(poll_interval)

    return []


def convert_backfill_ts(source):
    """Convert one old TS with bounded temporary space; never delete its source."""
    source = Path(source)
    output = source.with_suffix('.mp4')
    temporary = source.with_suffix('.converting.mp4')
    if not file_is_stable(source) or file_is_open(source):
        log(f"源文件不稳定或正在录制，跳过: {source.name}")
        return False
    if output.exists() and output.stat().st_mtime >= source.stat().st_mtime and probe_media_file(output):
        return True
    required = int(source.stat().st_size * 1.2) + 512 * 1024 * 1024
    free = shutil.disk_usage(source.parent).free
    if free < required:
        log(f"空间不足，跳过 {source.name}: 可用 {free} 字节，至少需要 {required} 字节")
        return False
    try:
        for audio_codec in ('copy', 'aac'):
            if temporary.exists():
                temporary.unlink()
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-y',
                       '-i', str(source), '-c:v', 'copy', '-c:a', audio_codec]
            if audio_codec == 'aac':
                command += ['-b:a', '192k']
            command += ['-f', 'mp4', str(temporary)]
            result = subprocess.run(command, capture_output=True, timeout=7200, check=False)
            if result.returncode == 0 and probe_media_file(temporary):
                if file_is_open(source) or source.stat().st_size <= 0:
                    log(f"源文件在转换期间变为活动文件，保留 TS: {source.name}")
                    return False
                os.replace(temporary, output)
                return True
            detail = result.stderr[-3000:].decode('utf-8', errors='replace')
            log(f"转换失败 {source.name} ({audio_codec}): {detail}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"转换异常，保留 TS {source.name}: {exc}")
    finally:
        if temporary.exists():
            temporary.unlink()
    return False


def backfill(args, cfg):
    if not yes(cfg.get('自动上传录像', '否')):
        log('自动上传未开启，停止补传')
        return 1
    folder = Path(args.backfill_dir).resolve()
    if not folder.is_dir() or not args.match or not args.record_name:
        log('补传需要有效目录、--match 文件匹配及 --record_name 主播名称')
        return 1
    if '/' in args.match or '\\' in args.match or '..' in args.match:
        log('文件匹配不得包含目录或上级路径')
        return 1
    files = sorted(file for file in folder.glob(args.match) if file.is_file() and file.suffix.lower() == '.ts')
    log(f"找到 {len(files)} 个 TS 文件；按单文件顺序处理")
    if not args.apply:
        for file in files:
            log(f"预览: {file.name} ({file.stat().st_size} 字节)")
        log('预览完成；确认停止录制后使用 --apply --confirm-recorder-stopped 执行')
        return 0
    if not args.confirm_recorder_stopped:
        log('请先停止录制，并显式指定 --confirm-recorder-stopped')
        return 1
    failed = 0
    for file in files:
        if time.time() - file.stat().st_mtime < 300 or not convert_backfill_ts(file):
            log(f"跳过近期、活动或转换失败文件: {file.name}")
            failed += 1
            continue
        if main(['--record_name', args.record_name, '--save_file_path', str(file),
                 '--save_type', 'TS', '--converts_to_mp4', 'True', '--date',
                 args.date or datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d')]):
            failed += 1
    return 1 if failed else 0


def remote_content_length(response):
    try:
        root = ET.fromstring(response.content)
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() == "getcontentlength":
                value = (element.text or "").strip()
                if value.isdigit():
                    return int(value)
    except ET.ParseError:
        pass
    header = response.headers.get("getcontentlength")
    if header and str(header).isdigit():
        return int(header)
    return None


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

    def verified_remote_size():
        response = session.request('PROPFIND', remote_url, auth=auth,
                                   headers={'Depth': '0'}, timeout=30, allow_redirects=True)
        if response.status_code not in (200, 207):
            return None
        return remote_content_length(response)

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

            # Idempotent recovery: a prior PUT may have completed before the script exited.
            if verified_remote_size() == size:
                log(f"远端已有同名同大小文件，跳过重复上传: {local_file.name}")
                return True

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
                remote_size = verified_remote_size()
                if remote_size == size:
                    log("上传成功并通过大小校验: " + local_file.name)
                    return True
                log(f"远端大小校验失败: 本地={size}, 远端={remote_size}")

            else:
                log(
                    "上传失败 HTTP "
                    + str(
                        response.status_code
                    )
                )

        except Exception as exc:
            log(f"上传异常: {type(exc).__name__}；保留本地文件")

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


def main(argv=None):
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

    parser.add_argument("--save_type", default="TS")
    parser.add_argument("--split_video_by_time", default="False")
    parser.add_argument("--converts_to_mp4", default="True")
    parser.add_argument('--backfill-dir')
    parser.add_argument('--match')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-recorder-stopped', action='store_true')
    parser.add_argument('--date', help='历史补传的远端日期目录，格式 YYYY-MM-DD')

    args, _ = (
        parser.parse_known_args(argv)
    )

    if args.backfill_dir:
        return backfill(args, load_config())

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
        (args.date or datetime.now().strftime("%Y-%m-%d"))
        if by_date
        else ""
    )
    if args.date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.date):
        log('日期格式必须为 YYYY-MM-DD')
        return 1

    mp4_files = wait_for_mp4(
        args.save_file_path,
        wait_seconds,
        require_video="音频" not in args.save_type,
        require_audio=True,
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
        log(f"创建网盘目录失败: {type(exc).__name__}；请检查地址及访问权限")

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
                source = Path(args.save_file_path)
                if source.is_file() and (file_is_open(source) or not file_is_stable(source)):
                    log(f"源录像仍在写入，保留所有本地文件: {source.name}")
                    all_ok = False
                    continue
                if file_is_open(file) or not file_is_stable(file):
                    log(f"MP4 仍在写入，保留本地: {file.name}")
                    all_ok = False
                    continue
                file.unlink()

                log(
                    "上传校验成功，"
                    "删除本地 MP4: "
                    f"{file.name}"
                )

                if source.is_file() and source.suffix.lower() == '.ts':
                    source.unlink()
                    log(f"对应 MP4 已校验上传，删除本地 TS: {source.name}")

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
