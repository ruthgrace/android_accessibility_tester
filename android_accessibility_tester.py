"""
Android Accessibility Testing Library

A Python library for testing Android apps using ADB commands, screenshots,
and Claude LLM for visual assertions.
"""

import os
import subprocess
import base64
import json
import re
import time
import tempfile
import threading
from typing import Optional, List, Tuple
from functools import wraps
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator
from anthropic import Anthropic
from anthropic import APITimeoutError, APIConnectionError, RateLimitError, InternalServerError
try:
    from PIL import Image
except ImportError:
    Image = None


def timing(f):
    """Decorator to print execution time of methods (only in verbose mode)."""
    @wraps(f)
    def wrap(*args, **kwargs):
        start = time.time()
        result = f(*args, **kwargs)
        elapsed = time.time() - start
        # Only print timing if pytest is running in verbose mode
        import sys
        if '-v' in sys.argv or '--verbose' in sys.argv:
            print(f'⏱️  {f.__name__} took {elapsed:.3f}s')
        return result
    return wrap


TRANSIENT_API_ERRORS = (RateLimitError, InternalServerError, APITimeoutError, APIConnectionError)
RETRY_DELAYS = [1, 2, 4, 8]  # exponential backoff, plus one final attempt
MAX_SCHEMA_ATTEMPTS = 2

def _strict_schema(schema):
    """Return a copy of a JSON schema with `additionalProperties: false` on every object.

    The structured-outputs endpoint requires it on all objects; pydantic's
    `model_json_schema()` omits it. Applies to nested objects and `$defs` too.
    """
    if isinstance(schema, dict):
        out = {k: _strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_strict_schema(v) for v in schema]
    return schema


class StructuredCallError(RuntimeError):
    """The model could not produce output matching the requested schema."""


def structured_call(client, model: str, content, output_model, max_tokens: int = 1024):
    """Call Claude and return a validated instance of `output_model`.

    Uses structured outputs (`output_config.format`) so the response is JSON
    conforming to the model's schema — no prose parsing, and no forced tool use,
    which newer models reject. Transient API errors are retried with exponential
    backoff; responses that violate the schema are retried by showing the model
    its own validation errors.

    Args:
        client: An Anthropic client.
        model: Model id to call.
        content: A prompt string, or a list of content blocks (e.g. image + text).
        output_model: A pydantic BaseModel subclass describing the expected output.
        max_tokens: Token ceiling for the response.

    Returns:
        An instance of `output_model`.

    Raises:
        StructuredCallError: The model never produced schema-conforming output.
    """
    schema = _strict_schema(output_model.model_json_schema())
    description = (output_model.__doc__ or "").strip()
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if description:
        # The schema's docstring used to ride along as the tool description;
        # with structured outputs it goes in the prompt instead.
        content = list(content) + [{"type": "text", "text": f"Respond with: {description}"}]

    messages = [{"role": "user", "content": content}]
    api_attempt = 0
    schema_attempts = 0

    while True:
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                # Sent via extra_body so this works on every SDK version that
                # callers have pinned (>=0.39), not only those that type-check
                # `output_config`. The API reads the same body field either way.
                extra_body={
                    "output_config": {"format": {"type": "json_schema", "schema": schema}}
                },
            )
        except TRANSIENT_API_ERRORS as e:
            if api_attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[api_attempt]
            print(f"API error on attempt {api_attempt + 1}: {type(e).__name__}. Retrying in {delay}s...")
            time.sleep(delay)
            api_attempt += 1
            continue

        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return output_model.model_validate_json(text)
        except ValidationError as e:
            schema_attempts += 1
            if schema_attempts >= MAX_SCHEMA_ATTEMPTS:
                raise StructuredCallError(
                    f"{output_model.__name__} validation failed after {schema_attempts} attempts: {e}"
                ) from e
            messages = messages + [
                {"role": "assistant", "content": message.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Validation failed:\n{e}\nRespond again with corrected values.",
                        }
                    ],
                },
            ]


class Result(BaseModel):
    """Base for outcome-carrying returns. Truthy on success; .error explains failure."""

    ok: bool
    error: Optional[str] = None

    def __bool__(self):
        return self.ok

    def __repr__(self):
        if self.ok:
            return f"{type(self).__name__}(ok=True)"
        return f"{type(self).__name__}(ok=False): {self.error}"

    __str__ = __repr__


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class Check(Result):
    """Whether the screen matches the expected description."""

    ok: bool = Field(description="True only if ALL described elements are present and accurate.")
    error: Optional[str] = Field(
        default=None,
        description=(
            "If ok is false, one sentence naming what is wrong or missing. "
            "Null if ok is true."
        ),
    )
    # Filled in locally after the call — never part of the schema the model sees.
    _screenshot: Optional[str] = PrivateAttr(default=None)

    @property
    def screenshot(self) -> Optional[str]:
        """Path to the screenshot this check ran against, if it was kept."""
        return self._screenshot

    def __repr__(self):
        return Result.__repr__(self)

    __str__ = __repr__


class Element(BaseModel):
    """The location of a UI element within the accessibility hierarchy."""

    found: bool = Field(description="Whether an element matching the description was found.")
    bounds: Optional[str] = Field(
        default=None,
        description='Copied EXACTLY from the hierarchy, in the form "[x1,y1][x2,y2]". Required when found is true.',
    )
    explanation: str = Field(description="One sentence on why this element matches, or why none does.")
    resource_id: Optional[str] = Field(default=None, description="The matched element's resource_id, if it has one.")
    content_desc: Optional[str] = Field(default=None, description="The matched element's content_desc, if it has one.")
    text: Optional[str] = Field(default=None, description="The matched element's text, if it has any.")

    @model_validator(mode="after")
    def _check_bounds(self):
        if self.found and not self.bounds:
            raise ValueError("bounds is required when found is true")
        if self.bounds and not BOUNDS_RE.fullmatch(self.bounds):
            raise ValueError(f'bounds must match "[x1,y1][x2,y2]", got {self.bounds!r}')
        return self

    def box(self) -> Tuple[int, int, int, int]:
        """Return (x1, y1, x2, y2)."""
        return tuple(int(g) for g in BOUNDS_RE.fullmatch(self.bounds).groups())

    def center(self) -> Tuple[int, int]:
        """Return the center point of the element as (x, y)."""
        x1, y1, x2, y2 = self.box()
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def __repr__(self):
        if not self.found:
            return f"Element(found=False): {self.explanation}"
        return f"Element({self.bounds}, resource_id={self.resource_id!r})"


class ActResult(Result):
    """Outcome of an act() — carries the element that was acted on."""

    element: Optional[Element] = None


class PixelResult(Result):
    """Outcome of waiting on a pixel."""

    initial_color: Optional[str] = None
    final_color: Optional[str] = None


class LogcatResult(Result):
    """Outcome of waiting on a logcat line."""

    line: Optional[str] = None


def _to_rgb(color) -> Tuple[int, int, int]:
    """Accept '#rrggbb' or an (r, g, b) tuple and return an RGB tuple."""
    if isinstance(color, str):
        color = color.lstrip("#")
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    return tuple(color)


def _to_hex(rgb) -> str:
    """Format an RGB tuple as '#rrggbb'."""
    return "#{:02x}{:02x}{:02x}".format(*rgb)


class Tick:
    """One poll cycle. Captures the screen at most once, however many conditions ask."""

    def __init__(self, tester):
        self.tester = tester
        self._screenshot = None

    def screenshot(self) -> str:
        """Path to this cycle's screenshot, captured lazily and shared across conditions."""
        if self._screenshot is None:
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            self.tester.screenshot(path)
            self._screenshot = path
        return self._screenshot

    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        """RGB of a pixel on this cycle's screenshot."""
        return self.tester._get_pixel_color(self.screenshot(), x, y)

    def release(self):
        """Delete this cycle's screenshot, if one was taken."""
        if self._screenshot and os.path.exists(self._screenshot):
            os.unlink(self._screenshot)
        self._screenshot = None


class Condition:
    """Something wait_for() can poll for.

    Subclasses implement poll() and describe(). start()/stop() are for conditions
    that need a live resource (see Logcat).
    """

    def start(self, tester):
        """Set up before the first poll."""

    def stop(self, tester):
        """Tear down after the last poll."""

    def poll(self, tick) -> Optional[Result]:
        """Return a truthy Result once satisfied, else None."""
        raise NotImplementedError

    def timeout(self, tick, seconds: float) -> Result:
        """Build the failure Result for a condition that never came true."""
        return Result(ok=False, error=f"Timeout after {seconds}s - {self.describe()}")

    def describe(self) -> str:
        """One phrase naming what was awaited, used in timeout errors."""
        raise NotImplementedError


class PixelColor(Condition):
    """Wait for a pixel to become one of the given colors (or, with exclude, any other).

    Example:
        tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]), timeout=30)
        tester.wait_for(PixelColor(300, 1380, "#ffffff", exclude=True))
    """

    def __init__(self, x: int, y: int, colors, exclude: bool = False):
        self.x, self.y = x, y
        raw = colors if isinstance(colors, list) else [colors]
        self.targets = [_to_rgb(c) for c in raw]
        self.exclude = exclude
        self.initial = None

    def _satisfied(self, rgb) -> bool:
        hit = any(rgb == t for t in self.targets)
        return not hit if self.exclude else hit

    def poll(self, tick):
        rgb = tick.pixel(self.x, self.y)
        if self.initial is None:
            self.initial = _to_hex(rgb)
        if self._satisfied(rgb):
            return PixelResult(ok=True, initial_color=self.initial, final_color=_to_hex(rgb))
        return None

    def timeout(self, tick, seconds):
        final = _to_hex(tick.pixel(self.x, self.y))
        return PixelResult(
            ok=False,
            initial_color=self.initial,
            final_color=final,
            error=f"Timeout after {seconds}s - pixel at ({self.x}, {self.y}) is {final}, expected {self.describe()}",
        )

    def describe(self) -> str:
        wanted = ", ".join(_to_hex(t) for t in self.targets)
        return f"anything except {wanted}" if self.exclude else wanted


class PixelChanged(Condition):
    """Wait for a pixel to become any color other than the one it started as.

    Example:
        tester.wait_for(PixelChanged(500, 300), timeout=5)
    """

    def __init__(self, x: int, y: int):
        self.x, self.y = x, y
        self.initial_rgb = None

    def poll(self, tick):
        rgb = tick.pixel(self.x, self.y)
        if self.initial_rgb is None:
            self.initial_rgb = rgb
            return None
        if rgb != self.initial_rgb:
            return PixelResult(ok=True, initial_color=_to_hex(self.initial_rgb), final_color=_to_hex(rgb))
        return None

    def timeout(self, tick, seconds):
        initial = _to_hex(self.initial_rgb) if self.initial_rgb else None
        return PixelResult(
            ok=False,
            initial_color=initial,
            final_color=initial,
            error=f"Timeout after {seconds}s - pixel at ({self.x}, {self.y}) remained {initial}",
        )

    def describe(self) -> str:
        return f"pixel at ({self.x}, {self.y}) to change"


class Logcat(Condition):
    """Wait for a logcat line under `tag` containing `message`.

    Example:
        tester.wait_for(Logcat("MessageDraftOverlay", "created"), timeout=30)
    """

    def __init__(self, tag: str, message: str, clear_first: bool = True):
        self.tag = tag
        self.message = message
        self.clear_first = clear_first
        self._proc = None
        self._matched = [None]

    def start(self, tester):
        adb_cmd = tester._get_adb_command()
        if self.clear_first:
            subprocess.run(adb_cmd + ["logcat", "-c"], capture_output=True)
        self._proc = subprocess.Popen(
            adb_cmd + ["logcat", "-v", "brief", f"{self.tag}:D", "*:S"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

        def reader():
            for line in self._proc.stdout:
                if self.message in line:
                    self._matched[0] = line.strip()
                    return

        threading.Thread(target=reader, daemon=True).start()

    def poll(self, tick):
        if self._matched[0] is not None:
            return LogcatResult(ok=True, line=self._matched[0])
        return None

    def timeout(self, tick, seconds):
        return LogcatResult(ok=False, error=f"Timeout after {seconds}s - {self.describe()}")

    def stop(self, tester):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()
            self._proc = None

    def describe(self) -> str:
        return f'never saw "{self.message}" in tag {self.tag}'


class AnyOf(Condition):
    """Wait for whichever of several conditions is satisfied first.

    All branches share one screenshot and one hierarchy dump per poll.

    Example:
        tester.wait_for(AnyOf(Logcat("Overlay", "created"), PixelChanged(300, 1380)))
    """

    def __init__(self, *conditions):
        if not conditions:
            raise ValueError("AnyOf needs at least one condition")
        self.conditions = conditions

    def start(self, tester):
        for c in self.conditions:
            c.start(tester)

    def stop(self, tester):
        for c in self.conditions:
            c.stop(tester)

    def poll(self, tick):
        for c in self.conditions:
            result = c.poll(tick)
            if result:
                return result
        return None

    def timeout(self, tick, seconds):
        return Result(ok=False, error=f"Timeout after {seconds}s - none of: {self.describe()}")

    def describe(self) -> str:
        return "; ".join(c.describe() for c in self.conditions)


class AndroidAccessibilityTester:
    """Main class for Android accessibility testing with screenshot assertions."""

    # Model used for screenshot assertions when the caller doesn't pass one explicitly.
    # Resolution priority (see _resolve_default_model): SCREEN_AGENT_MODEL env override ->
    # newest Sonnet via the Anthropic Models API -> this pinned fallback (offline / API failure).
    DEFAULT_MODEL_FALLBACK = "claude-sonnet-4-6"
    _resolved_default_model = None  # cached across instances so the Models API is queried once

    def __init__(self, device_id: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None):
        """
        Initialize the tester.

        Args:
            device_id: Specific Android device ID. If None, uses the first available device.
            model: Claude model to use for assertions. If None, resolves the newest Sonnet via the
                Anthropic Models API (override with the SCREEN_AGENT_MODEL env var; falls back to
                "claude-sonnet-4-6" if the API can't be reached).
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY environment variable.
        """
        self.device_id = device_id
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.default_model = model or self._resolve_default_model()
        # The most recent check(), so failure artifacts can reuse the exact screenshot
        # the model judged rather than a fresh capture taken after the screen moved on.
        self.last_check = None
        self._verify_adb()

    def _resolve_default_model(self) -> str:
        """Resolve the default assertion model when the caller passes model=None.

        Priority: SCREEN_AGENT_MODEL env override -> newest Sonnet via the Models API ->
        pinned fallback. The result is cached on the class so the Models API is hit at most once
        per process, even though a fresh tester is created for every test.
        """
        if AndroidAccessibilityTester._resolved_default_model:
            return AndroidAccessibilityTester._resolved_default_model

        resolved = os.environ.get("SCREEN_AGENT_MODEL")
        if resolved:
            print(f"📌 android_accessibility_tester: model from SCREEN_AGENT_MODEL: {resolved}")
        else:
            try:
                sonnets = [m for m in self.client.models.list() if "sonnet" in m.id.lower()]
                resolved = max(sonnets, key=lambda m: m.created_at).id
                print(f"🔎 android_accessibility_tester: resolved newest Sonnet: {resolved}")
            except Exception as e:
                resolved = self.DEFAULT_MODEL_FALLBACK
                print(f"⚠️  android_accessibility_tester: Models API lookup failed ({e}); using {resolved}")

        AndroidAccessibilityTester._resolved_default_model = resolved
        return resolved

    def _verify_adb(self):
        """Verify ADB is installed and accessible."""
        try:
            subprocess.run(["adb", "version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("ADB not found. Please install Android SDK Platform Tools.")

    def _get_adb_command(self) -> List[str]:
        """Get base ADB command with device specification."""
        cmd = ["adb"]
        if self.device_id:
            cmd.extend(["-s", self.device_id])
        return cmd

    def shell(self, command: str) -> str:
        """
        Execute an ADB shell command.

        Args:
            command: Shell command to execute

        Returns:
            Command output as string
        """
        cmd = self._get_adb_command() + ["shell", command]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    @timing
    def screenshot(self, output_path: str) -> str:
        """
        Capture a screenshot from the Android device.

        Args:
            output_path: Local path to save the screenshot

        Returns:
            Path to the saved screenshot
        """
        # Take screenshot on device
        device_path = "/sdcard/screenshot.png"
        self.shell(f"screencap -p {device_path}")

        # Pull screenshot to local machine
        cmd = self._get_adb_command() + ["pull", device_path, output_path]
        subprocess.run(cmd, capture_output=True, check=True)

        # Clean up device screenshot (ignore failure — scoped storage may block rm)
        try:
            self.shell(f"rm {device_path}")
        except subprocess.CalledProcessError:
            pass

        return output_path

    def _structured_call(self, content, output_model, model=None, max_tokens=1024):
        """Call Claude for a schema-validated result using this tester's client and model."""
        return structured_call(
            self.client, model or self.default_model, content, output_model, max_tokens=max_tokens
        )

    def _image_block(self, screenshot_path: str) -> dict:
        """Read a screenshot into a base64 image content block."""
        with open(screenshot_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": image_data},
        }

    @timing
    def check(self, description: str, screenshot_path: Optional[str] = None,
              model: Optional[str] = None) -> Check:
        """Check that the current screen matches a described expectation.

        Captures a screenshot itself. When the check passes and no explicit path was
        requested, the screenshot is discarded; when it fails, the file is kept and
        exposed as `.screenshot` so the failure can be inspected.

        Args:
            description: What should be on screen. Be specific — the check only passes
                if ALL described elements are present.
            screenshot_path: Where to save the screenshot. If None, a temp file is used
                and kept only on failure.
            model: Claude model override. Defaults to this tester's model.

        Returns:
            Check — truthy when the screen matches; `.error` holds one sentence explaining
            the mismatch otherwise.

        Example:
            assert tester.check("the clock app is showing with an Alarms tab at the bottom")
        """
        keep = screenshot_path is not None
        path = screenshot_path
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)

        self.screenshot(path)
        result = self._structured_call(
            [
                self._image_block(path),
                {
                    "type": "text",
                    "text": (
                        "You are analyzing a screenshot from an Android application "
                        "accessibility test.\n\nDescription of what should be in the "
                        f"screenshot:\n{description}\n\nDoes the screenshot match? Set ok "
                        "to true only if ALL key elements described are present and the "
                        "description is completely accurate."
                    ),
                },
            ],
            Check,
            model=model,
            max_tokens=300,
        )

        if keep or not result.ok:
            result._screenshot = path
        else:
            os.unlink(path)
        self.last_check = result
        return result

    @timing
    def extract(self, description: str, schema, model: Optional[str] = None):
        """Extract structured data from the current screen into `schema`.

        Sends both a screenshot and the accessibility hierarchy, so the model can read
        values that appear in either.

        Args:
            description: What to extract, in natural language.
            schema: A pydantic BaseModel subclass describing the shape you want back.
            model: Claude model override. Defaults to this tester's model.

        Returns:
            An instance of `schema`.

        Example:
            class TimerState(BaseModel):
                '''The state of the on-screen timer.'''
                remaining_seconds: int
                is_running: bool

            state = tester.extract("the state of the running timer", TimerState)
            assert state.remaining_seconds > 0
        """
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            self.screenshot(path)
            return self._structured_call(
                [
                    self._image_block(path),
                    {
                        "type": "text",
                        "text": (
                            "You are reading data off an Android screen. Extract the "
                            f"following from the screenshot above:\n{description}"
                        ),
                    },
                ],
                schema,
                model=model,
                max_tokens=1024,
            )
        finally:
            if os.path.exists(path):
                os.unlink(path)

    @timing
    def observe(self, description: str, model: Optional[str] = None) -> Optional[Element]:
        """Locate an element in the accessibility hierarchy, without acting on it.

        The returned Element carries selectors (resource_id, content_desc, text) as well
        as bounds, so it can be cached by the caller and replayed later without another
        model call.

        Args:
            description: Description of the element to find.
            model: Claude model override. Defaults to this tester's model.

        Returns:
            Element if found, None otherwise.

        Example:
            start = tester.observe("the Start Timer button")
            if start:
                tester.tap(*start.center())
        """
        element = self._observe_element(description, model=model)
        return element if element.found else None

    def _observe_element(self, description: str, model: Optional[str] = None) -> Element:
        """Locate an element, keeping the model's explanation even when nothing matched.

        Reads the screen visually. Nothing here dumps a UI hierarchy, so the
        accessibility services of the app under test are left alone.
        """
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            self.screenshot(path)
            return self._structured_call(
                [
                    self._image_block(path),
                    {
                        "type": "text",
                        "text": (
                            "Find a specific element in this Android screenshot.\n\n"
                            f"Element description: {description}\n\n"
                            "Report its bounds as pixel coordinates in the screenshot, in "
                            "the form [x1,y1][x2,y2]. Be precise: the centre of these "
                            "bounds is where the tap will land."
                        ),
                    },
                ],
                Element,
                model=model,
                max_tokens=500,
            )
        finally:
            if os.path.exists(path):
                os.unlink(path)


    def list_devices(self) -> List[str]:
        """
        List all connected Android devices.

        Returns:
            List of device IDs
        """
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True)
        lines = result.stdout.strip().split("\n")[1:]  # Skip header
        devices = [line.split()[0] for line in lines if line.strip() and "device" in line]
        return devices

    @timing
    def tap(self, x: int, y: int):
        """
        Simulate a tap at the given coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
        """
        self.shell(f"input tap {x} {y}")

    def long_press(self, x: int, y: int, duration_ms: int = 1000):
        """
        Simulate a long press at the given coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            duration_ms: Duration of long press in milliseconds (default: 1000ms = 1 second)
        """
        self.shell(f"input swipe {x} {y} {x} {y} {duration_ms}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 1000):
        """
        Simulate a swipe gesture.

        Args:
            x1: Start X coordinate
            y1: Start Y coordinate
            x2: End X coordinate
            y2: End Y coordinate
            duration_ms: Duration of swipe in milliseconds (default: 1000ms = 1 second)
        """
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    @timing
    def input_text(self, text: str):
        """
        Input text into the focused field.

        Args:
            text: Text to input (spaces will be replaced with %s)
        """
        # ADB shell input text requires spaces to be encoded
        # Also escape special shell characters
        escaped_text = text.replace(" ", "%s")
        escaped_text = escaped_text.replace("(", "\\(")
        escaped_text = escaped_text.replace(")", "\\)")
        self.shell(f"input text {escaped_text}")

    def press_key(self, keycode: str):
        """
        Press a key by keycode.

        Args:
            keycode: Android keycode (e.g., "KEYCODE_BACK", "KEYCODE_HOME")
        """
        self.shell(f"input keyevent {keycode}")

    def open_app(self, package_name: str):
        """
        Open an app by package name.

        Args:
            package_name: Package name of the app (e.g., "com.android.settings")
        """
        self.shell(f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1")

    def press_back(self):
        """Press the Android back button."""
        self.press_key("KEYCODE_BACK")

    def _get_pixel_color(self, screenshot_path: str, x: int, y: int) -> Tuple[int, int, int]:
        """
        Get the RGB color of a pixel at the given coordinates.

        Args:
            screenshot_path: Path to the screenshot file
            x: X coordinate of the pixel
            y: Y coordinate of the pixel

        Returns:
            Tuple of (R, G, B) values

        Raises:
            RuntimeError: If PIL is not installed
            ValueError: If coordinates are out of bounds
        """
        if Image is None:
            raise RuntimeError("PIL/Pillow is required for pixel operations. Install with: pip install Pillow")

        with Image.open(screenshot_path) as img:
            # Verify coordinates are within bounds
            width, height = img.size
            if x < 0 or x >= width or y < 0 or y >= height:
                raise ValueError(f"Coordinates ({x}, {y}) out of bounds. Image size: {width}x{height}")

            # Get pixel color
            rgb_img = img.convert('RGB')
            return rgb_img.getpixel((x, y))

    def capture(self) -> "Capture":
        """Capture the current screen, for comparison against a later capture.

        Costs no model call and does not disturb the app under test. Call it purely to
        establish a baseline and ignore the result, or hold it and compare later.

        Returns:
            Capture, with `.screenshot` (path). Call `.release()` to delete the file.

        Example:
            before = tester.capture()
            tester.act("tap the send button")
            after = tester.capture()
        """
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.screenshot(path)
        return Capture(screenshot=path)

    @timing
    def act(self, target, model: Optional[str] = None) -> ActResult:
        """Tap an element, described in natural language or replayed from a cached Element.

        Given a string, the element is located with observe() — one model call. Given an
        Element from an earlier observe(), it is re-resolved against the current hierarchy
        with no model call at all, falling back to observe() only if its selectors are gone.

        Args:
            target: A natural-language description, or an Element to replay.
            model: Claude model override, used only when the model is consulted.

        Returns:
            ActResult — truthy on success, with `.element` holding what was tapped.

        Example:
            assert tester.act("tap the Start Timer button")

            start = tester.observe("the Start Timer button")   # once
            for _ in range(10):
                tester.act(start)                              # no model calls
        """
        if isinstance(target, Element):
            # A cached element replays its own coordinates: no model call, no screenshot,
            # and nothing that could disturb the app under test.
            self.tap(*target.center())
            return ActResult(ok=True, element=target)

        element = self._observe_element(target, model=model)
        description = target

        if not element.found:
            return ActResult(
                ok=False,
                error=f"Could not find an element matching {description!r}: {element.explanation}",
            )

        self.tap(*element.center())
        return ActResult(ok=True, element=element)

    def wait_for(self, condition, timeout: float = 10.0, poll_interval: float = 0.5) -> Result:
        """Wait for a condition to be satisfied.

        Polls until the condition comes true or `timeout` elapses. The condition is
        checked once before the first sleep, so an already-satisfied condition returns
        immediately. Each poll captures the screen at most once, no matter how many
        conditions look at it.

        Args:
            condition: A Condition — PixelColor, PixelChanged, Logcat, or AnyOf(...)
                of those.
            timeout: Maximum time to wait, in seconds.
            poll_interval: Time between polls, in seconds.

        Returns:
            Result — truthy when satisfied; `.error` explains the timeout otherwise.
            Pixel and logcat conditions return richer subclasses (PixelResult,
            LogcatResult) carrying their diagnostics.

        Example:
            assert tester.wait_for(Logcat("MyService", "upload finished"), timeout=30)

            result = tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]))
            assert result, result.error
        """
        condition.start(self)
        try:
            return self._poll_until(condition, timeout, poll_interval)
        finally:
            condition.stop(self)

    def expect(self, condition, timeout: float = 10.0, poll_interval: float = 0.5) -> "Expectation":
        """Arm a condition before an action, then wait for it after.

        Use this instead of act-then-wait_for when the action itself produces the
        signal. `Logcat` clears the log buffer when it starts listening, so if the
        app logs the line before `wait_for` is reached, the line is wiped and the
        wait times out. `expect` starts listening on entry, runs the body, and waits
        on exit, so anything the body triggers is guaranteed to be seen while
        anything older is still cleared.

        If the body raises, the condition is torn down without waiting and the
        exception propagates.

        Returns:
            Expectation — truthy after the block if the condition was met; `.error`
            explains a timeout. Fields of the underlying Result (e.g. `.line`) are
            available on it directly.

        Example:
            with tester.expect(Logcat("MyService", "overlay created"), timeout=30) as done:
                tester.act("tap the Start button")
            assert done
        """
        return Expectation(self, condition, timeout, poll_interval)

    def _poll_until(self, condition, timeout: float, poll_interval: float) -> Result:
        """Poll an already-started condition until it fires or `timeout` elapses."""
        started = time.time()
        while True:
            tick = Tick(self)
            try:
                result = condition.poll(tick)
                if result:
                    return result
                if time.time() - started >= timeout:
                    return condition.timeout(tick, timeout)
            finally:
                tick.release()
            time.sleep(poll_interval)


    def save_debug_artifacts(self, output_dir: str, test_name: str, step_name: str,
                             screenshot: bool = True, ui_dump: bool = True,
                             existing_screenshot: Optional[str] = None) -> dict:
        """Save screenshot and/or UI dump as debug artifacts.

        Saves a screenshot and UI hierarchy dump with timestamped filenames for
        post-mortem debugging. If `existing_screenshot` is provided and readable,
        that file is copied instead of capturing a new one — use this when the
        failure was detected against a specific screenshot (e.g. a
        check() failure) so the saved artifact matches what the
        validator actually saw. Otherwise a fresh screenshot is captured.

        Args:
            output_dir: Directory to save artifacts (created if it doesn't exist)
            test_name: Name of the test (used in filename)
            step_name: Name of the step (used in filename)
            screenshot: Whether to capture and save a screenshot (default True)
            ui_dump: Whether to capture and save a UI dump (default True)
            existing_screenshot: Optional path to an already-captured screenshot
                to preserve. If set and the file exists, it is copied; otherwise
                a fresh screenshot is taken.

        Returns:
            Dict with saved file paths: {"screenshot": path_or_None, "ui_dump": path_or_None}
        """
        import shutil
        from datetime import datetime

        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result = {"screenshot": None, "ui_dump": None}

        if screenshot:
            try:
                dest = os.path.join(output_dir, f"{test_name}_{step_name}_{timestamp}.png")
                if existing_screenshot and os.path.exists(existing_screenshot):
                    shutil.copy(existing_screenshot, dest)
                    print(f"Saved screenshot to: {dest} (copied from {existing_screenshot})")
                else:
                    tmp_path = "/tmp/debug_screenshot.png"
                    self.screenshot(tmp_path)
                    shutil.copy(tmp_path, dest)
                    print(f"Saved screenshot to: {dest}")
                result["screenshot"] = dest
            except Exception as e:
                print(f"Failed to save screenshot: {e}")

        if ui_dump:
            try:
                device_dump_path = "/sdcard/window_dump.xml"
                self.shell(f"uiautomator dump {device_dump_path}")
                dest = os.path.join(output_dir, f"{test_name}_{step_name}_{timestamp}_uidump.xml")
                self._pull(device_dump_path, dest)
                print(f"Saved UI dump to: {dest}")
                result["ui_dump"] = dest
                self.shell(f"rm {device_dump_path}")
            except Exception as e:
                print(f"Failed to save UI dump: {e}")

        return result

    def _pull(self, remote_path: str, local_path: str):
        """Pull a file off the device, raising if adb fails."""
        cmd = self._get_adb_command() + ["pull", remote_path, local_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"adb pull {remote_path} failed: {result.stderr.strip()}")

class Expectation:
    """The handle returned by `tester.expect()`.

    Starts its condition on entry and waits for it on exit. Afterwards it is truthy
    if the condition was met, and forwards attribute access to the underlying Result
    so `.error`, `.line`, `.final_color` etc. read as they would from `wait_for()`.
    """

    def __init__(self, tester, condition, timeout: float, poll_interval: float):
        self._tester = tester
        self._condition = condition
        self._timeout = timeout
        self._poll_interval = poll_interval
        self.result: Optional[Result] = None

    def __enter__(self):
        self._condition.start(self._tester)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.result = self._tester._poll_until(
                    self._condition, self._timeout, self._poll_interval
                )
        finally:
            self._condition.stop(self._tester)
        return False

    def __bool__(self):
        return bool(self.result)

    def __getattr__(self, name):
        # Only called when normal lookup fails, so the Expectation's own attributes win.
        result = self.__dict__.get("result")
        if result is None:
            raise AttributeError(
                f"{name!r} is not available until the `with tester.expect(...)` block has exited"
            )
        return getattr(result, name)

    def __repr__(self):
        if self.result is None:
            return f"Expectation(pending: {self._condition.describe()})"
        return f"Expectation({self.result!r})"


class Capture:
    """A screenshot of the device at one moment, held for later comparison.

    Costs no model call and does not disturb the app under test. Hold one to compare
    against later, or several at once.
    """

    def __init__(self, screenshot: Optional[str] = None):
        self.screenshot = screenshot

    def release(self):
        """Delete this capture's screenshot from disk, if it took one."""
        if self.screenshot and os.path.exists(self.screenshot):
            os.unlink(self.screenshot)
        self.screenshot = None

    def __repr__(self):
        return f"Capture(screenshot={self.screenshot!r})"


DEFAULT_ARTIFACT_DIR = "test-failures"


def capture_failure(tester, test_name: str, output_dir: Optional[str] = None,
                    step_name: str = "failure") -> dict:
    """Save post-mortem artifacts for a failed test: a screenshot and a hierarchy dump.

    The hierarchy dump is deliberately confined to this path. `uiautomator dump` runs
    through UiAutomation, which suppresses the accessibility services of the app under
    test — so it must never run while a test is still executing. Once a test has failed
    there is nothing left to disturb, and the dump is the richest diagnostic available.

    If the last check() failed, its screenshot is reused, so the artifact is what the
    model actually judged rather than a fresh capture of a screen that has since moved on.

    Args:
        tester: The AndroidAccessibilityTester used by the failing test.
        test_name: Name of the failing test, used in the artifact filenames.
        output_dir: Where to write artifacts. Defaults to $ANDROID_TEST_ARTIFACTS,
            then "test-failures".
        step_name: Label for this capture within the test.

    Returns:
        Dict of saved paths: {"screenshot": path_or_None, "ui_dump": path_or_None}
    """
    output_dir = output_dir or os.environ.get("ANDROID_TEST_ARTIFACTS", DEFAULT_ARTIFACT_DIR)
    last = getattr(tester, "last_check", None)
    return tester.save_debug_artifacts(
        output_dir=output_dir,
        test_name=test_name,
        step_name=step_name,
        existing_screenshot=last.screenshot if last else None,
    )


def find_tester(funcargs: dict):
    """Return the AndroidAccessibilityTester among a test's fixtures, if there is one.

    Matches by type rather than by fixture name, so the failure hook works whatever
    the fixture is called.
    """
    for value in funcargs.values():
        if isinstance(value, AndroidAccessibilityTester):
            return value
    return None


def failure_artifact_hook(item, call):
    """pytest hook body: save artifacts when a test body raises.

    Wire it up from a conftest.py:

        from android_accessibility_tester import failure_artifact_hook

        def pytest_runtest_makereport(item, call):
            failure_artifact_hook(item, call)
    """
    if call.when != "call" or call.excinfo is None:
        return

    tester = find_tester(getattr(item, "funcargs", {}) or {})
    if tester is None:
        return

    try:
        artifacts = capture_failure(tester, item.name)
    except Exception as e:
        # Never let artifact capture mask the real test failure.
        print(f"Could not capture failure artifacts: {e}")
        return

    saved = [p for p in artifacts.values() if p]
    if saved:
        print("\nFailure artifacts:\n  " + "\n  ".join(saved))
