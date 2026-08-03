"""Unit tests for act() and deterministic element replay."""

import pytest

from android_accessibility_tester import Element
from test_structured import FakeClient

HIERARCHY = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node class="android.widget.Button" text="Start" resource-id="com.example:id/start_button"
          content-desc="Start timer" bounds="[100,200][300,280]" clickable="true" enabled="true"/>
  </node>
</hierarchy>
"""

# Same screen after a layout shift — same button, different position.
HIERARCHY_MOVED = HIERARCHY.replace("[100,200][300,280]", "[100,500][300,580]")


@pytest.fixture
def tester(monkeypatch):
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.hierarchy = HIERARCHY
    t.taps = []
    monkeypatch.setattr(t, "_get_ui_hierarchy", lambda: t.hierarchy)
    monkeypatch.setattr(t, "tap", lambda x, y: t.taps.append((x, y)))
    return t


class TestActFromDescription:
    def test_observes_then_taps_the_element_centre(self, tester):
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,200][300,280]", "explanation": "the Start button"}
        )

        result = tester.act("tap the Start button")

        assert result
        assert tester.taps == [(200, 240)]

    def test_returns_a_falsy_result_when_the_element_is_not_found(self, tester):
        tester.client = FakeClient({"found": False, "explanation": "no Start button on screen"})

        result = tester.act("tap the Start button")

        assert not result
        assert "no Start button on screen" in result.error
        assert tester.taps == []

    def test_exposes_the_element_it_acted_on(self, tester):
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,200][300,280]", "explanation": "x",
             "resource_id": "start_button"}
        )

        result = tester.act("tap the Start button")

        assert result.element.resource_id == "start_button"


class TestActReplay:
    def test_replays_a_cached_element_without_calling_the_model(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="the Start button",
                         resource_id="start_button")
        tester.client = FakeClient()  # any API call would IndexError

        result = tester.act(cached)

        assert result
        assert tester.taps == [(200, 240)]

    def test_re_resolves_fresh_bounds_when_the_element_has_moved(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="the Start button",
                         resource_id="start_button")
        tester.hierarchy = HIERARCHY_MOVED
        tester.client = FakeClient()

        tester.act(cached)

        assert tester.taps == [(200, 540)]  # the new centre, not the cached one

    def test_matches_on_content_desc_when_there_is_no_resource_id(self, tester):
        cached = Element(found=True, bounds="[0,0][1,1]", explanation="x", content_desc="Start timer")
        tester.client = FakeClient()

        tester.act(cached)

        assert tester.taps == [(200, 240)]

    def test_falls_back_to_the_model_when_the_selector_is_gone(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="the Start button",
                         resource_id="a_button_that_no_longer_exists")
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,500][300,580]", "explanation": "relocated Start button"}
        )

        result = tester.act(cached)

        assert result
        assert tester.taps == [(200, 540)]
        assert len(tester.client.messages.calls) == 1


class TestResolve:
    def test_returns_none_when_no_selector_matches(self, tester):
        cached = Element(found=True, bounds="[0,0][1,1]", explanation="x", resource_id="nope")

        assert tester.resolve(cached) is None

    def test_returns_none_for_an_element_with_no_selectors_at_all(self, tester):
        cached = Element(found=True, bounds="[0,0][1,1]", explanation="x")

        assert tester.resolve(cached) is None
