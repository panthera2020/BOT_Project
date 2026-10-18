"""
Tests for scripts/run_on_phone.sh: refuses unsafe setups, keeps the state file on the device,
restarts after a hard kill and the restarted app loads the last saved state.
"""
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_on_phone.sh")
pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash launcher")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_env(tmp_path, **over):
    vals = {"ADMIN_USER": "tester", "ADMIN_PASS": "a-long-test-password"}
    vals.update(over)
    f = tmp_path / "bot.env"
    f.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    os.chmod(f, 0o600)
    return str(f)


def launch(tmp_path, env_file, port, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("ADMIN_USER", "ADMIN_PASS", "DATABASE_URL", "STATE_ENCRYPTION_KEY", "AUTO_RESUME_LIVE")}
    env.update(BOT_ENV_FILE=env_file, BOT_STATE_DIR=str(tmp_path / "state"), PORT=str(port),
               BOT_PYTHON=sys.executable, BOT_RESTART_DELAY="1", HOME=str(tmp_path))
    env.update(extra)
    return subprocess.Popen(["bash", SCRIPT], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)


def call(port, path, user="tester", pw="a-long-test-password", method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def wait_up(port, secs=30):
    end = time.time() + secs
    while time.time() < end:
        try:
            call(port, "/api/status")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return proc.communicate(timeout=15)[0]
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        return proc.communicate()[0]


@pytest.mark.parametrize("over,msg", [
    ({"ADMIN_PASS": "P@ssword123!"}, "public default"),
    ({"ADMIN_PASS": "short"}, "at least 10"),
])
def test_refuses_unsafe_passwords(tmp_path, over, msg):
    p = launch(tmp_path, write_env(tmp_path, **over), free_port())
    out = p.communicate(timeout=20)[0]
    assert p.returncode != 0 and msg in out


def test_refuses_when_admin_user_missing(tmp_path):
    f = tmp_path / "bot.env"
    f.write_text("ADMIN_PASS=a-long-test-password\n")
    p = launch(tmp_path, str(f), free_port())
    assert "ADMIN_USER is not set" in p.communicate(timeout=20)[0]
    assert p.returncode != 0


def test_state_survives_kill_9_and_a_clean_restart(tmp_path):
    port = free_port()
    env_file = write_env(tmp_path, STATE_ENCRYPTION_KEY="a-test-passphrase-123")
    p = launch(tmp_path, env_file, port)
    try:
        assert wait_up(port), "app did not start"
        assert (tmp_path / "state").is_dir()
        call(port, "/api/demo/start", method="POST")
        assert call(port, "/api/status")["persistence"]["enabled"] is True
        assert os.path.exists(tmp_path / "state" / "trading_bot.db")

        # hard-kill the app itself (not the launcher), as Android would
        out = subprocess.run(["pgrep", "-f", f"uvicorn src.web.app:app.*--port {port}"], capture_output=True, text=True).stdout.split()
        assert out, "app process not found"
        for pid in out:
            os.kill(int(pid), signal.SIGKILL)

        # the launcher brings it back and it loads the saved state, no click needed
        time.sleep(1.5)
        assert wait_up(port), "app was not restarted"
        status = call(port, "/api/status")
        assert status["persistence"]["resumed"], status["persistence"]
    finally:
        log = stop(p)
    assert "Restarting in" in log
    assert "Stopped." in log


def test_stopping_the_launcher_stops_the_app_and_does_not_restart(tmp_path):
    port = free_port()
    p = launch(tmp_path, write_env(tmp_path), port)
    assert wait_up(port)
    log = stop(p)
    assert "Restarting in" not in log
    time.sleep(1)
    with pytest.raises(Exception):
        call(port, "/api/status")


def test_backups_are_written_when_a_backup_folder_is_set(tmp_path):
    port = free_port()
    backups = tmp_path / "bk"
    p = launch(tmp_path, write_env(tmp_path), port, BOT_BACKUP_DIR=str(backups), BOT_BACKUP_EVERY_SEC="1")
    try:
        assert wait_up(port)
        call(port, "/api/demo/start", method="POST")
        end = time.time() + 15
        while time.time() < end and not list(backups.glob("trading_bot-*.db")):
            time.sleep(0.5)
    finally:
        stop(p)
    assert list(backups.glob("trading_bot-*.db"))
