#!/usr/bin/env python3

"""
Test script for the observe() method. Requires a connected device.
"""

import android_accessibility_tester


def show(label, element):
    """Print what observe() returned, if anything."""
    if element:
        print(f"Found {label}!")
        print(f"Bounds: {element.bounds}")
        print(f"Center: {element.center()}")
        print(f"Selectors: resource_id={element.resource_id!r}, "
              f"content_desc={element.content_desc!r}, text={element.text!r}")
        print(f"Explanation: {element.explanation}")
    else:
        print(f"{label} not found!")


def test_find_clock_app():
    """Test finding the Clock app icon."""
    tester = android_accessibility_tester.AndroidAccessibilityTester()

    clock = tester.observe("Clock app icon")
    show("Clock app", clock)

    if clock:
        # The Element carries selectors, so this position can be cached and
        # replayed later without another model call.
        print(f"\nTapping at center {clock.center()}...")
        tester.tap(*clock.center())


def test_find_chrome_app():
    """Test finding the Chrome app icon."""
    tester = android_accessibility_tester.AndroidAccessibilityTester()

    # Go to home screen first
    tester.press_key("KEYCODE_HOME")

    show("Chrome app", tester.observe("Chrome browser app"))


if __name__ == "__main__":
    print("Testing observe()...")
    print("\n=== Test 1: Find Clock app ===")
    test_find_clock_app()

    print("\n=== Test 2: Find Chrome app ===")
    test_find_chrome_app()
