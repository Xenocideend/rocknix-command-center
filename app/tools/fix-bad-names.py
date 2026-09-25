#!/usr/bin/env python3
r"""fix-bad-names.py - NM1 (owner's decision, 24 Sep 2026): a device-only quick
fix for the look-alike subset of name_guard's findings.

Why: ES passes game names/paths to event hooks through `sh -c` with naive
quoting (Scripting.cpp executeScript(): an argument is double-quoted ONLY if
it has an ASCII space, `"` is stripped, nothing is escaped - see
../name_guard.py). Four characters are the actual damage on this device:

    '  (apostrophe)  starts a shell quote            -> U+2019 RIGHT SINGLE QUOTATION MARK
    `  (backtick)    command substitution             -> U+2019 (same look-alike)
    $  (dollar)      variable/command expansion        -> U+FF04 FULLWIDTH DOLLAR SIGN
    ;  (semicolon)   ends the hook command             -> U+FF1B FULLWIDTH SEMICOLON

The owner chose a device-only quick fix: swap the ASCII character for a
visually-identical look-alike inside gamelist <name> text (never <path> text
directly - see below), and rename the few ROM files whose FILENAME itself
contains a '$' (so their bare, unquoted argument to a hook is dangerous too),
along with every media file named after that ROM's stem and every gamelist
reference to it. Nothing here is a permanent fix (a later sync from the PC
library may revert it) - see TASKS.md NM1.

WHAT THIS TOOL DOES
--------------------
1. Runs name_guard's scan (or reads a saved --guard-json result).
2. For every affected gamelist.xml, finds every <name>...</name> whose text
   name_guard would flag for '/`/$/; and rewrites ONLY the bytes inside that
   element to the look-alike form. <path> and media tags are never touched by
   this step - only by step 3, and only for the ROM it renamed.
3. For every ROM file flagged because its FILENAME contains '$' AND that file
   sits directly in a system folder (<root>/<system>/<file>, not a media
   folder or something like the Steam runtime), plans a rename to the '$' ->
   U+FF04 form, plus every media file in that system's media folders whose
   name starts with the ROM's stem + "-", plus the matching <path> and every
   OTHER tag in that <game> block that references the old stem (image, video,
   manual, thumbnail, marquee, ...). A game with any save/state data for that
   stem (same system folder .srm/.sav/.state*, or a savestates/saves folder)
   is reported but SKIPPED, never renamed.
4. Anything name_guard flagged that this tool does not touch (e.g. a
   backtick in a theme .svg, backslashes in the Steam runtime, Steam's own
   $$$autosave.vdf) is listed under "excluded", never modified.

NM2 (24 Sep 2026 test-day fix): name_guard itself now only scans what ES's
own es_systems.cfg says it will show (see ../name_guard.py), so Steam/theme
findings like the ones above should no longer even be produced. As defence
in depth against a stale or hand-edited --guard-json result, this tool
independently re-checks every candidate rename against the same system list:
anything not under a declared ES system path with a matching extension is
REFUSED (--apply aborts entirely, no changes at all), not silently excluded.

Both ROM roots (/storage/roms and /storage/games-internal/roms) are the SAME
underlying partition, bind-mounted twice (MANUALS-NOTES.md: identical device
+ inode from either path) - editing/renaming through one path is visible
through the other automatically. Every finding is deduped by (st_dev,
st_ino) (falling back to realpath if a file has vanished since the scan) so
nothing is edited or renamed twice.

SAFETY
------
Dry run is the DEFAULT: it prints the exact plan (every gamelist line old ->
new, every rename old -> new including media, every skip with its reason,
every exclusion) and changes nothing. --apply requires --i-stopped-es (ES
rewrites gamelists from memory at exit, so an apply while ES is running would
be overwritten or would clobber a concurrent ES write). Before any change,
every gamelist.xml this run will touch is backed up next to itself as
`gamelist.xml.bak-<timestamp>`, and an undo log (JSON) records every rename
and every gamelist backup; `--undo LOG` reverses all of it. --apply refuses
(no changes at all) if ANY rename target already exists, or if any planned
gamelist edit cannot be verified safe. Re-running after a successful apply
finds nothing left to do (idempotent): the look-alike characters are not
"dangerous" any more, so name_guard (and this tool) no longer flag them.

USAGE
-----
    python3 fix-bad-names.py                              # dry run, defaults
    python3 fix-bad-names.py --guard-json guard.json       # dry run from a saved scan
    python3 fix-bad-names.py --json                        # dry run, JSON plan
    python3 fix-bad-names.py --apply --i-stopped-es        # apply for real
    python3 fix-bad-names.py --undo NM1-undo-<ts>.json      # revert an apply

Exit codes: 0 ok (plan shown / applied / undone, even if empty), 2 usage
error, 3 nothing scanned (name_guard found no ROM root), 4 refused to apply
(conflict, missing --i-stopped-es, or a safety check failed), 5 undo failed.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import name_guard  # noqa: E402

# Plan text/paths can contain the look-alike characters themselves (U+2019,
# U+FF04, U+FF1B) or non-ASCII scraped names; Windows' default console codec
# (cp1252) raises on those and would kill the whole run (same fix as rk.py).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# The look-alike map (owner's decision, NM1).
CHAR_MAP = {
    "'": "’",   # RIGHT SINGLE QUOTATION MARK
    "`": "’",   # backtick -> the same look-alike as an apostrophe
    "$": "＄",   # FULLWIDTH DOLLAR SIGN
    ";": "；",   # FULLWIDTH SEMICOLON
}
_ALWAYS_CHARS = "$`"      # dangerous in every value (name_guard.ALWAYS)
_UNQUOTED_CHARS = "';"    # dangerous only in a value with no ASCII space
_RENAME_CHAR = "$"        # the only character this tool renames a ROM for

_FIELD_RE = re.compile(rb"<(name|path)>(<!\[CDATA\[.*?\]\]>|[^<]*)</\1>", re.DOTALL)
_TAG_RE = re.compile(rb"<([A-Za-z][\w:.-]*)>(<!\[CDATA\[.*?\]\]>|[^<]*)</\1>", re.DOTALL)
_GAME_OPEN_RE = re.compile(rb"<game(?:\s[^>]*)?>")
SAVE_EXTS = (".srm", ".sav")
DEFAULT_SAVE_DIRS = ("/storage/roms/savestates", "/storage/roms/saves",
                     "/storage/games-internal/roms/savestates",
                     "/storage/games-internal/roms/saves")


def chars_to_fix(value):
    """Which of CHAR_MAP's characters are present AND dangerous per ES's own
    quoting rule - a mirror of name_guard.value_reasons(), restricted to the
    4 characters this tool knows how to fix. Pure."""
    if not isinstance(value, str) or not value:
        return set()
    out = set(c for c in _ALWAYS_CHARS if c in value)
    if " " not in value:
        out.update(c for c in _UNQUOTED_CHARS if c in value)
    return out


def fix_text(value):
    """Apply the look-alike substitution to every dangerous char in value.
    Returns (new_value, chars_fixed). Pure."""
    chars = chars_to_fix(value)
    if not chars:
        return value, set()
    return "".join(CHAR_MAP[c] if c in chars else c for c in value), chars


def substitute_raw(raw_bytes, chars):
    """Literal-byte substitution of each target char (all pure ASCII, so a
    plain bytes.replace is byte-exact and never touches anything else in the
    span - no decode/re-encode of the whole field is needed for this step)."""
    out = raw_bytes
    for ch in chars:
        out = out.replace(ch.encode("ascii"), CHAR_MAP[ch].encode("utf-8"))
    return out


def _encode_es(text):
    """ES's own escaping style for a gamelist value: only '&' is escaped."""
    return text.replace("&", "&amp;").encode("utf-8")


def file_key(path):
    """A dedup key that is identical for two paths naming the same physical
    file (bind mount): (st_dev, st_ino), falling back to a realpath string if
    the file cannot be stat'ed (already gone, or a fixture edge case)."""
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return ("path", os.path.realpath(path))


def _root_rank(path, roots):
    """Index of the root this path is under (for picking a stable, readable
    canonical path among duplicates from bind-mounted roots); len(roots) if
    none match (sorts last)."""
    rp = os.path.realpath(path)
    for i, r in enumerate(roots):
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep):
            return i
    return len(roots)


def pick_canonical(paths, roots):
    return sorted(set(paths), key=lambda p: (_root_rank(p, roots), p))[0]


def _build_system_index(result):
    """{realpath(system dir): extensions} from a name_guard.scan()-shaped
    result. `extensions` is a frozenset (lowercase, with the leading dot) in
    the normal (NM2-scoped) case. In LEGACY mode (result["systems"] is empty
    because es_systems.cfg could not be read - see name_guard.py) there is no
    per-system extension info, so every immediate subdirectory of every
    scanned root is indexed instead, the same shape name_guard's pre-NM2
    unscoped walk assumed, with extensions=None meaning "any extension is
    accepted" (matching the pre-NM2 behaviour of this tool byte for byte)."""
    systems = result.get("systems") or []
    if systems:
        return {os.path.realpath(s["path"]):
                frozenset(x.lower() for x in (s.get("extensions") or ())) for s in systems}
    idx = {}
    for r in result.get("roots") or ():
        rr = os.path.realpath(r)
        try:
            entries = os.scandir(rr)
        except OSError:
            continue
        with entries:
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    idx[os.path.realpath(e.path)] = None
    return idx


def is_system_top_level(path, system_index):
    """True (and the system name) iff path is exactly <system dir>/<file> for
    one of system_index's keys - the shape of a real ROM, never a media file
    (one level deeper: <system dir>/images/...) or something nested further
    down (Steam's userdata tree, a theme's art/logos/collections/...)."""
    d = os.path.realpath(os.path.dirname(path))
    if d in system_index:
        return True, os.path.basename(d)
    return False, None


def path_under_a_known_system(path, system_index):
    """Defence in depth (NM2, 24 Sep 2026): True iff `path` sits ANYWHERE
    under one of ES's own declared system directories AND its extension is
    one that system actually declares (extensions=None, the legacy-mode
    marker, accepts any extension - see _build_system_index). name_guard
    itself now only ever flags paths that already satisfy this (Steam's ROM-
    shaped game data and a theme's .svg are never under a declared system
    path at all), so this should never fire in normal operation; it exists so
    a stale or hand-edited --guard-json result can never make this tool
    rename something outside ES's own idea of a game."""
    rp = os.path.realpath(path)
    ext = os.path.splitext(rp)[1].lower()
    for sysdir, exts in system_index.items():
        if rp == sysdir or rp.startswith(sysdir.rstrip(os.sep) + os.sep):
            return exts is None or ext in exts
    return False


def find_media(system_dir, stem):
    """Every file in a media subfolder of system_dir (images/, videos/,
    manuals/, or any other one-level-deep folder) named "<stem>-*"."""
    media = []
    try:
        subdirs = [e.path for e in os.scandir(system_dir) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return media
    for d in sorted(subdirs):
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    continue
                if e.name.startswith(stem + "-"):
                    media.append(e.path)
    return sorted(media)


def find_saves(system_dir, stem, save_dirs):
    """Every existing file that looks like save/state data for stem: right
    next to the ROM (<stem>.srm, <stem>.sav, <stem>.state*), or anywhere
    under one of save_dirs (name_guard-style: never raises)."""
    hits = []
    try:
        for name in os.listdir(system_dir):
            if name == stem or not name.startswith(stem):
                continue
            rest = name[len(stem):]
            if rest in SAVE_EXTS or rest.startswith(".state"):
                hits.append(os.path.join(system_dir, name))
    except OSError:
        pass
    seen_dirs = set()
    for d in save_dirs:
        try:
            rd = os.path.realpath(d)
        except OSError:
            continue
        if not os.path.isdir(d) or rd in seen_dirs:
            continue
        seen_dirs.add(rd)
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.startswith(stem):
                    hits.append(os.path.join(root, f))
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# Gamelist text editing - always on raw bytes; only the bytes inside a
# specific element's span are ever touched.

def _line_of(data, offset):
    return data.count(b"\n", 0, offset) + 1


def _decode(raw_bytes):
    return name_guard.xml_text(raw_bytes.decode("utf-8"))


def _find_enclosing_game_block(data, pos):
    start = -1
    for m in _GAME_OPEN_RE.finditer(data, 0, pos):
        start = m.start()
    if start == -1:
        return None
    end = data.find(b"</game>", pos)
    if end == -1:
        return None
    return start, end + len(b"</game>")


def _rewrite_media_ref(value, old_base, new_base, old_stem, new_stem):
    """The new text for a <path>/<image>/<video>/.../ value that referenced
    the old ROM, or None if it does not reference it at all."""
    if not isinstance(value, str) or not value:
        return None
    dirpart, sep, filepart = value.rpartition("/")
    if filepart == old_base:
        newfile = new_base
    elif filepart.startswith(old_stem + "-"):
        newfile = new_stem + filepart[len(old_stem):]
    else:
        return None
    return (dirpart + sep + newfile) if sep else newfile


def scan_gamelist(path, renames_here):
    """Build the edit list for one physical gamelist file: every <name> fix
    name_guard would flag, plus (for each rename whose system this gamelist
    belongs to) the matching <path> and every other tag in that <game> block
    referencing the old stem. Returns a dict; never raises (errors recorded,
    nothing applied for the entries they concern)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"path": path, "edits": [], "errors": ["cannot read: %s" % e]}

    edits = []
    errors = []

    for m in _FIELD_RE.finditer(data):
        if m.group(1) != b"name":
            continue
        raw = m.group(2)
        try:
            decoded = _decode(raw)
        except UnicodeDecodeError as e:
            errors.append("line %d: undecodable <name>: %s" % (_line_of(data, m.start()), e))
            continue
        chars = chars_to_fix(decoded)
        if not chars:
            continue
        new_raw = substitute_raw(raw, chars)
        try:
            new_decoded = _decode(new_raw)
        except UnicodeDecodeError:
            new_decoded = None
        if new_decoded is None or chars_to_fix(new_decoded):
            # A literal-byte replace could not clear the danger - e.g. the
            # character was numeric-entity-encoded rather than literal.
            # Never guess: leave it and report it as uncertain.
            errors.append("line %d: could not safely rewrite <name> %r (left unchanged)"
                          % (_line_of(data, m.start()), decoded))
            continue
        edits.append({"kind": "name", "line": _line_of(data, m.start()),
                      "start": m.start(2), "end": m.end(2), "new": new_raw,
                      "old_text": decoded, "new_text": new_decoded})

    for r in renames_here:
        if r.get("skip"):
            # The physical ROM is not being renamed (e.g. it has save/state
            # data): the gamelist must keep pointing at the OLD filename, so
            # <path> and every media tag are left untouched for this game.
            continue
        old_rel = "./" + r["old_base"]
        matches = []
        for m in _FIELD_RE.finditer(data):
            if m.group(1) != b"path":
                continue
            try:
                if _decode(m.group(2)) == old_rel:
                    matches.append(m)
            except UnicodeDecodeError:
                continue
        if len(matches) != 1:
            errors.append("expected exactly one <path>%s</path>, found %d"
                          % (old_rel, len(matches)))
            r["skip"] = True
            r["skip_reason"] = "gamelist <path> not found or ambiguous"
            continue
        pm = matches[0]
        block = _find_enclosing_game_block(data, pm.start())
        if block is None:
            errors.append("no enclosing <game> block for %s" % old_rel)
            r["skip"] = True
            r["skip_reason"] = "no enclosing <game> block in the gamelist"
            continue
        block_start, block_end = block
        r["gamelist"] = path
        for tm in _TAG_RE.finditer(data, block_start, block_end):
            traw = tm.group(2)
            if traw.startswith(b"<![CDATA["):
                continue     # media/path values are never CDATA in practice; skip, don't guess
            try:
                tdecoded = _decode(traw)
            except UnicodeDecodeError:
                continue
            new_text = _rewrite_media_ref(tdecoded, r["old_base"], r["new_base"],
                                          r["old_stem"], r["new_stem"])
            if new_text is None:
                continue
            edits.append({"kind": "path" if tm.group(1) == b"path" else "media",
                          "tag": tm.group(1).decode("ascii"),
                          "line": _line_of(data, tm.start()),
                          "start": tm.start(2), "end": tm.end(2),
                          "new": _encode_es(new_text),
                          "old_text": tdecoded, "new_text": new_text})

    edits.sort(key=lambda e: e["start"])
    for i in range(1, len(edits)):
        if edits[i]["start"] < edits[i - 1]["end"]:
            errors.append("overlapping edits at line %d and %d" %
                          (edits[i - 1]["line"], edits[i]["line"]))
    return {"path": path, "data_len": len(data), "edits": edits, "errors": errors}


def apply_edits(data, edits):
    """Return new bytes with every edit applied. Edits must be pre-sorted and
    non-overlapping (scan_gamelist / build_plan guarantee this, or the edit
    is dropped with an error instead of reaching here)."""
    out = bytearray()
    pos = 0
    for e in sorted(edits, key=lambda e: e["start"]):
        out += data[pos:e["start"]]
        out += e["new"]
        pos = e["end"]
    out += data[pos:]
    return bytes(out)


# ---------------------------------------------------------------------------
# Plan building

def build_plan(result, save_dirs=None):
    """From a name_guard.scan()-shaped dict, build the full plan: renames,
    per-gamelist edits, skips and exclusions. Never raises."""
    save_dirs = list(save_dirs) if save_dirs else list(DEFAULT_SAVE_DIRS)
    roots = result.get("roots") or list(name_guard.DEFAULT_ROOTS)
    system_index = _build_system_index(result)
    findings = result.get("findings") or []

    fs_groups = {}
    for f in findings:
        if f.get("source") == "filesystem" and f.get("field") == "filename":
            fs_groups.setdefault(file_key(f["file"]), []).append(f["file"])

    renames = []
    excluded = []
    hard_refusals = []
    for _key, paths in fs_groups.items():
        canon = pick_canonical(paths, roots)
        base = os.path.basename(canon)
        chars = chars_to_fix(base)
        if _RENAME_CHAR not in chars:
            excluded.append({"file": canon, "reason":
                             "not a '$' filename (%s): left alone" %
                             (", ".join(sorted(chars)) or "no dangerous character")})
            continue
        # Defence in depth (NM2): even before asking whether this is a
        # top-level ROM this simple tool knows how to rename, refuse HARD -
        # do not even add it to "excluded" and move on - if it is not
        # somewhere under one of ES's own declared system directories with a
        # matching extension. name_guard's own scoped scan should never
        # produce such a finding; this only matters for a stale or
        # hand-edited --guard-json result (e.g. a Steam file).
        if not path_under_a_known_system(canon, system_index):
            hard_refusals.append({"file": canon, "reason":
                                  "not under any ES system path with a matching extension "
                                  "(refusing, not just skipping)"})
            continue
        top, system = is_system_top_level(canon, system_index)
        if not top:
            excluded.append({"file": canon, "reason":
                             "not directly under a <root>/<system>/ folder: left alone"})
            continue
        system_dir = os.path.dirname(canon)
        stem, ext = os.path.splitext(base)
        new_base, _ = fix_text(base)
        new_stem = new_base[:len(new_base) - len(ext)] if ext else new_base
        media = find_media(system_dir, stem)
        media_renames = []
        for m in media:
            mdir, mname = os.path.split(m)
            new_mname = new_stem + mname[len(stem):]
            media_renames.append({"old": m, "new": os.path.join(mdir, new_mname)})
        saves = find_saves(system_dir, stem, save_dirs)
        renames.append({
            "system": system, "system_dir": system_dir,
            "old_path": canon, "new_path": os.path.join(system_dir, new_base),
            "old_base": base, "new_base": new_base,
            "old_stem": stem, "new_stem": new_stem,
            "media": media_renames,
            "gamelist": None,           # filled in by scan_gamelist
            "skip": bool(saves), "skip_reason": ("has save/state data: %s" %
                                                 ", ".join(saves)) if saves else None,
        })

    gl_groups = {}
    for f in findings:
        if f.get("source") == "gamelist":
            gl_groups.setdefault(file_key(f["file"]), []).append(f["file"])
    canonical_gamelists = [pick_canonical(paths, roots) for paths in gl_groups.values()]

    gamelist_plans = []
    for path in sorted(canonical_gamelists):
        system_dir = os.path.dirname(path)
        here = [r for r in renames if os.path.realpath(r["system_dir"]) == os.path.realpath(system_dir)]
        gamelist_plans.append(scan_gamelist(path, here))

    return {
        "roots": roots,
        "renames": renames,
        "gamelist_plans": gamelist_plans,
        "excluded": excluded,
        "hard_refusals": hard_refusals,
        "scan_summary": {k: result.get(k) for k in
                         ("files_checked", "gamelists", "entries_checked", "elapsed_s")},
    }


def plan_counts(plan):
    n_names = sum(1 for gp in plan["gamelist_plans"] for e in gp["edits"] if e["kind"] == "name")
    n_media_edits = sum(1 for gp in plan["gamelist_plans"] for e in gp["edits"] if e["kind"] != "name")
    n_renames = sum(1 for r in plan["renames"] if not r["skip"])
    n_skipped = sum(1 for r in plan["renames"] if r["skip"])
    n_media_files = sum(len(r["media"]) for r in plan["renames"] if not r["skip"])
    n_errors = sum(len(gp["errors"]) for gp in plan["gamelist_plans"])
    return {"name_fixes": n_names, "path_and_media_tag_fixes": n_media_edits,
            "rom_renames": n_renames, "media_file_renames": n_media_files,
            "skipped_renames": n_skipped, "excluded": len(plan["excluded"]),
            "hard_refusals": len(plan.get("hard_refusals") or ()),
            "gamelist_errors": n_errors}


def render_plan(plan, verbose=True):
    c = plan_counts(plan)
    lines = ["fix-bad-names: plan (%s)" % ", ".join("%s=%d" % kv for kv in c.items())]
    for gp in plan["gamelist_plans"]:
        if not gp["edits"] and not gp["errors"]:
            continue
        lines.append("gamelist %s" % gp["path"])
        for e in gp["edits"]:
            lines.append("  line %d <%s>: %r -> %r" %
                         (e["line"], e.get("tag", "name"), e["old_text"], e["new_text"]))
        for err in gp["errors"]:
            lines.append("  ERROR: %s" % err)
    for r in plan["renames"]:
        tag = "SKIP" if r["skip"] else "RENAME"
        lines.append("%s rom %s -> %s" % (tag, r["old_path"], r["new_path"]))
        if r["skip"]:
            lines.append("  reason: %s" % r["skip_reason"])
            continue
        for m in r["media"]:
            lines.append("  media %s -> %s" % (m["old"], m["new"]))
        if r["gamelist"]:
            lines.append("  gamelist: %s" % r["gamelist"])
    if verbose:
        for x in plan["excluded"]:
            lines.append("EXCLUDED %s: %s" % (x["file"], x["reason"]))
    for x in plan.get("hard_refusals") or ():
        lines.append("REFUSED %s: %s" % (x["file"], x["reason"]))
    return "\n".join(lines) + "\n"


def _edit_view(e):
    """A JSON-safe view of one edit: drops the raw byte offsets/replacement
    bytes (internal to apply_edits), keeps what a human or --undo audit
    needs to see."""
    v = {"kind": e["kind"], "line": e["line"], "old_text": e["old_text"],
        "new_text": e["new_text"]}
    if "tag" in e:
        v["tag"] = e["tag"]
    return v


def plan_to_jsonable(plan):
    return {
        "roots": plan["roots"],
        "renames": plan["renames"],
        "gamelist_plans": [{"path": gp["path"], "errors": gp["errors"],
                            "edits": [_edit_view(e) for e in gp["edits"]]}
                           for gp in plan["gamelist_plans"]],
        "excluded": plan["excluded"],
        "hard_refusals": plan.get("hard_refusals") or [],
        "scan_summary": plan["scan_summary"],
    }


def plan_to_json(plan):
    return json.dumps(plan_to_jsonable(plan), ensure_ascii=True, indent=1)


# ---------------------------------------------------------------------------
# Apply / undo

def validate_plan(plan):
    """Every reason --apply must refuse, computed WITHOUT changing anything."""
    problems = []
    for x in plan.get("hard_refusals") or ():
        problems.append("REFUSED (defence in depth): %s: %s" % (x["file"], x["reason"]))
    for gp in plan["gamelist_plans"]:
        if gp["errors"]:
            problems.append("gamelist %s has unresolved issues: %s" %
                            (gp["path"], "; ".join(gp["errors"])))
    seen_targets = set()
    for r in plan["renames"]:
        if r["skip"]:
            continue
        if r.get("gamelist") is None:
            problems.append("rename %s has no confirmed gamelist reference" % r["old_path"])
        targets = [r["new_path"]] + [m["new"] for m in r["media"]]
        for t in targets:
            if os.path.lexists(t):
                problems.append("rename target already exists: %s" % t)
            if t in seen_targets:
                problems.append("two renames would produce the same target: %s" % t)
            seen_targets.add(t)
    return problems


def safe_rename(old, new):
    if os.path.lexists(new):
        raise FileExistsError("target exists: %s" % new)
    os.rename(old, new)


def apply_plan(plan, log_dir="."):
    problems = validate_plan(plan)
    if problems:
        sys.stderr.write("fix-bad-names: REFUSING to apply - %d problem(s):\n" % len(problems))
        for p in problems:
            sys.stderr.write("  %s\n" % p)
        return None

    ts = time.strftime("%Y%m%d-%H%M%S")
    undo = {"version": 1, "created": ts, "gamelist_backups": [], "renames": []}

    # 1) back up every gamelist that will be edited
    for gp in plan["gamelist_plans"]:
        if not gp["edits"]:
            continue
        backup = gp["path"] + ".bak-" + ts
        shutil.copy2(gp["path"], backup)
        undo["gamelist_backups"].append({"gamelist": gp["path"], "backup": backup})

    # 2) write every edited gamelist (temp file + atomic replace)
    for gp in plan["gamelist_plans"]:
        if not gp["edits"]:
            continue
        with open(gp["path"], "rb") as f:
            data = f.read()
        new_data = apply_edits(data, gp["edits"])
        tmp = gp["path"] + ".tmp-%s" % ts
        with open(tmp, "wb") as f:
            f.write(new_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, gp["path"])

    # 3) renames (media first, then the ROM itself)
    for r in plan["renames"]:
        if r["skip"]:
            continue
        for m in r["media"]:
            safe_rename(m["old"], m["new"])
            undo["renames"].append({"old": m["old"], "new": m["new"], "kind": "media"})
        safe_rename(r["old_path"], r["new_path"])
        undo["renames"].append({"old": r["old_path"], "new": r["new_path"], "kind": "rom"})

    log_path = os.path.join(log_dir, "NM1-undo-%s.json" % ts)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(undo, f, ensure_ascii=True, indent=1)
    return log_path


def undo_plan(log_path):
    with open(log_path, "r", encoding="utf-8") as f:
        undo = json.load(f)
    problems = []
    for rec in undo.get("renames", []):
        if not os.path.lexists(rec["new"]):
            problems.append("undo: rename target missing, cannot revert: %s" % rec["new"])
        elif os.path.lexists(rec["old"]):
            problems.append("undo: original path already exists, refusing to overwrite: %s"
                            % rec["old"])
    for rec in undo.get("gamelist_backups", []):
        if not os.path.isfile(rec["backup"]):
            problems.append("undo: backup missing: %s" % rec["backup"])
    if problems:
        for p in problems:
            sys.stderr.write(p + "\n")
        return False
    for rec in reversed(undo.get("renames", [])):
        os.rename(rec["new"], rec["old"])
    for rec in undo.get("gamelist_backups", []):
        shutil.copy2(rec["backup"], rec["gamelist"])
    return True


# ---------------------------------------------------------------------------

def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--guard-json", metavar="FILE", help="read a saved name_guard --json result "
                    "instead of scanning")
    ap.add_argument("--es-systems-cfg", default=None, metavar="FILE",
                    help="passed through to name_guard.scan() (ignored with --guard-json)")
    ap.add_argument("--root", action="append", default=None, metavar="DIR")
    ap.add_argument("--gamelist-dir", action="append", default=None, metavar="DIR")
    ap.add_argument("--save-dir", action="append", default=None, metavar="DIR",
                    help="extra save/state dir to check (repeatable; default: %s)"
                    % ", ".join(DEFAULT_SAVE_DIRS))
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--i-stopped-es", action="store_true",
                    help="required with --apply: confirms ES is stopped")
    ap.add_argument("--undo", metavar="LOG", help="revert everything an earlier --apply did")
    ap.add_argument("--log-dir", default=".", metavar="DIR",
                    help="where --apply writes the undo log (default: cwd)")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    ap.add_argument("--out", metavar="FILE", help="also write the plan (JSON if --json) here")
    a = ap.parse_args(argv)

    if a.undo:
        ok = undo_plan(a.undo)
        if ok:
            print("fix-bad-names: undo complete (%s)" % a.undo)
            return 0
        print("fix-bad-names: undo FAILED - nothing further was changed", file=sys.stderr)
        return 5

    if a.guard_json:
        with open(a.guard_json, "r", encoding="utf-8") as f:
            result = json.load(f)
    else:
        gds = a.gamelist_dir if a.gamelist_dir is not None else list(name_guard.DEFAULT_GAMELIST_DIRS)
        result = name_guard.scan(a.es_systems_cfg, a.root, gds)

    if result.get("nothing_scanned"):
        print("fix-bad-names: nothing scanned (no ROM root found: %s)" %
              ", ".join(result.get("roots_missing") or ()) or "none given")
        return 3

    plan = build_plan(result, save_dirs=a.save_dir)
    text = render_plan(plan)
    out = plan_to_json(plan) if a.json else text
    print(out)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(out)

    if not a.apply:
        return 0

    if not a.i_stopped_es:
        print("fix-bad-names: REFUSING to apply - pass --i-stopped-es (ES rewrites gamelists "
              "from memory; it must be stopped first)", file=sys.stderr)
        return 4

    log_path = apply_plan(plan, log_dir=a.log_dir)
    if log_path is None:
        return 4
    print("fix-bad-names: applied. Undo log: %s" % log_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as e:          # noqa: BLE001 - report, never a bare traceback
        sys.stderr.write("fix-bad-names: %s: %s\n" % (type(e).__name__, e))
        sys.exit(1)
