from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import shutil
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "founderos_streamdeck_profiles",
    ROOT / "streamdeck_profiles.py",
)
assert SPEC and SPEC.loader
profiles = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profiles
SPEC.loader.exec_module(profiles)


class GeneratedProfilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not profiles.DEFAULT_PROFILES_ROOT.is_dir():
            raise unittest.SkipTest("Local Stream Deck configuration is missing")
        cls.temporary = tempfile.TemporaryDirectory(prefix="founderos-streamdeck-test-")
        cls.output = Path(cls.temporary.name)
        cls.paths = profiles.Paths(
            profiles_root=profiles.DEFAULT_PROFILES_ROOT,
            plugins_root=profiles.DEFAULT_PLUGINS_ROOT,
            app_plugins_root=profiles.DEFAULT_APP_PLUGINS_ROOT,
            contract=profiles.DEFAULT_CONTRACT,
            plan=profiles.DEFAULT_PLAN,
        )
        cls.builder = profiles.ProfileBuilder(cls.paths, cls.output)
        cls.builder.build()
        cls.live = cls.output / "live"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def page(self, key: str) -> dict:
        path = (
            self.live
            / f"{profiles.PROFILE_IDS[key]}.sdProfile"
            / "Profiles"
            / profiles.PAGE_IDS[key]
            / "manifest.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def outer(self, key: str) -> dict:
        path = self.live / f"{profiles.PROFILE_IDS[key]}.sdProfile" / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_full_validation(self) -> None:
        result = self.builder.validate(self.live)
        self.assertTrue(result["valid"])
        self.assertEqual(result["profileCount"], 5)
        self.assertEqual(result["visibleActionCount"], 53)
        self.assertEqual(result["iconReferenceCount"], 60)

    def test_every_visible_surface_has_a_distinct_dedicated_icon(self) -> None:
        visible = [
            action
            for key in profiles.PROFILE_KEYS
            for action in profiles._iter_visible_actions(self.page(key))
        ]
        references = [
            state.get("Image")
            for action in visible
            for state in action.get("States", [])
        ]
        expected = {
            f"Images/{filename}"
            for filename in profiles.REQUIRED_ICON_FILES
        }
        self.assertEqual(len(visible), 53)
        self.assertEqual(len(references), 60)
        self.assertEqual(len(set(references)), 60)
        self.assertEqual(set(references), expected)
        two_state_actions = [action for action in visible if len(action.get("States", [])) == 2]
        self.assertEqual(len(two_state_actions), 7)
        for action in visible:
            self.assertTrue(all(state.get("ShowTitle") for state in action["States"]))
            self.assertTrue(
                all(state.get("TitleAlignment") == "bottom" for state in action["States"])
            )
        for action in two_state_actions:
            images = [state["Image"] for state in action["States"]]
            self.assertEqual(len(set(images)), 2)
        for key in profiles.PROFILE_KEYS:
            image_root = (
                self.live
                / f"{profiles.PROFILE_IDS[key]}.sdProfile"
                / "Profiles"
                / profiles.PAGE_IDS[key]
            )
            for action in profiles._iter_visible_actions(self.page(key)):
                state_images = [state["Image"] for state in action.get("States", [])]
                state_digests = [
                    hashlib.sha256((image_root / image).read_bytes()).hexdigest()
                    for image in state_images
                ]
                self.assertEqual(len(state_digests), len(set(state_digests)))
                if action.get("UUID") not in profiles.VISIBLE_CONTAINER_UUIDS:
                    continue
                container_images = list(state_images)
                for child in action.get("Actions", []):
                    container_images.extend(
                        state["Image"] for state in child.get("States", [])
                    )
                container_digests = [
                    hashlib.sha256((image_root / image).read_bytes()).hexdigest()
                    for image in container_images
                ]
                self.assertEqual(len(container_digests), len(set(container_digests)))

    def test_source_icon_suite_is_complete_and_valid(self) -> None:
        catalog = profiles._validate_icon_suite(profiles.DEFAULT_ICON_ASSETS_ROOT)
        self.assertEqual(len(catalog), 60)
        self.assertEqual(set(catalog), {Path(name).stem for name in profiles.REQUIRED_ICON_FILES})
        self.assertFalse(hasattr(profiles, "_write_solid_png"))
        self.assertFalse(hasattr(profiles, "PALETTE"))

    def test_v3_default_page_and_smart_profiles(self) -> None:
        for key in profiles.PROFILE_KEYS:
            outer = self.outer(key)
            self.assertEqual(outer["Version"], "3.0")
            self.assertIn(outer["Pages"]["Current"], outer["Pages"]["Pages"])
            self.assertNotIn(outer["Pages"]["Default"], outer["Pages"]["Pages"])
            self.assertNotEqual(outer["Pages"]["Current"], outer["Pages"]["Default"])
            self.assertEqual(outer.get("AppIdentifier"), profiles.SMART_PROFILE_APPS.get(key))

    def test_empty_materialized_default_pages_are_accepted_only_for_live_validation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founderos-streamdeck-live-defaults-") as root:
            live = Path(root) / "live"
            shutil.copytree(self.live, live)
            for key in profiles.PROFILE_KEYS:
                profile = live / f"{profiles.PROFILE_IDS[key]}.sdProfile"
                outer = json.loads((profile / "manifest.json").read_text(encoding="utf-8"))
                default_page = profile / "Profiles" / outer["Pages"]["Default"].upper()
                (default_page / "Images").mkdir(parents=True)
                controllers = [
                    {"Actions": None, "Type": controller["Type"]}
                    for controller in self.page(key)["Controllers"]
                ]
                (default_page / "manifest.json").write_text(
                    json.dumps(
                        {"Controllers": controllers, "Icon": "", "Name": ""},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            with self.assertRaises(profiles.ProfileError):
                self.builder.validate(live)
            result = self.builder.validate(live, allow_materialized_defaults=True)
            self.assertTrue(result["valid"])
            self.assertEqual(result["visibleActionCount"], 53)

    def test_option_space_is_the_only_voice_modifier(self) -> None:
        actions = list(profiles._iter_actions(self.page("pedal")))
        voice = next(action for action in actions if "Voice" in profiles._action_titles(action))
        first = voice["Settings"]["Hotkeys"][0]
        self.assertTrue(first["KeyOption"])
        self.assertFalse(first["KeyCmd"])
        self.assertFalse(first["KeyCtrl"])
        self.assertFalse(first["KeyShift"])
        self.assertEqual(first["KeyModifiers"], 4)
        self.assertEqual(first["NativeCode"], 49)
        self.assertEqual(first["QTKeyCode"], 32)

    def test_meet_shortcuts_use_french_azerty_physical_codes(self) -> None:
        actions = list(profiles._iter_actions(self.page("call")))
        expected = {
            "Meet\nmic": {"NativeCode": 2, "QTKeyCode": 68, "VKeyCode": 2},
            "Meet\ncamera": {"NativeCode": 14, "QTKeyCode": 69, "VKeyCode": 14},
        }
        for title, keycodes in expected.items():
            matching = [
                action
                for action in actions
                if action.get("UUID") == "com.elgato.streamdeck.system.hotkey"
                and title in profiles._action_titles(action)
            ]
            self.assertEqual(len(matching), 1)
            first = matching[0]["Settings"]["Hotkeys"][0]
            self.assertTrue(first["KeyCmd"])
            self.assertFalse(first["KeyCtrl"])
            self.assertFalse(first["KeyOption"])
            self.assertFalse(first["KeyShift"])
            self.assertEqual(first["KeyModifiers"], 8)
            for field, value in keycodes.items():
                self.assertEqual(first[field], value)

    def test_meet_profile_is_manual_and_prepare_meet_activates_arc(self) -> None:
        self.assertNotIn("AppIdentifier", self.outer("call"))
        self.assertEqual(
            profiles.SMART_PROFILE_APPS,
            {"studio": "/Applications/OBS.app"},
        )
        cockpit_actions = list(profiles._iter_actions(self.page("cockpit")))
        arc_actions = [
            action
            for action in cockpit_actions
            if action.get("UUID") == "com.elgato.streamdeck.system.open"
            and action.get("Settings", {}).get("path")
            == json.dumps("/Applications/Arc.app")
        ]
        self.assertEqual(len(arc_actions), 1)
        titles = {title for action in cockpit_actions for title in profiles._action_titles(action)}
        self.assertIn("Prepare\nMeet", titles)
        self.assertNotIn("Prepare\ncall", titles)

    def test_rebuild_removes_stale_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founderos-streamdeck-rebuild-") as root:
            output = Path(root)
            builder = profiles.ProfileBuilder(self.paths, output)
            builder.build()
            stale_icon = output / "live" / ".call-icons" / "stale.png"
            stale_icon.write_bytes(b"stale")
            stale_export = output / "exports" / "FounderOS Legacy.streamDeckProfile"
            stale_export.write_bytes(b"stale")

            builder.build()

            self.assertFalse(stale_icon.exists())
            self.assertFalse(stale_export.exists())
            self.assertTrue(builder.validate(output / "live")["valid"])

    def test_things_uses_official_deep_link(self) -> None:
        paths = [
            action.get("Settings", {}).get("path")
            for key in profiles.PROFILE_KEYS
            for action in profiles._iter_actions(self.page(key))
        ]
        self.assertIn("things:///add?show-quick-entry=true", paths)
        self.assertNotIn("superwhisper://record", paths)

    def test_no_nested_multi_actions(self) -> None:
        for key in profiles.PROFILE_KEYS:
            for action in profiles._iter_actions(self.page(key)):
                if action.get("UUID") != "com.elgato.streamdeck.multiactions.routine":
                    continue
                nested = [
                    child
                    for child in profiles._iter_actions(action.get("Actions", []))
                    if child.get("UUID") == "com.elgato.streamdeck.multiactions.routine"
                ]
                self.assertEqual(nested, [])

    def test_meet_lights_acquire_presence_and_restore_work_lights(self) -> None:
        actions = list(profiles._iter_actions(self.page("call")))
        presets = {
            action.get("Settings", {}).get("preset")
            for action in actions
            if action.get("UUID") == "com.yanndouchin.founderos-actions.presence"
        }
        self.assertIn("manualCallStart", presets)
        self.assertIn("manualCallStop", presets)
        titles = {title for action in actions for title in profiles._action_titles(action)}
        self.assertIn("Task scene", titles)

    def test_switch_profile_actions_are_bound_to_the_plus(self) -> None:
        for key in ("cockpit", "call", "studio"):
            switches = [
                action
                for action in profiles._iter_actions(self.page(key))
                if action.get("UUID") == "com.elgato.streamdeck.profile.rotate"
            ]
            self.assertTrue(switches)
            self.assertTrue(all(action["Settings"].get("DeviceUUID") for action in switches))

    def test_default_profile_preferences_have_no_collision(self) -> None:
        profile_paths = {
            key: self.live / f"{profiles.PROFILE_IDS[key]}.sdProfile"
            for key in profiles.PROFILE_KEYS
        }
        device_ids = {
            profiles._profile_device_uuid(profile_paths[key])
            for key in ("cockpit", "pedal", "presentation")
        }
        value = {
            "Devices": {
                device_id: {
                    "ESDProfilesInfo": {
                        "ESDProfilesPreferred": "legacy",
                        "ESDProfilesSorting": "legacy",
                    }
                }
                for device_id in device_ids
            }
        }
        updated = profiles._updated_preferences(
            plistlib.dumps(value, fmt=plistlib.FMT_XML),
            profile_paths,
        )
        profiles._validate_active_preferences(updated, profile_paths)

    def test_installation_requires_explicit_apply(self) -> None:
        with self.assertRaises(profiles.ProfileError):
            profiles.install_profiles(self.paths, self.output, apply=False)


class SafetyHelpersTest(unittest.TestCase):
    def test_resume_stream_deck_only_continues_stopped_processes(self) -> None:
        process_results = [
            profiles.subprocess.CompletedProcess([], 0, stdout="42\n43\n"),
            profiles.subprocess.CompletedProcess([], 0, stdout="T\n"),
            profiles.subprocess.CompletedProcess([], 0, stdout="S\n"),
        ]
        with patch.object(profiles.subprocess, "run", side_effect=process_results) as run:
            with patch.object(profiles.os, "kill") as kill:
                profiles._resume_stream_deck_if_stopped()

        self.assertEqual(run.call_count, 3)
        self.assertEqual(kill.call_args_list, [call(42, profiles.signal.SIGCONT)])

    def test_indicator_sanitizer_preserves_key_logic_positions(self) -> None:
        indicator = profiles._action(
            "com.elgato.philips-hue.color",
            "Color",
            "Desk Recording Indicator",
            settings={"light": "synthetic-indicator"},
        )
        key_logic = profiles._action(
            "com.elgato.streamdeck.keys.logic",
            "Key Logic",
            "Test",
        )
        key_logic["Actions"] = [profiles._empty_action(), indicator, profiles._empty_action()]
        sanitized, removed = profiles._sanitize_indicator_tree(
            key_logic,
            ["Desk Recording Indicator"],
            {"synthetic-indicator"},
        )
        self.assertEqual(removed, 1)
        self.assertEqual(len(sanitized["Actions"]), 3)
        self.assertEqual(sanitized["Actions"][1], profiles._empty_action())

    def test_versioned_text_is_nfc_and_has_no_forbidden_character(self) -> None:
        for path in (ROOT / "streamdeck_profiles.py", ROOT / "profile-plan.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", text)
            self.assertNotIn("\u2014", text)
            self.assertEqual(text, unicodedata.normalize("NFC", text))


if __name__ == "__main__":
    unittest.main()
