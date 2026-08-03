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
import xml.etree.ElementTree as ET
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

TOOL_NAME = "record_result"


class StructuredCallError(RuntimeError):
    """The model could not produce output matching the requested schema."""


def structured_call(client, model: str, content, output_model, max_tokens: int = 1024):
    """Call Claude and return a validated instance of `output_model`.

    Uses forced tool use so the response is JSON conforming to the model's schema —
    no prose parsing. Transient API errors are retried with exponential backoff;
    responses that violate the schema are retried by showing the model its own
    validation errors.

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
    tool = {
        "name": TOOL_NAME,
        "description": (output_model.__doc__ or "Record the result.").strip(),
        "input_schema": output_model.model_json_schema(),
    }
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]

    messages = [{"role": "user", "content": content}]
    api_attempt = 0
    schema_attempts = 0

    while True:
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                tools=[tool],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=messages,
            )
        except TRANSIENT_API_ERRORS as e:
            if api_attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[api_attempt]
            print(f"API error on attempt {api_attempt + 1}: {type(e).__name__}. Retrying in {delay}s...")
            time.sleep(delay)
            api_attempt += 1
            continue

        block = next(b for b in message.content if b.type == "tool_use")
        try:
            return output_model.model_validate(block.input)
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
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": f"Validation failed:\n{e}\nCall the tool again with corrected values.",
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

    def hierarchy(self) -> str:
        """This cycle's UI hierarchy XML."""
        return self.tester._get_ui_hierarchy()

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


class _ElementCondition(Condition):
    """Shared matching for ElementPresent / ElementGone."""

    def __init__(self, text=None, resource_id=None, content_desc=None):
        if not any([text, resource_id, content_desc]):
            raise ValueError("Provide at least one of text, resource_id or content_desc")
        self.text = text
        self.resource_id = resource_id
        self.content_desc = content_desc

    def _present(self, tick) -> bool:
        root = ET.fromstring(tick.hierarchy())
        for node in root.iter():
            if self.text and node.get("text") == self.text:
                return True
            if self.content_desc and node.get("content-desc") == self.content_desc:
                return True
            if self.resource_id:
                rid = node.get("resource-id") or ""
                if rid == self.resource_id or rid.split("/")[-1] == self.resource_id:
                    return True
        return False

    def describe(self) -> str:
        parts = [f"{k}={v!r}" for k, v in (
            ("text", self.text), ("resource_id", self.resource_id), ("content_desc", self.content_desc)
        ) if v]
        return f"element {' '.join(parts)}"


class ElementPresent(_ElementCondition):
    """Wait for an element to appear in the accessibility hierarchy.

    Example:
        assert tester.wait_for(ElementPresent(resource_id="start_button"), timeout=5)
    """

    def poll(self, tick):
        return Result(ok=True) if self._present(tick) else None

    def describe(self) -> str:
        return f"{super().describe()} to appear"


class ElementGone(_ElementCondition):
    """Wait for an element to disappear from the accessibility hierarchy.

    Example:
        assert tester.wait_for(ElementGone(resource_id="loading_spinner"), timeout=30)
    """

    def poll(self, tick):
        return Result(ok=True) if not self._present(tick) else None

    def describe(self) -> str:
        return f"{super().describe()} to disappear"


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

    def _hierarchy_json(self, max_depth: int = 5) -> str:
        """Dump the UI hierarchy and simplify it into JSON for the model."""
        root = ET.fromstring(self._get_ui_hierarchy())
        return json.dumps(self._simplify_node_for_llm(root, max_depth=max_depth), indent=2)

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
                            f"following:\n{description}\n\nUse both the screenshot above and "
                            "this accessibility hierarchy:\n"
                            f"{self._hierarchy_json()}"
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
        """Locate an element, keeping the model's explanation even when nothing matched."""
        return self._structured_call(
            (
                "You are analyzing an Android UI accessibility hierarchy to find a "
                f"specific element.\n\nElement description: {description}\n\n"
                f"UI Hierarchy (simplified JSON):\n{self._hierarchy_json()}\n\n"
                "Find the element that best matches the description. Copy its bounds "
                "EXACTLY from the hierarchy."
            ),
            Element,
            model=model,
            max_tokens=400,
        )


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

    def resolve(self, element: Element) -> Optional[Element]:
        """Re-find a previously observed element in the current hierarchy, without the model.

        Matches on the element's selectors (resource_id, then content_desc, then text) and
        returns it with fresh bounds, so a cached element still taps the right place after a
        layout shift. Returns None if no selector matches — the caller can then fall back to
        observe().

        Args:
            element: An Element from a previous observe().

        Returns:
            The same element with current bounds, or None if it is no longer on screen.
        """
        root = ET.fromstring(self._get_ui_hierarchy())
        for node in root.iter():
            rid = node.get("resource-id") or ""
            matched = (
                (element.resource_id and (rid == element.resource_id
                                          or rid.split("/")[-1] == element.resource_id))
                or (element.content_desc and node.get("content-desc") == element.content_desc)
                or (element.text and node.get("text") == element.text)
            )
            if matched and node.get("bounds"):
                return element.model_copy(update={"bounds": node.get("bounds")})
        return None

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
            element = self.resolve(target) or self._observe_element(target.explanation, model=model)
            description = target.explanation
        else:
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
            condition: A Condition — PixelColor, PixelChanged, ElementPresent,
                ElementGone, Logcat, or AnyOf(...) of those.
            timeout: Maximum time to wait, in seconds.
            poll_interval: Time between polls, in seconds.

        Returns:
            Result — truthy when satisfied; `.error` explains the timeout otherwise.
            Pixel and logcat conditions return richer subclasses (PixelResult,
            LogcatResult) carrying their diagnostics.

        Example:
            assert tester.wait_for(ElementGone(resource_id="loading_spinner"), timeout=30)

            result = tester.wait_for(PixelColor(300, 1380, ["#fffad0", "#d2cea4"]))
            assert result, result.error
        """
        condition.start(self)
        started = time.time()
        try:
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
        finally:
            condition.stop(self)


    def _get_ui_hierarchy(self) -> str:
        """
        Get the UI hierarchy XML from the device.

        Returns:
            XML string of the UI hierarchy
        """
        # Dump UI hierarchy to device
        device_path = "/sdcard/ui_hierarchy.xml"
        self.shell(f"uiautomator dump {device_path}")

        # Pull XML to local machine
        local_path = "/tmp/ui_hierarchy.xml"
        cmd = self._get_adb_command() + ["pull", device_path, local_path]
        subprocess.run(cmd, capture_output=True, check=True)

        # Clean up device file
        self.shell(f"rm {device_path}")

        # Read XML content
        with open(local_path, "r") as f:
            return f.read()

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
                cmd = self._get_adb_command() + ["pull", device_dump_path, dest]
                pull_result = subprocess.run(cmd, capture_output=True, text=True)
                if pull_result.returncode == 0:
                    print(f"Saved UI dump to: {dest}")
                    result["ui_dump"] = dest
                    self.shell(f"rm {device_dump_path}")
                else:
                    print(f"Failed to pull UI dump: {pull_result.stderr}")
            except Exception as e:
                print(f"Failed to save UI dump: {e}")

        return result

    def _simplify_node_for_llm(self, node: ET.Element, max_depth: int = 3, current_depth: int = 0) -> dict:
        """
        Simplify an XML node into a dict for LLM consumption.

        Args:
            node: XML element node
            max_depth: Maximum depth to traverse
            current_depth: Current traversal depth

        Returns:
            Simplified dict representation
        """
        result = {
            "class": node.get("class", "").split(".")[-1],  # Just the class name, not full package
            "text": node.get("text", ""),
            "content_desc": node.get("content-desc", ""),
            "resource_id": node.get("resource-id", "").split("/")[-1] if node.get("resource-id") else "",
            "bounds": node.get("bounds", ""),
            "clickable": node.get("clickable") == "true",
            "enabled": node.get("enabled") == "true",
        }

        # Only include non-empty fields
        result = {k: v for k, v in result.items() if v or k == "bounds"}

        # Add children if not at max depth
        if current_depth < max_depth:
            children = [self._simplify_node_for_llm(child, max_depth, current_depth + 1) for child in node]
            # Keep children with meaningful content — and plain containers whose own
            # descendants are meaningful, so nesting doesn't drop a subtree.
            children = [
                c for c in children
                if c.get("text") or c.get("content_desc") or c.get("clickable") or c.get("children")
            ]
            if children:
                result["children"] = children

        return result

