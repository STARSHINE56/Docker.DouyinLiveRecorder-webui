"""Small, process-safe runtime state bridge between recorder and WebUI.

The recorder remains the source of truth.  This module only persists its
observations and FFmpeg lifecycle so the WebUI never has to infer business
state from human-readable log messages.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


STATE_FILE = Path(os.environ.get("DLR_RUNTIME_STATE", "config/runtime_state.json"))
LOCK_FILE = STATE_FILE.with_suffix(".lock")
EVENT_LIMIT = 200

MONITOR_STATUSES = {"waiting", "checking", "running", "error", "disabled"}
LIVE_STATUSES = {"unknown", "offline", "suspected_live", "live", "suspected_offline"}
RECORDING_STATUSES = {
    "idle", "starting", "recording", "recovering", "stopping", "completed", "error", "interrupted"
}

_thread_lock = threading.RLock()
_active_detection_tasks: set[str] = set()
_active_recording_tasks: set[str] = set()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_state() -> dict[str, Any]:
    return {"version": 1, "monitors": {}, "events": [], "event_keys": []}


def _default_monitor(url: str, name: str = "") -> dict[str, Any]:
    return {
        "url": url,
        "name": name or "待识别主播",
        "monitor_status": "waiting",
        "live_status": "unknown",
        "recording_status": "idle",
        "last_checked_at": None,
        "last_success_at": None,
        "live_started_at": None,
        "recording_started_at": None,
        "recording_file": None,
        "recording_pid": None,
        "recording_mode": None,
        "recording_recovery_pending": False,
        "manual_stop": False,
        "last_error": None,
        "session_id": None,
        "offline_confirmations": 0,
    }


@contextmanager
def _locked_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, LOCK_FILE.open("a+") as lock_handle:
        if fcntl:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_unlocked()
        try:
            yield state
            _save_unlocked(state)
        finally:
            if fcntl:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked() -> dict[str, Any]:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _default_state()
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _default_state()
    state = _default_state()
    state.update(raw)
    state["monitors"] = raw.get("monitors") if isinstance(raw.get("monitors"), dict) else {}
    state["events"] = raw.get("events") if isinstance(raw.get("events"), list) else []
    state["event_keys"] = raw.get("event_keys") if isinstance(raw.get("event_keys"), list) else []
    return state


def _save_unlocked(state: dict[str, Any]) -> None:
    state["events"] = state.get("events", [])[-EVENT_LIMIT:]
    state["event_keys"] = state.get("event_keys", [])[-EVENT_LIMIT * 2:]
    fd, temp_name = tempfile.mkstemp(prefix="runtime-", suffix=".json", dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_state() -> dict[str, Any]:
    with _thread_lock:
        return _load_unlocked()


def _monitor(state: dict[str, Any], url: str, name: str = "") -> dict[str, Any]:
    item = state["monitors"].setdefault(url, _default_monitor(url, name))
    defaults = _default_monitor(url, name)
    for key, value in defaults.items():
        item.setdefault(key, value)
    if name and name not in {"待识别主播", "等待获取主播名"}:
        item["name"] = name
    return item


def _emit_locked(
    state: dict[str, Any], item: dict[str, Any], event_type: str, message: str,
    *, once_per_session: bool = True,
) -> bool:
    session_id = item.get("session_id") or "no-session"
    key = f"{item['url']}:{event_type}:{session_id}"
    if once_per_session and key in state["event_keys"]:
        return False
    state["event_keys"].append(key)
    state["events"].append({
        "key": key,
        "type": event_type,
        "streamer_id": item["url"],
        "streamer_name": item.get("name") or "待识别主播",
        "session_id": item.get("session_id"),
        "message": message,
        "timestamp": _now(),
    })
    return True


def emit_event(url: str, name: str, event_type: str, message: str, *, once_per_session: bool = True) -> bool:
    with _locked_state() as state:
        return _emit_locked(state, _monitor(state, url, name), event_type, message, once_per_session=once_per_session)


def mark_checking(url: str, name: str = "") -> None:
    with _locked_state() as state:
        item = _monitor(state, url, name)
        item["monitor_status"] = "checking"
        item["last_checked_at"] = _now()


def update_live_status(url: str, name: str, is_live: bool, *, stream_valid: bool = False) -> str:
    """Apply a real parser/API result, including two-pass offline confirmation."""
    with _locked_state() as state:
        item = _monitor(state, url, name)
        previous = item["live_status"]
        item["monitor_status"] = "running"
        item["last_checked_at"] = _now()
        item["last_success_at"] = item["last_checked_at"]
        item["last_error"] = None

        if is_live and stream_valid:
            if previous not in {"live", "suspected_offline"}:
                item["session_id"] = uuid.uuid4().hex
                item["live_started_at"] = _now()
                _emit_locked(state, item, "LIVE_STARTED", "检测到开播")
            item["live_status"] = "live"
            item["offline_confirmations"] = 0
            return "live"

        if is_live:
            item["live_status"] = "suspected_live"
            item["offline_confirmations"] = 0
            return "suspected_live"

        if previous in {"live", "suspected_offline"}:
            confirmations = int(item.get("offline_confirmations") or 0) + 1
            item["offline_confirmations"] = confirmations
            if confirmations < 2:
                item["live_status"] = "suspected_offline"
                return "suspected_offline"
            item["live_status"] = "offline"
            item["manual_stop"] = False
            if item["recording_status"] not in {"idle", "completed", "error", "interrupted"}:
                item["recording_status"] = "completed"
                item["recording_pid"] = None
                item["recording_recovery_pending"] = False
                _emit_locked(state, item, "RECORDING_ENDED", "录制完成")
            _emit_locked(state, item, "LIVE_ENDED", "检测到下播")
            return "offline"

        item["live_status"] = "offline"
        item["offline_confirmations"] = 0
        item["manual_stop"] = False
        return "offline"


def mark_check_failed(url: str, name: str, error: str) -> None:
    """Record a failed detection pass.

    When the check itself failed and there is no evidence of an active
    recording process, clear any stale live_status so the UI cannot keep
    showing "直播中" after a parse/API error.
    """
    with _locked_state() as state:
        item = _monitor(state, url, name)
        item["monitor_status"] = "error"
        item["last_checked_at"] = _now()
        item["last_error"] = str(error)[:500]
        # Only clear live evidence when we do not currently have a real
        # recording task.  Active / recovering recordings keep their state
        # until the process finishes or reconcile_stale_recordings runs.
        recording = item.get("recording_status")
        if recording not in {"starting", "recording", "recovering", "stopping"}:
            item["live_status"] = "unknown"
            item["offline_confirmations"] = 0
        _emit_locked(state, item, "CHECK_FAILED", "直播状态检测失败", once_per_session=False)


def mark_recording_starting(
    url: str, name: str, file_path: str | None = None, *, mode: str = "ffmpeg",
) -> None:
    with _locked_state() as state:
        item = _monitor(state, url, name)
        # Preserve recovery intent across the real lifecycle transition
        # recovering -> starting -> recording.
        item["recording_recovery_pending"] = (
            item["recording_status"] == "recovering"
            or bool(item.get("recording_recovery_pending"))
        )
        item["recording_status"] = "starting"
        item["recording_file"] = file_path
        item["recording_mode"] = mode


def mark_recording_started(
    url: str, name: str, pid: int, file_path: str, *, mode: str = "ffmpeg",
) -> None:
    with _locked_state() as state:
        item = _monitor(state, url, name)
        recovering = (
            item["recording_status"] == "recovering"
            or bool(item.get("recording_recovery_pending"))
        )
        item["recording_status"] = "recording"
        item["recording_pid"] = pid
        item["recording_mode"] = mode
        item["recording_file"] = file_path
        item["recording_started_at"] = item.get("recording_started_at") or _now()
        item["recording_recovery_pending"] = False
        if recovering:
            _emit_locked(state, item, "RECORDING_RECOVERED", "录制已恢复")
        else:
            _emit_locked(state, item, "RECORDING_STARTED", "开始录制")


def mark_recording_finished(
    url: str, name: str, return_code: int, *, intentional: bool = False,
    recover_if_live: bool = True,
) -> str:
    with _locked_state() as state:
        item = _monitor(state, url, name)
        item["recording_pid"] = None
        # Any unrequested FFmpeg exit while the API still says live is treated
        # as a stream break, even when FFmpeg happens to return zero.
        if recover_if_live and not intentional and item["live_status"] in {"live", "suspected_offline"}:
            item["recording_status"] = "recovering"
            item["recording_recovery_pending"] = True
            item["last_error"] = f"FFmpeg 已退出（代码 {return_code}），等待重新检测直播状态"
            _emit_locked(state, item, "RECORDING_RECOVERING", "录制断流，正在恢复")
            return "recovering"
        item["recording_status"] = "completed" if return_code == 0 or intentional else "error"
        item["recording_recovery_pending"] = False
        if item["recording_status"] == "error":
            item["last_error"] = f"FFmpeg 退出，代码 {return_code}"
        if intentional and item.get("manual_stop"):
            message = "手动停止录制"
        else:
            message = "录制完成" if item["recording_status"] == "completed" else "录制异常结束"
        _emit_locked(state, item, "RECORDING_ENDED", message)
        return item["recording_status"]


def request_manual_stop(url: str) -> tuple[bool, str]:
    """Request a cooperative stop for one FFmpeg recording only."""
    with _locked_state() as state:
        item = state["monitors"].get(url)
        if not item or item.get("recording_status") not in {"starting", "recording", "recovering"}:
            return False, "当前主播没有可停止的录制任务"
        if item.get("recording_mode") == "direct":
            return False, "当前录制模式暂不支持手动停止"
        item["manual_stop"] = True
        item["recording_status"] = "stopping"
        item["recording_recovery_pending"] = False
        _emit_locked(state, item, "RECORDING_STOP_REQUESTED", "手动请求停止录制")
        return True, "正在停止录制"


def is_manual_stop_requested(url: str) -> bool:
    with _thread_lock:
        item = _load_unlocked().get("monitors", {}).get(url, {})
        return bool(item.get("manual_stop"))


def run_direct_recording(
    url: str, name: str, file_path: str, recorder: Callable[[], Any],
) -> bool:
    """Bridge an in-process blocking recorder (such as urlretrieve) to runtime state."""
    if not claim_recording_task(url):
        return False
    mark_recording_starting(url, name, file_path, mode="direct")
    mark_recording_started(url, name, os.getpid(), file_path, mode="direct")
    try:
        recorder()
    except Exception:
        mark_recording_finished(url, name, 1, recover_if_live=False)
        raise
    else:
        mark_recording_finished(url, name, 0, recover_if_live=False)
        return True
    finally:
        release_recording_task(url)


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, OSError, ProcessLookupError):
        return False


def reconcile_stale_recordings(pid_checker: Callable[[Any], bool] = _pid_alive) -> int:
    repaired = 0
    with _locked_state() as state:
        for item in state["monitors"].values():
            if item.get("recording_status") in {"starting", "recording", "recovering", "stopping"}:
                if not pid_checker(item.get("recording_pid")):
                    item["recording_status"] = "interrupted"
                    item["recording_pid"] = None
                    item["recording_recovery_pending"] = False
                    item["last_error"] = "服务重启后未发现原 FFmpeg 进程"
                    _emit_locked(state, item, "RECORDING_ENDED", "录制因服务重启中断")
                    repaired += 1
    return repaired


def claim_detection_task(url: str) -> bool:
    with _thread_lock:
        if url in _active_detection_tasks:
            return False
        _active_detection_tasks.add(url)
        return True


def release_detection_task(url: str) -> None:
    with _thread_lock:
        _active_detection_tasks.discard(url)


def claim_recording_task(url: str) -> bool:
    with _locked_state() as state:
        item = state.get("monitors", {}).get(url, {})
        if item.get("manual_stop"):
            return False
        if url in _active_recording_tasks:
            return False
        _active_recording_tasks.add(url)
        return True


def release_recording_task(url: str) -> None:
    with _thread_lock:
        _active_recording_tasks.discard(url)


def reset_for_tests(path: Path) -> None:
    global STATE_FILE, LOCK_FILE
    STATE_FILE = path
    LOCK_FILE = path.with_suffix(".lock")
    with _thread_lock:
        _active_detection_tasks.clear()
        _active_recording_tasks.clear()
