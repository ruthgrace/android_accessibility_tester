"""Unit tests for the images observe() sends: fit_for_model() resizing and add_rulers().
No device, no API key."""

import pytest
from PIL import Image

from android_accessibility_tester import add_rulers, RULER_MARGIN


@pytest.fixture
def screen(tmp_path):
    """A 1080x2400 screenshot with a distinctive gradient so pixel preservation is testable."""
    im = Image.new("RGB", (1080, 2400))
    px = im.load()
    for y in range(0, 2400, 40):
        for x in range(0, 1080, 40):
            px[x, y] = (x % 256, y % 256, 7)
    path = tmp_path / "screen.png"
    im.save(path)
    return path


def dark(im, x, y):
    r, g, b = im.getpixel((x, y))[:3]
    return r + g + b < 200


class TestAddRulers:
    def test_adds_a_margin_on_every_side(self, screen, tmp_path):
        out = add_rulers(str(screen), str(tmp_path / "out.png"))

        assert Image.open(out).size == (1080 + 2 * RULER_MARGIN, 2400 + 2 * RULER_MARGIN)

    def test_leaves_the_screenshot_pixels_untouched(self, screen, tmp_path):
        out = Image.open(add_rulers(str(screen), str(tmp_path / "out.png"))).convert("RGB")
        orig = Image.open(screen).convert("RGB")

        for x, y in [(0, 0), (40, 80), (1040, 2360), (1079, 2399), (520, 1200)]:
            assert out.getpixel((x + RULER_MARGIN, y + RULER_MARGIN)) == orig.getpixel((x, y))

    def test_draws_a_tick_at_every_hundred_device_pixels_on_all_four_edges(self, screen, tmp_path):
        out = Image.open(add_rulers(str(screen), str(tmp_path / "out.png"))).convert("RGB")
        m = RULER_MARGIN

        for dx in range(0, 1080, 100):
            assert dark(out, m + dx, m - 2), f"no top tick at x={dx}"
            assert dark(out, m + dx, m + 2400 + 1), f"no bottom tick at x={dx}"
        for dy in range(0, 2400, 100):
            assert dark(out, m - 2, m + dy), f"no left tick at y={dy}"
            assert dark(out, m + 1080 + 1, m + dy), f"no right tick at y={dy}"

    def test_margin_between_ticks_is_blank(self, screen, tmp_path):
        out = Image.open(add_rulers(str(screen), str(tmp_path / "out.png"))).convert("RGB")
        m = RULER_MARGIN

        assert not dark(out, m + 50, m - 2)
        assert not dark(out, m - 2, m + 50)

    def test_labels_every_two_hundred_pixels(self, screen, tmp_path):
        """A label is a cluster of dark pixels in the outer margin beside a major tick."""
        out = Image.open(add_rulers(str(screen), str(tmp_path / "out.png"))).convert("RGB")
        m = RULER_MARGIN

        def inked(x0, y0, x1, y1):
            return sum(dark(out, x, y) for x in range(x0, x1) for y in range(y0, y1))

        # label for x=600 sits above its tick, in the outer part of the top margin
        assert inked(m + 600 - 40, 0, m + 600 + 40, m - 12) > 30
        # label for y=1000 sits left of its tick, in the outer part of the left margin
        assert inked(0, m + 1000 - 30, m - 12, m + 1000 + 30) > 30
        # nothing but the tick at an unlabelled minor position
        assert inked(m + 700 - 40, 0, m + 700 + 40, m - 12) == 0

    def test_defaults_output_next_to_the_input(self, screen):
        out = add_rulers(str(screen))

        assert out.endswith("_rulers.png")
        assert Image.open(out).size[0] > 1080


class TestObserveResizes:
    @pytest.fixture
    def tester(self, monkeypatch):
        from android_accessibility_tester import AndroidAccessibilityTester
        from test_structured import FakeClient

        monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
        monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
        t = AndroidAccessibilityTester(api_key="not-a-real-key")
        monkeypatch.setattr(t, "shell", lambda c: (_ for _ in ()).throw(AssertionError(c)))
        monkeypatch.setattr(t, "screenshot", lambda path: Image.new("RGB", (1080, 2400), "gray").save(path) or path)
        return t

    def sent_image(self, tester):
        import base64, io
        content = tester.client.messages.calls[0]["messages"][0]["content"]
        image = next(b for b in content if b["type"] == "image")
        prompt = " ".join(b["text"] for b in content if b["type"] == "text")
        return Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))), prompt

    def test_shrinks_a_phone_screenshot_under_the_api_cap_and_says_so(self, tester):
        from test_structured import FakeClient
        from android_accessibility_tester import MODEL_IMAGE_MAX_PIXELS
        tester.client = FakeClient({"found": True, "bounds": "[0,0][10,10]", "explanation": "x"})

        tester.observe("the thing")

        sent, prompt = self.sent_image(tester)
        w, h = sent.size
        assert w * h <= MODEL_IMAGE_MAX_PIXELS
        assert abs(w / h - 1080 / 2400) < 0.01          # aspect ratio preserved
        assert f"{w}x{h}" in prompt                     # the model is told what it is looking at

    def test_scales_bounds_back_to_device_pixels(self, tester):
        from test_structured import FakeClient
        # 1080x2400 shrinks by 0.666; the model answers in the shrunken image's pixels.
        tester.client = FakeClient({"found": True, "bounds": "[100,200][300,280]", "explanation": "x"})

        el = tester.observe("the thing")

        x1, y1, x2, y2 = el.box()
        assert abs(x1 - 150) <= 1 and abs(y1 - 300) <= 1
        assert abs(x2 - 450) <= 1 and abs(y2 - 420) <= 1

    def test_small_screenshots_are_sent_as_is(self, tester, monkeypatch):
        from test_structured import FakeClient
        monkeypatch.setattr(tester, "screenshot", lambda path: Image.new("RGB", (400, 800), "gray").save(path) or path)
        tester.client = FakeClient({"found": True, "bounds": "[100,200][300,280]", "explanation": "x"})

        el = tester.observe("the thing")

        assert self.sent_image(tester)[0].size == (400, 800)
        assert el.box() == (100, 200, 300, 280)
