"""Unit tests for wait_for() and its conditions.

No device: the tester's screenshot and hierarchy calls are stubbed, and pixel
conditions read real PNGs generated per poll from a scripted colour sequence.
"""

import pytest
from PIL import Image

from android_accessibility_tester import (
    AnyOf,
    ElementGone,
    ElementPresent,
    PixelChanged,
    PixelColor,
    Result,
)

FAST = {"timeout": 0.3, "poll_interval": 0.01}


def dumpsys_with(resource_id=None):
    """A minimal `dumpsys activity top` window containing at most one identified button."""
    button = (
        f"          android.widget.Button{{aa11bb2 VFE...CL. ......ID 0,0-100,100 #7f0a0999 {resource_id}}}"
        if resource_id else
        "          android.widget.Button{aa11bb2 VFE...CL. ......ID 0,0-100,100}"
    )
    return (
        "TASK com.example id=1 userId=0\n"
        "    View Hierarchy:\n"
        "      com.android.internal.policy.DecorView{db331f6 I.E...... R.....ID 0,0-1080,2400 aid=0}[Main]\n"
        f"{button}\n"
    )


@pytest.fixture
def tester(monkeypatch):
    """A tester whose screen is a 10x10 image of a scripted colour, one per poll."""
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.colors = [(255, 0, 0)]      # tests overwrite; last colour repeats forever
    t.trees = [dumpsys_with()]
    t.shots = 0
    t.dumps = 0

    def fake_screenshot(output_path):
        color = t.colors[min(t.shots, len(t.colors) - 1)]
        t.shots += 1
        Image.new("RGB", (10, 10), color).save(output_path)
        return output_path

    def fake_shell(command):
        assert "uiautomator" not in command, "element conditions must not dump the hierarchy"
        if "dumpsys activity top" not in command:
            return ""
        tree = t.trees[min(t.dumps, len(t.trees) - 1)]
        t.dumps += 1
        return tree

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


class TestWaitForElement:
    def test_present_waits_for_the_id_to_appear(self, tester):
        tester.trees = [dumpsys_with(), dumpsys_with("app:id/start_button")]

        assert tester.wait_for(ElementPresent(resource_id="start_button"), **FAST)

    def test_present_matches_the_fully_qualified_id_too(self, tester):
        tester.trees = [dumpsys_with("app:id/start_button")]

        assert tester.wait_for(ElementPresent(resource_id="app:id/start_button"), **FAST)

    def test_present_times_out_naming_what_it_looked_for(self, tester):
        tester.trees = [dumpsys_with()]

        result = tester.wait_for(ElementPresent(resource_id="start_button"), **FAST)

        assert not result
        assert "start_button" in result.error

    def test_gone_waits_for_the_element_to_disappear(self, tester):
        tester.trees = [dumpsys_with("app:id/spinner"), dumpsys_with()]

        assert tester.wait_for(ElementGone(resource_id="spinner"), **FAST)

    def test_gone_times_out_while_the_element_remains(self, tester):
        tester.trees = [dumpsys_with("app:id/spinner")]

        assert not tester.wait_for(ElementGone(resource_id="spinner"), **FAST)

    def test_requires_a_resource_id(self):
        with pytest.raises(ValueError):
            ElementPresent()

    def test_rejects_text_selectors_because_they_would_need_uiautomator(self):
        with pytest.raises(ValueError, match="accessibility services"):
            ElementPresent(text="Welcome")


class TestAnyOf:
    def test_returns_as_soon_as_one_condition_is_satisfied(self, tester):
        tester.colors = [(255, 0, 0), (0, 255, 0)]
        tester.trees = [dumpsys_with()]

        result = tester.wait_for(
            AnyOf(ElementPresent(resource_id="never_appears"), PixelColor(5, 5, "#00ff00")),
            **FAST,
        )

        assert result

    def test_times_out_reporting_every_branch(self, tester):
        tester.colors = [(255, 0, 0)]

        result = tester.wait_for(
            AnyOf(ElementPresent(resource_id="welcome_banner"), PixelColor(5, 5, "#00ff00")),
            **FAST,
        )

        assert not result
        assert "welcome_banner" in result.error
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
