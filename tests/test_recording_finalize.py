import ast
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import status_runtime


def load_recording_functions(namespace):
    """Load the two focused functions without executing main.py's service loop."""
    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"run_script", "finalize_recording", "check_subprocess"}
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


class RecordingFinalizeTests(unittest.TestCase):
    def base_namespace(self):
        return {
            "Path": Path,
            "os": os,
            "os_type": os.name,
            "signal": signal,
            "subprocess": subprocess,
            "time": time,
            "logger": MagicMock(),
            "converts_to_mp4": True,
            "split_video_by_time": False,
            "delete_origin_file": False,
            "recording_segment_paths": MagicMock(return_value=[]),
            "converts_mp4": MagicMock(),
            "run_post_record_script": MagicMock(),
            "runtime_status": status_runtime,
            "recording": set(),
            "create_time_file": False,
            "create_var": {},
            "generate_subtitles": MagicMock(),
            "get_startup_info": MagicMock(return_value=None),
            "url_comments": [],
            "exit_recording": False,
            "clear_record_info": MagicMock(),
        }

    def test_finalize_ts_converts_then_runs_post_record_script(self):
        namespace = load_recording_functions(self.base_namespace())

        namespace["finalize_recording"](
            "序号1 测试主播", "/downloads/live.ts", "TS", "python upload_after_record.py"
        )

        namespace["converts_mp4"].assert_called_once_with("/downloads/live.ts", False)
        namespace["run_post_record_script"].assert_called_once_with(
            "python upload_after_record.py", "序号1 测试主播", "/downloads/live.ts", "TS"
        )

    def test_post_record_script_nonzero_exit_is_logged_as_failure(self):
        namespace = self.base_namespace()

        class FailedScript:
            returncode = 1

            def communicate(self):
                return b"", b"upload failed"

        namespace["subprocess"] = SimpleNamespace(
            Popen=MagicMock(return_value=FailedScript()),
            PIPE=-1,
            CalledProcessError=subprocess.CalledProcessError,
        )
        load_recording_functions(namespace)

        with self.assertRaises(subprocess.CalledProcessError):
            namespace["run_script"]("python upload_after_record.py")
        namespace["logger"].error.assert_called_once_with(
            "录制后脚本执行失败，退出码: 1"
        )

    def test_manual_stop_finalizes_and_upload_failure_does_not_recover(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        status_runtime.reset_for_tests(Path(temp.name) / "runtime_state.json")
        url = "https://live.douyin.com/manual-stop"
        name = "序号1 手动停止测试"
        status_runtime.update_live_status(url, "手动停止测试", True, stream_valid=True)

        class FakeProcess:
            pid = 4321

            def __init__(self):
                self.returncode = None
                self.stop_requested = False

            def poll(self):
                if not self.stop_requested:
                    success, _ = status_runtime.request_manual_stop(url)
                    if not success:
                        raise AssertionError("manual stop request was rejected")
                    self.stop_requested = True
                    return None
                return self.returncode

            def send_signal(self, sent_signal):
                if sent_signal != signal.SIGINT:
                    raise AssertionError("FFmpeg did not receive SIGINT")

            def wait(self):
                self.returncode = 255
                return self.returncode

        process = FakeProcess()
        namespace = self.base_namespace()
        namespace["run_post_record_script"].side_effect = RuntimeError("upload failed")
        namespace["subprocess"] = SimpleNamespace(
            Popen=MagicMock(return_value=process), PIPE=-1, STDOUT=-2
        )
        load_recording_functions(namespace)

        stopped = namespace["check_subprocess"](
            name,
            url,
            ["ffmpeg", "-i", "stream", "/downloads/manual.ts"],
            "TS",
            "python upload_after_record.py",
        )

        self.assertTrue(stopped)
        namespace["converts_mp4"].assert_called_once_with("/downloads/manual.ts", False)
        namespace["run_post_record_script"].assert_called_once()
        namespace["logger"].exception.assert_called_once()
        state = status_runtime.load_state()
        item = state["monitors"][url]
        self.assertEqual(item["recording_status"], "completed")
        self.assertTrue(item["manual_stop"])
        event_types = [event["type"] for event in state["events"]]
        self.assertIn("RECORDING_STOP_REQUESTED", event_types)
        self.assertIn("RECORDING_ENDED", event_types)
        self.assertNotIn("RECORDING_RECOVERING", event_types)
        self.assertFalse(status_runtime.claim_recording_task(url))

        self.assertEqual(status_runtime.update_live_status(url, "手动停止测试", False), "suspected_offline")
        self.assertFalse(status_runtime.claim_recording_task(url))
        self.assertEqual(status_runtime.update_live_status(url, "手动停止测试", False), "offline")
        self.assertFalse(status_runtime.load_state()["monitors"][url]["manual_stop"])
        self.assertTrue(status_runtime.claim_recording_task(url))
        status_runtime.release_recording_task(url)


if __name__ == "__main__":
    unittest.main()
