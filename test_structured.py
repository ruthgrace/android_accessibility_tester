"""Unit tests for the structured-output layer.

These tests never touch a device or the Anthropic API — they exercise the
schema types and the structured_call retry/validation logic against a fake
client.
"""

from typing import List, Optional

import json
import os

import pytest
from anthropic import APIConnectionError, RateLimitError
from pydantic import BaseModel, Field, ValidationError

from android_accessibility_tester import (
    Check,
    Element,
    Result,
    StructuredCallError,
    _strict_schema,
    structured_call,
)


class FakeText:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeMessages:
    """Returns a queued response per call, recording the kwargs it was called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeMessage([FakeText(json.dumps(response))])


class FakeClient:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


def _raw_text_then(fake_messages):
    """Wrap FakeMessages.create so plain-string responses are returned as raw text blocks."""

    def create(**kwargs):
        fake_messages.calls.append(kwargs)
        response = fake_messages._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        text = response if isinstance(response, str) else json.dumps(response)
        return FakeMessage([FakeText(text)])

    return create


class Alarm(Result):
    """An alarm read off the screen."""

    time: str
    enabled: bool


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep backoff tests instant."""
    monkeypatch.setattr("android_accessibility_tester.time.sleep", lambda _s: None)


def api_error(cls):
    """Build a transient API error without needing a real request object."""
    if cls is RateLimitError:
        return cls.__new__(cls)
    return cls.__new__(cls)


class TestResult:
    def test_is_truthy_when_ok(self):
        assert Result(ok=True)

    def test_is_falsy_when_not_ok(self):
        assert not Result(ok=False, error="something went wrong")

    def test_repr_of_failure_leads_with_the_error_text(self):
        r = Result(ok=False, error="no World Clock tab was visible")
        assert repr(r) == "Result(ok=False): no World Clock tab was visible"

    def test_repr_of_success_is_terse(self):
        assert repr(Result(ok=True)) == "Result(ok=True)"

    def test_subclass_repr_uses_the_subclass_name(self):
        class Check(Result):
            pass

        assert repr(Check(ok=False, error="nope")) == "Check(ok=False): nope"

    def test_error_defaults_to_none(self):
        assert Result(ok=True).error is None


class TestStructuredCall:
    def test_returns_a_validated_instance_of_the_output_model(self):
        client = FakeClient({"ok": True, "error": None, "time": "7:00 AM", "enabled": True})

        result = structured_call(client, "a-model", "read the alarm", Alarm)

        assert isinstance(result, Alarm)
        assert result.time == "7:00 AM"
        assert result.enabled is True

    def test_requests_the_output_model_schema_as_structured_output(self):
        client = FakeClient({"ok": True, "time": "7:00 AM", "enabled": True})

        structured_call(client, "a-model", "read the alarm", Alarm)

        kwargs = client.messages.calls[0]
        fmt = kwargs["extra_body"]["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"] == _strict_schema(Alarm.model_json_schema())
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs

    def test_schema_forbids_additional_properties_on_every_object(self):
        class Inner(BaseModel):
            x: int

        class Outer(BaseModel):
            inner: Inner
            items: List[Inner]

        schema = _strict_schema(Outer.model_json_schema())

        assert schema["additionalProperties"] is False
        assert schema["$defs"]["Inner"]["additionalProperties"] is False

    def test_puts_the_model_docstring_in_the_prompt(self):
        client = FakeClient({"ok": True, "time": "7:00 AM", "enabled": True})

        structured_call(client, "a-model", "read the alarm", Alarm)

        content = client.messages.calls[0]["messages"][0]["content"]
        assert content[-1] == {"type": "text", "text": "Respond with: An alarm read off the screen."}

    def test_wraps_a_plain_string_prompt_as_a_text_block(self):
        client = FakeClient({"ok": True, "time": "7:00 AM", "enabled": True})

        structured_call(client, "a-model", "read the alarm", Alarm)

        content = client.messages.calls[0]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "read the alarm"}

    def test_passes_block_content_through_untouched(self):
        client = FakeClient({"ok": True, "time": "7:00 AM", "enabled": True})
        blocks = [{"type": "text", "text": "hi"}]

        structured_call(client, "a-model", blocks, Alarm)

        assert client.messages.calls[0]["messages"][0]["content"][: len(blocks)] == blocks
        assert blocks == [{"type": "text", "text": "hi"}]  # caller's list is not mutated

    def test_retries_a_schema_violation_and_returns_the_corrected_value(self):
        client = FakeClient(
            {"ok": True, "time": "7:00 AM", "enabled": "yes please"},  # not a bool
            {"ok": True, "time": "7:00 AM", "enabled": True},
        )

        result = structured_call(client, "a-model", "read the alarm", Alarm)

        assert result.enabled is True
        assert len(client.messages.calls) == 2

    def test_shows_the_model_its_validation_errors_on_retry(self):
        client = FakeClient(
            {"ok": True, "time": "7:00 AM", "enabled": "yes please"},
            {"ok": True, "time": "7:00 AM", "enabled": True},
        )

        structured_call(client, "a-model", "read the alarm", Alarm)

        retry_messages = client.messages.calls[1]["messages"]
        assert retry_messages[-2]["role"] == "assistant"
        correction = retry_messages[-1]["content"][0]
        assert retry_messages[-1]["role"] == "user"
        assert correction["type"] == "text"
        assert "Validation failed" in correction["text"]
        assert "enabled" in correction["text"]

    def test_treats_non_json_text_as_a_schema_violation(self):
        client = FakeClient("not json at all", {"ok": True, "time": "7:00 AM", "enabled": True})
        client.messages.create = _raw_text_then(client.messages)  # pass the string through raw

        result = structured_call(client, "a-model", "read the alarm", Alarm)

        assert result.enabled is True
        assert len(client.messages.calls) == 2

    def test_raises_when_the_schema_never_validates(self):
        client = FakeClient(
            {"ok": True, "time": "7:00 AM", "enabled": "maybe"},
            {"ok": True, "time": "7:00 AM", "enabled": "still not a bool"},
        )

        with pytest.raises(StructuredCallError, match="Alarm"):
            structured_call(client, "a-model", "read the alarm", Alarm)

    def test_retries_transient_api_errors_then_succeeds(self):
        client = FakeClient(
            api_error(APIConnectionError),
            api_error(RateLimitError),
            {"ok": True, "time": "7:00 AM", "enabled": True},
        )

        result = structured_call(client, "a-model", "read the alarm", Alarm)

        assert result.time == "7:00 AM"
        assert len(client.messages.calls) == 3

    def test_gives_up_on_transient_errors_after_the_backoff_schedule(self):
        client = FakeClient(*[api_error(APIConnectionError) for _ in range(6)])

        with pytest.raises(APIConnectionError):
            structured_call(client, "a-model", "read the alarm", Alarm)

        assert len(client.messages.calls) == 5  # 4 backoff delays + a final attempt

    def test_does_not_retry_unexpected_errors(self):
        client = FakeClient(ValueError("boom"))

        with pytest.raises(ValueError, match="boom"):
            structured_call(client, "a-model", "read the alarm", Alarm)

        assert len(client.messages.calls) == 1


class TestCheck:
    def test_passing_check_is_truthy_with_no_error(self):
        c = Check(ok=True)
        assert c
        assert c.error is None

    def test_failing_check_carries_the_models_prose_verbatim(self):
        c = Check(ok=False, error="The World Clock tab is missing from the bottom bar.")
        assert not c
        assert c.error == "The World Clock tab is missing from the bottom bar."

    def test_error_field_asks_the_model_for_one_sentence(self):
        described = Check.model_json_schema()["properties"]["error"]["description"]
        assert "one sentence" in described.lower()

    def test_screenshot_path_is_not_part_of_the_model_schema(self):
        # The path is filled in locally after the call; the model must not be asked for it.
        assert "screenshot" not in Check.model_json_schema()["properties"]


class TestElement:
    def test_parses_bounds_and_computes_its_center(self):
        e = Element(found=True, bounds="[10,20][30,60]", explanation="the Start button")
        assert e.center() == (20, 40)

    def test_rejects_found_element_with_no_bounds(self):
        with pytest.raises(ValidationError, match="bounds"):
            Element(found=True, explanation="found it but forgot the bounds")

    def test_rejects_malformed_bounds(self):
        with pytest.raises(ValidationError, match="bounds"):
            Element(found=True, bounds="10,20,30,60", explanation="wrong format")

    def test_not_found_carries_empty_bounds(self):
        e = Element(found=False, bounds="", explanation="no element matches")
        assert e.bounds == ""


@pytest.fixture
def tester(monkeypatch, tmp_path):
    """A tester with no device and no network: adb, screenshots and the API are all stubbed."""
    from android_accessibility_tester import AndroidAccessibilityTester

    monkeypatch.setattr(AndroidAccessibilityTester, "_verify_adb", lambda self: None)
    monkeypatch.setattr(AndroidAccessibilityTester, "_resolve_default_model", lambda self: "test-model")
    t = AndroidAccessibilityTester(api_key="not-a-real-key")

    def fake_screenshot(output_path):
        from PIL import Image
        Image.new("RGB", (40, 60), "gray").save(output_path)   # observe() frames it with rulers
        return output_path

    def fake_shell(command):
        raise AssertionError(f"nothing on the happy path may shell out: {command!r}")

    monkeypatch.setattr(t, "screenshot", fake_screenshot)
    monkeypatch.setattr(t, "shell", fake_shell)
    return t


class TestCheckMethod:
    def test_returns_a_check_built_from_the_models_answer(self, tester):
        tester.client = FakeClient({"ok": False, "error": "The World Clock tab is missing."})

        result = tester.check("the clock app shows a World Clock tab")

        assert isinstance(result, Check)
        assert not result
        assert result.error == "The World Clock tab is missing."

    def test_sends_the_screenshot_and_the_description(self, tester):
        tester.client = FakeClient({"ok": True})

        tester.check("the clock app shows a World Clock tab")

        content = tester.client.messages.calls[0]["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["source"]["media_type"] == "image/png"
        assert "the clock app shows a World Clock tab" in content[1]["text"]

    def test_discards_the_screenshot_when_the_check_passes(self, tester):
        tester.client = FakeClient({"ok": True})

        result = tester.check("anything")

        assert result.screenshot is None

    def test_keeps_the_screenshot_when_the_check_fails(self, tester):
        tester.client = FakeClient({"ok": False, "error": "nope"})

        result = tester.check("anything")

        assert result.screenshot is not None
        assert os.path.exists(result.screenshot)
        os.unlink(result.screenshot)

    def test_keeps_an_explicitly_requested_path_even_when_passing(self, tester, tmp_path):
        tester.client = FakeClient({"ok": True})
        path = str(tmp_path / "shot.png")

        result = tester.check("anything", screenshot_path=path)

        assert result.screenshot == path
        assert os.path.exists(path)


class TestObserve:
    def test_returns_an_element_with_a_usable_center(self, tester):
        tester.client = FakeClient(
            {"found": True, "bounds": "[100,200][300,280]", "explanation": "the Start button"}
        )

        element = tester.observe("the Start button")

        assert element.center() == (200, 240)

    def test_returns_none_when_nothing_matches(self, tester):
        tester.client = FakeClient({"found": False, "bounds": "", "explanation": "no such element"})

        assert tester.observe("a nonexistent widget") is None

    def test_sends_a_screenshot_and_the_description(self, tester):
        tester.client = FakeClient({"found": False, "bounds": "", "explanation": "no"})

        tester.observe("the Start button")

        content = tester.client.messages.calls[0]["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert "the Start button" in content[1]["text"]


class TestExtract:
    def test_returns_an_instance_of_the_caller_supplied_schema(self, tester):
        class TimerState(BaseModel):
            """The state of the on-screen timer."""

            remaining_seconds: int
            is_running: bool

        tester.client = FakeClient({"remaining_seconds": 42, "is_running": True})

        state = tester.extract("the state of the running timer", TimerState)

        assert isinstance(state, TimerState)
        assert state.remaining_seconds == 42
        assert state.is_running is True

    def test_sends_the_screenshot(self, tester):
        class Anything(BaseModel):
            """Anything at all."""

            value: str

        tester.client = FakeClient({"value": "x"})

        tester.extract("something", Anything)

        content = tester.client.messages.calls[0]["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert "something" in content[1]["text"]


class TestCapture:
    def test_captures_a_screenshot(self, tester):
        capture = tester.capture()

        assert capture.screenshot is not None
        assert os.path.exists(capture.screenshot)
        capture.release()

    def test_does_not_shell_out(self, tester):
        # The fixture's fake_shell raises: capturing must never dump a hierarchy.
        tester.capture().release()

    def test_release_removes_the_file(self, tester):
        capture = tester.capture()
        path = capture.screenshot

        capture.release()

        assert not os.path.exists(path)
        assert capture.screenshot is None

    def test_captures_are_independent(self, tester):
        first = tester.capture()
        second = tester.capture()

        assert first.screenshot != second.screenshot
        first.release()
        second.release()
