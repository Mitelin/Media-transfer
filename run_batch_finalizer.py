#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml

CONFIG = Path("config/sonarr-finalizer.yml")
SCRIPT = "scripts/sonarr_post_import_finalizer.py"
PROGRESS_STATE_PATH = Path("logs/progress-state.json")

URL_MODE = "lan"
COMMON = [
    sys.executable,
    SCRIPT,
    "--config", str(CONFIG),
    "--url-mode", URL_MODE,
    "--allow-non-docker-execute",
    "--execute",
]


def load_config():
    with CONFIG.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def resolve_log_path(config):
    logging_config = config.get("logging", {}) if isinstance(config, dict) else {}
    log_path = Path(str(logging_config.get("path", "logs/sonarr-finalizer.log")))
    if not log_path.is_absolute():
        log_path = CONFIG.parent.parent / log_path
    return log_path


def rotate_batch_log(config):
    log_path = resolve_log_path(config)
    previous_path = log_path.with_suffix(log_path.suffix + ".1")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if previous_path.exists():
        previous_path.unlink()
    if log_path.exists():
        log_path.replace(previous_path)


def write_progress_state(payload):
    path = PROGRESS_STATE_PATH
    if not path.is_absolute():
        path = CONFIG.parent.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def update_progress_state(state, phase, current_item="", detail=""):
    payload = {
        "phase": phase,
        "current_item": current_item,
        "detail": detail,
        "failures": state["failures"],
        "phases": state["phases"],
    }
    write_progress_state(payload)


def collect_sonarr_series(config, instance_name, source_root):
    instance = config["sonarr_instances"][instance_name]
    url = sonarr_url(instance)
    api_key = instance["api_key"]
    series_list = api_get(url, api_key, "/api/v3/series")
    selected = [
        series for series in series_list
        if str(series.get("path", "")).startswith(source_root)
    ]
    return instance, url, selected


def collect_radarr_movies(config, instance_name, source_root):
    instance = config["radarr_instances"][instance_name]
    url = radarr_url(instance)
    api_key = instance["api_key"]
    movies = api_get(url, api_key, "/api/v3/movie")
    selected = [
        movie for movie in movies
        if str(movie.get("path", "")).startswith(source_root)
    ]
    return instance, url, selected


def api_get(url, api_key, path):
    response = requests.get(
        url.rstrip("/") + path,
        headers={"X-Api-Key": api_key},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def run(cmd, env=None):
    print()
    print("============================================================")
    print("RUN:", " ".join(cmd))
    print("============================================================")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"ERROR: command failed with exit code {result.returncode}")
        return False
    return True


def sonarr_url(instance):
    return instance.get("lan_url") or instance["url"]


def radarr_url(instance):
    return instance.get("lan_url") or instance["url"]


def run_sonarr_instance(config, instance_name, source_root, selected, url, state):
    print(f"\n### SONARR {instance_name} source={source_root} url={url}")

    print(f"Found {len(selected)} series in {source_root}")
    update_progress_state(state, instance_name, detail=f"Queued {len(selected)} folders in {source_root}")

    for index, series in enumerate(selected, start=1):
        series_id = series["id"]
        title = series.get("title", "?")
        seasons = series.get("seasons", [])

        monitored_seasons = [
            season_number
            for season in seasons
            for season_number in [season.get("seasonNumber")]
            if season.get("monitored") and isinstance(season_number, int) and season_number >= 0
        ]

        if not monitored_seasons:
            print(f"SKIP series id={series_id} title={title}: no monitored seasons")
            state["phases"][instance_name]["done"] += 1
            update_progress_state(state, instance_name, current_item=title, detail=f"Skipped {index} of {len(selected)} folders: no monitored seasons")
            continue

        for season_index, season_number in enumerate(monitored_seasons, start=1):
            update_progress_state(
                state,
                instance_name,
                current_item=title,
                detail=f"Folder {index} of {len(selected)}: season {season_number} ({season_index}/{len(monitored_seasons)})",
            )
            cmd = COMMON + [
                "--instance", instance_name,
                "--series-id", str(series_id),
                "--season-number", str(season_number),
            ]
            ok = run(cmd)
            if not ok:
                state["failures"] += 1
                print(f"FAILED series id={series_id} title={title} season={season_number}")

        state["phases"][instance_name]["done"] += 1
        update_progress_state(state, instance_name, current_item=title, detail=f"Processed {index} of {len(selected)} folders")


def run_radarr_instance(config, instance_name, source_root, selected, url, state):
    print(f"\n### RADARR {instance_name} source={source_root} url={url}")

    print(f"Found {len(selected)} movies in {source_root}")
    update_progress_state(state, instance_name, detail=f"Queued {len(selected)} folders in {source_root}")

    for index, movie in enumerate(selected, start=1):
        movie_id = movie["id"]
        title = movie.get("title", "?")
        movie_file = movie.get("movieFile") or {}
        movie_file_path = movie_file.get("path")

        if not movie_file_path:
            print(f"SKIP movie id={movie_id} title={title}: no movie file path")
            state["phases"][instance_name]["done"] += 1
            update_progress_state(state, instance_name, current_item=title, detail=f"Skipped {index} of {len(selected)} folders: no movie file path")
            continue

        env = os.environ.copy()
        env["radarr_eventtype"] = "Download"
        env["radarr_movie_id"] = str(movie_id)
        env["radarr_moviefile_path"] = movie_file_path
        update_progress_state(state, instance_name, current_item=title, detail=f"Folder {index} of {len(selected)}")

        cmd = COMMON + [
            "--instance", instance_name,
        ]
        ok = run(cmd, env=env)
        if not ok:
            state["failures"] += 1
            print(f"FAILED movie id={movie_id} title={title}")

        state["phases"][instance_name]["done"] += 1
        update_progress_state(state, instance_name, current_item=title, detail=f"Processed {index} of {len(selected)} folders")


def main():
    config = load_config()
    rotate_batch_log(config)

    anime_instance, anime_url, anime_selected = collect_sonarr_series(config, "anime", "/anime-jp")
    tv_instance, tv_url, tv_selected = collect_sonarr_series(config, "tv", "/tv-en")
    movies_instance, movies_url, movie_selected = collect_radarr_movies(config, "movies", "/movies-en")

    _ = anime_instance, tv_instance, movies_instance
    state = {
        "failures": 0,
        "phases": {
            "anime": {"done": 0, "total": len(anime_selected)},
            "tv": {"done": 0, "total": len(tv_selected)},
            "movies": {"done": 0, "total": len(movie_selected)},
            "jellyfin": {"done": 0, "total": 1},
        },
    }
    update_progress_state(state, "anime", detail="Preparing anime batch")

    run_sonarr_instance(config, "anime", "/anime-jp", anime_selected, anime_url, state)
    run_sonarr_instance(config, "tv", "/tv-en", tv_selected, tv_url, state)
    run_radarr_instance(config, "movies", "/movies-en", movie_selected, movies_url, state)

    update_progress_state(state, "jellyfin", detail="Batch complete. Waiting for Jellyfin refresh.")

    print()
    print("BATCH DONE")


if __name__ == "__main__":
    main()