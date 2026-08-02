# version 2.2.0

* When `model` is not passed, the default assertion model is now resolved dynamically: `SCREEN_AGENT_MODEL` env override -> newest Sonnet via the Anthropic Models API -> `claude-sonnet-4-6` fallback (offline / API failure). Replaces the retired hard-coded `claude-sonnet-4-20250514`, which returned a 404. The lookup is cached per process, so the Models API is queried at most once. Pass `model=` to force a specific model.
* Parse screenshot-validation responses by selecting the text content block instead of assuming `content[0]` — adaptive-thinking models (e.g. Claude Sonnet 4.6+/5) can return a thinking block first, which broke `content[0].text`.

# version 2.0.0

* Add methods for waiting until a pixel changes color, or waiting until a pixel changes to a specific color
* ADB longpress command and ADB swipe command

# version 1.0.0
* adjust prompt so screenshot validation only passes when it's an exact match
* exponential backoff for claude requests
* Return true/false plus error message for screenshot validation

# version 0.1.0

* basic screenshot validation and some ADB commands.
