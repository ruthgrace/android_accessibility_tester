# Android Accessibility Tester

A Python framework for integration testing Android apps that hold accessibility permissions. It
drives the device with ADB and reads the screen with Claude's vision, so it never runs an
accessibility service of its own and never disturbs the one under test.

## API

Everything below is importable from `android_accessibility_tester`. Each method is described in
its own section further down.

```python
tester = AndroidAccessibilityTester(device_id=None, model=None, api_key=None)
```

**Model-backed** (one Claude call each)

| Method | Returns |
|---|---|
| `check(description, screenshot_path=None, model=None)` | `Check` — truthy if the screen matches the description |
| `extract(description, schema, model=None)` | an instance of `schema`, a pydantic model |
| `observe(description, model=None)` | `Element` or `None` |
| `act(target, model=None)` | `ActResult` — `target` is a description, or a cached `Element` (no model call) |

**Waiting** (no model calls)

| Method | Returns |
|---|---|
| `wait_for(condition, timeout=10.0, poll_interval=0.5)` | `Result` |
| `expect(condition, timeout=10.0, poll_interval=0.5)` | `Expectation` context manager; arms before the body, waits after |

Conditions: `PixelColor(x, y, colors, exclude=False)`, `PixelChanged(x, y)`,
`Logcat(tag, message, clear_first=True)`, `AnyOf(*conditions)`.

**Device control** (plain ADB)

| Method | Notes |
|---|---|
| `tap(x, y)`, `long_press(x, y, duration_ms=1000)`, `swipe(x1, y1, x2, y2, duration_ms=1000)` | |
| `input_text(text)`, `press_key(keycode)`, `press_back()` | |
| `open_app(package_name)`, `list_devices()`, `shell(command)` | `shell` returns stdout |
| `screenshot(output_path)` | saves a PNG, returns the path |
| `capture()` | `Capture` with `.screenshot`, `.pixel(x, y)`, `.release()` |

**Failure artifacts**

| Function | Notes |
|---|---|
| `failure_artifact_hook(item, call)` | call from `pytest_runtest_makereport` in `conftest.py` |
| `save_debug_artifacts(output_dir, test_name, step_name, screenshot=True, ui_dump=True, existing_screenshot=None)` | screenshot and `uiautomator` dump; failure path only |

**Types**

- `Result` — `.ok`, `.error`. Subclasses: `Check` (`.screenshot`), `ActResult` (`.element`),
  `PixelResult` (`.initial_color`, `.final_color`), `LogcatResult` (`.line`).
- `Element` — `.bounds`, `.box()` → `(x1, y1, x2, y2)`, `.center()` → `(x, y)`, `.explanation`.

## Design

The API is organized around **what you want**, not which mechanism supplies it, and is layered by
cost and determinism. Reach for the lowest rung that can express your intent:

1. **Deterministic, free** — `tap`, `swipe`, `input_text`, `press_key`, `shell`, `screenshot`
2. **Deterministic, polled** — `wait_for(condition)`
3. **Cached** — `observe()` once, then `act(element)` with no model calls
4. **Model-backed** — `check()`, `extract()`, `act("natural language")`

Every model call uses structured outputs with a pydantic schema, so responses are validated JSON.
Forced tool use is not used: the newest models reject `tool_choice: tool`/`any` with a 400.

Model resolution when `model=None`: `SCREEN_AGENT_MODEL` env override → newest Sonnet via the
Anthropic Models API → pinned `claude-sonnet-4-6` fallback. Cached per process.

## Results

Every outcome-carrying method returns a `Result`: truthy when `ok`, with `.error` holding one
sentence explaining failure. Its `__repr__` leads with that sentence, so a bare `assert` produces
a useful pytest message.

```python
result = tester.wait_for(Logcat("MyService", "ready"), timeout=30)
assert result            # on failure: "LogcatResult(ok=False): Timeout after 30s - ..."
```

## check

Captures a screenshot and validates it against a natural-language description. The screenshot
is discarded when the check passes and kept when it fails (path in `.screenshot`); pass
`screenshot_path` to always keep it.

```python
assert tester.check(
    'The screen shows a clock app with tabs at the bottom including "Alarms", '
    '"World Clock", "Timers", "Stopwatch", and "Bedtime".'
)
```

The check only passes if ALL described elements are present and accurate, so be precise. When
asserting on a value, prefer `extract`.

## extract

Reads structured data off the screen into a pydantic model, so assertions are ordinary ones.

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

Field docstrings and `Field(description=...)` are what steer the model. Keep schemas flat where
possible; nested models produce `$ref`/`$defs` in the JSON Schema.

## observe and act

`observe()` finds an element on screen from the screenshot alone and returns an `Element` with
`bounds` in device pixels. `act()` taps: given a string it calls `observe()` first (one model
call); given a cached `Element` it replays the coordinates with **no model call** and nothing
else touching the device.

```python
assert tester.act("tap the Start Timer button")

start = tester.observe("the Start Timer button")   # one model call
for _ in range(10):
    tester.act(start)                              # none
```

A replay does not survive a layout shift. Re-`observe()` when the layout changes.

When a description fits both a label and a tappable control, `observe()` prefers the control.

## wait_for and expect

`wait_for` polls a condition until it holds or the timeout passes. The condition is checked once
before the first sleep, so an already-satisfied condition returns immediately.

| Condition | Waits for |
|---|---|
| `PixelColor(x, y, colors, exclude=False)` | Pixel to become one of `colors` (a hex string, RGB tuple, or list). With `exclude=True`, any color *other* than those. |
| `PixelChanged(x, y)` | Pixel to become any color other than its starting one |
| `Logcat(tag, message, clear_first=True)` | A logcat line under `tag` containing `message` |
| `AnyOf(*conditions)` | Whichever fires first |

```python
tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]), timeout=30)
assert tester.wait_for(Logcat("MyService", "upload finished"), timeout=30)
assert tester.wait_for(AnyOf(Logcat("Overlay", "created"), PixelChanged(300, 1380)))
```

`expect` is `wait_for` for signals the action itself produces. It arms the condition on entry,
runs the body, and waits on exit. Use it for `Logcat`: the condition clears the log buffer when
it starts listening, so with act-then-`wait_for` a fast service can log its line before the
clear and the wait times out. If the body raises, the exception propagates without waiting.

```python
with tester.expect(Logcat("MyService", "overlay created"), timeout=30) as created:
    tester.act("tap the Start button")
assert created
```

For an app you control, `Logcat` is the strongest wait: exact, free, and disturbs nothing.

## capture

A screenshot of the current moment, held for later comparison. No model call.

```python
before = tester.capture()
tester.act("tap the send button")
after = tester.capture()
before.release()   # deletes the file
```

## The accessibility service constraint

Nothing on the happy path may read the UI hierarchy. `uiautomator dump` runs through
`UiAutomation`, which silently suppresses the accessibility services of the app under test: the
service stays enabled while its events stop arriving. `dumpsys activity top` was tried instead
and removed in 4.0.0: it carries no text, cannot see overlays, and a timed-out dump parses as an
empty screen. See the CHANGELOG for the measurements.

So the framework reads the screen visually, and there are no element-based conditions or
lookups by resource id. `uiautomator dump` survives in exactly one place: failure artifacts,
after the test is already over.

If you need a real hierarchy: an instrumentation APK with
`FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` gives full fidelity, or a debug-only receiver inside
your own app can walk its own view tree. Verify either functionally, since the suppression is
silent.

## Failure artifacts

`conftest.py` saves a screenshot and a hierarchy dump when a test fails:

```python
from android_accessibility_tester import failure_artifact_hook

def pytest_runtest_makereport(item, call):
    failure_artifact_hook(item, call)
```

Artifacts go to `$ANDROID_TEST_ARTIFACTS` (default `./test-failures`), named after the test. The
tester is found by type among the test's fixtures, so the fixture can have any name. If the last
`check()` failed, its screenshot is reused. Errors while saving are printed and swallowed. Copy
`conftest.py` into your own suite to get the same behaviour.

## Setup and usage

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=your_api_key_here
./venv/bin/pytest example_test.py
```

Prerequisites: ADB (Android SDK Platform Tools), `ANTHROPIC_API_KEY`, and a connected device or
running emulator.

Unit tests for the framework need no device or API key. The `Logcat` tests run the real
subprocess code against a fake `adb` script placed on `PATH`:

```bash
./venv/bin/pytest test_structured.py test_wait.py test_act.py test_failure_artifacts.py test_images.py
```

`test_find_coordinates.py` exercises `observe()` and needs a device and an API key.

A typical test:

```python
import android_accessibility_tester
from android_accessibility_tester import Logcat
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

## Error handling

Claude API calls retry with exponential backoff (1s, 2s, 4s, 8s, then one final attempt) for
`RateLimitError`, `InternalServerError`, `APITimeoutError`, and `APIConnectionError`.

If a response doesn't validate against the requested schema, the pydantic errors are sent back
to the model and it retries once. If it still fails, `StructuredCallError` is raised. Pydantic's
lax mode coerces some values: `"yes"`, `"on"` and `"1"` all validate as `True` for a `bool`.
Use strict types if you need that rejected.

## Implementation notes

- `input_text()` escapes for ADB: spaces become `%s`, and `()` are escaped.
- Screenshots are captured to `/sdcard/screenshot.png`, pulled, and the device file removed
  (failure ignored, since scoped storage may block `rm`).
- The API shrinks any image over about 1.15 megapixels, and some models then answer in the
  shrunken image's coordinates. So `observe()` resizes the screenshot itself with
  `fit_for_model()`, tells the model the image size, and divides the returned bounds back into
  device pixels. `check()` and `extract()` send the raw screenshot. `calibration/measure.py`
  reruns the coordinate measurement for any model without a device, so a model change can be
  checked before it ships.

## License

See LICENSE.md
