"""Unit tests for failure-time debug artifacts.

The hierarchy dump is a post-mortem tool: `uiautomator dump` suppresses the app's
accessibility service, so it must never run while a test is still executing. These
tests pin that it fires on failure and only on failure.
"""

import os

import pytest

from android_accessibility_tester import capture_failure
from test_structured import FakeClient


@pytest.fixture
def tester(monkeypatch, tmp_path):
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.dumps = 0

    def fake_screenshot(output_path):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        return output_path

    def fake_shell(command):
        if "uiautomator dump" in command:
            t.dumps += 1
        return ""

    monkeypatch.setattr(t, "screenshot", fake_screenshot)
    monkeypatch.setattr(t, "shell", fake_shell)
    # save_debug_artifacts pulls the dump off the device; write it locally instead.
    monkeypatch.setattr(t, "_pull", lambda remote, local: open(local, "w").write("<hierarchy/>"))
    return t


class TestLastCheck:
    def test_check_records_its_result_on_the_tester(self, tester):
        tester.client = FakeClient({"ok": True})

        result = tester.check("anything")

        assert tester.last_check is result

    def test_a_failed_check_keeps_the_screenshot_it_judged(self, tester):
        tester.client = FakeClient({"ok": False, "error": "nope"})

        tester.check("anything")

        assert tester.last_check.screenshot is not None
        assert os.path.exists(tester.last_check.screenshot)

    def test_a_passing_check_leaves_no_screenshot_behind(self, tester):
        tester.client = FakeClient({"ok": True})

        tester.check("anything")

        assert tester.last_check.screenshot is None

    def test_last_check_starts_empty(self, tester):
        assert tester.last_check is None


class TestCaptureFailure:
    def test_writes_a_screenshot_and_a_hierarchy_dump(self, tester, tmp_path):
        artifacts = capture_failure(tester, "test_send_message", output_dir=str(tmp_path))

        assert os.path.exists(artifacts["screenshot"])
        assert os.path.exists(artifacts["ui_dump"])

    def test_dumps_the_hierarchy_exactly_once(self, tester, tmp_path):
        capture_failure(tester, "test_send_message", output_dir=str(tmp_path))

        assert tester.dumps == 1

    def test_reuses_the_screenshot_the_failed_check_judged(self, tester, tmp_path):
        tester.client = FakeClient({"ok": False, "error": "nope"})
        tester.check("anything")
        judged = tester.last_check.screenshot

        artifacts = capture_failure(tester, "test_send_message", output_dir=str(tmp_path))

        # The artifact must be what the model actually saw, not a fresh capture
        # taken seconds later once the screen has moved on.
        assert open(artifacts["screenshot"], "rb").read() == open(judged, "rb").read()

    def test_captures_fresh_when_no_check_failed(self, tester, tmp_path):
        artifacts = capture_failure(tester, "test_send_message", output_dir=str(tmp_path))

        assert os.path.exists(artifacts["screenshot"])

    def test_names_artifacts_after_the_test(self, tester, tmp_path):
        artifacts = capture_failure(tester, "test_send_message", output_dir=str(tmp_path))

        assert "test_send_message" in os.path.basename(artifacts["screenshot"])


class TestFindTester:
    def test_finds_the_tester_among_other_fixtures(self, tester):
        from android_accessibility_tester import find_tester

        assert find_tester({"tmp_path": "/tmp", "tester": tester, "n": 3}) is tester

    def test_returns_none_when_no_test_uses_a_tester(self):
        from android_accessibility_tester import find_tester

        assert find_tester({"tmp_path": "/tmp", "n": 3}) is None

    def test_finds_it_under_any_fixture_name(self, tester):
        from android_accessibility_tester import find_tester

        assert find_tester({"my_device": tester}) is tester


class TestHookEndToEnd:
    """Runs a real pytest session in a temp dir to prove the conftest hook fires."""

    def test_fires_on_failure_and_stays_quiet_on_success(self, pytester, tmp_path, monkeypatch):
        repo = os.path.dirname(os.path.abspath(__file__))
        artifacts = tmp_path / "artifacts"
        monkeypatch.setenv("ANDROID_TEST_ARTIFACTS", str(artifacts))

        pytester.makeconftest(f"""
import sys
sys.path.insert(0, {repo!r})
from android_accessibility_tester import failure_artifact_hook

def pytest_runtest_makereport(item, call):
    failure_artifact_hook(item, call)
""")
        pytester.makepyfile(f"""
import sys
sys.path.insert(0, {repo!r})
import pytest
from android_accessibility_tester import AndroidAccessibilityTester

@pytest.fixture
def tester(monkeypatch):
    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "m")
    t = AndroidAccessibilityTester(api_key="x")

    def shot(path):
        with open(path, "wb") as f:
            f.write(b"png")
        return path

    monkeypatch.setattr(t, "screenshot", shot)
    monkeypatch.setattr(t, "shell", lambda c: "")
    monkeypatch.setattr(t, "_pull", lambda r, l: open(l, "w").write("<hierarchy/>"))
    return t

def test_that_passes(tester):
    assert True

def test_that_fails(tester):
    assert False, "boom"
""")
        result = pytester.runpytest("-p", "no:cacheprovider")
        result.assert_outcomes(passed=1, failed=1)

        written = sorted(p.name for p in artifacts.iterdir())
        assert any("test_that_fails" in n for n in written)
        assert not any("test_that_passes" in n for n in written)
