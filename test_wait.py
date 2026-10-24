"""Unit tests for wait_for() and its conditions.

No device: the tester's screenshot and hierarchy calls are stubbed, and pixel
conditions read real PNGs generated per poll from a scripted colour sequence.
"""

import pytest
from PIL import Image

from android_accessibility_tester import (
    AnyOf,
    Logcat,
    PixelChanged,
    PixelColor,
    Result,
)

FAST = {"timeout": 0.3, "poll_interval": 0.01}


@pytest.fixture
def tester(monkeypatch):
    """A tester whose screen is a 10x10 image of a scripted colour, one per poll."""
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.colors = [(255, 0, 0)]      # tests overwrite; last colour repeats forever
    t.shots = 0

    def fake_screenshot(output_path):
        color = t.colors[min(t.shots, len(t.colors) - 1)]
        t.shots += 1
        Image.new("RGB", (10, 10), color).save(output_path)
        return output_path

    def fake_shell(command):
        raise AssertionError(f"waiting must not shell out: {command!r}")

    monkeypatch.setattr(t, "screenshot", fake_screenshot)
    monkeypatch.setattr(t, "shell", fake_shell)
    return t


class TestWaitForPixelColor:
    def test_matches_immediately_without_polling_again(self, tester):
        tester.colors = [(0, 255, 0)]

        result = tester.wait_for(PixelColor(5, 5, "#00ff00"), **FAST)

        assert result
        assert tester.shots == 1

    def test_waits_until_the_colour_arrives(self, tester):
        tester.colors = [(255, 0, 0), (255, 0, 0), (0, 255, 0)]

        result = tester.wait_for(PixelColor(5, 5, "#00ff00"), **FAST)

        assert result
        assert result.final_color == "#00ff00"
        assert result.initial_color == "#ff0000"

    def test_accepts_any_of_several_colours(self, tester):
        tester.colors = [(255, 0, 0), (210, 206, 164)]

        assert tester.wait_for(PixelColor(5, 5, ["#fffad0", "#d2cea4"]), **FAST)

    def test_exclude_mode_waits_for_anything_else(self, tester):
        tester.colors = [(255, 255, 255), (255, 255, 255), (27, 27, 27), (5, 5, 5)]

        result = tester.wait_for(PixelColor(5, 5, ["#ffffff", "#1b1b1b"], exclude=True), **FAST)

        assert result
        assert result.final_color == "#050505"

    def test_times_out_with_an_explanatory_error(self, tester):
        tester.colors = [(255, 0, 0)]

        result = tester.wait_for(PixelColor(5, 5, "#00ff00"), **FAST)

        assert not result
        assert "#ff0000" in result.error
        assert "#00ff00" in result.error


class TestWaitForPixelChange:
    def test_detects_a_change_from_the_starting_colour(self, tester):
        tester.colors = [(255, 0, 0), (0, 0, 255)]

        result = tester.wait_for(PixelChanged(5, 5), **FAST)

        assert result
        assert result.initial_color == "#ff0000"
        assert result.final_color == "#0000ff"

    def test_times_out_when_the_pixel_never_changes(self, tester):
        tester.colors = [(255, 0, 0)]

        result = tester.wait_for(PixelChanged(5, 5), **FAST)

        assert not result
        assert "#ff0000" in result.error


class TestAnyOf:
    def test_returns_as_soon_as_one_condition_is_satisfied(self, tester):
        tester.colors = [(255, 0, 0), (0, 255, 0)]
        never = Logcat("NoSuchTag", "never logged", clear_first=False)
        never.start = lambda tester: None          # no logcat subprocess in unit tests
        never.stop = lambda tester: None

        result = tester.wait_for(AnyOf(never, PixelColor(5, 5, "#00ff00")), **FAST)

        assert result

    def test_times_out_reporting_every_branch(self, tester):
        tester.colors = [(255, 0, 0)]
        never = Logcat("NoSuchTag", "never logged", clear_first=False)
        never.start = lambda tester: None
        never.stop = lambda tester: None

        result = tester.wait_for(AnyOf(never, PixelColor(5, 5, "#00ff00")), **FAST)

        assert not result
        assert "never logged" in result.error
        assert "#00ff00" in result.error

    def test_shares_one_screenshot_across_pixel_branches_per_poll(self, tester):
        tester.colors = [(255, 0, 0)]

        tester.wait_for(AnyOf(PixelColor(5, 5, "#00ff00"), PixelChanged(1, 1)), timeout=0.0,
                        poll_interval=0.01)

        assert tester.shots == 1  # one capture serves both branches


class TestWaitForContract:
    def test_returns_a_result_so_it_can_be_asserted_directly(self, tester):
        tester.colors = [(0, 255, 0)]

        result = tester.wait_for(PixelColor(5, 5, "#00ff00"), **FAST)

        assert isinstance(result, Result)


# --- Logcat against a fake adb -------------------------------------------------
#
# These exercise the real Logcat subprocess code. A fake `adb` script on PATH
# backs logcat with a plain file: `logcat -c` truncates it, and the streaming
# `logcat` form tails it. Appending to the file stands in for the app logging.

FAKE_ADB = r"""#!/bin/bash
case "$1 $2" in
  "version ") echo "Android Debug Bridge version 0.0 (fake)";;
  "logcat -c") : > "$FAKE_LOGCAT_BUFFER";;
  "logcat -v") exec tail -n +1 -f "$FAKE_LOGCAT_BUFFER";;
  *) echo "fake adb: unexpected args: $*" >&2; exit 1;;
esac
"""


@pytest.fixture
def logcat_tester(tmp_path, monkeypatch):
    """A real tester whose `adb` is a script backed by a log buffer file."""
    import os
    import stat
    from android_accessibility_tester import AndroidAccessibilityTester

    buffer = tmp_path / "logcat.txt"
    buffer.write_text("")
    adb = tmp_path / "adb"
    adb.write_text(FAKE_ADB)
    adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOGCAT_BUFFER", str(buffer))
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")

    t = AndroidAccessibilityTester(api_key="not-a-real-key")
    t.log_buffer = buffer

    def log(line):
        with open(buffer, "a") as f:
            f.write(line + "\n")

    t.log = log
    return t


class TestLogcatCondition:
    def test_sees_a_line_logged_while_waiting(self, logcat_tester):
        import threading
        t = logcat_tester
        threading.Timer(0.1, t.log, args=["D/MyService( 123): overlay created"]).start()

        result = t.wait_for(Logcat("MyService", "overlay created"), timeout=2, poll_interval=0.02)

        assert result
        assert "overlay created" in result.line

    def test_times_out_when_the_line_never_arrives(self, logcat_tester):
        result = logcat_tester.wait_for(Logcat("MyService", "never"), timeout=0.2, poll_interval=0.02)

        assert not result
        assert "never" in result.error

    def test_wait_for_misses_a_line_logged_before_it_starts(self, logcat_tester):
        """Documents the trap: act-then-wait clears the line the act produced."""
        t = logcat_tester
        t.log("D/MyService( 123): overlay created")   # the app logs during act()

        result = t.wait_for(Logcat("MyService", "overlay created"), timeout=0.2, poll_interval=0.02)

        assert not result


class TestExpect:
    def test_sees_a_line_logged_inside_the_block(self, logcat_tester):
        t = logcat_tester

        with t.expect(Logcat("MyService", "overlay created"), timeout=2, poll_interval=0.02) as done:
            t.log("D/MyService( 123): overlay created")

        assert done
        assert "overlay created" in done.line

    def test_ignores_stale_lines_from_before_the_block(self, logcat_tester):
        t = logcat_tester
        t.log("D/MyService( 123): overlay created")   # left over from an earlier test

        with t.expect(Logcat("MyService", "overlay created"), timeout=0.2, poll_interval=0.02) as done:
            pass

        assert not done
        assert "overlay created" in done.error

    def test_exposes_the_result_fields(self, logcat_tester):
        t = logcat_tester

        with t.expect(Logcat("MyService", "ready"), timeout=0.2, poll_interval=0.02) as done:
            pass

        assert done.ok is False
        assert isinstance(done.result, Result)
        assert "ready" in repr(done)

    def test_does_not_wait_when_the_body_raises(self, logcat_tester):
        import time
        t = logcat_tester
        started = time.time()

        with pytest.raises(KeyError):
            with t.expect(Logcat("MyService", "never"), timeout=5, poll_interval=0.02):
                raise KeyError("boom")

        assert time.time() - started < 1

    def test_works_with_pixel_conditions_too(self, tester):
        tester.colors = [(255, 0, 0), (0, 255, 0)]

        with tester.expect(PixelColor(5, 5, "#00ff00"), **FAST) as done:
            pass

        assert done


class TestLogcatFilter:
    def test_listens_at_verbose_so_no_level_is_dropped(self):
        args = Logcat("MyService", "ready").logcat_args()

        assert "MyService:V" in args
        assert "*:S" in args
