# android_screenshot_testing

Accessible apps are notoriously hard to test on Android. This is a Python framework for integration testing accessibility services with ADB and screenshots. Importantly, it does not itself use accessibility services so it does not conflict with an accessibilty service app.

# Example

You can set up the example like so:

```
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

Set your Anthropic API key and run the example:

```
export ANTHROPIC_API_KEY=your_api_key_here
./venv/bin/pytest example_test.py
```

# Writing tests

The API is organized by what you want, not by which mechanism supplies it:

```python
from android_accessibility_tester import AndroidAccessibilityTester, Logcat

tester = AndroidAccessibilityTester()
tester.open_app("com.example.myapp")

with tester.expect(Logcat("MyService", "load complete"), timeout=30) as loaded:
    assert tester.act("tap the Start button")                      # find and tap
assert loaded                                                      # the line arrived
assert tester.check("the results list is showing with at least one row")
```

`expect()` starts listening *before* the action and waits *after* it. Use it whenever the
action is what produces the signal. Plain `wait_for()` is for signals that will arrive on
their own.

`observe()` locates an element and returns it *without* acting. Cache that result and `act()`
on it to replay the tap with no further model calls — this is the droidrun-and-hardcode
workflow with the manual step removed. Re-observe when the layout changes:

```python
start = tester.observe("the Start Timer button")   # one model call
for _ in range(10):
    tester.act(start)                              # none
```

`extract()` reads typed data off the screen, so you can assert on values rather than describe
the whole screen in prose:

```python
from pydantic import BaseModel

class TimerState(BaseModel):
    """The state of the on-screen timer."""
    remaining_seconds: int
    is_running: bool

state = tester.extract("the state of the running timer", TimerState)
assert state.remaining_seconds > 0
```

See CLAUDE.md for the full API.

Droidrun is also useful for identifying element coordinates by hand (though it cannot be running while testing an app that uses accessibility services, since only one app can hold the permissions at a time).
