from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sonarr_post_import_finalizer.py"
spec = importlib.util.spec_from_file_location("sonarr_post_import_finalizer", SCRIPT_PATH)
finalizer = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = finalizer
spec.loader.exec_module(finalizer)


class FinalizerHelperTests(unittest.TestCase):
    def test_infer_season_from_common_patterns(self) -> None:
        self.assertEqual(finalizer.infer_season_from_path("/anime-jp/Test/Season 02/Test S02E01.mkv"), 2)
        self.assertEqual(finalizer.infer_season_from_path("/tv-en/Test/Test.S09E05.mkv"), 9)
        self.assertIsNone(finalizer.infer_season_from_path("/tv-en/Test/movie.mkv"))

    def test_build_event_context_prefers_file_path_season_over_sonarr_metadata(self) -> None:
        context = finalizer.build_event_context(
            {
                "sonarr_eventtype": "Download",
                "sonarr_series_id": "252",
                "sonarr_episodefile_seasonnumber": "1",
                "sonarr_episodefile_path": "/anime-jp/Bookworm/Season 04/Bookworm S04E01.mkv",
            }
        )

        self.assertEqual(context.season_number, 4)

    def test_build_movie_event_context_reads_radarr_event(self) -> None:
        context = finalizer.build_movie_event_context(
            {
                "radarr_eventtype": "Download",
                "radarr_movie_id": "77",
                "radarr_movie_title": "Example Movie",
                "radarr_movie_path": "/movies-en/Example Movie (2026)",
                "radarr_moviefile_id": "700",
                "radarr_moviefile_path": "/movies-en/Example Movie (2026)/Example Movie.mkv",
            }
        )

        self.assertEqual(context.event_type, "Download")
        self.assertEqual(context.movie_id, 77)
        self.assertEqual(context.movie_file_id, 700)
        self.assertEqual(context.movie_file_path, "/movies-en/Example Movie (2026)/Example Movie.mkv")

    def test_resolve_missing_event_context_prefers_episode_file_path_season(self) -> None:
        context = finalizer.EventContext(
            event_type="Download",
            series_id=252,
            series_title=None,
            series_path=None,
            season_number=None,
            imported_file_path=None,
            episode_file_id=5650,
        )

        resolved = finalizer.resolve_missing_event_context(
            context,
            [
                {
                    "id": 5650,
                    "seasonNumber": 1,
                    "path": "/anime-jp/Bookworm/Season 04/Bookworm S04E01.mkv",
                }
            ],
        )

        self.assertEqual(resolved.season_number, 4)
        self.assertEqual(resolved.imported_file_path, "/anime-jp/Bookworm/Season 04/Bookworm S04E01.mkv")

    def test_build_season_state_uses_physical_folder_season_when_sonarr_season_is_wrong(self) -> None:
        series = {"title": "Ascendance of a Bookworm"}
        episodes = [
            {"id": 1, "episodeNumber": 1, "seasonNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 101},
            {"id": 2, "episodeNumber": 2, "seasonNumber": 1, "monitored": False, "hasFile": False, "episodeFileId": 0},
            {"id": 37, "episodeNumber": 37, "seasonNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 137},
            {"id": 38, "episodeNumber": 38, "seasonNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 138},
            {"id": 41, "episodeNumber": 41, "seasonNumber": 1, "monitored": False, "hasFile": False, "episodeFileId": 0},
        ]
        episode_files = [
            {"id": 101, "path": "/anime-jp/Bookworm/Season 01/Bookworm S01E01.mp4"},
            {"id": 137, "path": "/anime-jp/Bookworm/Season 04/Bookworm S04E01.mkv"},
            {"id": 138, "path": "/anime-jp/Bookworm/Season 04/Bookworm S04E02.mkv"},
        ]
        context = finalizer.EventContext(
            event_type="Download",
            series_id=252,
            series_title=None,
            series_path=None,
            season_number=4,
            imported_file_path=None,
            episode_file_id=None,
        )

        season_state = finalizer.build_season_state(series, episodes, episode_files, context)

        self.assertEqual(season_state.season_number, 4)
        self.assertEqual(season_state.source_folder, "/anime-jp/Bookworm/Season 04")
        self.assertEqual([episode.episode_number for episode in season_state.episodes], [37, 38])

    def test_evaluate_season_final_can_merge_sonarr_languages_with_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "Bookworm S04E03.mkv"
            media_path.write_text("media", encoding="utf-8")
            season_state = finalizer.SeasonState(
                series_id=252,
                series_title="Ascendance of a Bookworm",
                season_number=4,
                source_folder=str(media_path.parent),
                episodes=[
                    finalizer.EpisodeState(
                        episode_id=39,
                        episode_number=39,
                        monitored=True,
                        has_file=True,
                        episode_file_id=5648,
                        path=str(media_path),
                        sonarr_audio_languages=["en", "jp"],
                        sonarr_subtitle_languages=["en"],
                    )
                ],
            )
            original_detect_file_languages = finalizer.detect_file_languages
            finalizer.detect_file_languages = lambda _path: {"audio": ["jp"], "subtitles": []}
            try:
                result = finalizer.evaluate_season_final(
                    season_state,
                    {
                        "allowed_final_audio_languages": ["en"],
                        "evaluate_monitored_only": True,
                        "require_all_episode_files": True,
                        "allow_sonarr_language_fallback": True,
                    },
                    {"min_file_size_mb": 0},
                    {},
                )
            finally:
                finalizer.detect_file_languages = original_detect_file_languages

        self.assertTrue(result.is_final)
        self.assertEqual(season_state.episodes[0].audio_languages, ["en", "jp"])
        self.assertEqual(season_state.episodes[0].language_detection_source, "ffprobe-sonarr-api-merged")

    def test_evaluate_specials_final_uses_sonarr_download_completion_without_language(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=252,
            series_title="Example Specials",
            season_number=0,
            source_folder="/anime-jp/Example Specials/Season 00",
            episodes=[
                finalizer.EpisodeState(
                    episode_id=1,
                    episode_number=1,
                    monitored=True,
                    has_file=True,
                    episode_file_id=101,
                    path="/anime-jp/Example Specials/Season 00/OVA 01.mkv",
                ),
                finalizer.EpisodeState(
                    episode_id=2,
                    episode_number=2,
                    monitored=True,
                    has_file=True,
                    episode_file_id=102,
                    path="/anime-jp/Example Specials/Season 00/OVA 02.mkv",
                ),
                finalizer.EpisodeState(
                    episode_id=3,
                    episode_number=3,
                    monitored=False,
                    has_file=False,
                    episode_file_id=None,
                    path=None,
                ),
            ],
        )

        result = finalizer.evaluate_season_final(
            season_state,
            {
                "allowed_final_audio_languages": ["en"],
                "evaluate_monitored_only": False,
                "require_all_episode_files": True,
                "move_specials_when_complete": True,
            },
            {"min_file_size_mb": 0},
            {},
        )

        self.assertTrue(result.is_final)
        self.assertEqual(result.reason, "all relevant specials are downloaded")
        self.assertTrue(all(episode.is_final for episode in season_state.episodes[:2]))
        self.assertFalse(season_state.episodes[2].is_final)
        self.assertEqual(
            [episode.language_detection_source for episode in season_state.episodes[:2]],
            ["sonarr-specials-complete", "sonarr-specials-complete"],
        )

        plan = finalizer.build_move_plan(
            season_state,
            {"name": "anime", "source_prefix": "/anime-jp", "target_prefix": "/anime-en"},
            "/anime-en/Example Specials/Season 00",
            result,
            {
                "allowed_final_audio_languages": ["en"],
                "evaluate_monitored_only": False,
                "require_all_episode_files": True,
                "move_specials_when_complete": True,
            },
            {"move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"},
            dry_run=True,
        )

        self.assertIsNone(plan.unmonitor_season_number)
        self.assertEqual(plan.unmonitor_episode_ids, [1, 2])
        self.assertEqual(plan.relevant_episode_count, 2)

    def test_evaluate_specials_final_blocks_missing_sonarr_file(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=252,
            series_title="Example Specials",
            season_number=0,
            source_folder="/anime-jp/Example Specials/Season 00",
            episodes=[
                finalizer.EpisodeState(
                    episode_id=1,
                    episode_number=1,
                    monitored=True,
                    has_file=True,
                    episode_file_id=101,
                    path="/anime-jp/Example Specials/Season 00/OVA 01.mkv",
                ),
                finalizer.EpisodeState(
                    episode_id=2,
                    episode_number=2,
                    monitored=True,
                    has_file=False,
                    episode_file_id=None,
                    path=None,
                ),
            ],
        )

        result = finalizer.evaluate_season_final(
            season_state,
            {
                "allowed_final_audio_languages": ["en"],
                "evaluate_monitored_only": True,
                "require_all_episode_files": True,
                "move_specials_when_complete": True,
            },
            {"min_file_size_mb": 0},
            {},
        )

        self.assertFalse(result.is_final)
        self.assertEqual(result.reason, "one or more specials are not downloaded")
        self.assertEqual([episode.episode_number for episode in result.blocking_episodes], [2])

    def test_evaluate_movie_final_uses_radarr_language_fallback(self) -> None:
        movie_state = finalizer.MovieState(
            movie_id=77,
            title="Example Movie",
            movie_path="/movies-en/Example Movie (2026)",
            movie_file_id=700,
            file_path="/movies-en/Example Movie (2026)/Example Movie.mkv",
            radarr_audio_languages=["cz"],
        )

        result = finalizer.evaluate_movie_final(
            movie_state,
            {"allowed_final_audio_languages": ["cz"], "allow_radarr_language_fallback": True},
            {"min_file_size_mb": 0},
            {},
        )

        self.assertTrue(result.is_final)
        self.assertEqual(movie_state.audio_languages, ["cz"])
        self.assertEqual(movie_state.language_detection_source, "radarr-api-fallback")

    def test_normalize_language_tag_exact_and_title_fallback(self) -> None:
        self.assertEqual(finalizer.normalize_language_tag("eng", exact_only=True), "en")
        self.assertEqual(finalizer.normalize_language_tag("English Dub", exact_only=False), "en")
        self.assertEqual(finalizer.normalize_language_tag("cze", exact_only=True), "cz")
        self.assertEqual(finalizer.normalize_language_tag("Japanese Audio", exact_only=False), "jp")
        self.assertIsNone(finalizer.normalize_language_tag("unknown", exact_only=False))

    def test_posix_media_paths_are_preserved_on_windows(self) -> None:
        path = "/anime-jp/Example/Season 02/Episode.mkv"
        self.assertEqual(finalizer.media_dirname(path), "/anime-jp/Example/Season 02")
        self.assertEqual(finalizer.media_normpath("/anime-jp/Example/../Example/Season 02"), "/anime-jp/Example/Season 02")

    def test_determine_destination_uses_posix_paths(self) -> None:
        mapping = {
            "source_prefix": "/anime-jp",
            "target_prefix": "/anime-en",
        }
        self.assertEqual(finalizer.determine_destination("/anime-jp/Example/Season 02", mapping), "/anime-en/Example/Season 02")

    def test_is_physical_season_folder_only_matches_season_directory_name(self) -> None:
        self.assertTrue(finalizer.is_physical_season_folder("/anime-jp/Example/Season 01"))
        self.assertFalse(finalizer.is_physical_season_folder("/anime-jp/Example"))
        self.assertFalse(finalizer.is_physical_season_folder("/anime-jp/Example/Loose S01E01 files"))

    def test_find_path_mapping_only_matches_maintenance_source(self) -> None:
        config = {
            "paths": {
                "mappings": [
                    {
                        "instance_type": "anime",
                        "source_prefix": "/anime-jp",
                        "target_prefix": "/anime-en",
                        "final_language": "en",
                    }
                ]
            }
        }
        source_mapping = finalizer.find_path_mapping(config, "anime", "/anime-jp/Example/Season 01", "en")
        target_mapping = finalizer.find_path_mapping(config, "anime", "/anime-en/Example/Season 01", "en")
        self.assertIsNotNone(source_mapping)
        self.assertIsNone(target_mapping)

    def test_translate_media_path_for_local_read(self) -> None:
        config = {
            "paths": {
                "local_mounts": [
                    {
                        "docker_prefix": "/anime-en",
                        "local_prefix": r"\\NAS_HOST\share\ANIME\English",
                    }
                ]
            }
        }
        untranslated = finalizer.translate_media_path_for_local_read(
            "/anime-en/Example/Season 01/Episode 01.mkv",
            config,
        )
        translated = finalizer.translate_media_path_for_local_read(
            "/anime-en/Example/Season 01/Episode 01.mkv",
            config,
            enable_local_mounts=True,
        )
        self.assertEqual(untranslated, "/anime-en/Example/Season 01/Episode 01.mkv")
        self.assertEqual(translated, r"\\NAS_HOST\share\ANIME\English\Example\Season 01\Episode 01.mkv")

    def test_detect_sonarr_file_languages_from_languages_and_media_info(self) -> None:
        episode_file = {
            "languages": [{"name": "English"}, {"name": "Czech"}],
            "mediaInfo": {
                "audioLanguages": "eng/cze",
                "subtitles": "eng/cze",
            },
        }
        languages = finalizer.detect_sonarr_file_languages(episode_file)
        self.assertEqual(languages["audio"], ["cz", "en"])
        self.assertEqual(languages["subtitles"], ["cz", "en"])

    def test_detect_sonarr_file_languages_handles_unknown_media_info(self) -> None:
        episode_file = {
            "languages": [{"name": "Japanese"}],
            "mediaInfo": {
                "audioLanguages": "und",
                "subtitles": "",
            },
        }
        languages = finalizer.detect_sonarr_file_languages(episode_file)
        self.assertEqual(languages["audio"], ["jp"])
        self.assertEqual(languages["subtitles"], [])

    def test_validate_config_accepts_maintenance_mapping(self) -> None:
        config = {
            "active_instance": "tv",
            "sonarr_instances": {
                "tv": {
                    "url": "http://sonarr:8989",
                    "lan_url": "http://LAN_SONARR_HOST:8989",
                    "tailscale_url": "http://TAILSCALE_SONARR_HOST:8989",
                    "api_key": "secret",
                    "instance_type": "tv",
                    "maintenance_roots": {"staging_en": "/tv-en"},
                }
            },
            "paths": {
                "mappings": [
                    {
                        "instance_type": "tv",
                        "source_prefix": "/tv-en",
                        "target_prefix": "/tv-cz",
                        "final_language": "cz",
                    }
                ],
                "local_mounts": [],
            },
            "rules": {"tv": {"allowed_final_audio_languages": ["cz"]}},
            "safety": {"dry_run": True},
        }
        errors, warnings = finalizer.validate_config(config)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_validate_config_warns_for_missing_api_key(self) -> None:
        config = {
            "active_instance": "anime",
            "sonarr_instances": {
                "anime": {
                    "url": "http://sonarr-anime:8989",
                    "lan_url": "http://LAN_SONARR_HOST:8990",
                    "tailscale_url": "http://TAILSCALE_SONARR_HOST:8990",
                    "api_key": "",
                    "instance_type": "anime",
                    "maintenance_roots": {"staging_jp": "/anime-jp"},
                }
            },
            "paths": {
                "mappings": [
                    {
                        "instance_type": "anime",
                        "source_prefix": "/anime-jp",
                        "target_prefix": "/anime-en",
                        "final_language": "en",
                    }
                ]
            },
            "rules": {"anime": {"allowed_final_audio_languages": ["en"]}},
        }
        errors, warnings = finalizer.validate_config(config)
        self.assertEqual(errors, [])
        self.assertIn("sonarr_instances.anime.api_key missing api key; fill it in local config before runtime", warnings)

    def test_resolve_active_instance_rejects_missing_api_key(self) -> None:
        config = {
            "active_instance": "anime",
            "sonarr_instances": {
                "anime": {
                    "url": "http://sonarr-anime:8989",
                    "api_key": "",
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "missing api key"):
            finalizer.get_active_sonarr_config(config)

    def test_validate_config_rejects_invalid_mapping(self) -> None:
        config = {
            "active_instance": "tv",
            "sonarr_instances": {
                "tv": {
                    "url": "http://sonarr:8989",
                    "api_key": "secret",
                    "instance_type": "tv",
                    "maintenance_roots": {"staging_en": "/tv-en"},
                }
            },
            "paths": {
                "mappings": [
                    {
                        "instance_type": "tv",
                        "source_prefix": "/tv-en",
                        "target_prefix": "/tv-en",
                        "final_language": "en",
                    }
                ]
            },
            "rules": {"tv": {"allowed_final_audio_languages": ["cz"]}},
        }
        errors, _ = finalizer.validate_config(config)
        self.assertIn("paths.mappings[0].source_prefix and target_prefix must differ", errors)
        self.assertIn("paths.mappings[0].final_language 'en' is not allowed by rules.tv", errors)

    def test_build_move_plan_summarizes_final_action(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=42,
            series_title="Example Show",
            season_number=2,
            source_folder="/tv-en/Example Show/Season 02",
            episodes=[
                finalizer.EpisodeState(
                    episode_id=1,
                    episode_number=1,
                    monitored=True,
                    has_file=True,
                    episode_file_id=101,
                    path="/tv-en/Example Show/Season 02/Episode 01.mkv",
                ),
                finalizer.EpisodeState(
                    episode_id=2,
                    episode_number=2,
                    monitored=False,
                    has_file=True,
                    episode_file_id=102,
                    path="/tv-en/Example Show/Season 02/Episode 02.mkv",
                ),
            ],
        )
        mapping = {
            "name": "TV English maintenance to Czech target",
            "source_prefix": "/tv-en",
            "target_prefix": "/tv-cz",
        }
        result = finalizer.EvaluationResult(True, "cz", "all relevant episodes are final", [])
        plan = finalizer.build_move_plan(
            season_state,
            mapping,
            "/tv-cz/Example Show/Season 02",
            result,
            {"evaluate_monitored_only": True},
            {"move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"},
            dry_run=True,
        )

        self.assertEqual(plan.series_id, 42)
        self.assertEqual(plan.mapping_name, "TV English maintenance to Czech target")
        self.assertEqual(plan.source_folder, "/tv-en/Example Show/Season 02")
        self.assertEqual(plan.destination_folder, "/tv-cz/Example Show/Season 02")
        self.assertEqual(plan.temporary_destination_folder, "/tv-cz/Example Show/Season 02.__moving__")
        self.assertTrue(plan.dry_run)
        self.assertFalse(plan.partial_move)
        self.assertTrue(plan.will_move)
        self.assertTrue(plan.will_unmonitor)
        self.assertTrue(plan.will_rescan)
        self.assertEqual(plan.unmonitor_season_number, 2)
        self.assertEqual(plan.unmonitor_episode_ids, [1])
        self.assertEqual(plan.move_items, [])
        self.assertEqual(plan.episode_count, 2)
        self.assertEqual(plan.relevant_episode_count, 1)
        self.assertEqual(plan.episode_file_count, 1)

    def test_build_move_plan_can_partially_move_final_files_from_loose_folder(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=253,
            series_title="HELL MODE",
            season_number=1,
            source_folder="/anime-jp/HELL MODE",
            episodes=[
                finalizer.EpisodeState(
                    episode_id=10,
                    episode_number=1,
                    monitored=True,
                    has_file=True,
                    episode_file_id=100,
                    path="/anime-jp/HELL MODE/HELL MODE S01E01.mkv",
                    is_final=True,
                ),
                finalizer.EpisodeState(
                    episode_id=11,
                    episode_number=2,
                    monitored=True,
                    has_file=True,
                    episode_file_id=101,
                    path="/anime-jp/HELL MODE/HELL MODE S01E02.mkv",
                    is_final=False,
                ),
                finalizer.EpisodeState(
                    episode_id=12,
                    episode_number=3,
                    monitored=True,
                    has_file=True,
                    episode_file_id=102,
                    path="/anime-jp/HELL MODE/HELL MODE S01E03.mkv",
                    is_final=False,
                ),
            ],
        )

        plan = finalizer.build_move_plan(
            season_state,
            {"name": "anime", "source_prefix": "/anime-jp", "target_prefix": "/anime-en"},
            "/anime-en/HELL MODE",
            finalizer.EvaluationResult(False, "en", "one or more episodes are not final", [season_state.episodes[1]]),
            {"evaluate_monitored_only": True},
            {"move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"},
            dry_run=True,
        )

        self.assertTrue(plan.partial_move)
        self.assertIsNone(plan.unmonitor_season_number)
        self.assertEqual(plan.unmonitor_episode_ids, [10])
        self.assertEqual(plan.episode_count, 3)
        self.assertEqual(plan.relevant_episode_count, 3)
        self.assertEqual(plan.episode_file_count, 1)
        self.assertEqual(len(plan.move_items), 1)
        self.assertEqual(plan.move_items[0].source_path, "/anime-jp/HELL MODE/HELL MODE S01E01.mkv")
        self.assertEqual(plan.move_items[0].destination_path, "/anime-en/HELL MODE/HELL MODE S01E01.mkv")
        self.assertEqual(plan.move_items[0].temporary_destination_path, "/anime-en/HELL MODE/HELL MODE S01E01.mkv.__moving__")

    def test_missing_required_episodes_detects_monitored_missing_files(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=253,
            series_title="HELL MODE",
            season_number=1,
            source_folder="/anime-jp/HELL MODE",
            episodes=[
                finalizer.EpisodeState(
                    episode_id=10,
                    episode_number=1,
                    monitored=True,
                    has_file=True,
                    episode_file_id=100,
                    path="/anime-jp/HELL MODE/HELL MODE S01E01.mkv",
                    is_final=True,
                ),
                finalizer.EpisodeState(
                    episode_id=11,
                    episode_number=2,
                    monitored=True,
                    has_file=False,
                    episode_file_id=None,
                    path=None,
                    is_final=False,
                ),
                finalizer.EpisodeState(
                    episode_id=12,
                    episode_number=3,
                    monitored=False,
                    has_file=False,
                    episode_file_id=None,
                    path=None,
                    is_final=False,
                ),
            ],
        )

        missing = finalizer.missing_required_episodes(season_state, {"evaluate_monitored_only": True})

        self.assertEqual([episode.episode_number for episode in missing], [2])

    def test_build_move_plan_can_disable_temporary_destination(self) -> None:
        season_state = finalizer.SeasonState(
            series_id=7,
            series_title="Example Anime",
            season_number=1,
            source_folder="/anime-jp/Example Anime/Season 01",
            episodes=[],
        )
        plan = finalizer.build_move_plan(
            season_state,
            {"source_prefix": "/anime-jp", "target_prefix": "/anime-en"},
            "/anime-en/Example Anime/Season 01",
            finalizer.EvaluationResult(True, "en", "all relevant episodes are final", []),
            {"evaluate_monitored_only": True},
            {"move_to_temporary_folder_first": False},
            dry_run=False,
        )

        self.assertIsNone(plan.temporary_destination_folder)
        self.assertFalse(plan.dry_run)
        self.assertFalse(plan.partial_move)
        self.assertEqual(plan.move_method, "shutil.move")

    def test_preflight_move_plan_accepts_ready_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "Season 01"
            destination = root / "target" / "Season 01"
            source.mkdir(parents=True)
            (source / "Episode 01.mkv").write_text("media", encoding="utf-8")
            plan = finalizer.MovePlan(
                series_id=1,
                series_title="Example",
                season_number=1,
                mapping_name="test",
                target_language="cz",
                source_folder=str(source),
                destination_folder=str(destination),
                temporary_destination_folder=str(destination) + ".__moving__",
                dry_run=False,
                move_method="shutil.move",
                partial_move=False,
                will_move=True,
                will_unmonitor=True,
                will_rescan=True,
                unmonitor_season_number=1,
                unmonitor_episode_ids=[1],
                move_items=[],
                episode_count=1,
                relevant_episode_count=1,
                episode_file_count=1,
            )

            result = finalizer.preflight_move_plan(plan, {"fail_if_destination_exists": True})

            self.assertEqual(result.errors, [])
            self.assertEqual(result.warnings, [f"destination parent will be created: {destination.parent}"])

    def test_preflight_move_plan_rejects_missing_source_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "target" / "Season 01"
            destination.mkdir(parents=True)
            plan = finalizer.MovePlan(
                series_id=1,
                series_title="Example",
                season_number=1,
                mapping_name="test",
                target_language="cz",
                source_folder=str(root / "missing"),
                destination_folder=str(destination),
                temporary_destination_folder=str(destination) + ".__moving__",
                dry_run=False,
                move_method="shutil.move",
                partial_move=False,
                will_move=True,
                will_unmonitor=True,
                will_rescan=True,
                unmonitor_season_number=1,
                unmonitor_episode_ids=[1],
                move_items=[],
                episode_count=1,
                relevant_episode_count=1,
                episode_file_count=1,
            )

            result = finalizer.preflight_move_plan(plan, {"fail_if_destination_exists": True})

            self.assertIn(f"source folder does not exist: {root / 'missing'}", result.errors)
            self.assertIn(f"destination already exists: {destination}", result.errors)

    def test_move_season_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "Episode 01.mkv").write_text("media", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                finalizer.move_season(str(source), str(destination), {"fail_if_destination_exists": True})

            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())

    def test_move_season_creates_missing_destination_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "Season 01"
            destination = root / "target" / "Example" / "Season 01"
            source.mkdir(parents=True)
            (source / "Episode 01.mkv").write_text("media", encoding="utf-8")

            finalizer.move_season(str(source), str(destination), {"fail_if_destination_exists": True})

            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            self.assertTrue((destination / "Episode 01.mkv").exists())

    def test_move_season_rolls_back_when_final_rename_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "Season 01"
            destination = root / "target" / "Example" / "Season 01"
            source.mkdir(parents=True)
            (source / "Episode 01.mkv").write_text("media", encoding="utf-8")
            original_rename = finalizer.os.rename

            def failing_rename(current_path: str, target_path: str) -> None:
                raise OSError(f"simulated rename failure: {current_path} -> {target_path}")

            finalizer.os.rename = failing_rename
            try:
                with self.assertRaisesRegex(OSError, "simulated rename failure"):
                    finalizer.move_season(str(source), str(destination), {"fail_if_destination_exists": True})
            finally:
                finalizer.os.rename = original_rename

            self.assertTrue(source.exists())
            self.assertTrue((source / "Episode 01.mkv").exists())
            self.assertFalse(destination.exists())
            self.assertFalse(Path(str(destination) + ".__moving__").exists())

    def test_move_episode_files_moves_only_selected_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            final_file = source / "Episode 01.mkv"
            blocked_file = source / "Episode 02.mkv"
            final_file.write_text("english", encoding="utf-8")
            blocked_file.write_text("japanese", encoding="utf-8")
            item = finalizer.MoveItem(
                episode_id=1,
                episode_number=1,
                source_path=str(final_file),
                destination_path=str(destination / "Episode 01.mkv"),
                temporary_destination_path=str(destination / "Episode 01.mkv") + ".__moving__",
            )

            finalizer.move_episode_files([item], {"fail_if_destination_exists": True})

            self.assertFalse(final_file.exists())
            self.assertTrue((destination / "Episode 01.mkv").exists())
            self.assertTrue(blocked_file.exists())

    def test_move_episode_files_rolls_back_when_final_rename_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            first_file = source / "Episode 01.mkv"
            second_file = source / "Episode 02.mkv"
            first_file.write_text("english", encoding="utf-8")
            second_file.write_text("english too", encoding="utf-8")
            items = [
                finalizer.MoveItem(
                    episode_id=1,
                    episode_number=1,
                    source_path=str(first_file),
                    destination_path=str(destination / "Episode 01.mkv"),
                    temporary_destination_path=str(destination / "Episode 01.mkv") + ".__moving__",
                ),
                finalizer.MoveItem(
                    episode_id=2,
                    episode_number=2,
                    source_path=str(second_file),
                    destination_path=str(destination / "Episode 02.mkv"),
                    temporary_destination_path=str(destination / "Episode 02.mkv") + ".__moving__",
                ),
            ]
            original_rename = finalizer.os.rename
            rename_calls = 0

            def fail_second_rename(current_path: str, target_path: str) -> None:
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 2:
                    raise OSError(f"simulated rename failure: {current_path} -> {target_path}")
                original_rename(current_path, target_path)

            finalizer.os.rename = fail_second_rename
            try:
                with self.assertRaisesRegex(OSError, "simulated rename failure"):
                    finalizer.move_episode_files(items, {"fail_if_destination_exists": True})
            finally:
                finalizer.os.rename = original_rename

            self.assertTrue(first_file.exists())
            self.assertTrue(second_file.exists())
            self.assertFalse((destination / "Episode 01.mkv").exists())
            self.assertFalse((destination / "Episode 02.mkv").exists())
            self.assertFalse(Path(str(destination / "Episode 02.mkv") + ".__moving__").exists())

    def test_partial_move_includes_matching_subtitle_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            video = source / "A Gatherer's Adventure in Isekai S01E01.mp4"
            subtitle = source / "A Gatherer's Adventure in Isekai S01E01.en.srt"
            unrelated_subtitle = source / "A Gatherer's Adventure in Isekai S01E02.en.srt"
            video.write_text("video", encoding="utf-8")
            subtitle.write_text("subtitle", encoding="utf-8")
            unrelated_subtitle.write_text("other subtitle", encoding="utf-8")
            episode = finalizer.EpisodeState(
                episode_id=1,
                episode_number=1,
                monitored=True,
                has_file=True,
                episode_file_id=10,
                path=str(video),
                is_final=True,
            )

            items = finalizer.build_move_items([episode], str(source), str(destination), {"move_to_temporary_folder_first": True})
            finalizer.move_episode_files(items, {"fail_if_destination_exists": True})

            self.assertEqual([Path(item.source_path).name for item in items], [video.name, subtitle.name])
            self.assertFalse(video.exists())
            self.assertFalse(subtitle.exists())
            self.assertTrue((destination / video.name).exists())
            self.assertTrue((destination / subtitle.name).exists())
            self.assertTrue(unrelated_subtitle.exists())

    def test_build_series_folder_move_plan_when_all_source_seasons_are_final(self) -> None:
        series = {"title": "Test", "path": "/tv-en/Test"}
        episodes = [
            {"id": 101, "episodeNumber": 1, "seasonNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 1001},
            {"id": 201, "episodeNumber": 1, "seasonNumber": 2, "monitored": True, "hasFile": True, "episodeFileId": 2001},
        ]
        episode_files = [
            {"id": 1001, "path": "/tv-en/Test/Season 01/Test S01E01.mkv", "languages": [{"name": "Czech"}]},
            {"id": 2001, "path": "/tv-en/Test/Season 02/Test S02E01.mkv", "languages": [{"name": "Czech"}]},
        ]
        current_state = finalizer.build_season_state(
            series,
            episodes,
            episode_files,
            finalizer.EventContext("Download", 42, None, None, 2, None, None),
        )
        rules = {"allowed_final_audio_languages": ["cz"], "evaluate_monitored_only": True, "allow_sonarr_language_fallback": True}
        safety = {"min_file_size_mb": 0, "move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"}
        result = finalizer.evaluate_season_final(current_state, rules, safety, {})

        plan = finalizer.build_series_folder_move_plan_if_complete(
            series,
            episodes,
            episode_files,
            current_state,
            result,
            {"name": "tv", "source_prefix": "/tv-en", "target_prefix": "/tv-cz"},
            rules,
            safety,
            {},
            False,
            True,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.move_scope, "series")
        self.assertEqual(plan.source_folder, "/tv-en/Test")
        self.assertEqual(plan.destination_folder, "/tv-cz/Test")
        self.assertEqual(plan.temporary_destination_folder, "/tv-cz/Test.__moving__")
        self.assertEqual(plan.unmonitor_season_numbers, [1, 2])
        self.assertEqual(plan.unmonitor_episode_ids, [101, 201])

    def test_build_series_folder_move_plan_blocks_in_progress_source_season(self) -> None:
        series = {"title": "Test", "path": "/tv-en/Test"}
        episodes = [
            {"id": 101, "episodeNumber": 1, "seasonNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 1001},
            {"id": 201, "episodeNumber": 1, "seasonNumber": 2, "monitored": True, "hasFile": True, "episodeFileId": 2001},
            {"id": 301, "episodeNumber": 1, "seasonNumber": 3, "monitored": True, "hasFile": True, "episodeFileId": 3001},
        ]
        episode_files = [
            {"id": 1001, "path": "/tv-en/Test/Season 01/Test S01E01.mkv", "languages": [{"name": "Czech"}]},
            {"id": 2001, "path": "/tv-en/Test/Season 02/Test S02E01.mkv", "languages": [{"name": "Czech"}]},
            {"id": 3001, "path": "/tv-en/Test/Season 03/Test S03E01.mkv", "languages": [{"name": "English"}]},
        ]
        current_state = finalizer.build_season_state(
            series,
            episodes,
            episode_files,
            finalizer.EventContext("Download", 42, None, None, 2, None, None),
        )
        rules = {"allowed_final_audio_languages": ["cz"], "evaluate_monitored_only": True, "allow_sonarr_language_fallback": True}
        safety = {"min_file_size_mb": 0, "move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"}
        result = finalizer.evaluate_season_final(current_state, rules, safety, {})

        plan = finalizer.build_series_folder_move_plan_if_complete(
            series,
            episodes,
            episode_files,
            current_state,
            result,
            {"name": "tv", "source_prefix": "/tv-en", "target_prefix": "/tv-cz"},
            rules,
            safety,
            {},
            False,
            True,
        )

        self.assertIsNone(plan)

    def test_build_movie_move_plan_moves_whole_movie_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "movies-en" / "Example Movie (2026)"
            source.mkdir(parents=True)
            video = source / "Example Movie.mkv"
            subtitle = source / "Example Movie.en.srt"
            artwork = source / "folder.jpg"
            video.write_text("video", encoding="utf-8")
            subtitle.write_text("subtitle", encoding="utf-8")
            artwork.write_text("artwork", encoding="utf-8")
            movie_state = finalizer.MovieState(
                movie_id=77,
                title="Example Movie",
                movie_path=str(source),
                movie_file_id=700,
                file_path=str(video),
                is_final=True,
            )

            plan = finalizer.build_movie_move_plan(
                movie_state,
                {
                    "name": "movies",
                    "source_prefix": str(root / "movies-en"),
                    "target_prefix": str(root / "movies-cz"),
                },
                finalizer.MovieEvaluationResult(True, "cz", "movie is final", None),
                {"move_to_temporary_folder_first": True, "temporary_suffix": ".__moving__"},
                dry_run=True,
            )

            self.assertEqual(plan.movie_id, 77)
            self.assertEqual(plan.source_folder, str(source))
            self.assertEqual(plan.destination_folder, str(root / "movies-cz" / "Example Movie (2026)"))
            self.assertEqual(plan.temporary_destination_folder, str(root / "movies-cz" / "Example Movie (2026)") + ".__moving__")
            self.assertEqual(plan.file_path, str(video))

    def test_complete_radarr_movie_move_unmonitors_after_successful_move(self) -> None:
        class FakeRadarrClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            def unmonitor_movie(self, movie_id: int) -> None:
                self.calls.append(("unmonitor", movie_id))

            def rescan_movie(self, movie_id: int) -> None:
                self.calls.append(("rescan", movie_id))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "movies-en" / "Example Movie (2026)"
            destination = root / "movies-cz" / "Example Movie (2026)"
            source.mkdir(parents=True)
            video = source / "Example Movie.mkv"
            artwork = source / "folder.jpg"
            video.write_text("video", encoding="utf-8")
            artwork.write_text("artwork", encoding="utf-8")
            plan = finalizer.MovieMovePlan(
                movie_id=77,
                movie_title="Example Movie",
                mapping_name="movies",
                target_language="cz",
                source_folder=str(source),
                destination_folder=str(destination),
                temporary_destination_folder=str(destination) + ".__moving__",
                dry_run=False,
                move_method="shutil.move",
                will_move=True,
                will_unmonitor=True,
                will_rescan=True,
                file_path=str(video),
            )
            client = FakeRadarrClient()

            finalizer.complete_radarr_movie_move(client, plan, {"fail_if_destination_exists": True})

            self.assertFalse(source.exists())
            self.assertTrue((destination / "Example Movie.mkv").exists())
            self.assertTrue((destination / "folder.jpg").exists())
            self.assertEqual(client.calls, [("unmonitor", 77), ("rescan", 77)])

    def test_unmonitor_after_whole_season_move_unmonitors_season_and_episode_ids(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int | list[int]]] = []

            def unmonitor_season(self, series_id: int, season_number: int) -> None:
                self.calls.append(("season", season_number))

            def unmonitor_episodes(self, episode_ids: list[int]) -> None:
                self.calls.append(("episodes", episode_ids))

        plan = finalizer.MovePlan(
            series_id=42,
            series_title="Example",
            season_number=2,
            mapping_name="test",
            target_language="cz",
            source_folder="/tv-en/Example/Season 02",
            destination_folder="/tv-cz/Example/Season 02",
            temporary_destination_folder=None,
            dry_run=False,
            move_method="shutil.move",
            partial_move=False,
            will_move=True,
            will_unmonitor=True,
            will_rescan=True,
            unmonitor_season_number=2,
            unmonitor_episode_ids=[1, 2],
            move_items=[],
            episode_count=2,
            relevant_episode_count=2,
            episode_file_count=2,
        )
        client = FakeClient()

        finalizer.unmonitor_after_move(client, plan)

        self.assertEqual(client.calls, [("season", 2), ("episodes", [1, 2])])

    def test_unmonitor_after_partial_move_unmonitors_only_episode_ids(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int | list[int]]] = []

            def unmonitor_season(self, series_id: int, season_number: int) -> None:
                self.calls.append(("season", season_number))

            def unmonitor_episodes(self, episode_ids: list[int]) -> None:
                self.calls.append(("episodes", episode_ids))

        plan = finalizer.MovePlan(
            series_id=42,
            series_title="Example",
            season_number=1,
            mapping_name="test",
            target_language="en",
            source_folder="/anime-jp/Example",
            destination_folder="/anime-en/Example",
            temporary_destination_folder=None,
            dry_run=False,
            move_method="shutil.move",
            partial_move=True,
            will_move=True,
            will_unmonitor=True,
            will_rescan=True,
            unmonitor_season_number=None,
            unmonitor_episode_ids=[1],
            move_items=[],
            episode_count=2,
            relevant_episode_count=2,
            episode_file_count=1,
        )
        client = FakeClient()

        finalizer.unmonitor_after_move(client, plan)

        self.assertEqual(client.calls, [("episodes", [1])])


if __name__ == "__main__":
    unittest.main()
