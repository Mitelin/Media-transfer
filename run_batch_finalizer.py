#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml

CONFIG = Path("config/sonarr-finalizer.yml")
SCRIPT = "scripts/sonarr_post_import_finalizer.py"

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


def run_sonarr_instance(config, instance_name, source_root):
    instance = config["sonarr_instances"][instance_name]
    url = sonarr_url(instance)
    api_key = instance["api_key"]

    print(f"\n### SONARR {instance_name} source={source_root} url={url}")

    series_list = api_get(url, api_key, "/api/v3/series")
    selected = [
        series for series in series_list
        if str(series.get("path", "")).startswith(source_root)
    ]

    print(f"Found {len(selected)} series in {source_root}")

    for series in selected:
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
            continue

        for season_number in monitored_seasons:
            cmd = COMMON + [
                "--instance", instance_name,
                "--series-id", str(series_id),
                "--season-number", str(season_number),
            ]
            ok = run(cmd)
            if not ok:
                print(f"FAILED series id={series_id} title={title} season={season_number}")


def run_radarr_instance(config, instance_name, source_root):
    instance = config["radarr_instances"][instance_name]
    url = radarr_url(instance)
    api_key = instance["api_key"]

    print(f"\n### RADARR {instance_name} source={source_root} url={url}")

    movies = api_get(url, api_key, "/api/v3/movie")
    selected = [
        movie for movie in movies
        if str(movie.get("path", "")).startswith(source_root)
    ]

    print(f"Found {len(selected)} movies in {source_root}")

    for movie in selected:
        movie_id = movie["id"]
        title = movie.get("title", "?")
        movie_file = movie.get("movieFile") or {}
        movie_file_path = movie_file.get("path")

        if not movie_file_path:
            print(f"SKIP movie id={movie_id} title={title}: no movie file path")
            continue

        env = os.environ.copy()
        env["radarr_eventtype"] = "Download"
        env["radarr_movie_id"] = str(movie_id)
        env["radarr_moviefile_path"] = movie_file_path

        cmd = COMMON + [
            "--instance", instance_name,
        ]
        ok = run(cmd, env=env)
        if not ok:
            print(f"FAILED movie id={movie_id} title={title}")


def main():
    config = load_config()

    run_sonarr_instance(config, "anime", "/anime-jp")
    run_sonarr_instance(config, "tv", "/tv-en")
    run_radarr_instance(config, "movies", "/movies-en")

    print()
    print("BATCH DONE")


if __name__ == "__main__":
    main()