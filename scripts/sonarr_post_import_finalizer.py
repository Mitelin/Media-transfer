#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:
    raise SystemExit("Missing dependency: requests. Install with: pip install -r requirements.txt") from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit("Missing dependency: PyYAML. Install with: pip install -r requirements.txt") from exc

LOG = logging.getLogger("media-finalizer")

LANG_ALIASES = {
    "cz": ["cz", "cze", "ces", "czech", "cestina", "cesky", "čeština", "česky"],
    "en": ["en", "eng", "english"],
    "jp": ["ja", "jpn", "japanese", "nihongo"],
}

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts"}


@dataclass
class EventContext:
    event_type: str
    series_id: int | None
    series_title: str | None
    series_path: str | None
    season_number: int | None
    imported_file_path: str | None
    episode_file_id: int | None


@dataclass
class EpisodeState:
    episode_id: int
    episode_number: int | None
    monitored: bool
    has_file: bool
    episode_file_id: int | None
    path: str | None
    audio_languages: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)
    sonarr_audio_languages: list[str] = field(default_factory=list)
    sonarr_subtitle_languages: list[str] = field(default_factory=list)
    language_detection_source: str | None = None
    is_final: bool = False
    block_reason: str | None = None


@dataclass
class SeasonState:
    series_id: int
    series_title: str
    season_number: int
    source_folder: str | None
    episodes: list[EpisodeState]


@dataclass
class EvaluationResult:
    is_final: bool
    target_language: str | None
    reason: str
    blocking_episodes: list[EpisodeState]


@dataclass
class MoveItem:
    episode_id: int
    episode_number: int | None
    source_path: str
    destination_path: str
    temporary_destination_path: str | None


@dataclass
class MovePlan:
    series_id: int
    series_title: str
    season_number: int
    mapping_name: str | None
    target_language: str | None
    source_folder: str
    destination_folder: str
    temporary_destination_folder: str | None
    dry_run: bool
    move_method: str
    partial_move: bool
    will_move: bool
    will_unmonitor: bool
    will_rescan: bool
    unmonitor_episode_ids: list[int]
    move_items: list[MoveItem]
    episode_count: int
    relevant_episode_count: int
    episode_file_count: int


@dataclass
class MovePreflightResult:
    errors: list[str]
    warnings: list[str]


class SonarrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key})

    def get(self, path: str, **params: Any) -> Any:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def put(self, path: str, payload: Any) -> Any:
        response = self.session.put(f"{self.base_url}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: Any) -> Any:
        response = self.session.post(f"{self.base_url}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def put_json(self, path: str, payload: Any) -> Any:
        response = self.session.put(f"{self.base_url}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_series(self, series_id: int) -> dict[str, Any]:
        return self.get(f"/api/v3/series/{series_id}")

    def get_all_series(self) -> list[dict[str, Any]]:
        return normalize_records(self.get("/api/v3/series"))

    def get_system_status(self) -> dict[str, Any]:
        return self.get("/api/v3/system/status")

    def get_root_folders(self) -> list[dict[str, Any]]:
        return normalize_records(self.get("/api/v3/rootfolder"))

    def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        return normalize_records(self.get("/api/v3/episode", seriesId=series_id))

    def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return normalize_records(self.get("/api/v3/episodefile", seriesId=series_id))

    def unmonitor_season(self, series_id: int, season_number: int) -> None:
        series = self.get_series(series_id)
        for season in series.get("seasons", []):
            if season.get("seasonNumber") == season_number:
                season["monitored"] = False
                self.put(f"/api/v3/series/{series_id}", series)
                return
        raise RuntimeError(f"Season {season_number} not found for series {series_id}")

    def unmonitor_episodes(self, episode_ids: list[int]) -> None:
        if not episode_ids:
            return
        self.put_json("/api/v3/episode/monitor", {"episodeIds": episode_ids, "monitored": False})

    def rescan_series(self, series_id: int) -> None:
        self.post("/api/v3/command", {"name": "RescanSeries", "seriesId": series_id})


def normalize_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    return []


def default_config_path() -> Path:
    script_path = Path(__file__).resolve()
    project_config = script_path.parent.parent / "config" / "sonarr-finalizer.yml"
    if project_config.exists():
        return project_config
    docker_config = Path("/config/sonarr-finalizer.yml")
    if docker_config.exists():
        return docker_config
    return project_config


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def setup_logging(config: dict[str, Any], config_path: Path) -> None:
    logging_config = config.get("logging", {})
    level_name = str(logging_config.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_path = Path(str(logging_config.get("path", "logs/sonarr-finalizer.log")))
    if not log_path.is_absolute():
        log_path = config_path.parent.parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[file_handler, console_handler], force=True)
    LOG.info("Logging to %s", log_path)


def get_active_sonarr_config(config: dict[str, Any], instance_override: str | None = None) -> dict[str, Any]:
    active_instance = instance_override or config.get("active_instance")
    instances = config.get("sonarr_instances", {})
    if not active_instance:
        raise ValueError("Missing config key: active_instance")
    if active_instance not in instances:
        raise ValueError(f"active_instance {active_instance!r} not found in sonarr_instances")
    instance = dict(instances[active_instance])
    instance["name"] = active_instance
    if not instance.get("url") or not instance.get("api_key"):
        raise ValueError(f"Sonarr instance {active_instance!r} needs url and api_key")
    return instance


def get_sonarr_base_url(instance: dict[str, Any], url_mode: str) -> str:
    key_by_mode = {
        "docker": "url",
        "lan": "lan_url",
        "tailscale": "tailscale_url",
    }
    key = key_by_mode[url_mode]
    base_url = instance.get(key)
    if not base_url:
        raise ValueError(f"Sonarr instance {instance.get('name')!r} does not define {key}")
    return str(base_url)


def validate_config(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    instances = config.get("sonarr_instances")
    if not isinstance(instances, dict) or not instances:
        errors.append("sonarr_instances must be a non-empty mapping")
        instances = {}

    active_instance = config.get("active_instance")
    if active_instance and active_instance not in instances:
        errors.append(f"active_instance {active_instance!r} is not defined in sonarr_instances")

    instance_types: set[str] = set()
    for instance_name, instance in instances.items():
        if not isinstance(instance, dict):
            errors.append(f"sonarr_instances.{instance_name} must be a mapping")
            continue
        instance_type = str(instance.get("instance_type") or "")
        if not instance_type:
            errors.append(f"sonarr_instances.{instance_name}.instance_type is required")
        else:
            instance_types.add(instance_type)
        for key in ("url", "lan_url", "tailscale_url"):
            if not instance.get(key):
                warnings.append(f"sonarr_instances.{instance_name}.{key} is empty")
        api_key = str(instance.get("api_key") or "")
        if not api_key:
            warnings.append(f"sonarr_instances.{instance_name}.api_key is empty; fill it in local config before runtime")
        roots = instance.get("maintenance_roots") or {}
        if not isinstance(roots, dict) or not roots:
            warnings.append(f"sonarr_instances.{instance_name}.maintenance_roots is empty")

    rules_by_type = config.get("rules")
    if not isinstance(rules_by_type, dict) or not rules_by_type:
        errors.append("rules must be a non-empty mapping")
        rules_by_type = {}
    for instance_type in sorted(instance_types):
        if instance_type not in rules_by_type:
            errors.append(f"rules.{instance_type} is missing for configured Sonarr instance type")

    paths = config.get("paths")
    if not isinstance(paths, dict):
        errors.append("paths must be a mapping")
        paths = {}

    mappings = paths.get("mappings") or []
    if not isinstance(mappings, list) or not mappings:
        errors.append("paths.mappings must be a non-empty list")
        mappings = []
    for index, mapping in enumerate(mappings):
        label = f"paths.mappings[{index}]"
        if not isinstance(mapping, dict):
            errors.append(f"{label} must be a mapping")
            continue
        instance_type = str(mapping.get("instance_type") or "")
        source_prefix = str(mapping.get("source_prefix") or "")
        target_prefix = str(mapping.get("target_prefix") or "")
        final_language = str(mapping.get("final_language") or "")
        if not instance_type:
            errors.append(f"{label}.instance_type is required")
        elif instance_type not in instance_types:
            errors.append(f"{label}.instance_type {instance_type!r} has no matching Sonarr instance")
        if not source_prefix:
            errors.append(f"{label}.source_prefix is required")
        if not target_prefix:
            errors.append(f"{label}.target_prefix is required")
        if source_prefix and target_prefix and media_normpath(source_prefix) == media_normpath(target_prefix):
            errors.append(f"{label}.source_prefix and target_prefix must differ")
        if not final_language:
            errors.append(f"{label}.final_language is required")
        allowed = set((rules_by_type.get(instance_type) or {}).get("allowed_final_audio_languages") or [])
        if final_language and allowed and final_language not in allowed:
            errors.append(f"{label}.final_language {final_language!r} is not allowed by rules.{instance_type}")
        if instance_type:
            roots_for_type = maintenance_roots_for_instance_type(instances, instance_type)
            if roots_for_type and source_prefix and source_prefix not in roots_for_type:
                warnings.append(f"{label}.source_prefix {source_prefix!r} is not listed in maintenance_roots")

    local_mounts = paths.get("local_mounts") or []
    if not isinstance(local_mounts, list):
        errors.append("paths.local_mounts must be a list")
    else:
        for index, mapping in enumerate(local_mounts):
            label = f"paths.local_mounts[{index}]"
            if not isinstance(mapping, dict):
                errors.append(f"{label} must be a mapping")
                continue
            if not mapping.get("docker_prefix"):
                errors.append(f"{label}.docker_prefix is required")
            if not mapping.get("local_prefix"):
                errors.append(f"{label}.local_prefix is required")

    safety = config.get("safety") or {}
    if not isinstance(safety, dict):
        errors.append("safety must be a mapping")
    elif not safety.get("dry_run", True):
        warnings.append("safety.dry_run is false; runtime changes are possible when --execute is used")

    return errors, warnings


def maintenance_roots_for_instance_type(instances: dict[str, Any], instance_type: str) -> set[str]:
    roots: set[str] = set()
    for instance in instances.values():
        if not isinstance(instance, dict) or instance.get("instance_type") != instance_type:
            continue
        maintenance_roots = instance.get("maintenance_roots") or {}
        if isinstance(maintenance_roots, dict):
            roots.update(str(value) for value in maintenance_roots.values() if value)
    return roots


def validate_config_command(config: dict[str, Any]) -> int:
    errors, warnings = validate_config(config)
    for warning in warnings:
        LOG.warning("Config warning: %s", warning)
    for error in errors:
        LOG.error("Config error: %s", error)
    if errors:
        LOG.error("Config validation failed: %s error(s), %s warning(s)", len(errors), len(warnings))
        return 2
    LOG.info("Config validation OK: %s warning(s)", len(warnings))
    return 0


def test_sonarr_api(client: SonarrClient, instance: dict[str, Any], base_url: str) -> int:
    status = client.get_system_status()
    LOG.info(
        "Sonarr API OK: instance=%s url=%s app=%s version=%s",
        instance.get("name"),
        base_url,
        status.get("appName", "Sonarr"),
        status.get("version", "unknown"),
    )
    root_folders = client.get_root_folders()
    configured_roots = set((instance.get("maintenance_roots") or instance.get("root_folders") or {}).values())
    found_roots = {folder.get("path") for folder in root_folders}
    LOG.info("Sonarr root folders: %s", sorted(found_roots))
    for root in sorted(configured_roots):
        if root in found_roots:
            LOG.info("Configured root folder found: %s", root)
        else:
            LOG.warning("Configured root folder not returned by Sonarr API: %s", root)
    return 0


def list_sonarr_series(client: SonarrClient, text_filter: str | None, root_prefix: str | None, limit: int) -> int:
    series_items = client.get_all_series()
    if text_filter:
        needle = text_filter.lower()
        series_items = [item for item in series_items if needle in str(item.get("title", "")).lower()]
    if root_prefix:
        series_items = [item for item in series_items if path_starts_with(str(item.get("path", "")), root_prefix)]
    series_items = sorted(series_items, key=lambda item: str(item.get("title", "")).lower())
    for item in series_items[:limit]:
        seasons = item.get("seasons") or []
        monitored_seasons = [str(season.get("seasonNumber")) for season in seasons if season.get("monitored")]
        LOG.info(
            "Series id=%s title=%s path=%s monitored_seasons=%s",
            item.get("id"),
            item.get("title"),
            item.get("path"),
            ",".join(monitored_seasons) if monitored_seasons else "none",
        )
    LOG.info("Listed %s of %s matching series", min(len(series_items), limit), len(series_items))
    return 0


def read_sonarr_env() -> dict[str, str]:
    values = {key: value for key, value in os.environ.items() if key.lower().startswith("sonarr_")}
    for key in sorted(values):
        LOG.info("ENV %s=%s", key, values[key])
    return values


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def infer_season_from_path(path: str | None) -> int | None:
    if not path:
        return None
    patterns = [r"Season[ ._-]*(\d{1,2})", r"S(\d{1,2})E\d{1,3}"]
    for pattern in patterns:
        match = re.search(pattern, path, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def build_event_context(env: dict[str, str]) -> EventContext:
    imported_file_path = env.get("sonarr_episodefile_path") or env.get("sonarr_episodefile_relativepath")
    season_number = infer_season_from_path(imported_file_path)
    if season_number is None:
        season_number = parse_int(env.get("sonarr_episodefile_seasonnumber"))

    return EventContext(
        event_type=env.get("sonarr_eventtype", ""),
        series_id=parse_int(env.get("sonarr_series_id")),
        series_title=env.get("sonarr_series_title"),
        series_path=env.get("sonarr_series_path"),
        season_number=season_number,
        imported_file_path=imported_file_path,
        episode_file_id=parse_int(env.get("sonarr_episodefile_id")),
    )


def apply_manual_event_overrides(context: EventContext, args: argparse.Namespace) -> EventContext:
    if args.series_id is not None:
        context.series_id = args.series_id
    if args.imported_file_path:
        context.imported_file_path = args.imported_file_path
        inferred_season = infer_season_from_path(args.imported_file_path)
        if args.season_number is None and inferred_season is not None:
            context.season_number = inferred_season
    if args.season_number is not None:
        context.season_number = args.season_number
    if args.event_type:
        context.event_type = args.event_type
    elif context.series_id is not None and context.season_number is not None and not context.event_type:
        context.event_type = "Download"
    return context


def resolve_missing_event_context(context: EventContext, episode_files: list[dict[str, Any]]) -> EventContext:
    if context.season_number is not None:
        return context
    if context.episode_file_id is None:
        return context
    for episode_file in episode_files:
        if episode_file.get("id") == context.episode_file_id:
            season = infer_season_from_path(episode_file.get("path"))
            if season is None:
                season = parse_int(episode_file.get("seasonNumber"))
            context.season_number = season
            if not context.imported_file_path:
                context.imported_file_path = episode_file.get("path")
            return context
    return context


def ffprobe_streams(path: str) -> list[dict[str, Any]]:
    command = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout or "{}")
    return data.get("streams", [])


def normalize_language_tag(value: str | None, exact_only: bool = False) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    for language, aliases in LANG_ALIASES.items():
        if text in aliases:
            return language
    if exact_only:
        return None
    padded = f" {text} "
    for language, aliases in LANG_ALIASES.items():
        for alias in aliases:
            if len(alias) <= 2:
                if f" {alias} " in padded:
                    return language
            elif alias in text:
                return language
    return None


def normalize_language_values(values: list[Any], exact_only: bool = False) -> list[str]:
    languages: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value)
        for part in re.split(r"[/,;|]+", text):
            detected = normalize_language_tag(part, exact_only=exact_only)
            if detected:
                languages.add(detected)
    return sorted(languages)


def detect_sonarr_file_languages(episode_file: dict[str, Any] | None) -> dict[str, list[str]]:
    if not episode_file:
        return {"audio": [], "subtitles": []}

    language_names = []
    for language in episode_file.get("languages") or []:
        if isinstance(language, dict):
            language_names.append(language.get("name"))
        else:
            language_names.append(language)

    media_info = episode_file.get("mediaInfo") or {}
    media_audio = media_info.get("audioLanguages") if isinstance(media_info, dict) else None
    media_subtitles = media_info.get("subtitles") if isinstance(media_info, dict) else None

    audio = set(normalize_language_values(language_names, exact_only=False))
    audio.update(normalize_language_values([media_audio], exact_only=True))
    subtitles = normalize_language_values([media_subtitles], exact_only=True)
    return {"audio": sorted(audio), "subtitles": subtitles}


def detect_file_languages(path: str) -> dict[str, list[str]]:
    audio: set[str] = set()
    subtitles: set[str] = set()
    try:
        streams = ffprobe_streams(path)
    except FileNotFoundError:
        LOG.error("ffprobe binary not found. Install ffmpeg/ffprobe in the Sonarr container.")
        raise
    except subprocess.CalledProcessError as exc:
        LOG.warning("ffprobe failed for %s: %s", path, exc)
        return {"audio": [], "subtitles": []}
    except json.JSONDecodeError as exc:
        LOG.warning("ffprobe returned invalid JSON for %s: %s", path, exc)
        return {"audio": [], "subtitles": []}

    for stream in streams:
        codec_type = stream.get("codec_type")
        tags = stream.get("tags") or {}
        language = normalize_language_tag(tags.get("language"), exact_only=True)
        title = normalize_language_tag(tags.get("title"), exact_only=False)
        detected = language or title
        if not detected:
            continue
        if codec_type == "audio":
            audio.add(detected)
        elif codec_type == "subtitle":
            subtitles.add(detected)

    return {"audio": sorted(audio), "subtitles": sorted(subtitles)}


def episode_file_by_id(episode_files: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for episode_file in episode_files:
        file_id = parse_int(episode_file.get("id"))
        if file_id is not None:
            indexed[file_id] = episode_file
    return indexed


def episode_belongs_to_season(episode: dict[str, Any], episode_file: dict[str, Any] | None, season_number: int) -> bool:
    path = episode_file.get("path") if episode_file else None
    path_season = infer_season_from_path(path)
    if path_season is not None:
        return path_season == season_number
    return parse_int(episode.get("seasonNumber")) == season_number


def build_season_state(
    series: dict[str, Any],
    episodes: list[dict[str, Any]],
    episode_files: list[dict[str, Any]],
    context: EventContext,
) -> SeasonState:
    if context.series_id is None or context.season_number is None:
        raise ValueError("series_id and season_number are required")

    files_by_id = episode_file_by_id(episode_files)
    states: list[EpisodeState] = []
    source_candidates: list[str] = []

    for episode in episodes:
        episode_file_id = parse_int(episode.get("episodeFileId"))
        episode_file = files_by_id.get(episode_file_id) if episode_file_id is not None else None
        if not episode_belongs_to_season(episode, episode_file, context.season_number):
            continue
        path = episode_file.get("path") if episode_file else None
        has_file = bool(episode.get("hasFile")) and bool(path)
        sonarr_languages = detect_sonarr_file_languages(episode_file)
        state = EpisodeState(
            episode_id=int(episode.get("id")),
            episode_number=parse_int(episode.get("episodeNumber")),
            monitored=bool(episode.get("monitored", False)),
            has_file=has_file,
            episode_file_id=episode_file_id,
            path=path,
            sonarr_audio_languages=sonarr_languages["audio"],
            sonarr_subtitle_languages=sonarr_languages["subtitles"],
        )
        if path:
            source_candidates.append(media_dirname(path))
        states.append(state)

    source_folder = choose_source_folder(context.imported_file_path, source_candidates)
    return SeasonState(
        series_id=context.series_id,
        series_title=series.get("title") or context.series_title or "Unknown Series",
        season_number=context.season_number,
        source_folder=source_folder,
        episodes=states,
    )


def log_season_state_detail(season_state: SeasonState, rules: dict[str, Any]) -> None:
    evaluate_monitored_only = bool(rules.get("evaluate_monitored_only", True))
    relevant_count = 0
    file_count = 0
    monitored_count = 0
    for episode in season_state.episodes:
        if episode.monitored:
            monitored_count += 1
        if episode.has_file:
            file_count += 1
        if episode.monitored or not evaluate_monitored_only:
            relevant_count += 1
        LOG.info(
            "Episode state: episode=%s monitored=%s has_file=%s episode_file_id=%s path=%s",
            episode.episode_number,
            episode.monitored,
            episode.has_file,
            episode.episode_file_id,
            episode.path,
        )
        if episode.sonarr_audio_languages or episode.sonarr_subtitle_languages:
            LOG.info(
                "Episode Sonarr languages: episode=%s audio=%s subtitles=%s",
                episode.episode_number,
                episode.sonarr_audio_languages,
                episode.sonarr_subtitle_languages,
            )
    LOG.info(
        "Season summary: total=%s monitored=%s relevant=%s with_files=%s source_folder=%s",
        len(season_state.episodes),
        monitored_count,
        relevant_count,
        file_count,
        season_state.source_folder,
    )


def choose_source_folder(imported_file_path: str | None, candidates: list[str]) -> str | None:
    if imported_file_path:
        imported_parent = media_dirname(imported_file_path)
        if imported_parent:
            return imported_parent
    if not candidates:
        return None
    try:
        if all(is_posix_media_path(candidate) for candidate in candidates):
            return posixpath.commonpath(candidates)
        return os.path.commonpath(candidates)
    except ValueError:
        return candidates[0]


def is_posix_media_path(path: str) -> bool:
    return path.startswith("/")


def media_dirname(path: str) -> str:
    if is_posix_media_path(path):
        return posixpath.dirname(path)
    return os.path.dirname(path)


def media_normpath(path: str) -> str:
    if is_posix_media_path(path):
        return posixpath.normpath(path)
    return os.path.normpath(path)


def media_join(prefix: str, relative: str) -> str:
    if is_posix_media_path(prefix):
        return posixpath.normpath(posixpath.join(prefix, relative))
    return os.path.normpath(os.path.join(prefix, relative))


def translate_media_path_for_local_read(path: str, config: dict[str, Any], enable_local_mounts: bool = False) -> str:
    if not enable_local_mounts:
        return path
    for mapping in config.get("paths", {}).get("local_mounts", []):
        docker_prefix = str(mapping.get("docker_prefix", ""))
        local_prefix = str(mapping.get("local_prefix", ""))
        if not docker_prefix or not local_prefix:
            continue
        if path_starts_with(path, docker_prefix):
            normalized_path = media_normpath(path)
            normalized_prefix = media_normpath(docker_prefix)
            relative = normalized_path[len(normalized_prefix) :].lstrip("/\\")
            return os.path.normpath(os.path.join(local_prefix, *relative.split("/")))
    return path


def evaluate_season_final(
    season_state: SeasonState,
    rules: dict[str, Any],
    safety: dict[str, Any],
    config: dict[str, Any],
    enable_local_mounts: bool = False,
) -> EvaluationResult:
    allowed_audio = set(rules.get("allowed_final_audio_languages") or [])
    allow_subtitles = bool(rules.get("allow_subtitle_as_final", False))
    evaluate_monitored_only = bool(rules.get("evaluate_monitored_only", True))
    require_all_files = bool(rules.get("require_all_episode_files", True))
    allow_sonarr_fallback = bool(rules.get("allow_sonarr_language_fallback", True))
    min_file_size_mb = float(safety.get("min_file_size_mb", 0) or 0)
    relevant = [episode for episode in season_state.episodes if episode.monitored or not evaluate_monitored_only]

    if not relevant:
        return EvaluationResult(False, None, "no relevant episodes to evaluate", [])

    blocking: list[EpisodeState] = []
    target_language = sorted(allowed_audio)[0] if allowed_audio else None

    for episode in relevant:
        if require_all_files and not episode.has_file:
            episode.block_reason = "missing episode file"
            blocking.append(episode)
            continue
        if not episode.path:
            episode.block_reason = "missing file path"
            blocking.append(episode)
            continue
        local_read_path = translate_media_path_for_local_read(episode.path, config, enable_local_mounts)
        if local_read_path != episode.path:
            LOG.info("Translated media path for local read: %s -> %s", episode.path, local_read_path)

        if not os.path.exists(local_read_path):
            if allow_sonarr_fallback and (episode.sonarr_audio_languages or episode.sonarr_subtitle_languages):
                episode.audio_languages = episode.sonarr_audio_languages
                episode.subtitle_languages = episode.sonarr_subtitle_languages
                episode.language_detection_source = "sonarr-api-fallback"
                LOG.info(
                    "Episode %s file unavailable locally; using Sonarr language fallback audio=%s subtitles=%s",
                    episode.episode_number,
                    episode.audio_languages,
                    episode.subtitle_languages,
                )
            else:
                episode.block_reason = "file does not exist on disk"
                blocking.append(episode)
                continue
        else:
            size_mb = os.path.getsize(local_read_path) / 1024 / 1024
            if min_file_size_mb and size_mb < min_file_size_mb:
                episode.block_reason = f"file smaller than minimum size {min_file_size_mb:g} MB"
                blocking.append(episode)
                continue

            episode_languages = detect_file_languages(local_read_path)
            episode.audio_languages = episode_languages["audio"]
            episode.subtitle_languages = episode_languages["subtitles"]
            episode.language_detection_source = "ffprobe"
            if allow_sonarr_fallback and not episode.audio_languages and not episode.subtitle_languages and (
                episode.sonarr_audio_languages or episode.sonarr_subtitle_languages
            ):
                episode.audio_languages = episode.sonarr_audio_languages
                episode.subtitle_languages = episode.sonarr_subtitle_languages
                episode.language_detection_source = "ffprobe-empty-sonarr-api-fallback"
                LOG.info(
                    "Episode %s ffprobe had no language tags; using Sonarr fallback audio=%s subtitles=%s",
                    episode.episode_number,
                    episode.audio_languages,
                    episode.subtitle_languages,
                )
            elif allow_sonarr_fallback and (episode.sonarr_audio_languages or episode.sonarr_subtitle_languages):
                merged_audio = sorted(set(episode.audio_languages).union(episode.sonarr_audio_languages))
                merged_subtitles = sorted(set(episode.subtitle_languages).union(episode.sonarr_subtitle_languages))
                if merged_audio != episode.audio_languages or merged_subtitles != episode.subtitle_languages:
                    episode.audio_languages = merged_audio
                    episode.subtitle_languages = merged_subtitles
                    episode.language_detection_source = "ffprobe-sonarr-api-merged"
                    LOG.info(
                        "Episode %s supplementing ffprobe with Sonarr metadata audio=%s subtitles=%s",
                        episode.episode_number,
                        episode.audio_languages,
                        episode.subtitle_languages,
                    )

        final_by_audio = bool(allowed_audio.intersection(episode.audio_languages))
        final_by_subtitle = allow_subtitles and bool(allowed_audio.intersection(episode.subtitle_languages))
        episode.is_final = final_by_audio or final_by_subtitle
        LOG.info(
            "Episode %s file=%s source=%s audio=%s subtitles=%s final=%s",
            episode.episode_number,
            episode.path,
            episode.language_detection_source,
            episode.audio_languages,
            episode.subtitle_languages,
            episode.is_final,
        )
        if not episode.is_final:
            episode.block_reason = "final language not detected"
            blocking.append(episode)

    if blocking:
        return EvaluationResult(False, target_language, "one or more episodes are not final", blocking)
    return EvaluationResult(True, target_language, "all relevant episodes are final", [])


def find_path_mapping(config: dict[str, Any], instance_type: str, source_folder: str, target_language: str | None) -> dict[str, Any] | None:
    mappings = config.get("paths", {}).get("mappings", [])
    for mapping in mappings:
        if mapping.get("instance_type") != instance_type:
            continue
        if target_language and mapping.get("final_language") != target_language:
            continue
        if path_starts_with(source_folder, str(mapping.get("source_prefix", ""))):
            return mapping
    return None


def target_language_from_rules(rules: dict[str, Any]) -> str | None:
    allowed_audio = rules.get("allowed_final_audio_languages") or []
    return sorted(allowed_audio)[0] if allowed_audio else None


def path_starts_with(path: str, prefix: str) -> bool:
    normalized_path = media_normpath(path)
    normalized_prefix = media_normpath(prefix)
    separator = "/" if is_posix_media_path(normalized_path) else os.sep
    return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix + separator)


def determine_destination(source_folder: str, mapping: dict[str, Any]) -> str:
    source_prefix = media_normpath(str(mapping["source_prefix"]))
    target_prefix = media_normpath(str(mapping["target_prefix"]))
    normalized_source = media_normpath(source_folder)
    relative = normalized_source[len(source_prefix) :].lstrip("/\\")
    return media_join(target_prefix, relative)


def media_basename(path: str) -> str:
    if is_posix_media_path(path):
        return posixpath.basename(path)
    return os.path.basename(path)


def media_relpath(path: str, start: str) -> str:
    if is_posix_media_path(path) and is_posix_media_path(start):
        return posixpath.relpath(path, start)
    return os.path.relpath(path, start)


def is_physical_season_folder(path: str | None) -> bool:
    if not path:
        return False
    return bool(re.fullmatch(r"Season[ ._-]*\d{1,2}", media_basename(media_normpath(path)), flags=re.IGNORECASE))


def relevant_episodes_for_rules(season_state: SeasonState, rules: dict[str, Any]) -> list[EpisodeState]:
    evaluate_monitored_only = bool(rules.get("evaluate_monitored_only", True))
    return [episode for episode in season_state.episodes if episode.monitored or not evaluate_monitored_only]


def movable_final_episodes(season_state: SeasonState, rules: dict[str, Any]) -> list[EpisodeState]:
    return [episode for episode in relevant_episodes_for_rules(season_state, rules) if episode.is_final and episode.path]


def missing_required_episodes(season_state: SeasonState, rules: dict[str, Any]) -> list[EpisodeState]:
    return [episode for episode in relevant_episodes_for_rules(season_state, rules) if not episode.has_file]


def build_move_items(
    episodes: list[EpisodeState],
    source_folder: str,
    destination_folder: str,
    safety: dict[str, Any],
) -> list[MoveItem]:
    move_items: list[MoveItem] = []
    use_temporary = bool(safety.get("move_to_temporary_folder_first", True))
    temporary_suffix = str(safety.get("temporary_suffix", ".__moving__"))
    for episode in episodes:
        if not episode.path:
            continue
        relative = media_relpath(media_normpath(episode.path), media_normpath(source_folder))
        destination_path = media_join(destination_folder, relative)
        temporary_destination_path = destination_path + temporary_suffix if use_temporary else None
        move_items.append(
            MoveItem(
                episode_id=episode.episode_id,
                episode_number=episode.episode_number,
                source_path=episode.path,
                destination_path=destination_path,
                temporary_destination_path=temporary_destination_path,
            )
        )
    return move_items


def build_move_plan(
    season_state: SeasonState,
    mapping: dict[str, Any],
    destination: str,
    result: EvaluationResult,
    rules: dict[str, Any],
    safety: dict[str, Any],
    dry_run: bool,
) -> MovePlan:
    relevant_episodes = relevant_episodes_for_rules(season_state, rules)
    partial_move = not result.is_final
    move_episodes = movable_final_episodes(season_state, rules) if partial_move else relevant_episodes
    temporary_destination = None
    if not partial_move and safety.get("move_to_temporary_folder_first", True):
        temporary_destination = destination + str(safety.get("temporary_suffix", ".__moving__"))
    move_items = []
    if partial_move and season_state.source_folder:
        move_items = build_move_items(move_episodes, season_state.source_folder, destination, safety)
    return MovePlan(
        series_id=season_state.series_id,
        series_title=season_state.series_title,
        season_number=season_state.season_number,
        mapping_name=mapping.get("name"),
        target_language=result.target_language,
        source_folder=season_state.source_folder or "",
        destination_folder=destination,
        temporary_destination_folder=temporary_destination,
        dry_run=dry_run,
        move_method=str(safety.get("move_method", "shutil.move")),
        partial_move=partial_move,
        will_move=bool(move_episodes),
        will_unmonitor=bool(move_episodes),
        will_rescan=True,
        unmonitor_episode_ids=[episode.episode_id for episode in move_episodes],
        move_items=move_items,
        episode_count=len(season_state.episodes),
        relevant_episode_count=len(relevant_episodes),
        episode_file_count=sum(1 for episode in move_episodes if episode.has_file),
    )


def log_move_plan(plan: MovePlan) -> None:
    LOG.info("Move plan: %s", json.dumps(asdict(plan), ensure_ascii=False, sort_keys=True))
    action_prefix = "DRY RUN: would" if plan.dry_run else "EXECUTE: will"
    if plan.partial_move:
        LOG.info("%s partially move %s file(s) from %s to %s", action_prefix, len(plan.move_items), plan.source_folder, plan.destination_folder)
        for item in plan.move_items:
            LOG.info("%s move episode %s file %s to %s", action_prefix, item.episode_number, item.source_path, item.destination_path)
            if item.temporary_destination_path:
                LOG.info("%s use temporary file %s", action_prefix, item.temporary_destination_path)
    else:
        LOG.info("%s move %s to %s", action_prefix, plan.source_folder, plan.destination_folder)
        if plan.temporary_destination_folder:
            LOG.info("%s use temporary destination %s", action_prefix, plan.temporary_destination_folder)
    LOG.info("%s unmonitor moved episodes for season %s: %s", action_prefix, plan.season_number, plan.unmonitor_episode_ids)
    LOG.info("%s rescan series %s", action_prefix, plan.series_id)


def preflight_move_plan(plan: MovePlan, safety: dict[str, Any]) -> MovePreflightResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not plan.source_folder:
        errors.append("source folder is empty")
    elif not os.path.exists(plan.source_folder):
        errors.append(f"source folder does not exist: {plan.source_folder}")
    elif not os.path.isdir(plan.source_folder):
        errors.append(f"source path is not a directory: {plan.source_folder}")

    if plan.partial_move:
        for item in plan.move_items:
            if not os.path.exists(item.source_path):
                errors.append(f"source file does not exist: {item.source_path}")
            elif not os.path.isfile(item.source_path):
                errors.append(f"source path is not a file: {item.source_path}")
            if safety.get("fail_if_destination_exists", True) and os.path.exists(item.destination_path):
                errors.append(f"destination file already exists: {item.destination_path}")
            if item.temporary_destination_path and os.path.exists(item.temporary_destination_path):
                errors.append(f"temporary destination file already exists: {item.temporary_destination_path}")
            item_parent = os.path.dirname(item.destination_path)
            if not item_parent:
                errors.append(f"destination parent cannot be determined: {item.destination_path}")
            elif os.path.exists(item_parent) and not os.path.isdir(item_parent):
                errors.append(f"destination parent exists but is not a directory: {item_parent}")
            elif not os.path.exists(item_parent):
                warning = f"destination parent will be created: {item_parent}"
                if warning not in warnings:
                    warnings.append(warning)
        return MovePreflightResult(errors, warnings)

    if safety.get("fail_if_destination_exists", True) and os.path.exists(plan.destination_folder):
        errors.append(f"destination already exists: {plan.destination_folder}")

    if plan.temporary_destination_folder and os.path.exists(plan.temporary_destination_folder):
        errors.append(f"temporary destination already exists: {plan.temporary_destination_folder}")

    destination_parent = os.path.dirname(plan.destination_folder)
    if not destination_parent:
        errors.append(f"destination parent cannot be determined: {plan.destination_folder}")
    elif os.path.exists(destination_parent) and not os.path.isdir(destination_parent):
        errors.append(f"destination parent exists but is not a directory: {destination_parent}")
    elif not os.path.exists(destination_parent):
        warnings.append(f"destination parent will be created: {destination_parent}")

    return MovePreflightResult(errors, warnings)


def log_move_preflight(result: MovePreflightResult) -> None:
    for warning in result.warnings:
        LOG.warning("Move preflight warning: %s", warning)
    for error in result.errors:
        LOG.error("Move preflight error: %s", error)
    if result.errors:
        LOG.error("Move preflight failed: %s error(s), %s warning(s)", len(result.errors), len(result.warnings))
    else:
        LOG.info("Move preflight OK: %s warning(s)", len(result.warnings))


def verify_folder_sizes(source: str, destination: str) -> bool:
    source_files = collect_file_sizes(source)
    destination_files = collect_file_sizes(destination)
    return source_files == destination_files


def collect_file_sizes(folder: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for root, _, files in os.walk(folder):
        for file_name in files:
            path = os.path.join(root, file_name)
            relative = os.path.relpath(path, folder)
            sizes[relative] = os.path.getsize(path)
    return sizes


def move_season(source: str, destination: str, safety: dict[str, Any]) -> None:
    destination_parent = os.path.dirname(destination)
    os.makedirs(destination_parent, exist_ok=True)
    if safety.get("fail_if_destination_exists", True) and os.path.exists(destination):
        raise FileExistsError(f"Destination already exists: {destination}")

    temporary_destination = destination
    if safety.get("move_to_temporary_folder_first", True):
        temporary_destination = destination + str(safety.get("temporary_suffix", ".__moving__"))
        if os.path.exists(temporary_destination):
            raise FileExistsError(f"Temporary destination already exists: {temporary_destination}")

    source_sizes = collect_file_sizes(source)
    shutil.move(source, temporary_destination)
    destination_sizes = collect_file_sizes(temporary_destination)
    if source_sizes != destination_sizes:
        raise RuntimeError("Internal move verification failed")
    if temporary_destination != destination:
        os.rename(temporary_destination, destination)


def move_episode_files(move_items: list[MoveItem], safety: dict[str, Any]) -> None:
    for item in move_items:
        destination_parent = os.path.dirname(item.destination_path)
        os.makedirs(destination_parent, exist_ok=True)
        if safety.get("fail_if_destination_exists", True) and os.path.exists(item.destination_path):
            raise FileExistsError(f"Destination already exists: {item.destination_path}")
        if item.temporary_destination_path and os.path.exists(item.temporary_destination_path):
            raise FileExistsError(f"Temporary destination already exists: {item.temporary_destination_path}")

        source_size = os.path.getsize(item.source_path)
        move_destination = item.temporary_destination_path or item.destination_path
        shutil.move(item.source_path, move_destination)
        destination_size = os.path.getsize(move_destination)
        if source_size != destination_size:
            raise RuntimeError(f"Internal move verification failed for {item.source_path}")
        if move_destination != item.destination_path:
            os.rename(move_destination, item.destination_path)


def log_evaluation(result: EvaluationResult) -> None:
    LOG.info("Evaluation result: final=%s reason=%s target_language=%s", result.is_final, result.reason, result.target_language)
    for episode in result.blocking_episodes:
        LOG.info(
            "Blocking episode=%s path=%s reason=%s audio=%s subtitles=%s",
            episode.episode_number,
            episode.path,
            episode.block_reason,
            episode.audio_languages,
            episode.subtitle_languages,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sonarr post-import media finalizer")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("MEDIA_FINALIZER_CONFIG", default_config_path())))
    parser.add_argument("--instance", choices=["tv", "anime"], help="Override active_instance from config")
    parser.add_argument("--url-mode", choices=["docker", "lan", "tailscale"], default="docker")
    parser.add_argument("--validate-config", action="store_true", help="Validate config structure without calling Sonarr or touching media files")
    parser.add_argument("--test-api", action="store_true", help="Only test Sonarr API connectivity and root folders")
    parser.add_argument("--list-series", action="store_true", help="List Sonarr series IDs and monitored seasons")
    parser.add_argument("--filter", help="Filter --list-series by title text")
    parser.add_argument("--root-prefix", help="Filter --list-series by Sonarr root path prefix, e.g. /tv-en")
    parser.add_argument("--limit", type=int, default=30, help="Maximum rows for --list-series")
    parser.add_argument("--series-id", type=int, help="Manually provide Sonarr series ID for dry-run testing")
    parser.add_argument("--season-number", type=int, help="Manually provide Sonarr season number for dry-run testing")
    parser.add_argument("--imported-file-path", help="Manually provide imported episode path for dry-run testing")
    parser.add_argument("--event-type", help="Manually provide Sonarr event type for dry-run testing")
    parser.add_argument("--inspect-season", action="store_true", help="Load and log season state without ffprobe or move evaluation")
    parser.add_argument(
        "--enable-local-mounts",
        action="store_true",
        help="Use paths.local_mounts for read-only ffprobe tests from a development machine. Do not use in production Docker runs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    parser.add_argument("--execute", action="store_true", help="Allow file moves and Sonarr updates if config dry_run is false")
    parser.add_argument(
        "--allow-non-docker-execute",
        action="store_true",
        help="Allow --execute with --url-mode lan/tailscale. Intended only for deliberate advanced testing.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config, args.config)
    LOG.info("Config path: %s", args.config)

    if args.validate_config:
        return validate_config_command(config)

    sonarr_config = get_active_sonarr_config(config, args.instance)
    sonarr_base_url = get_sonarr_base_url(sonarr_config, args.url_mode)
    safety = config.get("safety", {})
    dry_run = bool(safety.get("dry_run", True)) or args.dry_run
    if args.execute and not safety.get("dry_run", True):
        dry_run = False
    if not dry_run and args.url_mode != "docker" and not args.allow_non_docker_execute:
        LOG.error("Refusing non-dry-run execution with url_mode=%s. Use --url-mode docker or --allow-non-docker-execute.", args.url_mode)
        return 2
    LOG.info(
        "Active Sonarr instance: %s type=%s url_mode=%s url=%s",
        sonarr_config["name"],
        sonarr_config.get("instance_type"),
        args.url_mode,
        sonarr_base_url,
    )
    LOG.info("Dry run: %s", dry_run)
    LOG.info("Local mount translation: %s", args.enable_local_mounts)

    client = SonarrClient(sonarr_base_url, str(sonarr_config["api_key"]))
    if args.test_api:
        return test_sonarr_api(client, sonarr_config, sonarr_base_url)
    if args.list_series:
        return list_sonarr_series(client, args.filter, args.root_prefix, args.limit)

    env = read_sonarr_env()
    context = build_event_context(env)
    context = apply_manual_event_overrides(context, args)
    LOG.info("Event context: %s", context)

    allowed_event_types = set(safety.get("allowed_event_types") or ["Download", "Import", "Upgrade"])
    if context.event_type not in allowed_event_types:
        LOG.info("Ignoring event type %r", context.event_type)
        return 0
    if context.series_id is None:
        LOG.error("Missing sonarr_series_id")
        return 2

    series = client.get_series(context.series_id)
    episodes = client.get_episodes(context.series_id)
    episode_files = client.get_episode_files(context.series_id)
    context = resolve_missing_event_context(context, episode_files)

    if context.season_number is None:
        LOG.error("Could not determine season number")
        return 2
    if context.season_number == 0 and safety.get("skip_specials", True):
        LOG.info("Skipping specials season 0")
        return 0

    rules_by_type = config.get("rules", {})
    instance_type = str(sonarr_config.get("instance_type"))
    rules = rules_by_type.get(instance_type, {})
    season_state = build_season_state(series, episodes, episode_files, context)
    LOG.info(
        "Season state: series=%s season=%s source=%s episodes=%s",
        season_state.series_title,
        season_state.season_number,
        season_state.source_folder,
        len(season_state.episodes),
    )
    log_season_state_detail(season_state, rules)

    if args.inspect_season:
        LOG.info("Inspect-season mode enabled. Stopping before ffprobe/evaluation.")
        return 0

    target_language = target_language_from_rules(rules)
    mapping = find_path_mapping(config, instance_type, season_state.source_folder or "", target_language)
    if not mapping:
        LOG.info(
            "Season source is not a configured maintenance source for target_language=%s. Skipping before language evaluation: %s",
            target_language,
            season_state.source_folder,
        )
        return 0

    result = evaluate_season_final(season_state, rules, safety, config, args.enable_local_mounts)
    log_evaluation(result)
    if not season_state.source_folder:
        LOG.error("Cannot determine source season folder")
        return 2
    if not result.is_final:
        missing_episodes = missing_required_episodes(season_state, rules)
        if missing_episodes:
            LOG.info(
                "Season has monitored/relevant missing episodes. Treating as in-progress and not moving anything: %s",
                [episode.episode_number for episode in missing_episodes],
            )
            return 0
        if is_physical_season_folder(season_state.source_folder):
            LOG.info("Season folder is not fully final yet. Nothing to do.")
            return 0
        if not movable_final_episodes(season_state, rules):
            LOG.info("Loose season folder has no final movable episodes yet. Nothing to do.")
            return 0
        LOG.info("Loose season folder is partially final. Planning move for final episode files only.")

    destination = determine_destination(season_state.source_folder, mapping)
    move_plan = build_move_plan(season_state, mapping, destination, result, rules, safety, dry_run)
    LOG.info("Selected mapping: %s", mapping.get("name"))
    LOG.info("Source folder: %s", season_state.source_folder)
    LOG.info("Destination folder: %s", destination)
    log_move_plan(move_plan)

    if dry_run:
        return 0

    preflight = preflight_move_plan(move_plan, safety)
    log_move_preflight(preflight)
    if preflight.errors:
        LOG.error("Move preflight failed. Not moving and not unmonitoring.")
        return 1

    if move_plan.partial_move:
        move_episode_files(move_plan.move_items, safety)
    else:
        move_season(season_state.source_folder, destination, safety)
    client.unmonitor_episodes(move_plan.unmonitor_episode_ids)
    client.rescan_series(season_state.series_id)
    LOG.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
