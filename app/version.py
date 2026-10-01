"""The Command Center's version number and its patch notes (CHANGELOG.md).

Bump VERSION and add a CHANGELOG.md section together, test_version.py fails if they dont match.
"""
import os
import re

VERSION = "1.6.2"
CHANGELOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
_HEAD = re.compile(r"^## (\d+\.\d+\.\d+) - (.+)$")


def releases(path=CHANGELOG):
    """Every version newest first as (version, date, summary lines), the bullets right under each heading."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = _HEAD.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip(), [], [True]))
        elif out and out[-1][3][0]:
            if line.startswith("- "):
                out[-1][2].append(line[2:].strip())
            elif out[-1][2] and line.startswith("  ") and line.strip():
                out[-1][2][-1] += " " + line.strip()  # a bullet that wrapped onto the next line
            elif out[-1][2] and not line.startswith("  "):
                out[-1][3][0] = False  # the summary stops at the first blank line
    return [(v, d, s) for v, d, s, _ in out]


def release_date(version=VERSION, path=CHANGELOG):
    for v, d, _s in releases(path):
        if v == version:
            return d
    return None


def whats_new(version=VERSION, path=CHANGELOG):
    for v, _d, s in releases(path):
        if v == version:
            return s
    return []


def label():
    d = release_date()
    return "%s (%s)" % (VERSION, d) if d else VERSION
