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
import xml.etree.ElementTree as ET
from typing import Optional, List, Tuple
from functools import wraps
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


class ValidationResult:
    """Result of a screenshot validation."""

    def __init__(self, result: bool, error: Optional[str] = None):
        """
        Initialize validation result.

        Args:
            result: Whether the validation passed
            error: Error message if validation failed, None otherwise
        """
        self.result = result
        self.error = error

    def __bool__(self):
        """Allow using the result directly in assertions."""
        return self.result

    def __repr__(self):
        """String representation of the result."""
        if self.result:
            return "ValidationResult(result=True)"
        return f"ValidationResult(result=False, error={self.error!r})"


class ElementCoordinates:
    """Coordinates of an element found on screen."""

    def __init__(self, x1: int, y1: int, x2: int, y2: int, method: str, confidence: Optional[str] = None):
        """
        Initialize element coordinates.

        Args:
            x1: Top-left x coordinate
            y1: Top-left y coordinate
            x2: Bottom-right x coordinate
            y2: Bottom-right y coordinate
            method: Method used to find coordinates ("accessibility" or "visual")
            confidence: Optional confidence/explanation from the method
        """
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.method = method
        self.confidence = confidence

    def center(self) -> Tuple[int, int]:
        """Get the center point of the element."""
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    def __repr__(self):
        """String representation of the coordinates."""
        return f"ElementCoordinates(({self.x1}, {self.y1}), ({self.x2}, {self.y2}), method={self.method!r})"


class AndroidAccessibilityTester:
    """Main class for Android accessibility testing with screenshot assertions."""

    def __init__(self, device_id: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None):
        """
        Initialize the tester.

        Args:
            device_id: Specific Android device ID. If None, uses the first available device.
            model: Claude model to use for assertions. If None, defaults to "claude-sonnet-4-20250514".
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY environment variable.
        """
        self.device_id = device_id
        self.default_model = model or "claude-sonnet-4-20250514"
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self._verify_adb()

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

    @timing
    def validate_screenshot(self, screenshot_path: str, description: str,
                           model: Optional[str] = None) -> ValidationResult:
        """
        Validate that a screenshot matches the given description using Claude LLM.

        Args:
            screenshot_path: Path to the screenshot file
            description: Description of what should be visible in the screenshot
            model: Claude model to use for validation. If None, uses the default model from initialization.

        Returns:
            ValidationResult object with .result (bool) and .error (str or None)
        """
        if model is None:
            model = self.default_model
        # Read and encode the screenshot
        with open(screenshot_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        # Create prompt for Claude
        prompt = f"""You are analyzing a screenshot from an Android application accessibility test.

Description of what should be in the screenshot:
{description}

Based on the screenshot, does it match this description?
Respond with ONLY a JSON object in this exact format:
{{"result": true, "error_message": null}}
or
{{"result": false, "error_message": "description of what is wrong or missing"}}

If ALL the key elements described are present and the description is completely accurate, set result to true and error_message to null.
If any key elements are missing or the description doesn't match, set result to false and provide a clear error_message explaining what is wrong."""

        # Call Claude API with exponential backoff retry
        retry_delays = [1, 2, 4, 8]  # Exponential backoff in seconds
        last_error = None

        for attempt, delay in enumerate(retry_delays + [None]):  # +[None] for one final attempt
            try:
                message = self.client.messages.create(
                    model=model,
                    max_tokens=200,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": image_data,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": prompt
                                }
                            ],
                        }
                    ],
                )

                # Parse response
                response_text = message.content[0].text.strip()

                # Strip markdown code blocks if present
                if response_text.startswith("```"):
                    # Remove opening ```json or ```
                    lines = response_text.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    # Remove closing ```
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    response_text = "\n".join(lines).strip()

                try:
                    response_data = json.loads(response_text)
                except json.JSONDecodeError:
                    # LLM sometimes adds explanatory text before/after JSON - extract it
                    json_match = re.search(r'\{[^{}]*"result"\s*:\s*(true|false)[^{}]*\}', response_text)
                    if json_match:
                        response_data = json.loads(json_match.group())
                    else:
                        raise ValueError(f"Could not extract JSON from response: {response_text[:200]}...")
                return ValidationResult(
                    result=response_data["result"],
                    error=response_data.get("error_message")
                )

            except (RateLimitError, InternalServerError, APITimeoutError, APIConnectionError) as e:
                last_error = e
                # If this was the last attempt, raise the error
                if delay is None:
                    break

                # Log retry attempt
                print(f"API error on attempt {attempt + 1}: {type(e).__name__}. Retrying in {delay}s...")
                time.sleep(delay)

        # If we exhausted all retries, raise the last error
        raise last_error

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

    def wait_for_pixel_change(self, x: int, y: int, timeout: float = 10.0,
                              poll_interval: float = 0.5) -> dict:
        """
        Wait for a pixel at the given coordinates to change color.

        This is useful for detecting when UI elements appear/disappear or change state
        without requiring accessibility services or UI hierarchy parsing.

        Args:
            x: X coordinate of the pixel to monitor
            y: Y coordinate of the pixel to monitor
            timeout: Maximum time to wait in seconds (default: 10.0)
            poll_interval: Time between checks in seconds (default: 0.5)

        Returns:
            Dictionary with:
                - 'changed': bool - Whether the pixel changed
                - 'initial_color': str - Hex color code of initial pixel (e.g., '#FF0000')
                - 'final_color': str - Hex color code of final pixel if changed
                - 'error': str - Error message if timeout occurred

        Example:
            # Wait for a pixel to change (e.g., loading indicator)
            result = tester.wait_for_pixel_change(500, 300, timeout=5.0)
            if result['changed']:
                print(f"Color changed from {result['initial_color']} to {result['final_color']}")
            else:
                print(f"Timeout: {result['error']}")
        """
        if Image is None:
            raise RuntimeError("PIL/Pillow is required for pixel operations. Install with: pip install Pillow")

        # Take initial screenshot
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            initial_screenshot = tmp.name

        try:
            self.screenshot(initial_screenshot)
            initial_color = self._get_pixel_color(initial_screenshot, x, y)
            initial_hex = f"#{initial_color[0]:02x}{initial_color[1]:02x}{initial_color[2]:02x}"

            start_time = time.time()

            while time.time() - start_time < timeout:
                time.sleep(poll_interval)

                # Take new screenshot
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    current_screenshot = tmp.name

                try:
                    self.screenshot(current_screenshot)
                    current_color = self._get_pixel_color(current_screenshot, x, y)

                    # Check if color changed
                    if current_color != initial_color:
                        final_hex = f"#{current_color[0]:02x}{current_color[1]:02x}{current_color[2]:02x}"
                        return {
                            'changed': True,
                            'initial_color': initial_hex,
                            'final_color': final_hex,
                            'error': None
                        }
                finally:
                    # Clean up current screenshot
                    if os.path.exists(current_screenshot):
                        os.unlink(current_screenshot)

            # Timeout occurred
            return {
                'changed': False,
                'initial_color': initial_hex,
                'final_color': None,
                'error': f'Timeout after {timeout}s - pixel at ({x}, {y}) remained {initial_hex}'
            }

        finally:
            # Clean up initial screenshot
            if os.path.exists(initial_screenshot):
                os.unlink(initial_screenshot)

    @timing
    def wait_for_pixel_color(self, x: int, y: int, target_color,
                              timeout: float = 10.0, poll_interval: float = 0.5,
                              exclude: bool = False) -> dict:
        """
        Wait for a pixel at the given coordinates to become a specific color or one of multiple acceptable colors.
        With exclude=True, waits for the pixel to become any color OTHER than the specified colors.

        This is useful for waiting for UI elements to reach a specific state
        (e.g., button turning green when enabled, or overlay appearing in light/dark mode).

        Args:
            x: X coordinate of the pixel to monitor
            y: Y coordinate of the pixel to monitor
            target_color: Target color as:
                - Single hex string (e.g., '#FF0000')
                - Single RGB tuple (255, 0, 0)
                - List of hex strings (e.g., ['#FF0000', '#00FF00'])
                - List of RGB tuples (e.g., [(255, 0, 0), (0, 255, 0)])
            timeout: Maximum time to wait in seconds (default: 10.0)
            poll_interval: Time between checks in seconds (default: 0.5)
            exclude: If True, wait for the pixel to NOT be any of the target colors (default: False)

        Returns:
            Dictionary with:
                - 'matched': bool - Whether the condition was met (color matched, or excluded colors avoided)
                - 'initial_color': str - Hex color code of initial pixel
                - 'final_color': str - Hex color code of final pixel
                - 'target_color': str or list - Hex color code(s) of target color(s)
                - 'matched_color': str - Which target color was matched (if matched and not exclude mode)
                - 'error': str - Error message if timeout occurred

        Example:
            # Wait for a button to turn green
            result = tester.wait_for_pixel_color(500, 300, '#00FF00', timeout=5.0)

            # Wait for overlay to appear in either light or dark mode
            result = tester.wait_for_pixel_color(300, 1380, ['#fffad0', '#d2cea4'], timeout=30.0)
            if result['matched']:
                print(f"Pixel matched {result['matched_color']}")

            # Wait for pixel to become any color OTHER than the background colors
            result = tester.wait_for_pixel_color(300, 1380, ['#ffffff', '#1b1b1b'], exclude=True, timeout=30.0)
        """
        if Image is None:
            raise RuntimeError("PIL/Pillow is required for pixel operations. Install with: pip install Pillow")

        # Convert target_color(s) to list of RGB tuples
        def to_rgb(color):
            if isinstance(color, str):
                # Remove '#' if present
                color = color.lstrip('#')
                # Convert hex to RGB
                return tuple(int(color[i:i+2], 16) for i in (0, 2, 4))
            else:
                return tuple(color)

        # Handle both single color and list of colors
        if isinstance(target_color, list):
            target_rgb_list = [to_rgb(c) for c in target_color]
            target_hex_list = [f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" for rgb in target_rgb_list]
            target_hex = target_hex_list  # For return value
        else:
            target_rgb_list = [to_rgb(target_color)]
            target_hex_list = [f"#{target_rgb_list[0][0]:02x}{target_rgb_list[0][1]:02x}{target_rgb_list[0][2]:02x}"]
            target_hex = target_hex_list[0]  # For return value (single string for backward compatibility)

        # Take initial screenshot
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            initial_screenshot = tmp.name

        try:
            self.screenshot(initial_screenshot)
            initial_color = self._get_pixel_color(initial_screenshot, x, y)
            initial_hex = f"#{initial_color[0]:02x}{initial_color[1]:02x}{initial_color[2]:02x}"

            # Check if condition already met
            if exclude:
                # In exclude mode, check that pixel is NOT any of the target colors
                is_excluded = any(initial_color == t for t in target_rgb_list)
                if not is_excluded:
                    import sys
                    if '-v' in sys.argv or '--verbose' in sys.argv:
                        print(f"⏱️  Pixel at ({x}, {y}) already not an excluded color (is {initial_hex})")
                    return {
                        'matched': True,
                        'initial_color': initial_hex,
                        'final_color': initial_hex,
                        'target_color': target_hex,
                        'matched_color': None,
                        'error': None
                    }
            else:
                for i, target_rgb in enumerate(target_rgb_list):
                    if initial_color == target_rgb:
                        matched_hex = target_hex_list[i]
                        import sys
                        if '-v' in sys.argv or '--verbose' in sys.argv:
                            print(f"⏱️  Pixel at ({x}, {y}) already at target color {matched_hex}")
                        return {
                            'matched': True,
                            'initial_color': initial_hex,
                            'final_color': initial_hex,
                            'target_color': target_hex,
                            'matched_color': matched_hex,
                            'error': None
                        }

            import sys
            if '-v' in sys.argv or '--verbose' in sys.argv:
                target_display = target_hex if isinstance(target_hex, str) else f"one of {target_hex}"
                if exclude:
                    print(f"⏱️  Waiting for pixel at ({x}, {y}) to change from {initial_hex} to anything except {target_display}...")
                else:
                    print(f"⏱️  Waiting for pixel at ({x}, {y}) to change from {initial_hex} to {target_display}...")
            start_time = time.time()

            while time.time() - start_time < timeout:
                time.sleep(poll_interval)

                # Take new screenshot
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    current_screenshot = tmp.name

                try:
                    self.screenshot(current_screenshot)
                    current_color = self._get_pixel_color(current_screenshot, x, y)
                    current_hex = f"#{current_color[0]:02x}{current_color[1]:02x}{current_color[2]:02x}"

                    if exclude:
                        # Check that pixel is NOT any of the excluded colors
                        is_excluded = any(current_color == t for t in target_rgb_list)
                        if not is_excluded:
                            elapsed = time.time() - start_time
                            print(f"⏱️  ✅ Pixel color is not an excluded color after {elapsed:.3f}s ({initial_hex} -> {current_hex})")
                            return {
                                'matched': True,
                                'initial_color': initial_hex,
                                'final_color': current_hex,
                                'target_color': target_hex,
                                'matched_color': None,
                                'error': None
                            }
                    else:
                        # Check if color matches any of the target colors
                        for i, target_rgb in enumerate(target_rgb_list):
                            if current_color == target_rgb:
                                elapsed = time.time() - start_time
                                matched_hex = target_hex_list[i]
                                print(f"⏱️  ✅ Pixel color matched after {elapsed:.3f}s ({initial_hex} -> {current_hex})")
                                return {
                                    'matched': True,
                                    'initial_color': initial_hex,
                                    'final_color': current_hex,
                                    'target_color': target_hex,
                                    'matched_color': matched_hex,
                                    'error': None
                                }
                finally:
                    # Clean up current screenshot
                    if os.path.exists(current_screenshot):
                        os.unlink(current_screenshot)

            # Timeout occurred - get final color
            elapsed = time.time() - start_time
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                final_screenshot = tmp.name

            try:
                self.screenshot(final_screenshot)
                final_color = self._get_pixel_color(final_screenshot, x, y)
                final_hex = f"#{final_color[0]:02x}{final_color[1]:02x}{final_color[2]:02x}"
            finally:
                if os.path.exists(final_screenshot):
                    os.unlink(final_screenshot)

            # Check if the final color meets condition (race condition: might have changed after timeout)
            if exclude:
                is_excluded = any(final_color == t for t in target_rgb_list)
                if not is_excluded:
                    print(f"⏱️  ✅ Pixel color is not an excluded color after {elapsed:.3f}s ({initial_hex} -> {final_hex}) [detected on timeout]")
                    return {
                        'matched': True,
                        'initial_color': initial_hex,
                        'final_color': final_hex,
                        'target_color': target_hex,
                        'matched_color': None,
                        'error': None
                    }
            else:
                for i, target_rgb in enumerate(target_rgb_list):
                    if final_color == target_rgb:
                        matched_hex = target_hex_list[i]
                        print(f"⏱️  ✅ Pixel color matched after {elapsed:.3f}s ({initial_hex} -> {final_hex}) [detected on timeout]")
                        return {
                            'matched': True,
                            'initial_color': initial_hex,
                            'final_color': final_hex,
                            'target_color': target_hex,
                            'matched_color': matched_hex,
                            'error': None
                        }

            target_display = target_hex if isinstance(target_hex, str) else ', '.join(target_hex)
            if exclude:
                print(f"⏱️  ❌ Timeout after {elapsed:.3f}s - pixel at ({x}, {y}) is {final_hex}, expected anything except {target_display}")
                return {
                    'matched': False,
                    'initial_color': initial_hex,
                    'final_color': final_hex,
                    'target_color': target_hex,
                    'matched_color': None,
                    'error': f'Timeout after {timeout}s - pixel at ({x}, {y}) is {final_hex}, expected anything except {target_display}'
                }
            else:
                print(f"⏱️  ❌ Timeout after {elapsed:.3f}s - pixel at ({x}, {y}) is {final_hex}, expected {target_display}")
                return {
                    'matched': False,
                    'initial_color': initial_hex,
                    'final_color': final_hex,
                    'target_color': target_hex,
                    'matched_color': None,
                    'error': f'Timeout after {timeout}s - pixel at ({x}, {y}) is {final_hex}, expected {target_display}'
                }

        finally:
            # Clean up initial screenshot
            if os.path.exists(initial_screenshot):
                os.unlink(initial_screenshot)

    def wait_for_logcat(self, tag: str, message: str, timeout: float = 30.0,
                        clear_first: bool = True) -> dict:
        """
        Wait for a specific logcat message to appear.

        Monitors logcat for a log line matching the given tag and message substring.
        This is useful for waiting on app-internal events like overlay creation,
        service starts, or state changes without relying on UI pixel colors.

        Args:
            tag: Logcat tag to filter on (e.g., 'MessageDraftOverlay')
            message: Substring to search for in the log message
            timeout: Maximum time to wait in seconds (default: 30.0)
            clear_first: If True, clears logcat before waiting to avoid matching old entries (default: True)

        Returns:
            Dictionary with:
                - 'matched': bool - Whether the log message was found
                - 'line': str - The full matching log line (if matched)
                - 'error': str - Error message if timeout occurred
        """
        adb_cmd = self._get_adb_command()

        if clear_first:
            subprocess.run(adb_cmd + ["logcat", "-c"], capture_output=True)

        # Start logcat filtered by tag
        proc = subprocess.Popen(
            adb_cmd + ["logcat", "-v", "brief", f"{tag}:D", "*:S"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        import sys
        if '-v' in sys.argv or '--verbose' in sys.argv:
            print(f"⏱️  Waiting for logcat tag={tag} message containing '{message}'...")

        start_time = time.time()
        try:
            import select
            while time.time() - start_time < timeout:
                # Use select to poll stdout with a short timeout
                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if ready:
                    line = proc.stdout.readline()
                    if line and message in line:
                        elapsed = time.time() - start_time
                        print(f"⏱️  ✅ Logcat matched after {elapsed:.3f}s: {line.strip()}")
                        return {
                            'matched': True,
                            'line': line.strip(),
                            'error': None
                        }

            elapsed = time.time() - start_time
            print(f"⏱️  ❌ Logcat timeout after {elapsed:.3f}s - never saw '{message}' in tag {tag}")
            return {
                'matched': False,
                'line': None,
                'error': f'Timeout after {timeout}s - never saw "{message}" in tag {tag}'
            }
        finally:
            proc.terminate()
            proc.wait()

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

    def check_element_exists(self, content_desc: Optional[str] = None,
                             text: Optional[str] = None,
                             wait_after_dump: float = 0.5) -> bool:
        """Check if an element exists in the UI hierarchy.

        Runs uiautomator dump, pulls XML, and searches for matching attributes.
        If wait_after_dump > 0, waits that long and re-dumps to account for
        accessibility service reconnection after uiautomator disconnects it.

        Args:
            content_desc: Content description to search for (exact match)
            text: Text content to search for (exact match)
            wait_after_dump: Seconds to wait and re-dump after initial dump (default 0.5)

        Returns:
            True if a matching element was found, False otherwise
        """
        try:
            device_path = "/sdcard/ui_hierarchy_check.xml"
            self.shell(f"uiautomator dump {device_path}")

            if wait_after_dump > 0:
                time.sleep(wait_after_dump)
                self.shell(f"uiautomator dump {device_path}")

            local_path = "/tmp/ui_hierarchy_check.xml"
            cmd = self._get_adb_command() + ["pull", device_path, local_path]
            subprocess.run(cmd, capture_output=True, check=True)

            with open(local_path, 'r') as f:
                xml_content = f.read()
            root = ET.fromstring(xml_content)

            for node in root.iter():
                if content_desc and node.get('content-desc') == content_desc:
                    return True
                if text and node.get('text') == text:
                    return True

            return False
        except Exception as e:
            print(f"Error checking UI hierarchy: {e}")
            return False

    def save_debug_artifacts(self, output_dir: str, test_name: str, step_name: str,
                             screenshot: bool = True, ui_dump: bool = True) -> dict:
        """Save screenshot and/or UI dump as debug artifacts.

        Captures a fresh screenshot and UI hierarchy dump, saving them with
        timestamped filenames for post-mortem debugging.

        Args:
            output_dir: Directory to save artifacts (created if it doesn't exist)
            test_name: Name of the test (used in filename)
            step_name: Name of the step (used in filename)
            screenshot: Whether to capture and save a screenshot (default True)
            ui_dump: Whether to capture and save a UI dump (default True)

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
                tmp_path = "/tmp/debug_screenshot.png"
                self.screenshot(tmp_path)
                dest = os.path.join(output_dir, f"{test_name}_{step_name}_{timestamp}.png")
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

    def _parse_bounds(self, bounds_str: str) -> Tuple[int, int, int, int]:
        """
        Parse bounds string from UI hierarchy XML.

        Args:
            bounds_str: Bounds string in format "[x1,y1][x2,y2]"

        Returns:
            Tuple of (x1, y1, x2, y2)
        """
        # Remove brackets and split
        bounds_str = bounds_str.replace("][", ",").replace("[", "").replace("]", "")
        coords = [int(x) for x in bounds_str.split(",")]
        return tuple(coords)

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
            # Only include children if they have meaningful content
            children = [c for c in children if c.get("text") or c.get("content_desc") or c.get("clickable")]
            if children:
                result["children"] = children

        return result

    def find_element_coordinates_accessibility(self, description: str,
                                               model: Optional[str] = None) -> Optional[ElementCoordinates]:
        """
        Find element coordinates using accessibility hierarchy (Approach 4).

        Args:
            description: Description of the element to find
            model: Claude model to use. If None, uses the default model from initialization.

        Returns:
            ElementCoordinates object if found, None if not found
        """
        if model is None:
            model = self.default_model

        # Get UI hierarchy
        xml_content = self._get_ui_hierarchy()
        root = ET.fromstring(xml_content)

        # Simplify the hierarchy for LLM
        simplified_tree = self._simplify_node_for_llm(root, max_depth=5)

        # Create prompt for Claude
        prompt = f"""You are analyzing an Android UI accessibility hierarchy to find a specific element.

Element description: {description}

UI Hierarchy (simplified JSON):
{json.dumps(simplified_tree, indent=2)}

Find the element that best matches the description. Respond with ONLY a JSON object in this exact format:
{{"found": true, "bounds": "[x1,y1][x2,y2]", "explanation": "why this element matches"}}
or
{{"found": false, "explanation": "why no element matches"}}

Look for elements with matching text, content_desc, or context. The bounds field should be copied EXACTLY from the hierarchy."""

        # Call Claude API with exponential backoff retry
        retry_delays = [1, 2, 4, 8]
        last_error = None

        for attempt, delay in enumerate(retry_delays + [None]):
            try:
                message = self.client.messages.create(
                    model=model,
                    max_tokens=300,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                )

                # Parse response
                response_text = message.content[0].text.strip()

                # Strip markdown code blocks if present
                if response_text.startswith("```"):
                    # Remove opening ```json or ```
                    lines = response_text.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    # Remove closing ```
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    response_text = "\n".join(lines).strip()

                response_data = json.loads(response_text)

                if response_data.get("found"):
                    bounds = response_data["bounds"]
                    x1, y1, x2, y2 = self._parse_bounds(bounds)
                    return ElementCoordinates(
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        method="accessibility",
                        confidence=response_data.get("explanation")
                    )
                else:
                    return None

            except (RateLimitError, InternalServerError, APITimeoutError, APIConnectionError) as e:
                last_error = e
                if delay is None:
                    break
                print(f"API error on attempt {attempt + 1}: {type(e).__name__}. Retrying in {delay}s...")
                time.sleep(delay)

        # If we exhausted all retries, raise the last error
        raise last_error