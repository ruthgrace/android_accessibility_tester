"""Unit tests for act() and deterministic element replay."""

import pytest

from android_accessibility_tester import Element
from test_structured import FakeClient

@pytest.fixture
def tester(monkeypatch):
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    t.taps = []

    def fake_shell(command):
        raise AssertionError(f"nothing on the happy path may shell out: {command!r}")

    def fake_screenshot(path):
        from PIL import Image
        Image.new("RGB", (40, 60), "gray").save(path)   # observe() frames it with rulers
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
        tester.client = FakeClient({"found": False, "bounds": "", "explanation": "no Start button on screen"})

        result = tester.act("tap the Start button")

        assert not result
        assert "no Start button on screen" in result.error
        assert tester.taps == []

    def test_exposes_the_element_it_acted_on(self, tester):
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,200][300,280]", "explanation": "x"}
        )

        result = tester.act("tap the Start button")

        assert result.element.center() == (200, 240)


class TestActReplay:
    """A cached Element replays its own bounds. There is no hierarchy to re-resolve
    against without disturbing the app, so a replay is a plain tap at the coordinates
    observed earlier — the droidrun-and-hardcode workflow, minus the manual step."""

    def test_replays_a_cached_element_without_calling_the_model(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="the Start button")
        tester.client = FakeClient()  # any API call would IndexError

        result = tester.act(cached)

        assert result
        assert tester.taps == [(200, 240)]

    def test_replays_the_same_coordinates_every_time(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="the Start button")
        tester.client = FakeClient()

        for _ in range(3):
            tester.act(cached)

        assert tester.taps == [(200, 240)] * 3

    def test_a_replay_takes_no_screenshot_either(self, tester):
        cached = Element(found=True, bounds="[100,200][300,280]", explanation="x")
        tester.client = FakeClient()
        shots = []
        tester.screenshot = lambda p: shots.append(p)

        tester.act(cached)

        assert shots == []


class TestElementSchema:
    """The schema is what the model is held to, so it must carry the bounds rule itself."""

    def schema(self):
        from android_accessibility_tester import _strict_schema
        return _strict_schema(Element.model_json_schema())

    def test_bounds_is_required_and_comes_before_the_explanation(self):
        schema = self.schema()

        assert "bounds" in schema["required"]
        keys = list(schema["properties"])
        assert keys.index("bounds") < keys.index("explanation")

    def test_has_no_hierarchy_selector_fields(self):
        assert not {"resource_id", "content_desc", "text"} & set(self.schema()["properties"])

    def test_not_found_carries_empty_bounds(self):
        el = Element(found=False, bounds="", explanation="nothing like it on screen")

        assert not el.found
        assert "nothing like it" in repr(el)

    def test_found_requires_well_formed_bounds(self):
        with pytest.raises(ValueError):
            Element(found=True, bounds="", explanation="x")
        with pytest.raises(ValueError):
            Element(found=True, bounds="100,200,300,400", explanation="x")
