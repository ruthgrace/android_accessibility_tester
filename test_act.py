"""Unit tests for act() and deterministic element replay."""

import pytest

from android_accessibility_tester import Element
from test_structured import FakeClient

def dumpsys(button_top=200):
    """A `dumpsys activity top` window with a Start button at the given y offset."""
    return (
        "TASK com.example id=1 userId=0\n"
        "    View Hierarchy:\n"
        "      com.android.internal.policy.DecorView{db331f6 I.E...... R.....ID 0,0-1080,2400 aid=0}[Main]\n"
        f"        android.widget.Button{{aa11bb2 VFE...CL. ......ID "
        f"100,{button_top}-300,{button_top + 80} #7f0a0999 app:id/start_button}}\n"
    )


TREE = dumpsys()
# Same screen after a layout shift — same button, different position.
TREE_MOVED = dumpsys(button_top=500)


@pytest.fixture
def tester(monkeypatch):
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.tree = TREE
    t.taps = []

    def fake_shell(command):
        assert "uiautomator" not in command, "replay must not dump the accessibility hierarchy"
        return t.tree if "dumpsys activity top" in command else ""

    def fake_screenshot(path):
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        return path

    monkeypatch.setattr(t, "shell", fake_shell)
    monkeypatch.setattr(t, "screenshot", fake_screenshot)
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
        tester.tree = TREE_MOVED
        tester.client = FakeClient()

        tester.act(cached)

        assert tester.taps == [(200, 540)]  # the new centre, not the cached one

    def test_an_element_without_a_resource_id_cannot_be_replayed(self, tester):
        # dumpsys carries no text or content descriptions, so there is nothing to match on.
        cached = Element(found=True, bounds="[0,0][1,1]", explanation="the Start button",
                         content_desc="Start timer")
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,200][300,280]", "explanation": "the Start button"}
        )

        tester.act(cached)

        assert len(tester.client.messages.calls) == 1  # fell back to the model

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

    def test_returns_none_for_an_element_with_no_resource_id(self, tester):
        cached = Element(found=True, bounds="[0,0][1,1]", explanation="x", text="Start")

        assert tester.resolve(cached) is None
