# version 2.1.2

* Default model is now "claude-sonnet-4-6" (the old default "claude-sonnet-4-20250514" was retired and returns a 404). Pass `model=` to override.
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
