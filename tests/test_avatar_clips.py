"""Avatar clips: upload a video, get a seamless loop added to the avatar."""
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.routers.avatar_clips import ffmpeg


@pytest.fixture()
def client(tmp_path):
    s = Settings(hermes_mode="mock", data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t",
                 _env_file=None)
    return TestClient(create_app(s))


def video(tmp_path, seconds=4, size="640x360"):
    path = tmp_path / "veo.mp4"
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-pix_fmt", "yuv420p",
                    "-shortest", str(path)], check=True)
    return path


def info(path):
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return err


def upload(c, path, **form):
    with open(path, "rb") as f:
        return c.post("/api/admin/avatar/clips", files={"file": ("veo.mp4", f, "video/mp4")}, data=form)


def test_upload_makes_a_loop_and_adds_it(client, tmp_path):
    src = video(tmp_path)
    assert upload(client, src, kind="idle").status_code == 401           # admin only
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    r = upload(client, src, kind="idle")
    assert r.status_code == 200, r.text
    out = r.json()
    assert re.fullmatch(r"/api/avatar/clips/idle_[a-f0-9]{8}\.mp4", out["url"]) and out["posterUrl"]
    assert 2.0 <= out["seconds"] <= 3.0                                  # 4 s, minus 0.5 skipped and the fade
    avatar = client.get("/api/admin/settings").json()["avatar"]
    assert avatar["idleVideos"] == [out["url"]] and avatar["posterUrl"] == out["posterUrl"]

    clip = client.get(out["url"])
    assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
    saved = tmp_path / "clip.mp4"
    saved.write_bytes(clip.content)
    meta = info(saved)
    assert "720x720" in meta and "Audio" not in meta                     # square, no sound

    r2 = upload(client, src, kind="talk", shape="wide").json()
    avatar = client.get("/api/admin/settings").json()["avatar"]
    assert avatar["talkVideos"] == [r2["url"]] and avatar["posterUrl"] == out["posterUrl"]   # poster kept
    assert r2["posterUrl"] is None


def test_bad_files_are_explained(client, tmp_path):
    client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    junk = tmp_path / "notes.mp4"
    junk.write_text("not a video")
    r = upload(client, junk, kind="talk")
    assert r.status_code == 400 and "doesn't look like a video" in r.json()["detail"]
    short = video(tmp_path, seconds=1)
    r = upload(client, short, kind="talk")
    assert r.status_code == 400 and "too short" in r.json()["detail"]
    assert client.get("/api/avatar/clips/../app.db").status_code == 404
    assert client.get("/api/avatar/clips/secret.txt").status_code == 404
