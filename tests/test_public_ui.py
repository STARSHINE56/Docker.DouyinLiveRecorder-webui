"""Regression coverage for the public UI's API and safety boundaries."""
from pathlib import Path
import configparser
import subprocess

import webui


def test_status_and_home_page(monkeypatch):
    monkeypatch.setattr(webui, "parse_monitor_lines", lambda: [{
        "url": "https://live.douyin.com/1", "name": "A", "monitor_status": "waiting",
        "live_status": "offline", "recording_status": "idle", "last_checked_at": None,
    }])
    monkeypatch.setattr(webui.runtime_status, "load_state", lambda: {"monitors": {}, "events": []})
    client = webui.app.test_client()
    data = client.get("/api/status").json
    assert data["counts"] == {"total": 1, "live": 0, "recording": 0, "error": 0}
    html = client.get("/home").get_data(as_text=True)
    assert "星辰直播监控助手" in html and 'data-filter="offline"' in html


def test_anchor_add_delete_and_batch_safety(tmp_path, monkeypatch):
    urls = tmp_path / "URL_config.ini"
    urls.write_text("# preserved\nhttps://live.douyin.com/1,主播: 一号\nhttps://live.douyin.com/2\n")
    monkeypatch.setattr(webui, "URL_CONFIG_FILE", str(urls))
    client = webui.app.test_client()
    assert client.post("/anchors/add", data={"anchor_url": "3", "anchor_name": ""}).status_code == 302
    assert "https://live.douyin.com/3" in urls.read_text(encoding="utf-8-sig")
    before = urls.read_text(encoding="utf-8-sig")
    assert client.post("/anchors/batch-delete", data={"anchor_url": "not-present"}).status_code == 302
    assert urls.read_text(encoding="utf-8-sig") == before
    assert client.post("/anchors/batch-delete", data={"anchor_url": ["https://live.douyin.com/1", "https://live.douyin.com/3"]}).status_code == 302
    assert urls.read_text(encoding="utf-8-sig").startswith("# preserved\nhttps://live.douyin.com/2")
    assert client.post("/anchors/delete/0").status_code == 302
    assert urls.read_text(encoding="utf-8-sig").strip() == "# preserved"


def test_recording_metadata_and_path_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(webui, "_active_recording_file_statuses", lambda: {})
    (tmp_path / "test.mp4").write_bytes(b"abc")
    client = webui.app.test_client()
    listing = client.get("/api/recordings").json["recordings"]
    assert listing[0]["size"] == 3
    assert listing[0]["format"] == "MP4"
    assert listing[0]["upload_status"] == "unknown"
    assert client.post("/api/recordings/delete", json={"path": "../outside.mp4"}).status_code == 400
    monkeypatch.setattr(webui, "_active_recording_file_statuses", lambda: {(tmp_path / "test.mp4").resolve(): "recording"})
    assert client.post("/api/recordings/delete", json={"path": "test.mp4"}).status_code == 409
    assert (tmp_path / "test.mp4").exists()
    (tmp_path / "link.mp4").symlink_to(tmp_path / "test.mp4")
    assert client.post("/api/recordings/delete", json={"path": "link.mp4"}).status_code == 400
    assert client.post("/api/recordings/delete", json={"path": "notes.txt"}).status_code == 400
    monkeypatch.setattr(webui, "_active_recording_file_statuses", lambda: {})
    assert client.post("/api/recordings/delete", json={"path": "test.mp4"}).status_code == 200


def test_settings_password_preserved_and_webdav_test(tmp_path, monkeypatch):
    config_path = tmp_path / "config.ini"
    config = configparser.ConfigParser()
    config["小蓝网盘"] = {"WebDAV地址": "https://example.org/webdav", "WebDAV用户名": "user", "WebDAV密码": "secret"}
    with config_path.open("w") as f:
        config.write(f)
    monkeypatch.setattr(webui, "CONFIG_FILE", str(config_path))
    client = webui.app.test_client()
    assert client.post("/xiaolan_webdav", data={"WebDAV地址": "https://example.org/new", "WebDAV密码": ""}).status_code == 302
    assert webui.read_config(config_path).get("小蓝网盘", "WebDAV密码") == "secret"
    class Response:
        status_code = 207
    monkeypatch.setattr(webui.requests, "request", lambda *args, **kwargs: Response())
    assert client.post("/xiaolan_webdav/test").json["success"] is True
    assert "secret" not in client.get("/xiaolan_webdav").get_data(as_text=True)


def test_update_scripts_parse():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["bash", "-n", str(root / "scripts/fast-update.sh"), str(root / "scripts/rollback.sh")], check=True)


def test_fast_update_and_rollback_in_isolated_git_repo(tmp_path):
    """Exercise fetch, clean-worktree gate, code rollback and data retention with stub Compose."""
    import os
    import shutil
    root = Path(__file__).resolve().parents[1]
    remote = tmp_path / "remote.git"
    repo = tmp_path / "checkout"
    def run(*args, cwd=repo, env=None, check=True):
        return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=check)
    run("git", "init", "--bare", str(remote), cwd=tmp_path)
    run("git", "init", "-b", "main", str(repo), cwd=tmp_path)
    run("git", "config", "user.email", "test@example.org")
    run("git", "config", "user.name", "Test")
    run("git", "remote", "add", "origin", str(remote))
    for name in ("config", "logs", "downloads", "backup_config", "scripts"):
        (repo / name).mkdir()
    for name in ("fast-update.sh", "rollback.sh"):
        shutil.copy(root / "scripts" / name, repo / "scripts" / name)
    for name in ("docker-compose.yaml", "docker-compose.fast-update.yaml"):
        shutil.copy(root / name, repo / name)
    (repo / ".gitignore").write_text(".deploy/\nconfig/\nlogs/\ndownloads/\nbackup_config/\n")
    (repo / "webui.py").write_text("version = 1\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "baseline")
    run("git", "push", "-u", "origin", "main")
    (repo / "downloads" / "record.mp4").write_bytes(b"keep")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text("#!/bin/sh\ncase \"$*\" in *' ps --status running app'*) echo douyin-live-recorder-webui;; esac\nexit 0\n")
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}")
    (repo / "webui.py").write_text("version = 2\n")
    run("bash", "scripts/fast-update.sh", env=env, check=False)
    assert (repo / "webui.py").read_text() == "version = 2\n"  # local edit untouched
    run("git", "add", "webui.py")
    run("git", "commit", "-m", "update")
    run("git", "push", "origin", "main")
    run("git", "reset", "--hard", "HEAD~1")  # test fixture only
    result = run("bash", "scripts/fast-update.sh", env=env)
    assert "已更新至" in result.stdout
    assert (repo / "webui.py").read_text() == "version = 2\n"
    result = run("bash", "scripts/rollback.sh", env=env)
    assert "已回退至" in result.stdout
    assert (repo / "webui.py").read_text() == "version = 1\n"
    assert (repo / "downloads" / "record.mp4").read_bytes() == b"keep"
