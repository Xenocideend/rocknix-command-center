#!/usr/bin/env python3
"""name_guard - find game names and paths that EmulationStation's hook
command line lets the shell re-parse (RV1-M3; owner's decision, 23 Sep).

Why: ES runs event hooks ASYNCHRONOUSLY through `sh -c` (Scripting.cpp
executeScript(), then Platform.cpp's fork + execl("/bin/sh", "sh", "-c", ...)).
It builds that command string itself: an argument is wrapped in double quotes
ONLY if it contains an ASCII space (find(" ")), every '"' is stripped first,
and nothing is escaped. So before an rp5deck hook ever runs, the shell has
already parsed the game's name, ROM path and ROM file name:

  * `$` and backticks are expanded in EVERY value, quoted or not: `$(...)` or
    `...` in a scraped name EXECUTES AS ROOT whenever the game is highlighted,
    and `Ma$ter` silently becomes `Ma`.
  * In a value WITHOUT an ASCII space (left unquoted): `' " & ( ) ; | < > \\`
    and newlines are shell syntax: `Tom&Jerry` backgrounds a truncated hook
    and runs `Jerry` as a command, `Disc(1)` is a syntax error (hook never
    runs), `Sonic's` starts a quote that eats the rest of the line.
  * "Space" means ASCII 0x20 only. A scraped name whose gaps are U+00A0 or tabs
    counts as space-less for ES and is passed unquoted.

The rp5deck hooks themselves are injection-safe; the damage happens before
exec, so it cannot be fixed inside a hook. The owner's decision: install
refuses (tools/install-es-hooks.sh calls `python3 name_guard.py --json`),
each app start warns in the log and on screen (check_async()), and an
upstream ES patch that single-quotes each argument is drafted in es-upstream/.

WHAT IS SCANNED (read-only, never raises, ~100k files in seconds), 24 Sep
test-day fix (NM2): ES only ever discovers a game through its own
es_systems.cfg (on the device: /storage/.config/emulationstation/
es_systems.cfg, falling back to /usr/share/emulationstation/es_systems.cfg -
the same file ES itself reads at startup). So this guard now scans exactly,
and only, what that file says ES will show:

  * for every <system>, its <path> (a %ROMPATH%-style variable is expanded if
    present, though none of the device's 139 systems use one) and its space-
    separated, case-insensitive <extension> list. A FILE under a system's
    path counts only if its extension is one of that system's; a DIRECTORY
    counts only if it holds a matching file at this level or deeper (ES shows
    sub-folders too, but a folder with nothing playable in it is never a
    "game" ES passes to a hook - see walk_system()'s docstring for the exact
    choice made here). This is what makes Steam's ROM-shaped game data
    (steamapps/, userdata/, the Wine runtime - <system>steam</system>'s real
    <path> is /storage/.local/share/applications, extension .desktop) and a
    theme's .svg (no <system> named "themes" exists at all) impossible to
    reach: they are never under any system's declared path;
  * every gamelist.xml where ES loads one - <system path>/gamelist.xml, and
    <gamelist dir>/<system>/gamelist.xml - each <name> and <path> (copies
    deeper down, e.g. backups, are counted but not read: ES never loads
    them). Gamelists are read as TEXT, never as one XML document: some have a
    second root element (<alternativeEmulator> beside <gameList>), ES escapes
    only '&', and a broken block must not hide the rest. Values are
    unescaped exactly as an XML parser would (&amp; &lt; &gt; &quot; &apos;
    and numeric references; CDATA kept literal), because ES passes the
    DECODED text.

If es_systems.cfg cannot be found or parsed, the guard falls back to the
pre-NM2 behaviour (scan every file/folder name under --root / the default ROM
roots, no extension filter) rather than refusing outright - the same
"nothing scanned is not the same as safe" rule as a missing ROM root, just
one level up. --root, when es_systems.cfg WAS read, instead narrows which
systems are scanned (only systems whose path sits under one of the given
directories); --es-systems-cfg overrides which file is read as ES's system
list. Two systems (or a system and a --root filter) that turn out to be the
same physical directory (a bind mount, e.g. /storage/roms and
/storage/games-internal/roms on this device) are only ever scanned once:
deduped by realpath AND by (st_dev, st_ino), so a non-realpath alias of the
same inode cannot double every finding.

    python3 name_guard.py [--json] [--es-systems-cfg FILE] [--root DIR]...
                          [--gamelist-dir DIR]...
    exit 0 = nothing dangerous, 1 = findings, 3 = nothing scanned (no root
    found: a guard that saw nothing must not say "safe"), 2 = usage error.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET

log = logging.getLogger("rp5deck.name_guard")

# Legacy ROM roots: used only when es_systems.cfg cannot be found/parsed (see
# the module docstring), or as the fallback --root list in that situation.
DEFAULT_ROOTS = ("/storage/roms", "/storage/games-internal/roms",
                 "/storage/games-external/roms")
DEFAULT_GAMELIST_DIRS = ("/storage/.config/emulationstation/gamelists",)

# The file ES itself reads its system list from (checked in order).
DEFAULT_ES_SYSTEMS_CFG = ("/storage/.config/emulationstation/es_systems.cfg",
                          "/usr/share/emulationstation/es_systems.cfg")

# Folder names that hold ES media, not games: ES never passes these to a hook.
SKIP_DIRS = frozenset(("images", "videos", "manuals", "media", "downloaded_images",
                       "downloaded_videos", "downloaded_manuals", "bios"))

ALWAYS = {"$": "$ (expanded even inside ES's double quotes)",
          "`": "backtick (command substitution even inside ES's double quotes)"}
UNQUOTED = {"'": "' (starts a shell quote)", '"': '" (stripped by ES)',
            "&": "& (backgrounds the hook, runs the rest as a command)",
            "(": "( (shell syntax error: the hook never runs)",
            ")": ") (shell syntax error: the hook never runs)",
            ";": "; (ends the hook command, runs the rest)",
            "|": "| (pipes the hook into the rest)",
            "<": "< (redirects the hook's input)",
            ">": "> (redirects the hook's output: can create files)",
            "\\": "\\ (shell escape: the character after it is changed)",
            "\n": "newline (ends the hook command, runs the rest)"}
_ALWAYS_RE = re.compile("[$`]")
_UNQUOTED_RE = re.compile("[%s]" % re.escape("".join(UNQUOTED)))
_SUFFIX = " in a value without an ASCII space (ES leaves it unquoted)"

_FIELD_RE = re.compile(r"<(name|path)>(<!\[CDATA\[.*?\]\]>|[^<]*)</\1>", re.DOTALL)
_ENTITY_RE = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
MAX_ERRORS = 50


def value_reasons(value):
    """Why ES's hook command line makes this one argument value dangerous,
    as a list of human-readable reasons ([] = safe). Pure."""
    if not isinstance(value, str) or not value:
        return []
    out = []
    if _ALWAYS_RE.search(value):
        out.extend(ALWAYS[c] for c in "$`" if c in value)
    if " " not in value and _UNQUOTED_RE.search(value):
        out.extend(UNQUOTED[c] + _SUFFIX for c in UNQUOTED if c in value)
    return out


def es_command_line(script, args):
    """The command string ES builds for one hook: a line-by-line port of
    ROCKNIX/emulationstation-next es-core/src/Scripting.cpp executeScript()
    (non-Windows, split form, so no event name is added). Used by the tests
    and mirrored in sh by tools/install-es-hooks.sh's self-test."""
    cmd = '"%s"' % script if " " in script else script
    for arg in (list(args) + ["", "", ""])[:3]:          # arg1, arg2, arg3
        if not arg:
            break                                        # ES stops at the first empty one
        data = arg
        if data.startswith('"'):                         # (ES tests startsWith twice)
            data = data[1:len(data) - 1] if len(data) >= 2 else ""
            data = data.replace('"', "")
        data = data.replace('"', "")
        cmd += (' "%s"' % data) if " " in data else (" " + data)
    return cmd


def es_wrapped(command, logdir):
    """Platform.cpp ProcessStartInfo::run()'s non-waiting wrapper, as quoted
    in review/RV1-findings.md; ES hands this string to execl("/bin/sh", "sh",
    "-c", ...) from a double fork and never waits for it."""
    return ("((((" + command + " 2> " + logdir + "/es_script_stderr.log ; echo $? >&3) | "
            "head -300 > " + logdir + "/es_script_stdout.log) 3>&1) | (read xs; exit $xs))")


def xml_text(raw):
    """Element text as an XML parser returns it (what ES passes)."""
    if raw.startswith("<![CDATA[") and raw.endswith("]]>"):
        return raw[9:-3]

    def ent(m):
        e = m.group(1)
        if e[0] != "#":
            return _NAMED[e]
        try:
            return chr(int(e[2:], 16) if e[1] in "xX" else int(e[1:]))
        except (ValueError, OverflowError):
            return m.group(0)
    return _ENTITY_RE.sub(ent, raw)


# ---------------------------------------------------------------------------
# es_systems.cfg: ES's own system list (NM2, 24 Sep 2026 test-day fix).

def _expand_path_vars(p):
    """es_systems.cfg's %VAR%-style substitutions. On this device's real
    es_systems.cfg (139 systems, checked 24 Sep 2026) every <path> is already
    a plain absolute path and none use a variable, but the format allows
    %ROMPATH% (seen in some ES-DE-derived builds); expand it defensively so a
    future cfg using it is still scanned correctly rather than silently
    skipped."""
    if "%ROMPATH%" in p:
        p = p.replace("%ROMPATH%", DEFAULT_ROOTS[0])
    return p


def parse_es_systems(cfg_path):
    """Parse an es_systems.cfg file into [{"name", "path", "extensions"
    (frozenset, lowercase, with the leading dot)}, ...]. A <system> with no
    (or empty) <path> is skipped. Never raises: returns ([], error string) on
    a read/parse failure."""
    try:
        tree = ET.parse(cfg_path)
    except (ET.ParseError, OSError, ValueError) as e:
        return [], "cannot parse %s: %s" % (cfg_path, e)
    systems = []
    for node in tree.getroot().iter("system"):
        path_el = node.find("path")
        if path_el is None or not (path_el.text or "").strip():
            continue
        p = _expand_path_vars(path_el.text.strip())
        name_el = node.find("name")
        name = (name_el.text or "").strip() if name_el is not None and name_el.text else ""
        ext_el = node.find("extension")
        exts = frozenset()
        if ext_el is not None and ext_el.text:
            exts = frozenset(x.lower() for x in ext_el.text.split() if x)
        systems.append({"name": name, "path": p, "extensions": exts})
    return systems, None


def _locate_es_systems_cfg(explicit):
    """The first existing candidate: `explicit` if given, else
    DEFAULT_ES_SYSTEMS_CFG in order. None if nothing exists."""
    for c in ([explicit] if explicit else list(DEFAULT_ES_SYSTEMS_CFG)):
        if c and os.path.isfile(c):
            return c
    return None


def _dedupe_paths(paths):
    """realpath a list of directories, drop anything that is not a directory
    (recorded in `missing`), and dedupe by realpath AND by (st_dev, st_ino) -
    a bind-mounted pair (same underlying directory, different mount path) is
    kept once. Returns (deduped_realpaths, missing_originals)."""
    out, missing, seen_rp, seen_key = [], [], set(), set()
    for p in paths or ():
        if not isinstance(p, str) or not p:
            continue
        try:
            rp = os.path.realpath(p)
            if not os.path.isdir(rp):
                missing.append(p)
                continue
            st = os.stat(rp)
            key = (st.st_dev, st.st_ino)
        except (OSError, ValueError):
            missing.append(p)
            continue
        if rp in seen_rp or (key != (0, 0) and key in seen_key):
            continue
        seen_rp.add(rp)
        seen_key.add(key)
        out.append(rp)
    return out, missing


def _dedupe_systems(systems):
    """Like _dedupe_paths, but for [{"name", "path", "extensions"}, ...]:
    keeps each system's name/extensions, keyed off its realpath'd <path>. Two
    <system>s (or a system and its bind-mounted twin) that resolve to the
    same physical directory are scanned once - the first one wins."""
    out, missing, seen_rp, seen_key = [], [], set(), set()
    for sysinfo in systems:
        p = sysinfo.get("path")
        if not isinstance(p, str) or not p:
            continue
        try:
            rp = os.path.realpath(p)
            if not os.path.isdir(rp):
                missing.append(p)
                continue
            st = os.stat(rp)
            key = (st.st_dev, st.st_ino)
        except (OSError, ValueError):
            missing.append(p)
            continue
        if rp in seen_rp or (key != (0, 0) and key in seen_key):
            continue
        seen_rp.add(rp)
        seen_key.add(key)
        out.append({"name": sysinfo.get("name") or "", "path": rp,
                    "extensions": sysinfo.get("extensions") or frozenset()})
    return out, missing


def _under_any(path, root_dirs):
    for r in root_dirs:
        if path == r or path.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


class _Scan:
    def __init__(self):
        self.findings = []
        self.errors = []
        self.error_count = 0
        self.files = 0
        self.gamelists = 0
        self.entries = 0
        self.skipped_gamelists = 0
        self._seen = set()

    def error(self, msg):
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(msg)

    def check(self, source, path, field, value, line=None, reasons=None):
        reasons = reasons or value_reasons(value)
        if not reasons:
            return
        k = (path, field, value)
        if k in self._seen:
            return
        self._seen.add(k)
        f = {"source": source, "file": path, "field": field, "value": value,
             "reasons": reasons}
        if line is not None:
            f["line"] = line
        self.findings.append(f)

    # -- gamelists -----------------------------------------------------------
    def gamelist(self, path):
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError as e:
            self.error("cannot read %s: %s" % (path, e))
            return
        self.gamelists += 1
        for m in _FIELD_RE.finditer(text):
            self.entries += 1
            field, value = m.group(1), xml_text(m.group(2))     # NOT stripped: ES passes it as is
            reasons = value_reasons(value)
            if not reasons and field == "path":
                # game-start/end also pass the bare file name, unquoted when
                # IT has no space even if the directory part does
                base = value.rsplit("/", 1)[-1]
                reasons = [r + " (in the file name %r)" % base for r in value_reasons(base)]
            if reasons:
                self.check("gamelist", path, field, value,
                           line=text.count("\n", 0, m.start()) + 1, reasons=reasons)

    # -- the filesystem, legacy (unscoped) mode -------------------------------
    def walk(self, root):
        """Pre-NM2 behaviour: every file/folder name under `root`, no
        extension filter. Used only when es_systems.cfg cannot be found or
        parsed (see the module docstring) - a fallback, not the normal path."""
        stack = [(root, 0)]
        visited = set()
        while stack:
            d, depth = stack.pop()
            try:
                st = os.stat(d)
                key = (st.st_dev, st.st_ino)
                if key in visited and key != (0, 0):
                    continue
                visited.add(key)
                it = os.scandir(d)
            except OSError as e:
                self.error("cannot list %s: %s" % (d, e))
                continue
            with it:
                for e in it:
                    try:
                        name = e.name
                        is_dir = e.is_dir(follow_symlinks=False)
                        if not is_dir and e.is_symlink() and depth == 0:
                            is_dir = e.is_dir()          # a symlinked system folder
                        # deeper links are not followed: Steam's Wine prefix has
                        # dosdevices/z: -> / (1320 findings from /var/run on the device)
                    except OSError as err:
                        self.error("cannot stat %s: %s" % (e.path, err))
                        continue
                    if is_dir:
                        if name.startswith(".") or name.lower() in SKIP_DIRS:
                            continue
                        self.files += 1
                        self.check("filesystem", e.path, "dirname", name)
                        if depth < 12:
                            stack.append((e.path, depth + 1))
                        continue
                    self.files += 1
                    if name == "gamelist.xml":
                        if depth == 1:          # <root>/<system>/gamelist.xml: what ES loads
                            self.gamelist(e.path)
                        else:
                            self.skipped_gamelists += 1     # a backup copy ES never reads
                        continue
                    # (the full path is an argument too, but a dangerous full
                    # path needs a dangerous space-less component, and that is
                    # already reported as its own dirname / filename finding)
                    self.check("filesystem", e.path, "filename", name)

    # -- the filesystem, scoped mode (NM2) ------------------------------------
    def walk_system(self, path, extensions):
        """Recursive, extension-scoped walk of one ES system directory. The
        directory itself may be a symlink (the caller has already realpath'd
        it - reading through it happens naturally); nothing found WHILE
        SCANNING is ever followed, at any depth, so a deep symlink (Steam's
        pfx/dosdevices/z: -> /, 1320 findings from /var/run on the device
        under the old unscoped walk) can never be reached.

        A FILE counts only if its extension is one of `extensions`
        (case-insensitive). ES also shows sub-folders (a game can be
        "<system>/Some Folder/Some Game.ext"), so a DIRECTORY counts too, but
        only if it holds a matching file at this level or deeper - an empty
        folder, or one holding only non-ROM litter, is never something ES
        passes to a hook. (Choice made here, per NM2's brief: "if unsure,
        include directories that contain at least one matching file at any
        depth" - this is that choice.)

        Returns True iff `path` itself qualifies (has a match at or below
        it), so the caller can decide whether to check ITS OWN name too."""
        return self._walk(path, 0, extensions, set())

    def _walk(self, d, depth, extensions, visited):
        try:
            st = os.stat(d)
            key = (st.st_dev, st.st_ino)
            if key in visited and key != (0, 0):
                return False
            visited.add(key)
            it = os.scandir(d)
        except OSError as e:
            self.error("cannot list %s: %s" % (d, e))
            return False
        has_match = False
        subdirs = []
        with it:
            for e in it:
                try:
                    name = e.name
                    is_dir = e.is_dir(follow_symlinks=False)   # never followed at any depth
                except OSError as err:
                    self.error("cannot stat %s: %s" % (e.path, err))
                    continue
                if is_dir:
                    if name.startswith(".") or name.lower() in SKIP_DIRS:
                        continue
                    self.files += 1
                    if depth < 12:
                        subdirs.append((e.path, name))
                    continue
                self.files += 1
                if name == "gamelist.xml":
                    if depth == 0:              # <system path>/gamelist.xml: what ES loads
                        self.gamelist(e.path)
                    else:
                        self.skipped_gamelists += 1     # a backup copy ES never reads
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in extensions:
                    continue                    # not one of this system's ROM extensions
                has_match = True
                self.check("filesystem", e.path, "filename", name)
        for child_path, child_name in subdirs:
            if self._walk(child_path, depth + 1, extensions, visited):
                has_match = True
                self.check("filesystem", child_path, "dirname", child_name)
        return has_match


def scan(es_systems_cfg=None, roots=None, gamelist_dirs=DEFAULT_GAMELIST_DIRS):
    """Scan what ES's own es_systems.cfg says it will show, plus gamelists.
    Never raises. Returns a JSON-able dict:

    ok (True only if something was scanned and nothing is dangerous),
    nothing_scanned, roots (the directories actually walked - system paths in
    the normal case, legacy ROM roots if es_systems.cfg could not be used),
    roots_missing, systems ([{name, path, extensions}] actually scanned - []
    in legacy mode), es_systems_cfg (the file read, or None), es_systems_cfg_error,
    legacy_scan (True if es_systems.cfg could not be found/parsed and the
    pre-NM2 unscoped walk ran instead), files_checked, gamelists,
    entries_checked, findings [{source, file, field, value, reasons, line?}],
    errors (first MAX_ERRORS), error_count, elapsed_s.

    es_systems_cfg: explicit path to ES's system list (default: try
    DEFAULT_ES_SYSTEMS_CFG in order).
    roots: when es_systems.cfg was read, narrows scanning to only the
    systems whose path sits under one of these directories (default: every
    system). When es_systems.cfg could NOT be read, this is instead the list
    of ROM roots to scan the old, unscoped way (default: DEFAULT_ROOTS)."""
    t0 = time.monotonic()
    s = _Scan()
    rts, missing, systems_info = [], [], []
    cfg_used, cfg_error, legacy = None, None, False
    try:
        cfg_used = _locate_es_systems_cfg(es_systems_cfg)
        systems_raw = []
        if cfg_used:
            systems_raw, cfg_error = parse_es_systems(cfg_used)
        else:
            tried = [es_systems_cfg] if es_systems_cfg else list(DEFAULT_ES_SYSTEMS_CFG)
            cfg_error = "no es_systems.cfg found (tried: %s)" % ", ".join(tried)

        deduped, sys_missing = _dedupe_systems(systems_raw) if systems_raw else ([], [])
        if deduped and roots:
            root_dirs, root_missing = _dedupe_paths(roots)
            deduped = [d for d in deduped if _under_any(d["path"], root_dirs)]
            missing = sys_missing + root_missing
        else:
            missing = sys_missing

        if deduped:
            for d in deduped:
                s.walk_system(d["path"], d["extensions"])
            rts = [d["path"] for d in deduped]
            systems_info = [{"name": d["name"], "path": d["path"],
                             "extensions": sorted(d["extensions"])} for d in deduped]
        else:
            legacy = True
            legacy_roots = roots if roots is not None else DEFAULT_ROOTS
            rts, missing = _dedupe_paths(legacy_roots)
            for r in rts:
                s.walk(r)

        gds, _ = _dedupe_paths(gamelist_dirs)
        for g in gds:
            if _under_any(g, rts):
                continue                      # already walked as part of a system/root
            try:
                subs = sorted(os.listdir(g))
            except OSError as e:
                s.error("cannot list %s: %s" % (g, e))
                subs = []
            for sub in subs:                  # <dir>/<system>/gamelist.xml only
                p = os.path.join(g, sub, "gamelist.xml")
                if os.path.isfile(p):
                    s.gamelist(p)
    except Exception as e:                   # noqa: BLE001 - the guard must never raise
        s.error("scan failed: %s: %s" % (type(e).__name__, e))
    nothing = not rts or (s.files == 0 and s.gamelists == 0)
    return {
        "version": 1,
        "ok": not s.findings and not nothing,
        "nothing_scanned": nothing,
        "roots": rts,
        "roots_missing": missing,
        "systems": systems_info,
        "es_systems_cfg": cfg_used,
        "es_systems_cfg_error": cfg_error,
        "legacy_scan": legacy,
        "files_checked": s.files,
        "gamelists": s.gamelists,
        "entries_checked": s.entries,
        "gamelists_skipped": s.skipped_gamelists,
        "findings": s.findings,
        "errors": s.errors,
        "error_count": s.error_count,
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def describe(f):
    where = f["file"] + (":%d" % f["line"] if f.get("line") else "")
    return "%s [%s %s] %r: %s" % (where, f["source"], f["field"], f["value"],
                                   "; ".join(f["reasons"]))


def _can_run_commands(value):
    """True only for the subset of dangerous values that can actually EXECUTE
    something: $( ) command substitution or a backtick. Plain '$' (variable
    expansion, e.g. 'Ma$ter') and the space-less metacharacters ('&', ';', ...)
    are garbled/mangled by ES's quoting but do not run arbitrary commands."""
    return isinstance(value, str) and ("$(" in value or "`" in value)


def summary(result):
    """One short line for the UI ("" when there is nothing to warn about).

    Most findings are names ES's hook quoting GARBLES (apostrophes, '&',
    ';', ...): the name is mangled, nothing runs. Only $( ) or a backtick in
    an actual value can execute a command, so that stronger wording is added
    only when a finding actually contains one."""
    if not result or result.get("ok"):
        return ""
    findings = result.get("findings") or ()
    n = len(findings)
    if not n:
        return ""                              # nothing_scanned or otherwise: nothing to say
    plural = n != 1
    msg = ("%d game name%s %s garbled by ES's hook quoting - see log"
           % (n, "s" if plural else "", "are" if plural else "is"))
    if any(_can_run_commands(f.get("value")) for f in findings):
        msg += " (some could run commands - see log)"
    return msg


def check(log_fn=None, es_systems_cfg=None, roots=None, gamelist_dirs=DEFAULT_GAMELIST_DIRS,
         show=10):
    """Scan and log a warning if anything is dangerous. Returns the result."""
    log_fn = log_fn or log.warning
    r = scan(es_systems_cfg, roots, gamelist_dirs)
    try:
        if r["findings"]:
            log_fn("name guard: %d dangerous game name/path value(s) in %d files and %d "
                   "gamelists: ES would shell-parse them when it runs event hooks "
                   "(RV1-M3). First %d:" % (len(r["findings"]), r["files_checked"],
                                            r["gamelists"], min(show, len(r["findings"]))))
            for f in r["findings"][:show]:
                log_fn("name guard:   " + describe(f))
        elif r["nothing_scanned"]:
            log.info("name guard: no ROM root found (%s): nothing scanned",
                     ", ".join(r["roots_missing"]) or "none given")
        else:
            log.info("name guard: %d files, %d gamelists, nothing dangerous (%.1f s)",
                     r["files_checked"], r["gamelists"], r["elapsed_s"])
    except Exception:                         # noqa: BLE001
        log.exception("name guard: logging failed")
    return r


def check_async(on_done=None, log_fn=None, es_systems_cfg=None, roots=None,
                gamelist_dirs=DEFAULT_GAMELIST_DIRS):
    """check() on a daemon thread (a cold SD card can take a while), then
    on_done(result) FROM THAT THREAD - the app posts it to its UI loop."""
    def run():
        try:
            r = check(log_fn, es_systems_cfg, roots, gamelist_dirs)
            if on_done is not None:
                on_done(r)
        except Exception:                     # noqa: BLE001 - a background check must not crash
            log.exception("name guard: background check failed")
    t = threading.Thread(target=run, name="rp5deck-name-guard", daemon=True)
    t.start()
    return t


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--es-systems-cfg", default=None, metavar="FILE",
                    help="ES's own system list (default: try %s)" %
                    ", ".join(DEFAULT_ES_SYSTEMS_CFG))
    ap.add_argument("--root", action="append", default=None, metavar="DIR",
                    help="repeatable; when es_systems.cfg can be read, restricts scanning "
                    "to systems whose path is under DIR; otherwise DIR is scanned the old, "
                    "unscoped way (default without --root: every system in es_systems.cfg, "
                    "or if that cannot be read: %s)" % ", ".join(DEFAULT_ROOTS))
    ap.add_argument("--gamelist-dir", action="append", default=None, metavar="DIR",
                    help="extra gamelist dir (default: %s)" % ", ".join(DEFAULT_GAMELIST_DIRS))
    ap.add_argument("--no-default-gamelist-dirs", action="store_true")
    ap.add_argument("--show", type=int, default=200, help="text mode: findings to list")
    a = ap.parse_args(argv)
    gds = a.gamelist_dir or ([] if a.no_default_gamelist_dirs else list(DEFAULT_GAMELIST_DIRS))
    try:
        r = scan(a.es_systems_cfg, a.root, gds)
    except Exception as e:                   # noqa: BLE001 - scan() never raises; belt and braces
        r = {"version": 1, "ok": False, "nothing_scanned": True, "findings": [],
             "errors": ["%s: %s" % (type(e).__name__, e)], "roots": [], "roots_missing": [],
             "systems": [], "es_systems_cfg": None, "es_systems_cfg_error": None,
             "legacy_scan": False, "files_checked": 0, "gamelists": 0, "entries_checked": 0,
             "error_count": 1}
    if a.json:
        sys.stdout.write(json.dumps(r, ensure_ascii=True, indent=1) + "\n")
    else:
        print("name guard: scanned %d files and %d gamelists (%d entries) under %s"
              % (r["files_checked"], r["gamelists"], r["entries_checked"],
                 ", ".join(r["roots"]) or "nothing"))
        if r.get("legacy_scan"):
            print("  NOTE: es_systems.cfg not usable (%s) - scanned the old, unscoped way"
                  % (r.get("es_systems_cfg_error") or "?"))
        elif r.get("es_systems_cfg"):
            print("  systems from %s" % r["es_systems_cfg"])
        for f in r["findings"][:a.show]:
            print("  " + describe(f))
        if len(r["findings"]) > a.show:
            print("  ... and %d more (use --json for all)" % (len(r["findings"]) - a.show))
        if r["error_count"]:
            print("  %d read errors, e.g. %s" % (r["error_count"], r["errors"][0]))
        if r["findings"]:
            print("RESULT: %d dangerous value(s)" % len(r["findings"]))
        elif r["nothing_scanned"]:
            print("RESULT: nothing scanned (missing: %s)" % ", ".join(r["roots_missing"]))
        else:
            print("RESULT: OK, nothing dangerous")
    if r["findings"]:
        return 1
    if r["nothing_scanned"]:
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as e:               # noqa: BLE001 - report, never a traceback
        sys.stderr.write("name_guard: %s: %s\n" % (type(e).__name__, e))
        sys.exit(4)
