"""Measure observe() accuracy offline against saved screenshots and hierarchy dumps.

    source your-api-key.sh
    ./venv/bin/python calibration/measure.py claude-sonnet-5-5 [more models...]

Each PNG in calibration/pixel8/ has a matching uiautomator XML. TARGETS lists, per screen,
the hierarchy label of a control and a description written the way a test author would write
it. The label looks up the true bounds in the dump; observe() is asked for the description;
a "hit" is a returned centre inside the true bounds, i.e. a tap that would have worked.
"""
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from android_accessibility_tester import AndroidAccessibilityTester, StructuredCallError

HERE = os.path.dirname(os.path.abspath(__file__))

TAB = 'the "{}" tab in the bottom navigation bar'
MORE = 'the three-dot "more options" menu button in the top-right corner'
PLUS = 'the large rounded "+" button in the lower right, above the bottom navigation bar'


def timer_card(shown, hierarchy):
    """Targets for one timer card. `shown` is the title on screen, `hierarchy` the dump's wording."""
    return [
        (f"{hierarchy} timer", f'the "{shown}" title text at the top-left corner of its timer card'),
        (f"Delete {hierarchy} timer", f'the "X" close button at the top-right corner of the "{shown}" card'),
        (f"Start {hierarchy} timer", f'the triangular play button inside the "{shown}" ring'),
    ]


# (hierarchy label, description a person would write). The label looks up the true bounds in
# the screen's uiautomator dump; the description is what observe() is asked for. Descriptions
# use the words visible on screen, which are not always the hierarchy's.
TARGETS = {
    "alarms": [
        ("More options", MORE),
        ("Add alarm", PLUS),
        ("World Clock", TAB.format("World Clock")),
        ("Timers", TAB.format("Timers")),
        ("Stopwatch", TAB.format("Stopwatch")),
        ("Bedtime", TAB.format("Bedtime")),
    ],
    # bedtime.png was captured mid-transition: the dump lists a schedule card and buttons the
    # image does not show. Only the chrome is usable until the fixture is recaptured.
    "bedtime": [
        ("More options", MORE),
        ("Alarms", TAB.format("Alarms")),
        ("World Clock", TAB.format("World Clock")),
        ("Timers", TAB.format("Timers")),
        ("Stopwatch", TAB.format("Stopwatch")),
    ],
    "stopwatch": [
        ("More options", MORE),
        ("Start", 'the "Start" button in the middle of the screen'),
        ("Alarms", TAB.format("Alarms")),
        ("World Clock", TAB.format("World Clock")),
        ("Timers", TAB.format("Timers")),
        ("Bedtime", TAB.format("Bedtime")),
    ],
    "timers": [
        ("More options", MORE),
        *timer_card("8m Timer", "8 minutes"),
        ("Reset 8 minutes timer", 'the circular-arrow reset button below the "8m Timer" ring'),
        *timer_card("10s Timer", "10 seconds"),
        *timer_card("1m Timer", "1 minute"),
        *timer_card("3m Timer", "3 minutes"),
        ("Add timer", PLUS),
        ("Alarms", TAB.format("Alarms")),
        ("World Clock", TAB.format("World Clock")),
        ("Stopwatch", TAB.format("Stopwatch")),
        ("Bedtime", TAB.format("Bedtime")),
    ],
    "worldclock": [
        ("More options", MORE),
        ("Add city", PLUS),
        ("Alarms", TAB.format("Alarms")),
        ("Timers", TAB.format("Timers")),
        ("Stopwatch", TAB.format("Stopwatch")),
        ("Bedtime", TAB.format("Bedtime")),
    ],
}


def truth_for(xml_path):
    """Map each clickable node's label to (cx, cy, w, h) from a uiautomator dump."""
    out = {}
    for n in ET.parse(xml_path).getroot().iter("node"):
        label = (n.get("content-desc") or n.get("text") or "").strip()
        if label and n.get("clickable") == "true" and label not in out:
            x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
            out[label] = ((x1 + x2) // 2, (y1 + y2) // 2, x2 - x1, y2 - y1)
    return out


class Fixture(AndroidAccessibilityTester):
    """A tester whose 'device' is a saved screenshot."""
    current = None

    def _verify_adb(self):
        pass

    def screenshot(self, output_path):
        shutil.copy(self.current, output_path)
        return output_path


def main(models):
    t = Fixture(model=models[0])
    for model in models:
        errs, hits, misses, notfound, broken = [], 0, [], 0, 0
        for screen, targets in TARGETS.items():
            t.current = os.path.join(HERE, "pixel8", f"{screen}.png")
            truth = truth_for(os.path.join(HERE, "pixel8", f"{screen}.xml"))
            for label, description in targets:
                tx, ty, w, h = truth[label]
                try:
                    el = t.observe(description, model=model)
                except StructuredCallError:
                    broken += 1
                    continue
                if not el:
                    notfound += 1
                    misses.append(f"{screen}:{label} NOT FOUND")
                    continue
                ox, oy = el.center()
                err = ((ox - tx) ** 2 + (oy - ty) ** 2) ** 0.5
                errs.append(err)
                if abs(ox - tx) <= w / 2 and abs(oy - ty) <= h / 2:
                    hits += 1
                else:
                    misses.append(f"{screen}:{label} truth=({tx},{ty}) obs=({ox},{oy}) {err:.0f}px")
        s = sorted(errs)
        n = len(s)
        print(f"{model}: targets={n + notfound + broken} hit={hits} miss={len(misses) - notfound} "
              f"notfound={notfound} schema_err={broken} median={s[n // 2]:.0f}px "
              f"p90={s[int(n * 0.9)]:.0f}px max={s[-1]:.0f}px")
        for m in misses:
            print("   ", m)


if __name__ == "__main__":
    main(sys.argv[1:] or [None])
