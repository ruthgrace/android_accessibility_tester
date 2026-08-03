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

