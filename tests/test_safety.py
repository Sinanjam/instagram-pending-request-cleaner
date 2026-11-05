"""Offline safety regression tests for the Instagram UI guards."""
import json
import contextlib
from datetime import datetime, timezone
import io
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from instagram_request_cleaner import cli as app
from instagram_request_cleaner import preflight


class FakeElement:
    def __init__(self, text="", role="dialog", controls=(), href=None, label=None):
        self.text = text
        self.role = role
        self.controls = list(controls)
        self.href = href
        self.label = label

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def find_elements(self, by, xpath):
        return self.controls

    def get_attribute(self, key):
        return {"role": self.role, "href": self.href, "aria-label": self.label}.get(key)


class FakeDriver:
    def __init__(self, dialogs=(), current_url="https://www.instagram.com/example_owner/",
                 header=None, observation=None):
        self.dialogs, self.current_url = list(dialogs), current_url
        self.header = header
        self.observation = observation

    def find_elements(self, by, xpath):
        if xpath == "//main//header":
            return [self.header] if self.header else []
        return self.dialogs

    def execute_script(self, script, *args):
        if script == app.ATOMIC_CONFIRMATION_OBSERVATION_JS:
            return self.observation
        return False


class FakeLiveDriver:
    def __init__(self):
        self.current_url = "about:blank"

    def set_page_load_timeout(self, _): pass
    def set_window_size(self, *_): pass
    def get(self, url): self.current_url = url
    def quit(self): pass
    def find_elements(self, by, selector):
        if selector == "body":
            return [FakeElement(text="", role="body")]
        return []


def fake_selenium_modules(driver_factory=lambda **_: FakeLiveDriver()):
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Firefox = driver_factory
    firefox = types.ModuleType("selenium.webdriver.firefox")
    options_module = types.ModuleType("selenium.webdriver.firefox.options")
    service_module = types.ModuleType("selenium.webdriver.firefox.service")
    class Options:
        def add_argument(self, _): pass
    class Service:
        def __init__(self, **_): pass
    options_module.Options = Options
    service_module.Service = Service
    common = types.ModuleType("selenium.webdriver.common")
    by_module = types.ModuleType("selenium.webdriver.common.by")
    class By: XPATH = "xpath"; TAG_NAME = "tag"
    by_module.By = By
    return {
        "selenium": selenium, "selenium.webdriver": webdriver,
        "selenium.webdriver.firefox": firefox,
        "selenium.webdriver.firefox.options": options_module,
        "selenium.webdriver.firefox.service": service_module,
        "selenium.webdriver.common": common,
        "selenium.webdriver.common.by": by_module,
    }


class SafetyTests(unittest.TestCase):
    def test_archive_resolves_known_size_and_digest(self):
        payload = json.dumps({"relationships_follow_requests_sent": [
            {"string_list_data": [{"value": "example_user_1"}]},
            {"string_list_data": [{"value": "example_user_2"}]},
        ]}).encode()
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "synthetic-export.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("connections/followers_and_following/pending_follow_requests.json", payload)
            names, digest = app.read_export(archive)
        self.assertEqual(names, ["example_user_1", "example_user_2"])
        self.assertEqual(digest, app.hashlib.sha256(payload).hexdigest())

    def test_identity_requires_own_profile_url_heading_and_edit_control(self):
        header = FakeElement(text="example_owner\nExample Owner\nEdit profile")
        self.assertEqual(app.logged_in_username(FakeDriver(header=header)), "example_owner")
        no_owner_control = FakeElement(text="example_owner\nExample Owner\nMessage")
        self.assertIsNone(app.logged_in_username(FakeDriver(header=no_owner_control)))
        other_url = FakeDriver(header=header, current_url="https://www.instagram.com/someone_else/")
        self.assertIsNone(app.logged_in_username(other_url))

    @staticmethod
    def modal_observation(status, button=None, dialogs=None, safe_count=0, button_count=0):
        dialogs = dialogs if dialogs is not None else []
        return {"urlMatchesTarget": True, "status": status, "button": button,
                "dialogs": dialogs, "visibleDialogCount": len(dialogs),
                "safeDialogCount": safe_count, "safeButtonCount": button_count,
                "trulyEmpty": status == "empty"}

    def test_conflicting_trace_populated_dialog_is_not_misclassified_as_empty(self):
        """Regression for @example_target_1: DOM had 90 chars + Unfollow/Cancel.

        The legacy Selenium facade deliberately still reports the same dialog
        as blank, reproducing the conflicting last-modal diagnostic.
        """
        button = FakeElement(text="Unfollow", role="button")
        selenium_blank = FakeElement(text="", controls=[])
        observation = self.modal_observation("valid", button=button, safe_count=1,
            button_count=1, dialogs=[{"role": "dialog", "ariaModal": True,
                "visible": True, "width": 600, "height": 520, "textPresent": True,
                "textLength": 90, "targetMessage": True, "safe": True,
                "controls": [{"label": "unfollow", "visible": True, "enabled": True},
                             {"label": "cancel", "visible": True, "enabled": True}]}])
        driver = FakeDriver(dialogs=[selenium_blank], observation=observation)
        self.assertFalse(app.visible_empty_dialog(driver, "example_owner"))
        with patch.object(app, "profile_state", return_value="requested"):
            self.assertIs(app.find_confirmation(driver, "example_owner"), button)

    def test_truly_empty_dialog_is_detected_and_never_selected(self):
        observation = self.modal_observation("empty", dialogs=[{"role": "dialog",
            "ariaModal": True, "visible": True, "width": 600, "height": 520,
            "textPresent": False, "textLength": 0, "targetMessage": False,
            "safe": False, "controls": []}])
        driver = FakeDriver(observation=observation)
        self.assertTrue(app.visible_empty_dialog(driver, "example_owner"))
        with patch.object(app, "profile_state", return_value="requested"):
            self.assertIsNone(app.find_confirmation(driver, "example_owner"))

    def test_correct_populated_dialog_is_selected_only_with_exact_pair(self):
        button = FakeElement(text="Unfollow", role="button")
        observation = self.modal_observation("valid", button=button, safe_count=1,
            button_count=1, dialogs=[{"role": "dialog", "visible": True,
                "textPresent": True, "textLength": 90, "targetMessage": True,
                "safe": True, "controls": [{"label": "unfollow", "visible": True},
                                               {"label": "cancel", "visible": True}]}])
        driver = FakeDriver(observation=observation)
        with patch.object(app, "profile_state", return_value="requested"):
            self.assertIs(app.find_confirmation(driver, "example_owner"), button)

    def test_multiple_possible_confirmation_controls_stop_without_click(self):
        observation = self.modal_observation("ambiguous", safe_count=2, button_count=2,
            dialogs=[{"role": "dialog", "visible": True, "textPresent": True,
                      "targetMessage": True, "safe": True},
                     {"role": "dialog", "visible": True, "textPresent": True,
                      "targetMessage": True, "safe": True}])
        driver = FakeDriver(observation=observation)
        self.assertFalse(app.visible_empty_dialog(driver, "example_owner"))
        with patch.object(app, "profile_state", return_value="requested"):
            self.assertIsNone(app.find_confirmation(driver, "example_owner"))

    @staticmethod
    def confirmation_status(status, **extra):
        value = {"status": status, "urlMatchesTarget": True, "dialogs": [],
                 "visibleDialogCount": 1, "safeDialogCount": 0, "safeButtonCount": 0}
        value.update(extra)
        return value

    def test_rendering_for_six_seconds_then_valid(self):
        observations = [self.confirmation_status("rendering"),
                        self.confirmation_status("valid", safeDialogCount=1, safeButtonCount=1)]
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom", side_effect=observations), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 6.0]), \
             patch.object(app.time, "sleep") as sleep:
            app.wait_for_valid_confirmation(FakeDriver(), "example_owner")
        sleep.assert_called_once_with(0.25)

    def test_wait_rejects_truly_empty_dialog(self):
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom",
                          return_value=self.confirmation_status("empty")), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 0.1]):
            with self.assertRaises(app.EmptyConfirmationDialogError):
                app.wait_for_valid_confirmation(FakeDriver(), "example_owner")

    def test_wait_stops_after_fifteen_seconds_of_rendering(self):
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom",
                          return_value=self.confirmation_status("rendering")), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 15.0]):
            with self.assertRaises(app.RenderingTimeoutBeforeConfirmationClick) as caught:
                app.wait_for_valid_confirmation(FakeDriver(), "example_owner")
        self.assertFalse(caught.exception.confirmation_clicked)
        self.assertEqual(caught.exception.timeout_observation["status"], "rendering")

    def test_timeout_evidence_keeps_trigger_observation_if_dom_turns_valid(self):
        timeout_observation = self.confirmation_status("rendering", renderingDialogCount=1)
        driver = FakeDriver(observation=self.confirmation_status(
            "valid", safeDialogCount=1, safeButtonCount=1))
        with patch.object(app, "observe_confirmation_dom") as fresh_observation, \
             patch.object(app, "save_debug_screenshot", return_value=" screenshot") as screenshot, \
             patch.object(app, "safe_modal_diagnostics", return_value=" diagnostic") as diagnostic:
            result = app.save_atomic_modal_evidence(driver, "target_user", timeout_observation)
        fresh_observation.assert_not_called()
        screenshot.assert_called_once_with(driver)
        diagnostic.assert_called_once_with(
            driver, "target_user", observation=timeout_observation,
            screenshot_captured=True)
        self.assertEqual(result, " screenshot diagnostic")

    def test_wait_rejects_multiple_confirmation_controls(self):
        ambiguous = self.confirmation_status("ambiguous", safeDialogCount=2, safeButtonCount=2)
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom", return_value=ambiguous), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 0.1]):
            with self.assertRaises(app.AmbiguousConfirmationDialogError):
                app.wait_for_valid_confirmation(FakeDriver(), "example_owner")

    def test_wait_rejects_visible_but_covered_control(self):
        covered = self.confirmation_status("ambiguous")
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom", return_value=covered), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 0.1]):
            with self.assertRaises(app.AmbiguousConfirmationDialogError):
                app.wait_for_valid_confirmation(FakeDriver(), "example_owner")

    def test_wait_rejects_wrong_target_user(self):
        wrong = self.confirmation_status("valid", urlMatchesTarget=False)
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom", return_value=wrong), \
             patch.object(app.time, "monotonic", return_value=0.0):
            with self.assertRaises(app.AmbiguousConfirmationDialogError):
                app.wait_for_valid_confirmation(FakeDriver(), "example_owner")

    def test_wait_reloads_dom_after_recreation(self):
        new_button = FakeElement(text="Unfollow", role="button")
        observations = [self.confirmation_status("rendering"),
                        self.confirmation_status("valid", safeDialogCount=1, safeButtonCount=1),
                        self.confirmation_status("valid", safeDialogCount=1, safeButtonCount=1,
                                                 button=new_button)]
        driver = FakeDriver()
        with patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="requested"), \
             patch.object(app, "observe_confirmation_dom", side_effect=observations), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 1.0]), \
             patch.object(app.time, "sleep"):
            app.wait_for_valid_confirmation(driver, "example_owner")
            self.assertIs(app.find_confirmation(driver, "example_owner"), new_button)

    def test_atomic_observer_classifies_offline_browser_dom(self):
        """Exercise the actual read-only JavaScript, not only mocked results."""
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        options = Options()
        options.add_argument("-headless")
        driver = webdriver.Firefox(
            options=options, service=Service(executable_path=app.shutil.which("geckodriver")))
        try:
            cases = {
                # Instagram'ın canlı DOM'u gibi düğmeler iç kapsayıcıdadır;
                # bu kapsayıcı aynı düğme için ikinci rolsüz modal sayılmamalı.
                "valid": """<div role='dialog' aria-modal='true' style='width:600px;height:520px'>
                    <div>If you change your mind, you will have to request to follow @target_user again.
                    <button>Unfollow</button><button>Cancel</button></div></div>""",
                "empty": "<div role='dialog' aria-modal='true' style='width:600px;height:520px'></div>",
                "ambiguous": """<div role='dialog' aria-modal='true' style='width:600px;height:520px'>
                    If you change your mind, you will have to request to follow @target_user again.
                    <button>Unfollow</button><button>Unfollow</button><button>Cancel</button></div>""",
            }
            for expected, body in cases.items():
                with self.subTest(expected=expected):
                    driver.get("data:text/html;charset=utf-8," + quote(body))
                    observed = app.observe_confirmation_dom(driver, "target_user")
                    self.assertEqual(observed["status"], expected)
                    self.assertEqual(observed["visibleDialogCount"], 1)
            two_dialogs = """<div role='dialog' style='width:600px;height:200px'>
                request to follow @target_user again <button>Unfollow</button><button>Cancel</button></div>
                <div role='dialog' style='width:600px;height:200px'>
                request to follow @target_user again <button>Unfollow</button><button>Cancel</button></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(two_dialogs))
            observed = app.observe_confirmation_dom(driver, "target_user")
            self.assertEqual(observed["visibleDialogCount"], 2)
            self.assertEqual(observed["safeButtonCount"], 2)
            self.assertEqual(observed["status"], "ambiguous")

            hidden_controls = """<div role='dialog' style='width:600px;height:300px'>
                request to follow @target_user again <div style='opacity:0'>
                <button>Unfollow</button><button>Cancel</button></div></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(hidden_controls))
            hidden_observation = app.observe_confirmation_dom(driver, "target_user")
            control = hidden_observation["dialogs"][0]["controls"][0]
            self.assertFalse(control["visible"])
            self.assertIn("ancestor-opacity-zero", control["visibilityDetails"]["reasons"])
            self.assertEqual(hidden_observation["status"], "rendering")
            self.assertEqual(hidden_observation["safeButtonCount"], 0)

            # Regression for @example_target_2: the structural pair appeared
            # under opacity:0 before the full withdrawal explanation. This is
            # safe to wait for, but never valid/clickable until messageMatch.
            partial_render = """<div role='dialog' style='width:600px;height:300px'>
                Unfollow @target_user? <div style='opacity:0'>
                <button>Unfollow</button><button>Cancel</button></div></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(partial_render))
            partial_observation = app.observe_confirmation_dom(driver, "target_user")
            self.assertFalse(partial_observation["dialogs"][0]["targetMessage"])
            self.assertEqual(partial_observation["status"], "rendering")
            self.assertEqual(partial_observation["safeButtonCount"], 0)

            wrong_identity_render = """<div role='dialog' style='width:600px;height:300px'>
                Unfollow @different_user? <div style='opacity:0'>
                <button>Unfollow</button><button>Cancel</button></div></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(wrong_identity_render))
            wrong_observation = app.observe_confirmation_dom(driver, "target_user")
            self.assertEqual(wrong_observation["status"], "ambiguous")
            self.assertEqual(wrong_observation["renderingDialogCount"], 0)

            offscreen_control = """<div role='dialog' style='width:600px;height:300px'>
                request to follow @target_user again
                <button style='position:fixed;left:-2000px'>Unfollow</button><button>Cancel</button></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(offscreen_control))
            offscreen_observation = app.observe_confirmation_dom(driver, "target_user")
            details = offscreen_observation["dialogs"][0]["controls"][0]["visibilityDetails"]
            self.assertTrue(details["visible"])
            self.assertFalse(details["inViewport"])
            self.assertEqual(offscreen_observation["status"], "ambiguous")

            covered_control = """<div role='dialog' style='width:600px;height:300px'>
                request to follow @target_user again <button>Unfollow</button><button>Cancel</button></div>
                <div style='position:fixed;inset:0;z-index:99;background:black'></div>"""
            driver.get("data:text/html;charset=utf-8," + quote(covered_control))
            covered_observation = app.observe_confirmation_dom(driver, "target_user")
            details = covered_observation["dialogs"][0]["controls"][0]["visibilityDetails"]
            self.assertTrue(details["covered"])
            self.assertFalse(details["center"]["topMatches"])
            self.assertEqual(covered_observation["status"], "ambiguous")
        finally:
            driver.quit()

    def test_stale_selenium_element_is_reloaded_before_standard_click(self):
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        options = Options()
        options.add_argument("-headless")
        driver = webdriver.Firefox(
            options=options, service=Service(executable_path=app.shutil.which("geckodriver")))
        try:
            driver.get("data:text/html,<button id='old'>Unfollow</button>")
            stale = driver.find_element(By.ID, "old")
            driver.execute_script("arguments[0].outerHTML='<button id=\"new\" onclick=\"window.clicked=true\">Unfollow</button>'", stale)
            fresh = driver.find_element(By.ID, "new")
            targets = iter([stale, fresh])
            app.click_when_unobscured(driver, lambda _driver: next(targets), "stale regression", timeout=2)
            self.assertTrue(driver.execute_script("return window.clicked === true"))
        finally:
            driver.quit()

    def test_transient_empty_dialog_gets_time_to_render(self):
        clock = iter([0.0, 0.0, 0.1])
        with patch.object(app.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(app.time, "sleep") as sleep, \
             patch.object(app, "visible_empty_dialog", side_effect=[True, False]):
            self.assertFalse(app.empty_dialog_persists(FakeDriver()))
        sleep.assert_called_once_with(0.1)

    def test_persistently_empty_dialog_remains_a_hard_stop(self):
        clock = iter([0.0, 0.0, 0.3, 0.7, 1.3])
        with patch.object(app.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(app.time, "sleep"), \
             patch.object(app, "visible_empty_dialog", side_effect=[True, True, True, True]):
            self.assertTrue(app.empty_dialog_persists(FakeDriver(), grace_seconds=1.2))

    def test_modal_timeline_sanitizer_drops_page_text_and_urls(self):
        raw = {
            "startedAtEpochMs": 1792900000000,
            "changes": [{"elapsedMs": 12, "reason": "network-resource", "state": {
                "dialogs": [{"scope": "document", "tag": "div", "role": "dialog",
                    "ariaModal": True, "textPresent": True, "textLength": 91,
                    "targetWithdrawalMessage": False, "controlCount": 0,
                    "controlLabels": [], "visibility": {"display": "flex", "visible": True,
                    "text": "PRIVATE PAGE TEXT", "width": 600, "height": 500}}],
                "visibleActions": [{"label": "PRIVATE USERNAME", "url": "https://example.invalid/private"}],
                "resourceCounts": {"fetch:200": 2, "https://example.invalid/private": 99},
                "guardSignals": ["restriction", "PRIVATE PAGE TEXT"],
            }}],
        }
        safe = app.sanitize_modal_timeline(raw, "target_user")
        serialized = json.dumps(safe)
        self.assertNotIn("PRIVATE PAGE TEXT", serialized)
        self.assertNotIn("PRIVATE USERNAME", serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertEqual(safe["events"][0]["state"]["dialogs"][0]["text_length"], 91)
        self.assertEqual(safe["events"][0]["state"]["visible_actions"][0]["label"], "other")
        self.assertEqual(safe["events"][0]["reason"], "network-resource")
        self.assertEqual(safe["events"][0]["state"]["guard_signals"], ["restriction"])

    def test_state_integrity_rejects_corrupt_or_overlapping_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress, deferred = root / "progress.json", root / "deferred.json"
            with patch.object(app, "PROGRESS_PATH", progress), patch.object(app, "DEFERRED_PATH", deferred):
                progress.write_text(json.dumps({"source_sha256": "wrong", "processed": {}}))
                deferred.write_text(json.dumps({"source_sha256": "digest", "accounts": {}}))
                with self.assertRaises(RuntimeError):
                    app.validate_state_integrity("digest", {"alice"})
                progress.write_text(json.dumps({"source_sha256": "digest", "processed": {"alice": "cancelled"}}))
                deferred.write_text(json.dumps({"source_sha256": "digest", "accounts": {"alice": {"reason": "x"}}}))
                with self.assertRaises(RuntimeError):
                    app.validate_state_integrity("digest", {"alice"})

    def test_empty_modal_stops_main_queue_and_returns_safety_code(self):
        driver = FakeLiveDriver()
        selenium = types.ModuleType("selenium")
        webdriver = types.ModuleType("selenium.webdriver")
        webdriver.Firefox = lambda **_: driver
        firefox = types.ModuleType("selenium.webdriver.firefox")
        options_module = types.ModuleType("selenium.webdriver.firefox.options")
        service_module = types.ModuleType("selenium.webdriver.firefox.service")
        class Options:
            def add_argument(self, _): pass
        class Service:
            def __init__(self, **_): pass
        options_module.Options = Options
        service_module.Service = Service
        common = types.ModuleType("selenium.webdriver.common")
        by_module = types.ModuleType("selenium.webdriver.common.by")
        class By: XPATH = "xpath"; TAG_NAME = "tag"
        by_module.By = By
        selenium_modules = {
            "selenium": selenium, "selenium.webdriver": webdriver,
            "selenium.webdriver.firefox": firefox,
            "selenium.webdriver.firefox.options": options_module,
            "selenium.webdriver.firefox.service": service_module,
            "selenium.webdriver.common": common,
            "selenium.webdriver.common.by": by_module,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir()
            progress, deferred = state / "progress.json", state / "deferred.json"
            progress.write_text(json.dumps({"source_sha256": "digest", "processed": {}}))
            deferred.write_text(json.dumps({"source_sha256": "digest", "accounts": {}}))
            outcomes = iter(["cancelled", "blank"])
            def fake_cancel(*_):
                outcome = next(outcomes)
                if outcome == "blank":
                    raise app.EmptyConfirmationDialogError("blank modal")
                return outcome
            with patch.dict(sys.modules, selenium_modules), \
                 patch.object(app, "read_export", return_value=(["first", "second", "third"], "digest")), \
                 patch.object(app, "verify_account_identity", return_value=True), \
                 patch.object(app, "cancel_one", side_effect=fake_cancel) as cancel_one, \
                 patch.object(app, "STATE_DIR", state), \
                 patch.object(app, "PROFILE_DIR", state / "profile"), \
                 patch.object(app, "PROGRESS_PATH", progress), \
                 patch.object(app, "DEFERRED_PATH", deferred), \
                 patch.object(app, "AUDIT_PATH", state / "actions.jsonl"), \
                 patch.object(app, "LOCK_PATH", state / "overnight.lock"), \
                 patch.object(app, "close_driver_and_verify"), \
                 patch.object(app.shutil, "which", side_effect=lambda x: f"/fake/{x}"), \
                 patch.object(sys, "argv", ["instagram-request-cleaner", "--archive", "archive.zip", "--account", "example_owner", "--state-dir", str(state), "--apply", "--limit", "3", "--non-interactive", "--defer-unverified"]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                result = app.main()
            self.assertEqual(result, app.EXIT_SAFETY_STOP, output.getvalue())
            self.assertEqual(cancel_one.call_count, 2)
            self.assertEqual(json.loads(progress.read_text())["processed"], {"first": "cancelled"})
            self.assertIn("second", json.loads(deferred.read_text())["accounts"])
            self.assertNotIn("third", json.loads(deferred.read_text())["accounts"])

    def test_single_render_timeout_defers_restarts_and_skips_processed(self):
        first_driver, second_driver = FakeLiveDriver(), FakeLiveDriver()
        modules = fake_selenium_modules()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            progress, deferred = state / "progress.json", state / "deferred.json"
            progress.write_text(json.dumps(
                {"source_sha256": "digest", "processed": {"done": "cancelled"}}))
            deferred.write_text(json.dumps({"source_sha256": "digest", "accounts": {}}))
            timeout = app.RenderingTimeoutBeforeConfirmationClick("render timeout; no click")
            with patch.dict(sys.modules, modules), \
                 patch.object(app, "read_export", return_value=(["done", "stuck", "next"], "digest")), \
                 patch.object(app, "start_verified_driver", side_effect=[first_driver, second_driver]) as start, \
                 patch.object(app, "close_driver_and_verify") as close, \
                 patch.object(app, "cancel_one", side_effect=[timeout, "cancelled"]) as cancel, \
                 patch.object(app, "recovery_evidence_is_persisted", return_value=True), \
                 patch.object(app, "STATE_DIR", state), \
                 patch.object(app, "PROFILE_DIR", state / "profile"), \
                 patch.object(app, "PROGRESS_PATH", progress), \
                 patch.object(app, "DEFERRED_PATH", deferred), \
                 patch.object(app, "AUDIT_PATH", state / "actions.jsonl"), \
                 patch.object(app, "LOCK_PATH", state / "overnight.lock"), \
                 patch.object(app.shutil, "which", side_effect=lambda x: f"/fake/{x}"), \
                 patch.object(app.time, "sleep"), \
                 patch.object(sys, "argv", ["instagram-request-cleaner", "--archive", "archive.zip", "--account", "example_owner", "--state-dir", str(state), "--apply", \
                     "--limit", "2", "--non-interactive", "--defer-unverified"]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                result = app.main()
            self.assertEqual(result, app.EXIT_OK, output.getvalue())
            self.assertEqual([call.args[1] for call in cancel.call_args_list], ["stuck", "next"])
            self.assertEqual(start.call_count, 2)
            self.assertEqual(close.call_count, 2)  # old recovery + final fresh session
            stored_progress = json.loads(progress.read_text())["processed"]
            stored_deferred = json.loads(deferred.read_text())["accounts"]
            self.assertEqual(stored_progress, {"done": "cancelled", "next": "cancelled"})
            self.assertEqual(stored_deferred["stuck"]["reason"],
                             "rendering_timeout_no_confirmation_click")

    def test_clicked_but_unverified_is_always_a_safety_stop(self):
        driver = FakeLiveDriver()
        modules = fake_selenium_modules()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            progress, deferred = state / "progress.json", state / "deferred.json"
            progress.write_text(json.dumps({"source_sha256": "digest", "processed": {}}))
            deferred.write_text(json.dumps({"source_sha256": "digest", "accounts": {}}))
            with patch.dict(sys.modules, modules), \
                 patch.object(app, "read_export", return_value=(["clicked", "next"], "digest")), \
                 patch.object(app, "start_verified_driver", return_value=driver) as start, \
                 patch.object(app, "close_driver_and_verify"), \
                 patch.object(app, "cancel_one",
                              side_effect=app.AmbiguousCancellationError("clicked; unknown")) as cancel, \
                 patch.object(app, "STATE_DIR", state), \
                 patch.object(app, "PROFILE_DIR", state / "profile"), \
                 patch.object(app, "PROGRESS_PATH", progress), \
                 patch.object(app, "DEFERRED_PATH", deferred), \
                 patch.object(app, "AUDIT_PATH", state / "actions.jsonl"), \
                 patch.object(app, "LOCK_PATH", state / "overnight.lock"), \
                 patch.object(app.shutil, "which", side_effect=lambda x: f"/fake/{x}"), \
                 patch.object(sys, "argv", ["instagram-request-cleaner", "--archive", "archive.zip", "--account", "example_owner", "--state-dir", str(state), "--apply", \
                     "--limit", "2", "--non-interactive", "--defer-unverified"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = app.main()
            self.assertEqual(result, app.EXIT_SAFETY_STOP)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(cancel.call_count, 1)
            self.assertEqual(json.loads(deferred.read_text())["accounts"]["clicked"]["reason"],
                             "click_done_but_not_verified")

    def test_ambiguity_thresholds_are_fail_closed(self):
        self.assertEqual(app.ambiguity_stop_reason([True, True]),
                         "art arda iki belirsizlik")
        separated = [False] * 12 + [True, False, True, False, True]
        self.assertEqual(app.ambiguity_stop_reason(separated),
                         "son 20 denemede üç belirsizlik")
        old_outside_window = [True, False, True] + [False] * 18 + [True]
        self.assertIsNone(app.ambiguity_stop_reason(old_outside_window))

    def test_wrong_owner_in_new_session_is_closed_and_rejected(self):
        driver = FakeLiveDriver()
        factory = types.SimpleNamespace(Firefox=lambda **_: driver)
        with patch.object(app, "guard_session"), \
             patch.object(app, "verify_account_identity", return_value=False), \
             patch.object(app, "close_driver_and_verify") as close:
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                app.start_verified_driver(
                    factory, object(), lambda **_: object(), "/fake/gecko", "example_owner")
        close.assert_called_once_with(driver)

    def test_selenium_firefox_service_has_unique_app_id_without_global_mutation(self):
        captured = {}

        class Service:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch.dict(app.os.environ, {"MOZ_APP_REMOTINGNAME": "personal-firefox"}):
            service = app.selenium_firefox_service(Service, "/fake/geckodriver")
            self.assertIsInstance(service, Service)
            self.assertEqual(captured["executable_path"], "/fake/geckodriver")
            self.assertEqual(
                captured["env"]["MOZ_APP_REMOTINGNAME"],
                "ig-request-cleaner-firefox",
            )
            self.assertEqual(app.os.environ["MOZ_APP_REMOTINGNAME"], "personal-firefox")

    def test_unclosed_owned_firefox_process_forces_stop(self):
        driver = FakeLiveDriver()
        with patch.object(app, "capture_owned_driver_processes", return_value={(123, 456)}), \
             patch.object(app, "owned_process_is_alive", return_value=True), \
             patch.object(app.time, "monotonic", side_effect=[0.0, 0.0]):
            with self.assertRaisesRegex(RuntimeError, "süreçleri sona ermedi"):
                app.close_driver_and_verify(driver, timeout=0.0)

    def test_recovery_evidence_requires_exact_render_timeout_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            timeline = root / "timeline.json"
            ui = root / "ui.json"
            screenshot = root / "shot.png"
            diagnostic = root / "diagnostic.json"
            timeline.write_text(json.dumps({"target": "target_user",
                "events": [{"elapsed_ms": 15001}]}))
            ui.write_text(json.dumps({"profile_state": "requested"}))
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            diagnostic.write_text(json.dumps({
                "requested_target": "target_user", "observation_status": "rendering",
                "profile_state": "requested", "url_matches_target": True,
                "rendering_dialog_count": 1,
            }))
            with patch.object(app, "MODAL_TRACE_PATH", timeline), \
                 patch.object(app, "UI_STATE_PATH", ui), \
                 patch.object(app, "SCREENSHOT_PATH", screenshot), \
                 patch.object(app, "MODAL_DIAGNOSTIC_PATH", diagnostic):
                self.assertTrue(app.recovery_evidence_is_persisted("target_user"))
                payload = json.loads(diagnostic.read_text())
                payload["observation_status"] = "valid"
                diagnostic.write_text(json.dumps(payload))
                self.assertFalse(app.recovery_evidence_is_persisted("target_user"))

    def test_deferred_classifier_distinguishes_safe_visible_states(self):
        expected = {
            "requested": "active_requested",
            "follow": "active_not_pending",
            "following": "active_following",
        }
        for state, classification in expected.items():
            with self.subTest(state=state), \
                 patch.object(app, "open_profile_fresh"), \
                 patch.object(app, "guard_session"), \
                 patch.object(app, "profile_state", return_value=state):
                record = app.classify_deferred_profile(
                    FakeLiveDriver(), "target_user", "click_done_but_not_verified")
            self.assertEqual(record["classification"], classification)
            self.assertEqual(record["confidence"], "high")

    def test_page_unavailable_alone_is_never_deactivated(self):
        driver = FakeLiveDriver()
        driver.current_url = "https://www.instagram.com/target_user/"
        with patch.object(app, "open_profile_fresh"), \
             patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="unknown"), \
             patch.object(app, "visible_profile_page_text",
                          return_value="sorry, this page isnt available"), \
             patch.object(app, "is_unavailable_profile_page", return_value=True):
            record = app.classify_deferred_profile(
                driver, "target_user", "profile_page_unavailable")
        self.assertEqual(record["classification"], "unavailable_unknown")
        self.assertEqual(record["evidence_type"], "explicit_page_unavailable_message")

    def test_deactivated_requires_explicit_instagram_message(self):
        driver = FakeLiveDriver()
        with patch.object(app, "open_profile_fresh"), \
             patch.object(app, "guard_session"), \
             patch.object(app, "profile_state", return_value="unknown"), \
             patch.object(app, "visible_profile_page_text",
                          return_value="this account has been deactivated"), \
             patch.object(app, "is_unavailable_profile_page", return_value=False):
            record = app.classify_deferred_profile(
                driver, "target_user", "profile_page_unavailable")
        self.assertEqual(record["classification"], "deactivated_confirmed")
        self.assertEqual(record["confidence"], "high")

    def test_deferred_apply_candidates_require_prior_active_requested_review(self):
        users = ["first", "clicked_unknown", "resolved", "not_pending", "manual"]
        deferred = {name: {"reason": "click_done_but_not_verified"} for name in users}
        reviews = {
            "first": {"classification": "active_requested"},
            "resolved": {"classification": "active_requested", "resolution": "cancelled"},
            "not_pending": {"classification": "active_not_pending"},
            "manual": {
                "classification": "active_requested",
                "manual_review_required": True,
            },
        }
        self.assertEqual(
            app.deferred_apply_candidates(users, deferred, reviews), ["first"])

    def test_deferred_allowlist_can_only_narrow_safe_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "allowlist.json"
            path.write_text(json.dumps({
                "source_sha256": "digest",
                "accounts": ["safe_two", "safe_one"],
            }))
            self.assertEqual(
                app.load_deferred_allowlist(
                    path, "digest", ["safe_one", "safe_two", "other"]),
                ["safe_two", "safe_one"],
            )
            path.write_text(json.dumps({
                "source_sha256": "digest",
                "accounts": ["safe_one", "manual_or_unknown"],
            }))
            with self.assertRaises(RuntimeError):
                app.load_deferred_allowlist(path, "digest", ["safe_one"])

    def test_deferred_reports_are_sanitized_and_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reviews = {
                "target_user": {
                    "old_reason": "profile_page_unavailable",
                    "classification": "unavailable_unknown",
                    "evidence_type": "explicit_page_unavailable_message",
                    "checked_at_utc": "2026-11-04T00:00:00+00:00",
                    "confidence": "high",
                }
            }
            deferred = {"target_user": {"reason": "profile_page_unavailable"}}
            with patch.object(app, "DEFERRED_REVIEW_PATH", root / "review.json"), \
                 patch.object(app, "DEFERRED_REVIEW_CSV_PATH", root / "report.csv"), \
                 patch.object(app, "DEFERRED_REVIEW_MD_PATH", root / "report.md"):
                summary = app.write_deferred_reports("digest", deferred, reviews)
            self.assertEqual(summary["manual_review"], 1)
            self.assertIn("target_user", (root / "report.csv").read_text())
            self.assertIn("unavailable_unknown", (root / "report.md").read_text())
            payload = json.loads((root / "review.json").read_text())
            self.assertEqual(payload["source_sha256"], "digest")
            combined = "".join(path.read_text() for path in root.iterdir())
            self.assertNotIn("cookie", combined.casefold())
            self.assertNotIn("token", combined.casefold())

    def test_preflight_gate_allows_only_zero_exit(self):
        self.assertTrue(preflight.allows_service_start(0))
        for code in (1, 2, 3, 4, 127):
            with self.subTest(code=code):
                self.assertFalse(preflight.allows_service_start(code))
        now = datetime(2026, 10, 31, 0, 30, tzinfo=timezone.utc)
        self.assertTrue(preflight.deadline_is_future("2026-10-31T08:00:00+03:00", now))
        self.assertTrue(preflight.deadline_is_future("next-istanbul-0800", now))
        self.assertFalse(preflight.deadline_is_future("2026-10-26T08:00:00+03:00", now))
        self.assertFalse(preflight.deadline_is_future("2026-10-31T08:00:00", now))

    def test_transient_and_safety_run_codes_are_distinct(self):
        self.assertEqual(app.run_exit_code(False, True, 1), app.EXIT_TRANSIENT)
        self.assertEqual(app.run_exit_code(True, False, 1), app.EXIT_SAFETY_STOP)

    def test_unexpected_operational_error_records_type_and_traceback(self):
        class FakeWebDriverException(Exception):
            pass

        with patch.object(app, "audit_event") as audit, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                raise FakeWebDriverException("marionette decode failed")
            except FakeWebDriverException as exc:
                app.log_unexpected_operational_error("target", exc)
        audit.assert_called_once_with(
            "target", "failed",
            "unexpected_operational_error; exception=FakeWebDriverException")
        self.assertIn("Traceback (most recent call last)", stderr.getvalue())
        self.assertIn("marionette decode failed", stderr.getvalue())

    def test_runtime_reports_are_outside_package_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            app.configure_runtime_paths(state)
            self.assertEqual(app.DEFERRED_REVIEW_CSV_PATH.parent, state.resolve())
            self.assertEqual(app.PROFILE_DIR.parent, state.resolve())

    def test_unrelated_text_cannot_authorize_unfollow_confirmation(self):
        self.assertFalse(app.is_request_withdrawal_dialog_text(
            "Unfollow @example_owner", "example_owner"))
        self.assertTrue(app.is_request_withdrawal_dialog_text(
            "If you change your mind, you will have to request to follow @example_owner again.",
            "example_owner"))


if __name__ == "__main__":
    unittest.main()
