# Android Accessibility Tester

A Python framework for integration testing Android apps that use accessibility services, using ADB commands, screenshots, and Claude AI for visual validation.

## Overview

This framework enables testing Android accessibility services without requiring another accessibility service to run the tests (which would conflict). It uses ADB shell commands to interact with devices and Claude's vision capabilities to validate UI states through screenshots.

## Design

The API is organized around **what you want**, not which mechanism supplies it, and is layered by cost and determinism. Reach for the lowest rung that can express your intent:

1. **Deterministic, free** — `tap`, `swipe`, `input_text`, `press_key`, `shell`, `screenshot`
2. **Deterministic, polled** — `wait_for(condition)`
3. **Cached** — `observe()` once, then `act(element)` with no model calls
4. **Model-backed** — `check()`, `extract()`, `act("natural language")`

Every model call uses structured outputs (`output_config.format`) with a pydantic schema, so responses are validated JSON rather than prose that has to be parsed. Forced tool use is not used: the newest models reject `tool_choice: tool`/`any` with a 400.

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
result = tester.wait_for(Logcat("MyService", "ready"), timeout=30)
assert result            # on failure: "LogcatResult(ok=False): Timeout after 30s - ..."
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

Finds an element on screen without acting on it. The screenshot is read visually; nothing
touches the UI hierarchy. The returned `Element` carries `bounds` in device pixels, so it can be
cached by the caller and replayed later.

```python
start = tester.observe("the Start Timer button")
if start:
    tester.tap(*start.center())
```

`Element` has `.bounds`, `.box()` → `(x1, y1, x2, y2)`, `.center()` → `(x, y)`, and `.explanation`.

**act(target, model=None) → ActResult**

Taps an element. Given a string, locates it with `observe()` (one model call). Given a cached `Element`, replays its coordinates with **no model call**.

```python
assert tester.act("tap the Start Timer button")

# Observe once, replay many times — no further model calls
start = tester.observe("the Start Timer button")
for _ in range(10):
    tester.act(start)
```

A cached `Element` replays its own coordinates — no model call, no screenshot, nothing that
touches the device beyond the tap itself. There is no re-resolution against a live hierarchy,
so a replay does not survive a layout shift; re-`observe()` when the layout changes.

## Waiting

**wait_for(condition, timeout=10.0, poll_interval=0.5) → Result**

One verb for all waiting. The condition is checked once before the first sleep, so an already-satisfied condition returns immediately. Each poll captures the screen at most once regardless of how many conditions inspect it.

Conditions:

| Condition | Waits for |
|---|---|
| `PixelColor(x, y, colors, exclude=False)` | Pixel to become one of `colors` (a hex string, RGB tuple, or list). With `exclude=True`, any color *other* than those. |
| `PixelChanged(x, y)` | Pixel to become any color other than its starting one |
| `Logcat(tag, message, clear_first=True)` | A logcat line under `tag` containing `message` |
| `AnyOf(*conditions)` | Whichever fires first |

```python
tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]), timeout=30)
tester.wait_for(PixelColor(300, 1380, ["#ffffff", "#1b1b1b"], exclude=True))
assert tester.wait_for(Logcat("MyService", "upload finished"), timeout=30)
assert tester.wait_for(AnyOf(Logcat("Overlay", "created"), PixelChanged(300, 1380)))
```

**expect(condition, timeout=10.0, poll_interval=0.5) → Expectation**

`wait_for` for signals the action itself produces. It arms the condition on entry, runs the
body, and waits on exit. After the block the `Expectation` is truthy if the condition was met
and forwards `.error`, `.line`, `.final_color` and so on from the underlying `Result`.

```python
with tester.expect(Logcat("MyService", "overlay created"), timeout=30) as created:
    tester.act("tap the Start button")
assert created
```

Why this exists: `Logcat` clears the log buffer when it starts listening, so stale lines from
an earlier test cannot produce a false pass. With act-then-`wait_for`, that clear runs *after*
the tap. A fast service logs its line in the gap, the clear wipes it, and the wait times out
even though the app did the right thing. `expect` puts the clear before the tap. If the body
raises, the condition is torn down without waiting and the exception propagates.

There are no element-based conditions: every way of reading the hierarchy either disturbs the
app under test or is unreliable. See the constraint section below.

## The Accessibility Service Constraint

This framework tests apps that hold accessibility permissions, so nothing on the happy path
may disturb them. In practice that rules out reading the UI hierarchy at all.

**`uiautomator dump`** runs through `UiAutomation`, which suppresses the accessibility
services of the app under test. `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` exists but is only
settable from instrumentation code, so `adb shell` cannot pass it. The failure is quiet: the
service stays bound and enabled while its events stop arriving, so a state check will not
catch it.

**`dumpsys activity top`** was tried as a replacement in 3.2.0 and removed in 4.0.0. It never
touches `UiAutomation`, but measured on a real device it fails three ways: it carries **no
text**; it cannot see `SYSTEM_ALERT_WINDOW` overlays, because it enumerates Activities and an
overlay added by a Service has no `ActivityRecord`; and it fans binder calls out to every task
on the device, so one hung app takes the whole dump down with a 10-second timeout. Worse, a
timed-out dump parses as an empty tree, which silently reads as "nothing is on screen" — an
`ElementGone` check would pass while the element was still there.

**So the framework reads the screen visually.** `uiautomator dump` survives in exactly one
place: `save_debug_artifacts`, on the failure path, where the test is already over.

### What that costs

There is no element location by resource id, and no waiting on an element by id or text. What
remains for deterministic waiting is `PixelColor`, `PixelChanged` and `Logcat`.

For an app you control, `Logcat` is usually the strongest option — it is exact, free, needs no
model call, and disturbs nothing:

```python
with tester.expect(Logcat("MyAccessibilityService", "overlay created"), timeout=30) as created:
    tester.act("tap the Start button")
assert created
```

### Options if you need a real hierarchy

- **An instrumentation APK** using `setUiAutomationFlags(FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)`.
  Full fidelity including text and overlay windows, at the cost of a test APK driven by
  `am instrument`. Verify it functionally before trusting it — the silent failure above means a
  state check proves nothing.
- **A debug-only receiver inside the app under test** that walks its own view tree and returns
  JSON. Full text, sees its own overlays, immune to other apps, no accessibility interaction.
  Only covers your own app.

## Capturing State

**capture() → Capture**

A screenshot of the current moment, held for later comparison. No model call, nothing that
disturbs the app.

```python
before = tester.capture()
tester.act("tap the send button")
after = tester.capture()
before.release()   # deletes the file
```

### Failure Artifacts

The one safe place for a `uiautomator` dump is after a test has already failed. `conftest.py`
wires this up:

```python
from android_accessibility_tester import failure_artifact_hook

def pytest_runtest_makereport(item, call):
    failure_artifact_hook(item, call)
```

On failure this saves a screenshot and a full hierarchy dump to `$ANDROID_TEST_ARTIFACTS`
(default `./test-failures`), named after the test. It finds the tester by type among the test's
fixtures, so the fixture can be called anything. If the last `check()` failed, its screenshot
is reused, so the artifact is what the model actually judged. Errors while saving are printed
and swallowed, so artifact capture can never mask the real failure.

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

Unit tests for the framework itself need no device or API key. The `Logcat` tests run the
real subprocess code against a fake `adb` script placed on `PATH`, backed by a plain file:

```bash
./venv/bin/pytest test_structured.py test_wait.py test_act.py test_failure_artifacts.py test_images.py
```

## Example Test Structure

```python
import android_accessibility_tester
from android_accessibility_tester import Logcat, PixelColor
import pytest

@pytest.fixture(scope="function")
def tester():
    tester = android_accessibility_tester.AndroidAccessibilityTester()
    tester.open_app("com.example.myapp")
    yield tester

def test_my_feature(tester):
    with tester.expect(Logcat("MyService", "load complete"), timeout=30) as loaded:
        assert tester.act("tap the Start button")
    assert loaded
    assert tester.check("the results list is showing with at least one row")
```

## Error Handling

### Transient API Errors

Claude API calls retry with exponential backoff (1s, 2s, 4s, 8s, then one final attempt) for `RateLimitError`, `InternalServerError`, `APITimeoutError`, and `APIConnectionError`. Implemented once in `structured_call`.

### Schema Violations

If a response doesn't validate against the requested schema (or is not JSON), the pydantic errors are sent back to the model as a follow-up user message and it retries once. If it still fails, `StructuredCallError` is raised.

Note that pydantic's default lax mode coerces some values — `"yes"`, `"on"` and `"1"` all validate as `True` for a `bool` field. Use strict types if you need that rejected.

## Implementation Notes

### Text Input Encoding

`input_text()` handles ADB requirements: spaces become `%s`, and `()` are escaped.

### Screenshot Workflow

Device captures to `/sdcard/screenshot.png`, ADB pulls to the local path, the device file is removed (failure ignored — scoped storage may block `rm`).

### Screenshot Scaling

The API shrinks any image over about 1.15 megapixels before the model sees it, and a phone
screenshot is well over that. Some models then answer in the shrunken image's coordinates: on a
1080x2400 Pixel 8, Sonnet 4.6 returned every position scaled by 0.666, so no tap landed.

So `observe()` does the shrinking itself. `fit_for_model()` resizes the screenshot to the cap at
a known scale, the prompt tells the model the image's dimensions, and the returned bounds are
divided by the scale back into device pixels. Screenshots already under the cap are sent as is.
`check()` and `extract()` send the raw screenshot, since they report on content, not position.

Measured offline on six Clock screens (42 visible targets, scored against `uiautomator` dumps):

| Model | Raw screenshot | Resized, scaled back |
|---|---|---|
| Sonnet 5.5 | 35 hits, median 3 px | 35 hits, median 5 px |
| Sonnet 4.6 | 0 hits, median 606 px | 27 hits, median 26 px |

The remaining misses are the same controls every run, where the hierarchy label is ambiguous
(a Timers card and its start button), not coordinate errors.

Coordinate rulers in a margin around the screenshot were also tried (`add_rulers()`, kept and
tested). They did not help Sonnet 5.5 and made Sonnet 4.6 worse: it read the vertical ruler
loosely and ignored the horizontal one. They are not used.

`calibration/pixel8/` holds the screenshots and dumps; `calibration/measure.py` reruns the
measurement for any model, with no device, so a model change can be checked before it ships.

## Tips & Best Practices

1. **Observe once, replay after** — `observe()` costs a model call; `act(element)` on a cached
   Element costs none. Re-observe when the layout changes.
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
