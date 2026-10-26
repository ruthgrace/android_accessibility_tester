"""Measure observe() accuracy offline against saved screenshots and hierarchy dumps.

    source your-api-key.sh
    ./venv/bin/python calibration/measure.py claude-sonnet-5-5 [more models...]

Each PNG in calibration/pixel8/ has a matching uiautomator XML. Every clickable node with a
short, unique label becomes a target; observe() is asked for it by that label and the
returned centre is compared with the node's true centre. A "hit" is a centre inside the
node's bounds, i.e. a tap that would have worked.

Labels come from the hierarchy, so some are invisible on screen (never found) and some are
ambiguous (a card and its button share words). Those show up as notfound/miss every run and
are not coordinate errors; compare runs against each other, not against zero.
"""
import glob
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter

from android_accessibility_tester import AndroidAccessibilityTester, StructuredCallError

HERE = os.path.dirname(os.path.abspath(__file__))
TIME = re.compile(r"^\d{1,2}:\d{2}")


def targets_for(xml_path):
    rows = []
    for n in ET.parse(xml_path).getroot().iter("node"):
        label = (n.get("content-desc") or n.get("text") or "").strip()
        if not label or n.get("clickable") != "true" or len(label) > 24 or TIME.match(label):
            continue
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
        if x2 - x1 < 40 or y2 - y1 < 40:
            continue
        rows.append((label, (x1 + x2) // 2, (y1 + y2) // 2, x2 - x1, y2 - y1))
    counts = Counter(r[0] for r in rows)
    return [r for r in rows if counts[r[0]] == 1]


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
    screens = sorted(glob.glob(os.path.join(HERE, "pixel8", "*.png")))
    for model in models:
        errs, hits, misses, notfound, broken = [], 0, [], 0, 0
        for png in screens:
            t.current = png
            for label, tx, ty, w, h in targets_for(png[:-4] + ".xml"):
                try:
                    el = t.observe(f'the control labelled "{label}"', model=model)
                except StructuredCallError:
                    broken += 1
                    continue
                if not el:
                    notfound += 1
                    continue
                ox, oy = el.center()
                err = ((ox - tx) ** 2 + (oy - ty) ** 2) ** 0.5
                errs.append(err)
                if abs(ox - tx) <= w / 2 and abs(oy - ty) <= h / 2:
                    hits += 1
                else:
                    misses.append(f"{os.path.basename(png)[:-4]}:{label} truth=({tx},{ty}) obs=({ox},{oy}) {err:.0f}px")
        s = sorted(errs)
        n = len(s)
        print(f"{model}: targets={n + notfound + broken} hit={hits} miss={len(misses)} notfound={notfound} "
              f"schema_err={broken} median={s[n // 2]:.0f}px p90={s[int(n * 0.9)]:.0f}px max={s[-1]:.0f}px")
        for m in misses:
            print("   ", m)


if __name__ == "__main__":
    main(sys.argv[1:] or [None])
