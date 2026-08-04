"""Automatic post-mortem artifacts for failing device tests.

When a test fails, save a screenshot and a UI hierarchy dump so the failure can be
diagnosed after the fact.

The hierarchy dump lives on this path and nowhere else on purpose. `uiautomator dump`
runs through UiAutomation, which suppresses the accessibility services of the app under
test — the very thing this framework exists to avoid disturbing. Once a test has failed
there is nothing left to disturb, so this is the one safe place to take one.

Artifacts go to $ANDROID_TEST_ARTIFACTS, defaulting to ./test-failures.

Copy this file into your own test suite to get the same behaviour.
"""

# Enables the `pytester` fixture used by this repo's own hook regression test.
pytest_plugins = ["pytester"]

from android_accessibility_tester import failure_artifact_hook


def pytest_runtest_makereport(item, call):
    failure_artifact_hook(item, call)
