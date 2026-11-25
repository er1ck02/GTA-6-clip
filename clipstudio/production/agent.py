"""``clipstudio production start|stop|status|supervise|shortcuts``: the command-line half of the production agent.

* ``start`` opens everything: the server in its own window (watched by a supervisor that restarts it if it
  crashes), waits until it answers, re-engages the last lineup (with stand-ins for offline channels), finishes
  what the last stop left, and opens the workstation in the browser.
* ``stop`` drains safely: recording stops at once, every saved moment, VOD and upload in flight is finished
  (``--now``: only what is mid-edit; the rest is saved for the next start), it counts down how long that takes,
  then closes the server so the PC is free.

The server settings used the first time (``--config/--profile/--set``) are remembered in
``~/.clipstudio/production.json`` so the desktop shortcuts need no arguments.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from clipstudio.config import load_config
from clipstudio.core.errors import ClipStudioError

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8765


def home() -> Path:
    return Path(os.environ.get("CLIPSTUDIO_HOME") or Path.home() / ".clipstudio")


def say(msg: str = "", quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


# ------------------------------------------------------------------ launch settings
def launch_file() -> Path:
    return home() / "production.json"


def default_server_args() -> list[str]:
    args = ["--config", str(REPO / "configs" / "default.yaml")]
    user = home() / "config.yaml"
    if user.is_file():
        args += ["--config", str(user)]
    args += ["--profile", "quality"]
    if (REPO / "yt_data").is_dir() and not os.environ.get("CLIPSTUDIO_DATA_DIR"):
        args += ["--set", f"paths.data_dir={REPO / 'yt_data'}"]
    return args


def resolve_launch(a, persist: bool = False) -> dict:
    """Server settings: the ones given now (remembered), else the remembered ones, else sensible defaults."""
    given = []
    for f in getattr(a, "config", None) or []:
        given += ["--config", str(Path(f).expanduser().resolve())]
    if getattr(a, "profile", None):
        given += ["--profile", a.profile]
    for o in getattr(a, "overrides", None) or []:
        given += ["--set", o]
    saved = {}
    try:
        saved = json.loads(launch_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    port = getattr(a, "port", None) or saved.get("port") or DEFAULT_PORT
    if given and getattr(a, "config", None) and not any("default.yaml" in x for x in given):
        given = ["--config", str(REPO / "configs" / "default.yaml")] + given
    launch = {"server_args": given or saved.get("server_args") or default_server_args(), "port": int(port)}
    if persist and (given or not saved or saved.get("port") != launch["port"]):
        try:
            launch_file().parent.mkdir(parents=True, exist_ok=True)
            launch_file().write_text(json.dumps(launch, indent=2), encoding="utf-8")
        except OSError:
            pass
    return launch


def config_for(launch: dict):
    """The Config the server will load, parsed from the same arguments."""
    files, overrides, profile = [], [], None
    it = iter(launch["server_args"])
    for x in it:
        if x == "--config":
            files.append(next(it))
        elif x == "--set":
            overrides.append(next(it))
        elif x == "--profile":
            profile = next(it)
    return load_config(files or None, overrides, profile)


def server_cmd(launch: dict) -> list[str]:
    return [sys.executable, "-m", "app.cli", "ui", "--port", str(launch["port"]), *launch["server_args"]]


# ------------------------------------------------------------------ HTTP
def api(port: int, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:  # noqa: BLE001
            msg = e.reason
        raise ClipStudioError(f"{path}: {msg}") from e


def server_state(port: int) -> str:
    """'production' (new server answering), 'old' (a server without production controls), or 'down'."""
    try:
        api(port, "GET", "/api/production", timeout=5)
        return "production"
    except ClipStudioError:
        return "old"
    except (urllib.error.URLError, OSError, ValueError):
        return "down"


def wait_for(port: int, want: str, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if server_state(port) == want:
            return True
        time.sleep(1.0)
    return False


# ------------------------------------------------------------------ PATH repair
def _registry_path() -> list[str]:
    """PATH as Windows has it saved now (user + machine). A console opened before winget installed FFmpeg, or
    started by another program, inherits an older PATH without it."""
    out: list[str] = []
    try:
        import winreg  # type: ignore[import-not-found]

        for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    out += [os.path.expandvars(x) for x in str(val).split(";") if x.strip()]
            except OSError:
                pass
    except ImportError:
        pass
    return out


def _known_ffmpeg_dirs() -> list[str]:
    import glob

    local = os.environ.get("LOCALAPPDATA", "")
    pats = [os.path.join(local, "Microsoft", "WinGet", "Links"),
            os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin"),
            os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "bin"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin"),
            r"C:\ffmpeg\bin", os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey", "bin"),
            os.path.join(os.path.expanduser("~"), "scoop", "shims")]
    out = []
    for p in pats:
        out += sorted(glob.glob(p), reverse=True)  # newest FFmpeg build first
    return out


def repair_path() -> list[str]:
    """Make sure FFmpeg, ffprobe and the venv's yt-dlp are on PATH for this process and the server it starts.
    Returns the folders that were added."""
    cur = os.environ.get("PATH", "").split(os.pathsep)
    seen = {os.path.normcase(os.path.normpath(x)) for x in cur if x}
    added = []

    def add(d: str) -> None:
        n = os.path.normcase(os.path.normpath(d))
        if d and n not in seen and os.path.isdir(d):
            seen.add(n)
            added.append(d)

    add(str(Path(sys.executable).parent))  # .venv\Scripts: yt-dlp, streamlink
    if sys.platform == "win32":
        for d in _registry_path():
            add(d)
        if not shutil.which("ffmpeg", path=os.pathsep.join(cur + added)):
            for d in _known_ffmpeg_dirs():
                if os.path.isfile(os.path.join(d, "ffmpeg.exe")):
                    add(d)
                    break
    if added:
        os.environ["PATH"] = os.pathsep.join(cur + added)
    return added


# ------------------------------------------------------------------ preflight
def preflight(cfg) -> list[tuple[str, bool, str]]:
    """Quick checks before starting (the full ``doctor`` runs inside the workstation)."""
    from clipstudio.media.ffmpeg import ffmpeg_bin

    out = []
    try:
        ffmpeg_bin()
        out.append(("FFmpeg", True, "found"))
    except Exception as e:  # noqa: BLE001
        out.append(("FFmpeg", False, f"{e} (install it, then start again)"))
    rec = shutil.which("streamlink") or shutil.which("yt-dlp")
    if not rec:
        try:
            import yt_dlp  # noqa: F401
            rec = "yt_dlp module"
        except ImportError:
            pass
    out.append(("Stream recorder", bool(rec), rec or "needs yt-dlp or streamlink: pip install -e \".[ingest]\""))
    data = Path(cfg.paths.data_dir).expanduser()
    try:
        data.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(data).free / 1e9
        out.append(("Disk space", free >= 10, f"{free:.0f} GB free in {data}"))
    except OSError as e:
        out.append(("Disk space", False, str(e)))
    tok = home() / "youtube_token.json"
    if (home() / "youtube_client_secret.json").exists() and not tok.exists():
        out.append(("YouTube sign-in", False, "not connected: open UPLOADS in the workstation and press CONNECT "
                    "YOUTUBE (clips wait in the review queue until then)"))
    return out


# ------------------------------------------------------------------ start
def spawn_supervisor(launch: dict) -> None:
    cmd = [sys.executable, "-m", "app.cli", "production", "supervise", "--port", str(launch["port"])]
    if sys.platform == "win32":
        subprocess.Popen(cmd, cwd=REPO, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
    else:
        logs = REPO / "logs"
        logs.mkdir(exist_ok=True)
        with open(logs / "production-server.log", "ab") as fh:
            subprocess.Popen(cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)


def start(a) -> dict:
    quiet = a.json
    launch = resolve_launch(a, persist=True)
    port = launch["port"]
    cfg = config_for(launch)
    say("Starting Clip Studio production", quiet)
    for d in repair_path():
        say(f"  .. added to PATH for Clip Studio: {d}", quiet)
    checks = preflight(cfg)
    for name, ok, note in checks:
        say(f"  {'OK ' if ok else '!! '} {name}: {note}", quiet)
    if not checks[0][1]:
        raise ClipStudioError("FFmpeg is missing, so nothing can be recorded or edited")
    st = server_state(port)
    if st == "old":
        raise ClipStudioError(f"a Clip Studio server without production controls is running on port {port}. "
                              "Close its window (or stop it), then start production again")
    if st == "down":
        say(f"  .. opening the server window (port {port})", quiet)
        spawn_supervisor(launch)
        if not wait_for(port, "production", 240):
            raise ClipStudioError(f"the server did not answer within 4 minutes; look at its window or "
                                  f"{REPO / 'logs'} for the error")
    else:
        say("  .. server already running", quiet)
    channels = list(getattr(a, "channel", None) or [])
    say("  .. checking which channels are live (about 30 s)", quiet)
    res = api(port, "POST", "/api/production/start", {"channels": channels or None}, timeout=600)
    for p in res.get("plan", []):
        mark = {"watch": "LIVE", "keep": "LIVE", "substitute": "SWAP", "fill": "EXTRA"}.get(p["action"], "WAIT")
        say(f"  {mark:<4} {p.get('channel') or p['wanted']}: {p['note']}", quiet)
    url = f"http://127.0.0.1:{port}"
    if cfg.production.open_browser and not getattr(a, "no_browser", False):
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    say("", quiet)
    say(f"{res.get('message', 'production running')}. Workstation: {url}", quiet)
    say("To stop: double-click Stop Production (or run: python -m app.cli production stop)", quiet)
    return res


# ------------------------------------------------------------------ stop
def stop(a) -> dict:
    quiet = a.json
    launch = resolve_launch(a)
    port = launch["port"]
    st = server_state(port)
    if st == "down":
        say("Clip Studio isn't running. Safe to use your PC.", quiet)
        return {"mode": "stopped", "safe": True, "message": "not running"}
    if st == "old":
        raise ClipStudioError("the running server has no production controls; stop its streams in the "
                              "workstation, wait for edits to finish, then close its window")
    mode = "now" if a.now else "safe"
    res = api(port, "POST", "/api/production/stop", {"mode": mode}, timeout=30)
    t0 = time.time()
    if not res["safe"]:
        mins = max(1, round(res["eta_seconds"] / 60))
        say(f"Stopping production. Please wait about {mins} minute{'s' if mins != 1 else ''}.", quiet)
        say(f"  {res['message']}", quiet)
        if mode == "safe" and res["eta_seconds"] > 300:
            say("  In a hurry? Run Stop Production NOW: it finishes only the clip being edited and saves the "
                "rest for the next start.", quiet)
    if a.no_wait:
        return res
    last = ""
    while not res["safe"]:
        time.sleep(3)
        try:
            res = api(port, "GET", "/api/production", timeout=15)
        except (ClipStudioError, urllib.error.URLError, OSError) as e:
            raise ClipStudioError(f"lost contact with the server while it was stopping: {e}") from e
        line = f"  {res['message']} (about {max(0, round(res['eta_seconds']))} s left)"
        if line != last and not quiet:
            print("\r" + line.ljust(110), end="", flush=True)
            last = line
    if last and not quiet:
        print()
    if not a.keep_server:
        api(port, "POST", "/api/production/shutdown", {}, timeout=15)
        wait_for(port, "down", 30)
    took = round(time.time() - t0)
    saved = (res.get("last_stop") or {}).get("saved_for_next_start") or 0
    say(f"Done{f' in {took} s' if took > 5 else ''}. {res['message'][0].upper() + res['message'][1:]}.", quiet)
    if not a.keep_server:
        say("Clip Studio is closed. Start Production picks up the same channels" +
            (f" and finishes the {saved} saved item(s)." if saved else "."), quiet)
    return {**res, "seconds": took, "server_closed": not a.keep_server}


# ------------------------------------------------------------------ status
def status(a) -> dict:
    launch = resolve_launch(a)
    port = launch["port"]
    st = server_state(port)
    if st != "production":
        res = {"server": st, "mode": "stopped" if st == "down" else "unknown", "safe": st == "down"}
        try:
            cfg = config_for(launch)
            doc = json.loads((Path(cfg.paths.data_dir).expanduser() / "production" / "state.json").read_text())
            res.update(lineup=doc.get("lineup"), last_stop=doc.get("last_stop"), saved_mode=doc.get("mode"))
        except (OSError, ValueError):
            pass
        if not a.json:
            say("Clip Studio is not running." if st == "down" else "An older server is running (no production "
                "controls).")
            for e in res.get("lineup") or []:
                say(f"  next start resumes: {e['channel']}")
        return res
    res = api(port, "GET", "/api/production", timeout=15)
    if not a.json:
        say(f"Production: {res['mode']}. {res['message']}")
        f = res["in_flight"]
        say(f"  streams {f['streams']} · editing {f['editing']} · waiting {f['waiting']} · VODs "
            f"{f['vod_running'] + f['vod_queued']} · uploading {f['uploading']} · avg edit "
            f"{res['avg_edit_seconds']:.0f} s")
        for s in res["slots"]:
            say(f"  {s['wanted']:<16} {s['action']:<10} {s.get('note', '')}")
        if res.get("backlog_saved"):
            say(f"  {res['backlog_saved']} item(s) saved for the next start")
    return res


# ------------------------------------------------------------------ supervise (runs in the server window)
def supervise(a) -> int:
    """Run the server and bring it back if it crashes, until production is stopped."""
    repair_path()  # the server inherits this PATH (FFmpeg from winget, the venv's yt-dlp)
    launch = resolve_launch(a)
    port = launch["port"]
    cfg = config_for(launch)
    state = Path(cfg.paths.data_dir).expanduser().resolve() / "production" / "state.json"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW("Clip Studio server (use Stop Production to close)")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print("Clip Studio server. Don't close this window: double-click Stop Production instead, so clips being "
          "edited are finished and saved.\n", flush=True)
    crashes: list[float] = []
    update: dict = {}  # set by the auto-update thread: {"sha": ..., "summary": ...} while restarting onto it
    running: dict = {}  # {"sha": the commit the server was last started on}
    if cfg.production.auto_update:
        threading.Thread(target=_auto_update, args=(port, cfg, state, update, None, running), daemon=True,
                         name="clipstudio-auto-update").start()
    while True:
        started = time.time()
        from clipstudio.production import updater

        running["sha"] = updater.head()
        try:
            rc = subprocess.call(server_cmd(launch), cwd=REPO)
        except KeyboardInterrupt:
            print("stopped with Ctrl+C. Unfinished clips are edited on the next Start Production.", flush=True)
            return 130
        if update.get("sha"):
            from clipstudio.production import updater

            ok, out = updater.fast_forward(update["sha"])
            print(("updated to " + update["sha"][:7] + ": " + update.get("summary", "")) if ok
                  else f"update could not be applied ({out}); restarting on the old code", flush=True)
            update.clear()
            threading.Thread(target=_reengage, args=(port,), daemon=True).start()
            continue
        try:
            mode = json.loads(state.read_text(encoding="utf-8")).get("mode")
        except (OSError, ValueError):
            mode = None
        if rc == 0 or mode not in ("running", "interrupted"):
            print(f"server closed (production {mode or 'not started'}).", flush=True)
            return 0
        crashes = [t for t in crashes if t > time.time() - 600] + [time.time()]
        if len(crashes) > 5:
            print("the server crashed 6 times in 10 minutes; giving up. Run Start Production again after "
                  "checking the error above.", flush=True)
            return 1
        print(f"server stopped unexpectedly (exit {rc} after {time.time() - started:.0f} s); restarting in 5 s",
              flush=True)
        time.sleep(5)
        threading.Thread(target=_reengage, args=(port,), daemon=True).start()


def _auto_update(port: int, cfg, state: Path, update: dict, rounds: int | None = None,
                 running: dict | None = None) -> None:
    """Every ``production.update_check_minutes``: if the branch has new commits that fast-forward a clean
    checkout and pass their unit tests, stop quickly (only the clip mid-edit finishes; waiting moments are saved
    and resumed) and let the supervisor loop restart the server on the new code. Also restarts when the checkout
    already moved past the commit the server was started on (``running``), e.g. a commit made in this checkout."""
    from clipstudio.production import updater

    tested: dict[str, bool] = {}
    n = 0
    while rounds is None or n < rounds:
        n += 1
        time.sleep(cfg.production.update_check_minutes * 60 if rounds is None else 0)
        try:
            mode = json.loads(state.read_text(encoding="utf-8")).get("mode")
        except (OSError, ValueError):
            mode = None
        if mode != "running" or update.get("sha"):
            continue
        chk = updater.check(running=(running or {}).get("sha", ""))
        if not chk.available:
            if chk.behind or chk.stale:
                print(f"update waiting but skipped: {chk.why_not()}", flush=True)
            continue
        sha = chk.target
        if sha not in tested:
            if cfg.production.update_run_tests:
                why = f"{chk.behind} new commit(s)" if chk.behind else "checkout is ahead of the running server"
                print(f"new code {sha[:7]} ({why}); running its tests first", flush=True)
                ok, tail = updater.test_commit(sha)
                print(f"  tests: {tail}", flush=True)
            else:
                ok = True
            tested[sha] = ok
        if not tested[sha]:
            continue  # this commit failed its tests; wait for the next one
        summary = updater.git("log", "-1", "--format=%s", sha).stdout.strip()
        print(f"restarting production onto {sha[:7]}: {summary}", flush=True)
        update.update(sha=sha, summary=summary)
        try:
            api(port, "POST", "/api/production/stop", {"mode": "now"}, timeout=30)
            while not api(port, "GET", "/api/production", timeout=15).get("safe"):
                time.sleep(3)
            api(port, "POST", "/api/production/shutdown", {}, timeout=15)
        except Exception as e:  # noqa: BLE001 - the server may already be gone; the loop restarts it
            print(f"  stopping for the update: {e}", flush=True)


def _reengage(port: int) -> None:
    """After a crash restart: once the server answers, resume the lineup and the unfinished clips."""
    if not wait_for(port, "production", 240):
        return
    try:
        res = api(port, "POST", "/api/production/start", {}, timeout=600)
        print(f"production resumed: {res.get('message')}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"could not resume production: {e}", flush=True)


# ------------------------------------------------------------------ desktop shortcuts
TOGGLE = "Toggle Production.bat"
SHORTCUTS = {"Production On-Off": TOGGLE, "Start Production": "Start Production.bat",
             "Stop Production": "Stop Production.bat", "Stop Production NOW": "Stop Production NOW.bat", "Production Status": "Production Status.bat"}


def toggle(a) -> dict:
    """One button: stop safely (every clip in flight finished or saved) if production is up, else start it."""
    launch = resolve_launch(a)
    if server_state(launch["port"]) == "down":
        for k, v in (("channel", None), ("no_browser", False)):
            if not hasattr(a, k):
                setattr(a, k, v)
        return start(a)
    for k, v in (("now", False), ("no_wait", False), ("keep_server", False)):
        if not hasattr(a, k):
            setattr(a, k, v)
    return stop(a)


def shortcuts(a) -> dict:
    """Put Start / Stop / Status shortcuts on the Windows desktop."""
    if sys.platform != "win32":
        raise ClipStudioError("desktop shortcuts are made on Windows; elsewhere run `production start|stop`")
    folder = REPO / "scripts" / "windows"
    lines = ["$d=[Environment]::GetFolderPath('Desktop')", "$m=[Environment]::GetFolderPath('Programs')",
             "$w=New-Object -ComObject WScript.Shell"]
    work = str(REPO).replace("'", "''")
    for name, bat in SHORTCUTS.items():
        target = str(folder / bat).replace("'", "''")
        if bat == TOGGLE:  # via cmd.exe so Windows allows "Pin to taskbar" (it refuses .bat targets)
            spec = (f"$s.TargetPath=$env:ComSpec; $s.Arguments='/c \"\"{target}\"\"'; "
                    "$s.IconLocation='shell32.dll,27'; ")
            homes = ("$d", "$m")  # desktop + Start menu
        else:
            spec, homes = f"$s.TargetPath='{target}'; ", ("$d",)
        for home in homes:
            lines.append(f"$s=$w.CreateShortcut((Join-Path {home} '{name}.lnk')); {spec}"
                         f"$s.WorkingDirectory='{work}'; $s.Save()")
    lines.append("Write-Output $d")
    script = base64.b64encode("; ".join(lines).encode("utf-16-le")).decode()  # no command-line quoting
    cp = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise ClipStudioError(f"could not make the shortcuts: {cp.stderr.strip()[-300:]}")
    desk = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "Desktop"
    say(f"Shortcuts added to {desk}: " + ", ".join(SHORTCUTS), a.json)
    return {"desktop": desk, "shortcuts": list(SHORTCUTS)}
