# version 4.3.2

**`observe()` prefers a tappable control over a text label when a description fits both.**
`observe()` exists to feed `act()`, which taps, so when "the Start timer" could mean the title
text or the play button beside it, the button is almost always what the test wants. A test that
wants the label can say "the text label".

**Changed:** `calibration/measure.py` now uses a curated target table with descriptions written
the way a test author would write them, using the words visible on screen, instead of generating
descriptions from hierarchy labels. The hierarchy's "8 minutes timer" is "8m Timer" on screen,
and its label and buttons shared words, which produced misses that were not coordinate errors.
The bedtime fixture was captured mid-transition and lists controls the image does not show;
only its chrome is used until it is recaptured.

Result, both models: 42 hits of 42, no schema errors, nothing unfound. Median error 5 px on
Sonnet 5.5 and 25 px on Sonnet 4.6.

# version 4.3.1

**`Element`'s schema now carries the bounds rule itself.** Sonnet 4.6 failed schema validation on
about one `observe()` call in six: it either left `bounds` out and narrated the coordinates in
`explanation`, or wrote an explanation long enough to be cut off by the token cap. The schema
sent to the model said `bounds` was optional; only a pydantic validator on our side disagreed.

**Changed:**

* `bounds` is a required string, empty when `found` is false, and comes before `explanation` so
  the coordinates are emitted first. `explanation` is capped at twenty words by its description
  and told to carry no coordinates. The response token cap rises from 500 to 600.
* `resource_id`, `content_desc` and `text` are removed from `Element`. They dated from the
  hierarchy era, could only be invented from a screenshot, and have been `None` since 4.0.0.

Offline, with `calibration/measure.py`: Sonnet 4.6 schema errors 9 → 0 and hits 27 → 36 of 42;
Sonnet 5.5 unchanged at 0 errors, 35 → 37 hits. The remaining misses are the same ambiguous
Timers labels in every run.

No change to how `observe()` or `act()` are called.

# version 4.3.0

**`observe()` now resizes the screenshot before sending it, and scales the answer back.**

The API shrinks images over ~1.15 megapixels before the model sees them, and some models then
report coordinates in the shrunken image. On a 1080x2400 Pixel 8, Sonnet 4.6 returned every
position scaled by 0.666 — zero taps landed. Sonnet 5.5 was unaffected.

**Changed:**

* `fit_for_model(path)` resizes a screenshot to the cap at a known scale and returns the image
  block plus that scale. `observe()` sends the resized image, names its dimensions in the prompt,
  and divides the returned bounds by the scale. Screenshots under the cap are sent unchanged.
  Offline, Sonnet 4.6 goes from 0 to 27 hits out of 42; Sonnet 5.5 is unchanged at 35.
* `Element`'s field descriptions no longer refer to a hierarchy that was removed in 4.0.0.

**Added:**

* `add_rulers(path)` draws labelled coordinate rulers in a margin around a screenshot. Tested,
  but not used by `observe()`: measured, it did not help Sonnet 5.5 and hurt Sonnet 4.6.
* `calibration/pixel8/` — six Clock-app screenshots with matching `uiautomator` dumps — and
  `calibration/measure.py`, which scores `observe()` against them for any model without a device.

# version 4.2.0

**Adds `expect()`, and fixes the act-then-`wait_for(Logcat(...))` pattern the docs recommended.**

`Logcat` runs `logcat -c` when it starts listening, so a stale line from an earlier test cannot
produce a false pass. But `wait_for` starts the condition *after* the preceding `act()`, so a
service that logs quickly has its line wiped by the clear before the reader is attached, and
the wait times out although the app behaved. The README's own example had this ordering.

**Added:**

* `tester.expect(condition, timeout, poll_interval)` — a context manager that arms the condition
  on entry, runs the body, and waits on exit. The returned `Expectation` is truthy when the
  condition was met and forwards the underlying `Result`'s fields (`.error`, `.line`, …). If the
  body raises, the condition is stopped without waiting and the exception propagates.
* Unit tests that drive the real `Logcat` subprocess code through a fake `adb` script on `PATH`,
  including one that reproduces the ordering trap.

**Changed:** README and CLAUDE.md examples use `expect()` wherever the action produces the signal.
`Logcat` now filters at `tag:V` rather than `tag:D`, so lines logged with `Log.v` are no longer
dropped. Verified live on a Pixel 8.

# version 4.1.0

**Structured outputs replace forced tool use.** `structured_call` now asks for JSON via
`output_config.format` (`json_schema`) instead of `tool_choice: {type: "tool"}`. The newest
models (Sonnet 5.5, Fable 5.1) reject forced tool choice with
`tool_choice: type "tool" and "any" are not supported for this model.`, and because the default
model resolves to the newest Sonnet, every `check()`/`observe()`/`extract()` started failing the
day that model shipped.

**Changed:**

* Schemas are sent with `additionalProperties: false` on every object (`_strict_schema`), which
  the structured-outputs endpoint requires. Pydantic models need no change.
* The output model's docstring, formerly the tool description, is appended to the prompt as
  `Respond with: <docstring>`.
* Schema-violation retries send the pydantic errors as a follow-up user text message rather than
  a `tool_result`. Non-JSON text is treated as a schema violation and retried the same way.
* `output_config` is passed through `extra_body`, so the existing `anthropic>=0.39` pin still
  holds for callers on older SDKs.

**Removed:** `TOOL_NAME`.

# version 4.0.0

**Removes the `dumpsys activity top` view tree added in 3.2.0.** Measured on a real device, it failed three independent ways:

* **No text.** `dumpsys` prints class names, ids, bounds and flags, and nothing a control actually says.
* **Cannot see overlays.** It enumerates Activities; a `SYSTEM_ALERT_WINDOW` added by a Service has no `ActivityRecord`, so an accessibility overlay never appears in it.
* **Unreliable.** It fans binder calls out to every task on the device. One hung app (Signal, in the measured case) exhausts the 10-second service dump budget, and everything after it is never reached. Five consecutive runs: 10.1s, zero hierarchies.

The third was the dangerous one. A timed-out dump parses as an empty tree, which reads as "nothing is on screen", so `ElementGone` reported success while the element was still present — a silent false pass.

**Removed:** `ElementPresent`, `ElementGone`, `resolve()`, `capture(views=...)`, `ViewNode`, `parse_view_tree`, `find_view`, `_get_view_tree`, `_view_tree_json`.

**Changed:**

* `observe()` and `extract()` read the screenshot alone.
* `act(element)` on a cached `Element` replays its coordinates directly — no model call, no screenshot, no device round-trip beyond the tap. It no longer re-resolves against a live hierarchy, so a replay does not survive a layout shift.
* `capture()` returns a screenshot only.

**What remains for deterministic waiting:** `PixelColor`, `PixelChanged`, `Logcat`, and `AnyOf`. For an app you control, `Logcat` is usually the strongest of these — exact, free, and it disturbs nothing.

**Why not go back to `uiautomator`:** it runs through `UiAutomation`, which suppresses the accessibility services of the app under test. The failure is silent — the service stays bound and enabled while its events stop arriving — so a state check cannot detect it. It survives in `save_debug_artifacts` only, on the failure path.

If a real hierarchy is needed, the two viable routes are an instrumentation APK using `setUiAutomationFlags(FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)`, or a debug-only receiver inside the app under test that walks its own view tree. Both are documented in CLAUDE.md; neither is implemented here.

# version 3.2.0

**Reads structure with `dumpsys activity top` instead of `uiautomator dump`.** `uiautomator dump` runs through `UiAutomation`, which suppresses the accessibility services of the app under test — the opposite of what this framework is for. `dumpsys activity top` goes through ActivityManager and never touches `UiAutomation`. `uiautomator dump` now appears in exactly one place: `save_debug_artifacts`, on the failure path.

* `parse_view_tree(dumpsys_output)` parses the dump into `ViewNode` trees, accumulating each node's parent-relative bounds into absolute screen coordinates. `find_view(roots, resource_id)` matches short or fully-qualified ids and skips invisible views by default.
* `capture(screenshot=True, views=True)` returns a `Capture` — a free snapshot of the screen with no model call and no disturbance to the app. Both sources by default. Call it purely for a baseline and ignore the result, or hold it and compare later. `.find(resource_id)` reaches into the tree; `.release()` deletes the screenshot.
* `observe()` and `extract()` now send **both** a screenshot and the view tree: the screenshot shows what each control says, the tree supplies exact bounds and resource ids.
* `ElementPresent` / `ElementGone` / `resolve()` read the view tree and match on `resource_id` only.

**Breaking:** `dumpsys` carries no text or content descriptions. `ElementPresent(text=...)` and `ElementGone(content_desc=...)` now raise `ValueError` explaining why, rather than silently reaching for a disruptive dump. A cached `Element` without a `resource_id` can no longer be replayed by `resolve()` and falls back to the model. Use `check()` or a pixel condition for anything about what the screen says.

**Removed:** `_get_ui_hierarchy` and `_simplify_node_for_llm`, replaced by `_get_view_tree` and `_view_tree_json`.

Caveat: `dumpsys activity top` output is a debugging dump, not a stable API, and its format can shift between Android versions. `parse_view_tree` concentrates that risk. The parser is built and tested against verbatim output from a real device.

# version 3.1.0

**Automatic failure artifacts.** A pytest hook (`failure_artifact_hook`, wired up in `conftest.py`) saves a screenshot and a UI hierarchy dump when a test fails, to `$ANDROID_TEST_ARTIFACTS` (default `./test-failures`). It locates the tester by type among the test's fixtures, so the fixture can be named anything, and reuses the screenshot from a failed `check()` so the artifact is what the model actually judged. Failures while saving are printed, never raised, so they cannot mask the real failure.

**Documentation correction.** The 3.0.0 notes and CLAUDE.md claimed element conditions polled the hierarchy "without requiring accessibility services, avoiding conflicts with apps that hold accessibility permissions." The second half was backwards. `uiautomator dump` runs through `UiAutomation`, which suppresses the accessibility services of the app under test; `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` cannot be passed via `adb shell`. For a framework meant to test accessibility apps, that makes hierarchy access unsafe during a test.

The governing principle is now documented: the hierarchy is a post-mortem tool, never an input to control flow. `observe`, `act`, `resolve`, `extract` and the `ElementPresent`/`ElementGone` conditions currently read it and are marked disruptive pending rework onto screenshots. `check`, the pixel and logcat conditions, and the ADB primitives are unaffected.

# version 3.0.0

Breaking release. The API is reorganized around what you want (check / extract / observe / act / wait_for) rather than which mechanism supplies it, and every model call now uses forced tool use with a pydantic schema instead of parsing JSON out of prose.

**New**

* `check(description)` — replaces `validate_screenshot`. Captures the screenshot itself; keeps the file only when the check fails, exposed as `.screenshot`.
* `extract(description, schema)` — read typed data off the screen into a pydantic model. Sends both the screenshot and the accessibility hierarchy.
* `observe(description)` — replaces `find_element_coordinates_accessibility`. Returns an `Element` carrying selectors (`resource_id`, `content_desc`, `text`) as well as bounds, so a result can be cached.
* `act(target)` — tap an element by natural-language description, or replay a cached `Element` with no model call. A replayed element is re-resolved against the current hierarchy, so it survives layout shifts; it falls back to the model only when its selectors are gone.
* `resolve(element)` — re-find a cached element in the current hierarchy without the model.
* `wait_for(condition, timeout, poll_interval)` — one verb replacing the five wait methods, taking `PixelColor`, `PixelChanged`, `ElementPresent`, `ElementGone`, `Logcat`, or `AnyOf(...)`. `AnyOf` waits for whichever fires first, which was not previously expressible. All conditions in a poll share a single screenshot and hierarchy dump.
* `structured_call(client, model, content, output_model)` — forced-tool-use helper behind all of the above. Retries transient API errors on the existing backoff, and schema violations by returning the pydantic errors to the model as a `tool_result`.
* Every outcome-carrying return is now a `Result` (`ok` / `error`, truthy on success), so `assert tester.wait_for(...)` reports a useful message. `PixelResult`, `LogcatResult`, `ActResult` and `Check` add their own diagnostics.

**Removed** — replaced as above: `validate_screenshot`, `find_element_coordinates_accessibility`, `wait_for_pixel_change`, `wait_for_pixel_color`, `wait_for_logcat`, `check_element_exists`, and the `ValidationResult` / `ElementCoordinates` classes.

**Fixed**

* `_simplify_node_for_llm` dropped any container that was not itself clickable or texted — and its entire subtree with it, so a button nested inside a plain `FrameLayout` never reached the model. Containers whose descendants are meaningful are now kept.
* Malformed or missing `bounds` in a model response is caught by schema validation and retried, instead of failing later inside `int()`.

**Requires** pydantic >= 2.0 (already a dependency of `anthropic`) and Python >= 3.9.

