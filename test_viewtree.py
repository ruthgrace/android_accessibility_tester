"""Unit tests for the `dumpsys activity top` view-tree parser.

Unlike `uiautomator dump`, `dumpsys activity top` reads the view tree through
ActivityManager and never touches UiAutomation, so it does not suppress the
accessibility services of the app under test.

The sample below is verbatim output from a real device (Pixel, launcher +
DeskClock windows), trimmed for length.
"""

import os

import pytest

from android_accessibility_tester import ViewNode, parse_view_tree

DUMPSYS = """TASK com.google.android.apps.nexuslauncher id=1 userId=0
  ACTIVITY com.google.android.apps.nexuslauncher/.NexusLauncherActivity 3f2a1 pid=3021
    Local FragmentActivity 9a0b state:
      mCreated=true
    View Hierarchy:
      com.android.internal.policy.DecorView{2403795 I.E...... R.....ID 0,0-1080,2400 aid=2}[NexusLauncherActivity]
        android.widget.LinearLayout{3805b4c V.E...... ......ID 0,0-1080,2400}
          android.view.ViewStub{6e75bf4 G.E...... ......I. 0,0-0,0 #10201e9 android:id/action_mode_bar_stub}
          android.widget.FrameLayout{3d66f7f V.E...... ......ID 0,0-1080,2400 #1020002 android:id/content}
            com.android.launcher3.LauncherRootView{d32389e V.E...... ......ID 0,0-1080,2400 #7f0a022d app:id/launcher}
              com.android.launcher3.folder.FolderIcon{4defc85 VFE...CL. ......ID 0,1415-165,1669 #d}
                com.android.launcher3.views.DoubleShadowBubbleTextView{e85374b V.ED..... ........ 0,183-165,254 #7f0a01b1 app:id/folder_icon_name}

TASK com.google.android.deskclock id=2 userId=0
  ACTIVITY com.google.android.deskclock/.DeskClock 7b1c2 pid=4102
    View Hierarchy:
      com.android.internal.policy.DecorView{db331f6 I.E...... R.....ID 0,0-1080,2400 aid=0}[DeskClock]
        android.widget.LinearLayout{4d87782 V.E...... ......ID 0,0-1080,2400 aid=1073741824}
          android.widget.Button{aa11bb2 VFE...CL. ......ID 100,200-300,280 #7f0a0999 app:id/start_button}
"""


def flatten(roots):
    for node in roots:
        yield node
        yield from flatten(node.children)


@pytest.fixture
def roots():
    return parse_view_tree(DUMPSYS)


class TestStructure:
    def test_finds_one_root_per_window(self, roots):
        assert len(roots) == 2
        assert all(r.cls.endswith("DecorView") for r in roots)

    def test_nests_children_by_indentation(self, roots):
        launcher = roots[0]
        assert launcher.children[0].cls == "android.widget.LinearLayout"
        assert len(launcher.children) == 1

    def test_keeps_siblings_at_the_same_depth(self, roots):
        linear_layout = roots[0].children[0]
        assert [c.cls.split(".")[-1] for c in linear_layout.children] == ["ViewStub", "FrameLayout"]

    def test_ignores_lines_outside_the_hierarchy_sections(self, roots):
        classes = [n.cls for n in flatten(roots)]
        assert not any("TASK" in c or "ACTIVITY" in c or "mCreated" in c for c in classes)


class TestNodeFields:
    def test_parses_the_resource_id_short_form(self, roots):
        button = next(n for n in flatten(roots) if n.cls.endswith("Button"))
        assert button.resource_id == "start_button"

    def test_keeps_the_fully_qualified_resource_id(self, roots):
        button = next(n for n in flatten(roots) if n.cls.endswith("Button"))
        assert button.full_resource_id == "app:id/start_button"

    def test_resource_id_is_none_when_the_view_has_no_name(self, roots):
        folder = next(n for n in flatten(roots) if n.cls.endswith("FolderIcon"))
        assert folder.resource_id is None

    def test_parses_flags(self, roots):
        button = next(n for n in flatten(roots) if n.cls.endswith("Button"))
        assert button.visible
        assert button.enabled
        assert button.clickable

    def test_a_gone_view_is_not_visible(self, roots):
        stub = next(n for n in flatten(roots) if n.cls.endswith("ViewStub"))
        assert not stub.visible

    def test_a_non_clickable_view_is_not_clickable(self, roots):
        layout = next(n for n in flatten(roots) if n.cls.endswith("LinearLayout"))
        assert not layout.clickable


class TestBounds:
    def test_root_bounds_are_as_printed(self, roots):
        assert roots[0].bounds == (0, 0, 1080, 2400)

    def test_child_bounds_are_made_absolute(self, roots):
        # FolderIcon is at 0,1415-165,1669 relative to a chain of parents at x=0,y=0,
        # so it is unchanged; its label is at 0,183-165,254 *within* the FolderIcon.
        label = next(n for n in flatten(roots) if n.resource_id == "folder_icon_name")
        assert label.bounds == (0, 1598, 165, 1669)

    def test_center_is_computed_from_absolute_bounds(self, roots):
        button = next(n for n in flatten(roots) if n.cls.endswith("Button"))
        assert button.center() == (200, 240)


class TestFind:
    def test_finds_a_node_by_short_resource_id(self, roots):
        from android_accessibility_tester import find_view

        assert find_view(roots, resource_id="start_button").cls.endswith("Button")

    def test_finds_a_node_by_full_resource_id(self, roots):
        from android_accessibility_tester import find_view

        assert find_view(roots, resource_id="app:id/start_button") is not None

    def test_returns_none_when_nothing_matches(self, roots):
        from android_accessibility_tester import find_view

        assert find_view(roots, resource_id="no_such_id") is None

    def test_ignores_views_that_are_gone(self, roots):
        from android_accessibility_tester import find_view

        # action_mode_bar_stub is present in the tree but has visibility G.
        assert find_view(roots, resource_id="action_mode_bar_stub") is None

    def test_can_include_invisible_views_when_asked(self, roots):
        from android_accessibility_tester import find_view

        assert find_view(roots, resource_id="action_mode_bar_stub", visible_only=False) is not None


class TestViewNodeIsUsableStandalone:
    def test_exposes_box_like_element_does(self):
        node = ViewNode(cls="android.widget.Button", bounds=(10, 20, 30, 60))
        assert node.box() == (10, 20, 30, 60)
        assert node.center() == (20, 40)


@pytest.fixture
def tester(monkeypatch):
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "m")
    t = AndroidAccessibilityTester(api_key="x")

    t.commands = []
    t.shots = 0
    t.dumpsys_output = DUMPSYS

    def fake_shell(command):
        t.commands.append(command)
        return t.dumpsys_output if "dumpsys activity top" in command else ""

    def fake_screenshot(path):
        t.shots += 1
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        return path

    monkeypatch.setattr(t, "shell", fake_shell)
    monkeypatch.setattr(t, "screenshot", fake_screenshot)
    return t


class TestCapture:
    def test_captures_both_a_screenshot_and_the_view_tree_by_default(self, tester):
        capture = tester.capture()

        assert capture.screenshot is not None
        assert os.path.exists(capture.screenshot)
        assert capture.views
        capture.release()

    def test_uses_dumpsys_not_uiautomator(self, tester):
        tester.capture().release()

        assert any("dumpsys activity top" in c for c in tester.commands)
        assert not any("uiautomator" in c for c in tester.commands)

    def test_can_skip_the_screenshot(self, tester):
        capture = tester.capture(screenshot=False)

        assert capture.screenshot is None
        assert tester.shots == 0
        assert capture.views

    def test_can_skip_the_view_tree(self, tester):
        capture = tester.capture(views=False)

        assert capture.views == []
        assert not any("dumpsys" in c for c in tester.commands)
        capture.release()

    def test_find_reaches_into_the_captured_tree(self, tester):
        capture = tester.capture(screenshot=False)

        assert capture.find("start_button").center() == (200, 240)

    def test_release_removes_the_screenshot(self, tester):
        capture = tester.capture()
        path = capture.screenshot

        capture.release()

        assert not os.path.exists(path)


class TestElementConditionsUseDumpsys:
    def test_element_present_matches_on_resource_id(self, tester):
        from android_accessibility_tester import ElementPresent

        assert tester.wait_for(ElementPresent(resource_id="start_button"), timeout=0.2, poll_interval=0.01)
        assert not any("uiautomator" in c for c in tester.commands)

    def test_element_gone_waits_for_the_id_to_disappear(self, tester):
        from android_accessibility_tester import ElementGone

        tester.dumpsys_output = DUMPSYS.replace("app:id/start_button", "app:id/something_else")

        assert tester.wait_for(ElementGone(resource_id="start_button"), timeout=0.2, poll_interval=0.01)

    def test_text_selectors_are_rejected_with_an_explanation(self):
        from android_accessibility_tester import ElementPresent

        with pytest.raises(ValueError, match="text"):
            ElementPresent(text="Start")

    def test_resolve_matches_a_cached_element_by_resource_id(self, tester):
        from android_accessibility_tester import Element

        cached = Element(found=True, bounds="[0,0][1,1]", explanation="x", resource_id="start_button")

        assert tester.resolve(cached).center() == (200, 240)
        assert not any("uiautomator" in c for c in tester.commands)
