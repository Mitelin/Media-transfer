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
                        "local_prefix": r"\\192.168.60.20\admin\ANIME\English",
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
        self.assertEqual(translated, r"\\192.168.60.20\admin\ANIME\English\Example\Season 01\Episode 01.mkv")

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
                    "lan_url": "http://192.168.0.10:8989",
                    "tailscale_url": "http://100.64.0.1:8989",
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

    def test_validate_config_warns_for_empty_template_secret(self) -> None:
        config = {
            "active_instance": "anime",
            "sonarr_instances": {
                "anime": {
                    "url": "http://sonarr-anime:8989",
                    "lan_url": "http://192.168.0.10:8990",
                    "tailscale_url": "http://100.64.0.1:8990",
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
        self.assertIn("sonarr_instances.anime.api_key is empty; fill it in local config before runtime", warnings)

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
        self.assertTrue(plan.will_move)
        self.assertTrue(plan.will_unmonitor)
        self.assertTrue(plan.will_rescan)
        self.assertEqual(plan.episode_count, 2)
        self.assertEqual(plan.relevant_episode_count, 1)
        self.assertEqual(plan.episode_file_count, 1)

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
                will_move=True,
                will_unmonitor=True,
                will_rescan=True,
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
                will_move=True,
                will_unmonitor=True,
                will_rescan=True,
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


if __name__ == "__main__":
    unittest.main()
