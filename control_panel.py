#!/usr/bin/env python3
import html
import json
import os
import re
import secrets
import shlex
import subprocess
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("CONTROL_PANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("CONTROL_PANEL_PORT", "8091"))

USER = os.environ.get("CONTROL_PANEL_USER", "admin")
PASSWORD = os.environ.get("CONTROL_PANEL_PASSWORD", "")
LOG_LINES = int(os.environ.get("CONTROL_PANEL_LOG_LINES", "500"))
REPO_DIR = os.environ.get("CONTROL_PANEL_REPO_DIR", os.path.dirname(os.path.abspath(__file__)))
APP_SERVICE = os.environ.get("CONTROL_PANEL_APP_SERVICE", "media-transfer-control-panel.service")
UPDATE_REMOTE = os.environ.get("CONTROL_PANEL_GIT_REMOTE", "origin")
PROGRESS_STATE_PATH = os.environ.get("CONTROL_PANEL_PROGRESS_STATE", os.path.join(REPO_DIR, "logs", "progress-state.json"))

SERVICE = "media-transfer-maintenance.service"
SESSION_COOKIE = "media_panel_session"
SESSIONS = set()
PROGRESS_STAGES = [
  {
    "key": "anime",
    "label": "Anime",
    "marker": "### SONARR anime",
    "found_pattern": r"Found (\d+) series in /anime-jp",
    "start": 5,
    "end": 35,
  },
  {
    "key": "tv",
    "label": "TV",
    "marker": "### SONARR tv",
    "found_pattern": r"Found (\d+) series in /tv-en",
    "start": 35,
    "end": 62,
  },
  {
    "key": "movies",
    "label": "Movies",
    "marker": "### RADARR movies",
    "found_pattern": r"Found (\d+) movies in /movies-en",
    "start": 62,
    "end": 86,
  },
]
PROGRESS_STATE_PHASES = [
  {"key": "anime", "label": "Anime"},
  {"key": "tv", "label": "TV"},
  {"key": "movies", "label": "Movies"},
  {"key": "jellyfin", "label": "Jellyfin"},
]


def run_cmd(cmd, timeout=20, cwd=None):
    try:
        result = subprocess.run(
            cmd,
      cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def schedule_service_restart(service_name):
    quoted = shlex.quote(service_name)
    try:
        subprocess.Popen(
            ["sudo", "/bin/sh", "-c", f"sleep 1; /usr/bin/systemctl restart --no-block {quoted}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0, "restart scheduled"
    except Exception as exc:
        return 1, str(exc)


def perform_application_update(command_runner=run_cmd):
    code, output = command_runner(["git", "rev-parse", "--is-inside-work-tree"], timeout=10, cwd=REPO_DIR)
    if code != 0 or output.strip() != "true":
        return {"ok": False, "changed": False, "message": "Application folder is not a git repository."}

    code, _ = command_runner(["git", "fetch", UPDATE_REMOTE, "--quiet"], timeout=90, cwd=REPO_DIR)
    if code != 0:
        return {"ok": False, "changed": False, "message": "Failed to fetch updates from git."}

    code, status_output = command_runner(["git", "status", "--porcelain"], timeout=10, cwd=REPO_DIR)
    if code != 0:
        return {"ok": False, "changed": False, "message": "Failed to inspect local git status."}
    if status_output.strip():
        return {"ok": False, "changed": False, "message": "Automatic update skipped because the working tree has local changes."}

    code, upstream = command_runner(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10, cwd=REPO_DIR)
    if code != 0 or not upstream.strip():
        return {"ok": False, "changed": False, "message": "Automatic update skipped because the current branch has no upstream."}

    code, counts = command_runner(["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream.strip()}"], timeout=10, cwd=REPO_DIR)
    if code != 0:
        return {"ok": False, "changed": False, "message": "Failed to compare local and remote git revisions."}

    parts = counts.split()
    if len(parts) != 2:
        return {"ok": False, "changed": False, "message": "Git revision comparison returned an unexpected result."}

    ahead, behind = (int(parts[0]), int(parts[1]))
    if ahead and behind:
        return {"ok": False, "changed": False, "message": "Automatic update skipped because the branch has diverged from upstream."}
    if ahead:
        return {"ok": False, "changed": False, "message": "Automatic update skipped because the branch has local commits ahead of upstream."}
    if not behind:
        return {"ok": True, "changed": False, "message": "Application is already up to date."}

    code, pull_output = command_runner(["git", "pull", "--ff-only", UPDATE_REMOTE], timeout=120, cwd=REPO_DIR)
    if code != 0:
        details = pull_output.strip()
        suffix = f" {details}" if details else ""
        return {"ok": False, "changed": False, "message": f"Failed to apply git update.{suffix}"}

    return {"ok": True, "changed": True, "message": "Update installed. Restarting application."}


def service_snapshot():
    active_code, active = run_cmd(["systemctl", "is-active", SERVICE])
    _, status = run_cmd(["systemctl", "status", SERVICE, "--no-pager"], timeout=10)
    _, timer = run_cmd(["systemctl", "list-timers", "media-transfer-maintenance.timer", "--no-pager"], timeout=10)
    _, logs = run_cmd(["journalctl", "-u", SERVICE, "-n", str(LOG_LINES), "--no-pager"], timeout=10)

    active = active.strip()
    active_label = active if active_code == 0 else "unavailable"
    return {
        "active": active_label,
        "running": active_label in {"activating", "active"},
        "status": status,
        "timer": timer,
        "logs": logs,
    }


def parse_nonnegative_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def load_progress_state(path=None):
    path = path or PROGRESS_STATE_PATH
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def progress_from_state(payload):
    if not isinstance(payload, dict):
        return None

    phases = payload.get("phases")
    if not isinstance(phases, dict):
        return None

    processed = 0
    total = 0
    for phase in PROGRESS_STATE_PHASES:
        phase_state = phases.get(phase["key"], {})
        if not isinstance(phase_state, dict):
            phase_state = {}
        phase_total = parse_nonnegative_int(phase_state.get("total"))
        phase_done = parse_nonnegative_int(phase_state.get("done"))
        total += phase_total
        processed += min(phase_done, phase_total)

    if total <= 0:
        return None

    phase_key = str(payload.get("phase") or "").lower()
    phase = next((item for item in PROGRESS_STATE_PHASES if item["key"] == phase_key), None)
    phase_label = phase["label"] if phase else (phase_key.title() if phase_key else "Pipeline")
    current_item = str(payload.get("current_item") or "").strip()
    detail_text = str(payload.get("detail") or "").strip()
    detail_parts = [part for part in (current_item, detail_text) if part]
    detail = " - ".join(detail_parts) if detail_parts else "Approx pipeline progress from persistent state file."

    return {
        "percent": round((processed / total) * 100),
        "label": f"{phase_label} pipeline",
        "detail": detail,
        "phase": phase_label,
        "processed": processed,
        "total": total,
        "failures": parse_nonnegative_int(payload.get("failures")),
        "current_item": current_item,
        "source": "state-file",
    }


def latest_maintenance_logs(logs):
    start_marker = "=== Media Transfer Maintenance START"
    start = logs.rfind(start_marker)
    return logs[start:] if start >= 0 else logs


def stage_section(logs, stage_index):
    stage = PROGRESS_STAGES[stage_index]
    start = logs.rfind(stage["marker"])
    if start < 0:
        return ""

    next_positions = []
    for next_stage in PROGRESS_STAGES[stage_index + 1:]:
        position = logs.find(next_stage["marker"], start + 1)
        if position >= 0:
            next_positions.append(position)

    for marker in ("BATCH DONE", "=== Media Transfer batch DONE", "=== Jellyfin scheduled task lookup"):
        position = logs.find(marker, start + 1)
        if position >= 0:
            next_positions.append(position)

    end = min(next_positions) if next_positions else len(logs)
    return logs[start:end]


def estimate_finalizer_progress(logs, failures):
    instances = re.findall(r"Active (?:Sonarr|Radarr) instance: (\w+)", logs)
    if not instances:
        return None

    instance = instances[-1]
    stage = next((item for item in PROGRESS_STAGES if item["key"] == instance), None)
    if not stage:
        return None

    latest_start = max(logs.rfind("Season state:"), logs.rfind("Movie state:"), logs.rfind("Movie event context:"))
    section = logs[latest_start:] if latest_start >= 0 else logs
    total_match = re.search(r"Season summary: total=(\d+)", section)
    total = int(total_match.group(1)) if total_match else 1
    processed = len(re.findall(r"Episode \d+ file=.* final=", section))

    if "Movie evaluation result:" in section or "Movie move plan:" in section:
        processed = 1
    if "Done" in section:
        processed = max(processed, total)

    ratio = min(processed / total, 1) if total else 0
    percent = round(stage["start"] + ratio * min(stage["end"] - stage["start"], 12))

    return {
        "percent": percent,
        "label": f"{stage['label']} item",
        "detail": "Approx from current finalizer item; full batch markers were not in the log window.",
        "phase": stage["label"],
        "processed": processed,
        "total": total,
        "failures": failures,
    }


def estimate_progress(logs, running=False):
    if running:
        state_progress = progress_from_state(load_progress_state())
        if state_progress:
            return state_progress

    run_logs = latest_maintenance_logs(logs)
    failures = len(re.findall(r"\b(?:ERROR|FAILED|WARNING):", run_logs))

    progress = {
        "percent": None,
        "label": "Waiting for maintenance log",
        "detail": "Progress appears after the next maintenance start.",
        "phase": "Idle",
        "processed": 0,
        "total": 0,
        "failures": failures,
    }

    if not run_logs.strip():
      if not running:
        progress.update({
          "percent": 0,
          "label": "Idle",
          "detail": "No active maintenance run detected.",
          "phase": "Idle",
        })
        return progress

    if "=== Media Transfer Maintenance END" in run_logs:
        progress.update({
            "percent": 100,
            "label": "Maintenance complete",
            "detail": "Batch and Jellyfin step finished.",
            "phase": "Done",
        })
        return progress

    if "Jellyfin refresh triggered." in run_logs or "skipping Jellyfin refresh" in run_logs or "refresh task not found" in run_logs:
        progress.update({
            "percent": 98,
            "label": "Jellyfin step complete",
            "detail": "Waiting for maintenance end marker.",
            "phase": "Jellyfin",
        })
        return progress

    if "=== Jellyfin scheduled task lookup" in run_logs or "Starting Jellyfin library refresh task" in run_logs:
        progress.update({
            "percent": 92,
            "label": "Refreshing Jellyfin",
            "detail": "Batch is done; Jellyfin refresh is being triggered.",
            "phase": "Jellyfin",
        })
        return progress

    if "=== Media Transfer batch DONE" in run_logs or "BATCH DONE" in run_logs:
        progress.update({
            "percent": 88,
            "label": "Batch complete",
            "detail": "Preparing Jellyfin refresh.",
            "phase": "Batch done",
        })
        return progress

    for index in range(len(PROGRESS_STAGES) - 1, -1, -1):
        stage = PROGRESS_STAGES[index]
        if stage["marker"] not in run_logs:
            continue

        section = stage_section(run_logs, index)
        found = re.search(stage["found_pattern"], section)
        total = int(found.group(1)) if found else 0
        processed = len(re.findall(r"\bRUN:\s", section))

        if total > 0:
            ratio = min(processed / total, 1)
            percent = round(stage["start"] + ratio * (stage["end"] - stage["start"]))
            detail = f"Approx {min(processed, total)} of {total} queued jobs started."
        elif found:
            percent = stage["end"]
            detail = "No queued jobs found for this phase."
        else:
            percent = stage["start"]
            detail = "Looking up items for this phase."

        progress.update({
            "percent": percent,
            "label": f"{stage['label']} phase",
            "detail": detail,
            "phase": stage["label"],
            "processed": processed,
            "total": total,
        })
        return progress

    finalizer_progress = estimate_finalizer_progress(run_logs, failures)
    if finalizer_progress:
        return finalizer_progress

    if "=== Media Transfer Maintenance START" in run_logs:
        progress.update({
            "percent": 2,
            "label": "Maintenance started",
            "detail": "Preparing batch finalizer.",
            "phase": "Starting",
        })
    elif not running:
        progress.update({
            "percent": 0,
            "label": "Idle",
            "detail": "No active maintenance run detected.",
            "phase": "Idle",
        })

    return progress


def percent_text(percent):
    return f"{percent}%" if percent is not None else "-"


def parse_session(headers):
    raw_cookie = headers.get("Cookie", "")
    jar = cookies.SimpleCookie()
    try:
        jar.load(raw_cookie)
    except cookies.CookieError:
        return ""
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else ""


def authorized(headers):
    token = parse_session(headers)
    return bool(token) and token in SESSIONS


def login_valid(username, password):
    return secrets.compare_digest(username, USER) and secrets.compare_digest(password, PASSWORD)


class Handler(BaseHTTPRequestHandler):
    def require_auth(self):
        if authorized(self.headers):
            return True

        self.redirect("/login")
        return False

    def send_html(self, body, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload, status=200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(raw)

    def login_page(self, error=""):
        error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
        body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Maintenance Login</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      background: #10151f;
      color: #e5e7eb;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 24px;
    }}
    main {{
      width: min(420px, 100%);
      background: #182131;
      border: 1px solid #2d3a4f;
      border-radius: 8px;
      padding: 28px;
      box-shadow: 0 18px 42px rgba(0, 0, 0, .35);
    }}
    h1 {{ margin: 0 0 6px; font-size: 26px; }}
    p {{ margin: 0 0 24px; color: #a7b0bf; }}
    label {{ display: block; margin: 16px 0 8px; color: #cbd5e1; font-size: 14px; }}
    input {{
      width: 100%;
      border: 1px solid #3b4a62;
      background: #0f1724;
      color: #f8fafc;
      border-radius: 6px;
      padding: 12px 13px;
      font-size: 16px;
    }}
    input:focus {{ outline: 2px solid #38bdf8; outline-offset: 2px; }}
    button {{
      width: 100%;
      margin-top: 22px;
      border: 0;
      border-radius: 6px;
      padding: 13px 18px;
      background: #0ea5a3;
      color: #05201f;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: #2dd4bf; }}
    .error {{
      background: #451a1a;
      border: 1px solid #ef4444;
      color: #fecaca;
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 14px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Media Maintenance</h1>
    <p>Control panel login</p>
    {error_html}
    <form method="POST" action="/login">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>"""
        self.send_html(body)

    def page(self, message=""):
        snapshot = service_snapshot()
        progress = estimate_progress(snapshot["logs"], snapshot["running"])
        msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
        status_class = "running" if snapshot["running"] else "unavailable" if snapshot["active"] == "unavailable" else "idle"

        body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Maintenance</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #10151f;
      color: #e5e7eb;
      margin: 0;
      padding: 24px;
    }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 18px; }}
    form {{ margin: 0; }}
    h1 {{ margin: 0 0 6px; font-size: 30px; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    p {{ color: #a7b0bf; margin: 0; }}
    .card {{
      background: #182131;
      border: 1px solid #2d3a4f;
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    button {{
      background: #0ea5a3;
      color: #05201f;
      border: 0;
      border-radius: 6px;
      padding: 12px 18px;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: #2dd4bf; }}
    button.secondary {{
      background: transparent;
      color: #cbd5e1;
      border: 1px solid #3b4a62;
      font-weight: 600;
    }}
    button.secondary:hover {{ background: #223047; color: #f8fafc; }}
    button:disabled {{
      background: #64748b;
      color: #dbe4ef;
      cursor: not-allowed;
    }}
    pre {{
      white-space: pre-wrap;
      background: #080d14;
      border: 1px solid #243044;
      border-radius: 6px;
      padding: 14px;
      overflow-x: auto;
      max-height: 460px;
      margin: 0;
      font: 13px/1.45 Consolas, "Liberation Mono", monospace;
    }}
    .console {{ min-height: 320px; }}
    .msg {{
      background: #064e3b;
      border: 1px solid #10b981;
      padding: 12px;
      border-radius: 6px;
      margin-bottom: 16px;
    }}
    .status {{
      display: inline-block;
      max-width: 100%;
      padding: 6px 10px;
      border-radius: 999px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-bottom: 16px;
    }}
    .status.running {{ background: #92400e; }}
    .status.idle {{ background: #065f46; }}
    .status.unavailable {{ background: #7f1d1d; }}
    .meter {{
      height: 14px;
      overflow: hidden;
      background: #0f1724;
      border: 1px solid #334155;
      border-radius: 999px;
      margin: 10px 0;
    }}
    .meter span {{ display: block; height: 100%; width: {progress["percent"] or 0}%; background: #22c55e; transition: width .25s ease; }}
    .progress-line {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }}
    .progress-line strong {{ font-size: 24px; line-height: 1; }}
    .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 12px; }}
    .stat {{ background: #101827; border: 1px solid #2d3a4f; border-radius: 6px; padding: 10px; }}
    .stat strong {{ display: block; font-size: 20px; overflow-wrap: anywhere; }}
    .muted {{ color: #a7b0bf; }}
    @media (max-width: 760px) {{
      body {{ padding: 16px; }}
      header, .grid {{ display: block; }}
      header form {{ margin-top: 12px; }}
      .stats {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Media Maintenance</h1>
        <p>Transfer control panel</p>
      </div>
      <form method="POST" action="/logout"><button class="secondary" type="submit">Sign out</button></form>
    </header>

    <section class="card">
      {msg_html}
      <div id="state" class="status {status_class}">Service state: {html.escape(snapshot["active"])}</div>
      <div class="actions">
        <form method="POST" action="/run">
          <button id="runButton" type="submit" {"disabled" if snapshot["running"] else ""}>Run maintenance now</button>
        </form>
        <form method="POST" action="/update">
          <button id="updateButton" class="secondary" type="submit" {"disabled" if snapshot["running"] else ""}>Update app</button>
        </form>
        <span class="muted">Spusti denni presun a Jellyfin library refresh.</span>
        <span class="muted">Update stahne nove commity z gitu a po zmene restartuje panel.</span>
      </div>
    </section>

    <section class="card">
      <h2>Progress</h2>
      <div class="meter"><span id="progressBar"></span></div>
      <p class="progress-line"><strong id="progressValue">{percent_text(progress["percent"])}</strong> <span id="progressNote" class="muted">{html.escape(progress["detail"])}</span></p>
      <div class="stats">
        <div class="stat"><strong id="phaseLabel">{html.escape(progress["phase"])}</strong><span class="muted">Phase</span></div>
        <div class="stat"><strong id="jobCount">{progress["processed"]}/{progress["total"]}</strong><span class="muted">Processed items</span></div>
        <div class="stat"><strong id="failureCount">{progress["failures"]}</strong><span class="muted">Warnings/errors</span></div>
      </div>
    </section>

    <section class="card">
      <h2>Live console</h2>
      <pre id="logs" class="console">{html.escape(snapshot["logs"])}</pre>
    </section>

    <div class="grid">
      <section class="card">
        <h2>Timer</h2>
        <pre id="timer">{html.escape(snapshot["timer"])}</pre>
      </section>

      <section class="card">
        <h2>Status</h2>
        <pre id="status">{html.escape(snapshot["status"])}</pre>
      </section>
    </div>
  </main>
  <script>
    const logs = document.getElementById('logs');
    const state = document.getElementById('state');
    const runButton = document.getElementById('runButton');
    const updateButton = document.getElementById('updateButton');
    const statusBox = document.getElementById('status');
    const timerBox = document.getElementById('timer');
    const progressBar = document.getElementById('progressBar');
    const progressValue = document.getElementById('progressValue');
    const progressNote = document.getElementById('progressNote');
    const phaseLabel = document.getElementById('phaseLabel');
    const jobCount = document.getElementById('jobCount');
    const failureCount = document.getElementById('failureCount');

    function text(value) {{ return value || ''; }}

    async function refresh() {{
      let data;
      try {{
        const response = await fetch('/api/status', {{ cache: 'no-store' }});
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
        data = await response.json();
      }} catch (error) {{
        return;
      }}
      logs.textContent = text(data.logs);
      logs.scrollTop = logs.scrollHeight;
      state.textContent = `Service state: ${{data.active || 'unknown'}}`;
      state.className = `status ${{data.running ? 'running' : data.active === 'unavailable' ? 'unavailable' : 'idle'}}`;
      runButton.disabled = Boolean(data.running);
      updateButton.disabled = Boolean(data.running);
      statusBox.textContent = text(data.status);
      timerBox.textContent = text(data.timer);

      if (data.progress) {{
        const percent = data.progress.percent;
        progressBar.style.width = `${{percent || 0}}%`;
        progressValue.textContent = percent === null ? '-' : `${{percent}}%`;
        progressNote.textContent = text(data.progress.detail);
        phaseLabel.textContent = text(data.progress.phase);
        jobCount.textContent = `${{data.progress.processed || 0}}/${{data.progress.total || 0}}`;
        failureCount.textContent = data.progress.failures || 0;
      }}
    }}

    refresh();
    setInterval(refresh, 2500);
  </script>
</body>
</html>"""
        self.send_html(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            if authorized(self.headers):
                self.redirect("/")
            else:
                self.login_page()
            return

        if not self.require_auth():
            return

        if path in {"/", "/status"}:
            self.page()
        elif path == "/api/status":
            snapshot = service_snapshot()
            snapshot["progress"] = estimate_progress(snapshot["logs"], snapshot["running"])
            self.send_json(snapshot)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            form = self.read_form()
            username = form.get("username", [""])[0]
            password = form.get("password", [""])[0]
            if login_valid(username, password):
                token = secrets.token_urlsafe(32)
                SESSIONS.add(token)
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/")
                self.end_headers()
            else:
                self.login_page("Invalid username or password.")
            return

        if not self.require_auth():
            return

        if path == "/logout":
            token = parse_session(self.headers)
            SESSIONS.discard(token)
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
            self.end_headers()
            return

        if path == "/run":
          code, output = run_cmd(
            ["sudo", "/usr/bin/systemctl", "start", "--no-block", SERVICE],
            timeout=10,
          )

          if code == 0:
            self.page("Maintenance started.")
          else:
            self.page("Failed to start maintenance: " + output)
          return

        if path != "/update":
            self.send_error(404)
            return

        snapshot = service_snapshot()
        if snapshot["running"]:
          self.page("Update is disabled while maintenance is running.")
          return

        result = perform_application_update()
        if not result["ok"]:
          self.page(result["message"])
          return

        self.page(result["message"])
        if not result["changed"]:
          return

        schedule_code, schedule_output = schedule_service_restart(APP_SERVICE)
        if schedule_code != 0:
          print(f"Failed to schedule control panel restart: {schedule_output}")


def main():
    if not PASSWORD:
        raise SystemExit("CONTROL_PANEL_PASSWORD is not set")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Media Maintenance Control Panel listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
