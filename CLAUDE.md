# Android Accessibility Tester

A Python framework for integration testing Android apps that use accessibility services, using ADB commands, screenshots, and Claude AI for visual validation.

## Overview

This framework enables testing Android accessibility services without requiring another accessibility service to run the tests (which would conflict). It uses ADB shell commands to interact with devices and Claude's vision capabilities to validate UI states through screenshots.

## Design

The API is organized around **what you want**, not which mechanism supplies it, and is layered by cost and determinism. Reach for the lowest rung that can express your intent:

1. **Deterministic, free** — `tap`, `swipe`, `input_text`, `press_key`, `shell`, `screenshot`
2. **Deterministic, polled** — `wait_for(condition)`
3. **Cached** — `observe()` once, then `act(element)` / `resolve(element)` with no model calls
4. **Model-backed** — `check()`, `extract()`, `act("natural language")`

Every model call uses forced tool use with a pydantic schema, so responses are validated JSON rather than prose that has to be parsed.

## Core Components

### AndroidAccessibilityTester

Main class in `android_accessibility_tester.py`.

```python
tester = AndroidAccessibilityTester(
    device_id=None,      # Specific device ID, or None for first available
    model=None,          # Claude model; defaults to newest Sonnet via the Models API
    api_key=None         # Anthropic API key, defaults to ANTHROPIC_API_KEY env var
)
```

Model resolution when `model=None`: `SCREEN_AGENT_MODEL` env override → newest Sonnet via the Anthropic Models API → pinned `claude-sonnet-4-6` fallback. Cached per process.

## Results

Every outcome-carrying method returns a `Result`: truthy when `ok`, with `.error` holding one sentence explaining failure. Its `__repr__` leads with that sentence, so a bare `assert` produces a useful pytest message.

```python
result = tester.wait_for(ElementGone(resource_id="spinner"), timeout=30)
assert result            # on failure: "ElementGone(ok=False): Timeout after 30s - ..."
```

Subclasses add diagnostics: `Check.screenshot`, `PixelResult.initial_color` / `.final_color`, `LogcatResult.line`, `ActResult.element`.

## Visual Validation

**check(description, screenshot_path=None, model=None) → Check**

Captures a screenshot and validates it against a natural-language description. The screenshot is discarded when the check passes and kept when it fails (path in `.screenshot`); pass `screenshot_path` to always keep it.

```python
assert tester.check(
    'The screen shows a clock app with tabs at the bottom including "Alarms", '
    '"World Clock", "Timers", "Stopwatch", and "Bedtime".'
)
```

**Important:** The description should be precise and comprehensive — the check only passes if ALL described elements are present and accurate.

## Data Extraction

**extract(description, schema, model=None) → schema instance**

Reads structured data off the screen into a pydantic model, using both the screenshot and the accessibility hierarchy. Turns prose assertions into ordinary ones.

```python
from pydantic import BaseModel

class TimerState(BaseModel):
    """The state of the on-screen timer."""
    remaining_seconds: int
    is_running: bool

state = tester.extract("the state of the running timer", TimerState)
assert state.remaining_seconds > 0
assert state.is_running
```

Field docstrings and `Field(description=...)` are what steer the model — they replace prompt text. Keep extraction schemas flat where possible; deeply nested models produce `$ref`/`$defs` in the JSON Schema.

## Element Location and Interaction

**observe(description, model=None) → Element | None**

Finds an element in the accessibility hierarchy without acting on it. The returned `Element` carries selectors (`resource_id`, `content_desc`, `text`) as well as `bounds`, so it can be cached by the caller and replayed later.

```python
start = tester.observe("the Start Timer button")
if start:
    tester.tap(*start.center())
```

`Element` has `.bounds`, `.box()` → `(x1, y1, x2, y2)`, `.center()` → `(x, y)`, and `.explanation`.

**act(target, model=None) → ActResult**

Taps an element. Given a string, locates it with `observe()` (one model call). Given a cached `Element`, re-resolves it against the current hierarchy with **no model call**, falling back to the model only if its selectors are gone.

```python
assert tester.act("tap the Start Timer button")

# Observe once, replay many times — no further model calls
start = tester.observe("the Start Timer button")
for _ in range(10):
    tester.act(start)
```

**resolve(element) → Element | None**

Re-finds a cached element in the current hierarchy without the model, returning it with fresh bounds. Returns None if no selector matches.

## Waiting

**wait_for(condition, timeout=10.0, poll_interval=0.5) → Result**

One verb for all waiting. The condition is checked once before the first sleep, so an already-satisfied condition returns immediately. Each poll captures the screen at most once regardless of how many conditions inspect it.

Conditions:

| Condition | Waits for |
|---|---|
| `PixelColor(x, y, colors, exclude=False)` | Pixel to become one of `colors` (a hex string, RGB tuple, or list). With `exclude=True`, any color *other* than those. |
| `PixelChanged(x, y)` | Pixel to become any color other than its starting one |
| `ElementPresent(text=, resource_id=, content_desc=)` | Element to appear. `resource_id` matches short or full form. |
| `ElementGone(text=, resource_id=, content_desc=)` | Element to disappear |
| `Logcat(tag, message, clear_first=True)` | A logcat line under `tag` containing `message` |
| `AnyOf(*conditions)` | Whichever fires first |

```python
tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]), timeout=30)
tester.wait_for(PixelColor(300, 1380, ["#ffffff", "#1b1b1b"], exclude=True))
assert tester.wait_for(ElementGone(resource_id="loading_spinner"), timeout=30)
assert tester.wait_for(AnyOf(Logcat("Overlay", "created"), PixelChanged(300, 1380)))
```

`ElementPresent` / `ElementGone` require at least one of `text`, `resource_id`, `content_desc`, or raise `ValueError`.

**These two conditions read the UI hierarchy — see the constraint below before using them.**

## The Accessibility Service Constraint

This framework exists to test apps that hold accessibility permissions. `uiautomator dump`
works *against* that goal: it runs through `UiAutomation`, which **suppresses the accessibility
services of the app under test**. The `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` flag exists
but is only settable from instrumentation code — `adb shell uiautomator dump` cannot pass it.
Reports indicate the services are destroyed rather than paused: they still read as enabled in
Settings but need a disable/re-enable cycle to actually return.

**The principle: the hierarchy is a post-mortem tool, never an input to control flow.**
Nothing a test does while running should depend on a dump.

Which methods currently read the hierarchy:

| Reads the hierarchy (disruptive) | Screenshot / ADB only (safe) |
|---|---|
| `observe()`, `act("...")` | `check()` |
| `extract()` | `wait_for(PixelColor / PixelChanged / Logcat)` |
| `resolve()`, `act(element)` | `tap`, `swipe`, `input_text`, `press_key`, `shell`, `screenshot` |
| `wait_for(ElementPresent / ElementGone)` | |

The left column is slated to be reworked onto screenshots. Until then, treat those methods as
disruptive to the app under test and prefer the right column.

### Failure Artifacts

The one safe place for a dump is after a test has already failed — there is nothing left to
disturb, and it is the richest diagnostic available. `conftest.py` wires this up:

```python
from android_accessibility_tester import failure_artifact_hook

def pytest_runtest_makereport(item, call):
    failure_artifact_hook(item, call)
```

On failure this saves a screenshot and a hierarchy dump to `$ANDROID_TEST_ARTIFACTS`
(default `./test-failures`), naming them after the test. It finds the tester by type among the
test's fixtures, so the fixture can be called anything. If the last `check()` failed, its
screenshot is reused, so the artifact is what the model actually judged rather than a fresh
capture of a screen that has since moved on. Artifact capture never masks the real failure —
errors while saving are printed and swallowed.

Copy `conftest.py` into your own suite to get the same behaviour.

## Device Control

- `shell(command)` — arbitrary ADB shell command
- `screenshot(output_path)` — capture to a local path
- `tap(x, y)`, `long_press(x, y, duration_ms=1000)`, `swipe(x1, y1, x2, y2, duration_ms=1000)`
- `input_text(text)` — type into the focused field
- `press_key(keycode)`, `press_back()`
- `open_app(package_name)`, `list_devices()`
- `save_debug_artifacts(output_dir, test_name, step_name, ...)` — screenshot + UI dump for post-mortems

## Setup & Usage

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

Prerequisites: ADB (Android SDK Platform Tools), `ANTHROPIC_API_KEY`, and a connected device or running emulator.

```bash
export ANTHROPIC_API_KEY=your_api_key_here
./venv/bin/pytest example_test.py
```

Unit tests for the framework itself need no device or API key:

```bash
./venv/bin/pytest test_structured.py test_wait.py test_act.py
```

## Example Test Structure

```python
import android_accessibility_tester
from android_accessibility_tester import ElementGone, PixelColor
import pytest

@pytest.fixture(scope="function")
def tester():
    tester = android_accessibility_tester.AndroidAccessibilityTester()
    tester.open_app("com.example.myapp")
    yield tester

def test_my_feature(tester):
    assert tester.act("tap the Start button")
    assert tester.wait_for(ElementGone(resource_id="spinner"), timeout=30)
    assert tester.check("the results list is showing with at least one row")
```

## Error Handling

### Transient API Errors

Claude API calls retry with exponential backoff (1s, 2s, 4s, 8s, then one final attempt) for `RateLimitError`, `InternalServerError`, `APITimeoutError`, and `APIConnectionError`. Implemented once in `structured_call`.

### Schema Violations

If a response doesn't validate against the requested schema, the pydantic errors are returned to the model as a `tool_result` and it retries once. If it still fails, `StructuredCallError` is raised.

Note that pydantic's default lax mode coerces some values — `"yes"`, `"on"` and `"1"` all validate as `True` for a `bool` field. Use strict types if you need that rejected.

## Implementation Notes

### Text Input Encoding

`input_text()` handles ADB requirements: spaces become `%s`, and `()` are escaped.

### UI Hierarchy Simplification

The hierarchy is simplified before being sent to Claude to reduce token usage: only meaningful attributes (text, content_desc, resource_id, bounds, clickable, enabled), class names truncated from package names, max depth 5. Containers are kept when their descendants are meaningful, so nesting doesn't drop a subtree.

### Screenshot Workflow

Device captures to `/sdcard/screenshot.png`, ADB pulls to the local path, the device file is removed (failure ignored — scoped storage may block `rm`).

## Tips & Best Practices

1. **Observe once, replay after** — `observe()` costs a model call; `act(element)` and `resolve(element)` cost none. For repeated interactions, cache the `Element`.
2. **Precise descriptions** — `check()` only passes if all described elements match.
3. **Prefer `extract` over prose checks** when asserting on a value: `assert state.remaining_seconds > 0` beats describing the whole screen.
4. **Per-test cleanup** — use function-scoped fixtures for clean state between tests.

## Architecture

The framework avoids conflicts with accessibility service apps:
- Uses ADB shell commands instead of accessibility APIs
- Runs externally to the Android system
- Can test apps that hold accessibility service permissions

## License

See LICENSE.md
