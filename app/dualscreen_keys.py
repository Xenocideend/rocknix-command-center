"""dualscreen_keys - the "dual-screen settings missing" guard and its
one-tap Restore (owner-approved: WARN ONLY at boot, plus a one-tap restore).

Why this exists: ROCKNIX's /usr/bin/chksysconfig runs `verify` at every boot
(/usr/lib/autostart/common/001-setup) - if system.cfg looks binary (e.g. NUL
blocks left by an unclean reset) it copies system.cfg.backup over it, and
system.cfg.backup is only refreshed at a CLEAN shutdown
(save-sysconfig.service -> `chksysconfig backup`). A crash between those two
points silently drops the dual-screen keys 092-dual-screen-persist/ES itself
add, and 3DS / Wii U / a configured per-game DS title then show on a single
screen again with no error anywhere. The owner's choice: never write
system.cfg while ES is starting (so this module NEVER edits it from a
background poll), just say so - plus a button that does the fix once the
owner has confirmed it is safe (no game running) and accepted that ES
restarts for a few seconds.

THE KEYS: two are generic and always checked, in BASE_KEYS / KEY_LINES below
(never re-derived, never guessed):
    3ds.screen_layout=5
    wiiu.gamepad_enabled=true
A DS title can need its own per-game override (e.g. `nds["<rom
filename>"].screen_layout=<value>`), but which title and what value is
entirely a matter of what is actually in an owner's own ROM collection - it
is never hard-coded here. Instead it comes from config.json's
dualscreen.extra_keys (config.py's schema; public default: empty list), a
list of {"key": the exact system.cfg key name, "line": the exact "key=value"
text restore() appends verbatim, "rom": optional, a path relative to
ROMS_DIR}. An entry with "rom" only counts as "expected" while that file
actually exists under roms_dir (an owner without the game never sees it
listed as missing); an entry with no "rom" is always expected once
configured. See DEVICE-CONFIG-NOTE.md style guidance in config.py's
dualscreen.extra_keys schema comment for how an owner adds their own entry.

A key is "present" if some line in system.cfg starts with "<key>=" - ANY
value counts (the owner may have changed it by hand through ES; this module
only ever reports a NAME missing, never a value it disagrees with), and the
"=" must follow the key immediately, so "3ds.screen_layout_x=..." is never
mistaken for "3ds.screen_layout=...".

Three pieces, like charge_limit.py / system_sleep.py / cleanstate.py before
it:
  missing_keys()   pure: file text + the already-resolved list of expected
                   extra key NAMES -> the list of missing key NAMES. No
                   filesystem, no clock.
  check()          reads system.cfg (tolerant of it being absent, unreadable
                   or undecodable - "cannot check", never reported as
                   "missing": a corrupt file must never be double-counted as
                   both chksysconfig's problem AND this module's) and
                   resolves dualscreen.extra_keys against roms_dir.
  restore()        the privileged fix: refuses while a game/emulator is
                   running (same "how do we know a game is running" signal
                   es_health.game_running() already uses for CC1), then
                   systemctl stop essway.service, waits for ES to actually
                   exit (bounded; aborts - and still restarts essway - if it
                   does not), backs up system.cfg, appends ONLY the keys
                   still missing (re-checked right before writing - never
                   trusts a caller's possibly-stale list), tells
                   chksysconfig to refresh ITS OWN boot-time snapshot so a
                   crash five minutes later cannot undo the fix, then always
                   restarts essway (try/finally) - a half-finished edit must
                   never leave the owner without EmulationStation.

Owner rules this module keeps: never prints system.cfg in full anywhere,
logs included (only key names ever appear in a log line or a returned
"detail" string); the whole flow is behind a UI confirmation - nothing here
runs on its own from a background poll.
"""
import logging
import os
import subprocess
import time

import config
import es_health

log = logging.getLogger("rp5deck.dualscreen_keys")

SYSTEM_CFG = "/storage/.config/system/configs/system.cfg"
ROMS_DIR = "/storage/roms"
BACKUP_DIR = "/storage/rp5deck-backups"

KEY_3DS = "3ds.screen_layout"
KEY_WIIU = "wiiu.gamepad_enabled"

BASE_KEYS = (KEY_3DS, KEY_WIIU)
# The two generic keys' own lines, verbatim - restore() never builds a
# "key=value" string any other way. Any further (per-game) key/line comes
# from config.json's dualscreen.extra_keys - see _key_lines() below.
KEY_LINES = {
    KEY_3DS: "3ds.screen_layout=5",
    KEY_WIIU: "wiiu.gamepad_enabled=true",
}

CMD_STOP_ES = ("systemctl", "stop", "essway.service")
CMD_START_ES = ("systemctl", "start", "essway.service")
CMD_CHKSYSCONFIG = ("/usr/bin/chksysconfig", "backup")

STOP_WAIT_S = 15.0           # bounded wait for ES to actually exit after the stop
POLL_S = 0.25


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------
def _present(lines, key):
    prefix = key + "="
    return any(ln.startswith(prefix) for ln in lines)


def missing_keys(text, expected_extra=()):
    """text (system.cfg's raw content, any line ending, a trailing newline
    or not) -> the list of expected key NAMES not present in it, in
    BASE_KEYS order with `expected_extra`'s keys last, in the order given.
    Pure: no filesystem, no clock - expected_extra is the plain list of
    already-resolved key NAMES the caller wants checked (check(), below,
    derives it from config.json's dualscreen.extra_keys, filtered by
    whether each entry's optional ROM file actually exists)."""
    lines = text.splitlines()
    keys = list(BASE_KEYS) + list(expected_extra)
    return [k for k in keys if not _present(lines, k)]


def _default_extra_specs():
    """The owner's own per-game keys, straight from config.json
    (dualscreen.extra_keys - RP5DECK_CONFIG override honoured, same as
    every other rp5deck setting). The public default is an empty list, so
    a fresh install checks/restores only the two generic BASE_KEYS."""
    cfg, _note = config.load()
    return config.get_value(cfg, ("dualscreen", "extra_keys"))


def _expected_extra_keys(specs, roms_dir):
    """specs (dualscreen.extra_keys, already validated by config.py) -> the
    key NAMES currently "expected": every entry with no "rom" is always
    expected once configured; an entry with a "rom" only while that file
    actually exists under roms_dir (an owner without the game never sees
    it listed as missing)."""
    out = []
    for spec in specs:
        key = spec.get("key")
        if not key:
            continue
        rom = spec.get("rom")
        if rom and not os.path.exists(os.path.join(roms_dir, rom)):
            continue
        out.append(key)
    return out


def _key_lines(specs):
    """BASE_KEYS' own KEY_LINES plus a "key" -> "line" entry for every
    configured extra spec that has both - the exact text restore() ever
    appends for that key, verbatim from config.json, never rebuilt."""
    out = dict(KEY_LINES)
    for spec in specs:
        key, line = spec.get("key"), spec.get("line")
        if key and line:
            out[key] = line
    return out


# ---------------------------------------------------------------------------
# Read-only check
# ---------------------------------------------------------------------------
def check(cfg_path=SYSTEM_CFG, roms_dir=ROMS_DIR, extra_key_specs=None):
    """{"available", "missing", "error"}. available False means "cannot
    check" (the file is absent, unreadable, or not valid UTF-8 text - e.g.
    the NUL-block state chksysconfig itself is watching for): the caller
    must never turn that into "keys are missing", only into its own
    separate "could not check" note. Never raises, never logs any of the
    file's own content. extra_key_specs defaults to config.json's
    dualscreen.extra_keys (None means "look it up"; tests pass an explicit
    list instead of touching a real config file)."""
    specs = _default_extra_specs() if extra_key_specs is None else extra_key_specs
    expected_extra = _expected_extra_keys(specs, roms_dir)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return {"available": False, "missing": None, "error": "cannot check: %s" % e}
    except UnicodeDecodeError:
        return {"available": False, "missing": None,
                "error": "cannot check: system.cfg is not valid text right now"}
    return {"available": True, "missing": missing_keys(text, expected_extra), "error": None}


# ---------------------------------------------------------------------------
# The restore (privileged; touch-only, behind a UI confirmation)
# ---------------------------------------------------------------------------
def _default_game_running():
    return es_health.game_running(es_health.Probe())[0]


def _run(run, argv, timeout=30.0):
    try:
        r = run(list(argv), capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()[-200:]
        return r.returncode, out
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def _wait_es_gone(probe, clock, sleep, timeout_s, poll_s):
    end = clock() + timeout_s
    while True:
        if not probe.es_pids():
            return True
        if clock() >= end:
            return False
        sleep(poll_s)


def _still_missing(cfg_path, requested):
    """Re-derive, from the file as it is RIGHT NOW (immediately before the
    append), which of the caller's requested keys are still absent - so a
    key the owner added by hand through ES between check() and this restore
    (or between the confirm sheet opening and the tap) is never appended a
    second time. Falls back to trusting the caller's list only if the file
    cannot be read here (it was readable moments ago, at backup time)."""
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return list(requested)
    return [k for k in requested if not _present(lines, k)]


def _backup(cfg_path, backup_dir, stamp):
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, "system.cfg.before-keys-restore-%s" % stamp)
        with open(cfg_path, "rb") as src:
            data = src.read()
        with open(dest, "wb") as dst:
            dst.write(data)
        return dest
    except OSError as e:
        log.warning("dualscreen_keys: could not back up system.cfg: %s", e)
        return None


def _append_lines(cfg_path, keys, key_lines=KEY_LINES):
    """Byte-level append: add a newline only if the file does not already end
    with one, then exactly `key_lines`'s text for `keys`. Opened for APPEND,
    never rewritten: a crash mid-write can only lose the new lines, never
    truncate the existing file (a truncated or NUL-padded system.cfg is what
    makes chksysconfig restore a stale backup at the next boot). fsync'd
    before returning, since chksysconfig backup copies the file right after."""
    try:
        with open(cfg_path, "rb") as f:
            data = f.read()
    except OSError as e:
        log.warning("dualscreen_keys: could not read system.cfg: %s", e)
        return False
    add = b"\n" if data and not data.endswith(b"\n") else b""
    add += "".join(key_lines[k] + "\n" for k in keys).encode("utf-8")
    try:
        with open(cfg_path, "ab") as f:
            f.write(add)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        log.warning("dualscreen_keys: could not write system.cfg: %s", e)
        return False
    return True


def restore(missing, cfg_path=SYSTEM_CFG, backup_dir=BACKUP_DIR, run=None, probe=None,
           game_running=None, clock=time.monotonic, sleep=time.sleep, stamp_fn=None,
           stop_wait_s=STOP_WAIT_S, poll_s=POLL_S, extra_key_specs=None):
    """Fix exactly the keys in `missing` (every one must be a known key name
    - BASE_KEYS or a configured dualscreen.extra_keys entry; an unknown name
    refuses rather than guessing a line for it). Returns {"ok", "detail",
    "restored", "backup"}. `detail`/log lines only ever name keys, never a
    "key=value" line or any other system.cfg content. extra_key_specs
    defaults to config.json's dualscreen.extra_keys (None means "look it
    up"; tests pass an explicit list instead of touching a real config
    file). Order: refuse while a game runs -> stop essway -> wait for ES
    to exit (bounded) -> back up -> append only what is STILL missing ->
    chksysconfig backup -> [finally] restart essway, even on any failure
    above (a partial edit must never leave the owner without ES)."""
    run = run or subprocess.run
    probe = probe or es_health.Probe()
    game_running = game_running or _default_game_running
    stamp_fn = stamp_fn or (lambda: time.strftime("%Y%m%dT%H%M%S"))
    specs = _default_extra_specs() if extra_key_specs is None else extra_key_specs
    key_lines = _key_lines(specs)
    unknown = [k for k in missing if k not in key_lines]
    if unknown:
        return {"ok": False, "detail": "refused: not a known key: %s" % ", ".join(unknown),
                "restored": [], "backup": None}
    if not missing:
        return {"ok": True, "detail": "nothing to restore", "restored": [], "backup": None}
    if game_running():
        log.warning("dualscreen_keys: restore refused - a game is running")
        return {"ok": False, "detail": "refused: a game is running", "restored": [],
                "backup": None}
    rc, out = _run(run, CMD_STOP_ES)
    if rc != 0:
        return {"ok": False, "detail": "could not stop essway (rc %s): %s" % (rc, out),
                "restored": [], "backup": None}
    try:
        if not _wait_es_gone(probe, clock, sleep, stop_wait_s, poll_s):
            return {"ok": False,
                    "detail": "EmulationStation did not exit within %g s" % stop_wait_s,
                    "restored": [], "backup": None}
        backup_path = _backup(cfg_path, backup_dir, stamp_fn())
        if backup_path is None:
            return {"ok": False, "detail": "could not back up system.cfg", "restored": [],
                    "backup": None}
        to_add = _still_missing(cfg_path, missing)
        if not to_add:
            return {"ok": True, "detail": "already present - nothing changed", "restored": [],
                    "backup": backup_path}
        if not _append_lines(cfg_path, to_add, key_lines):
            return {"ok": False, "detail": "could not write system.cfg", "restored": [],
                    "backup": backup_path}
        rc2, out2 = _run(run, CMD_CHKSYSCONFIG)
        if rc2 != 0:
            return {"ok": False,
                    "detail": "restored %s, but chksysconfig backup failed (rc %s): %s"
                              % (", ".join(to_add), rc2, out2),
                    "restored": to_add, "backup": backup_path}
        log.info("dualscreen_keys: restored %s", ", ".join(to_add))
        return {"ok": True, "detail": "restored: %s" % ", ".join(to_add), "restored": to_add,
                "backup": backup_path}
    finally:
        rc3, out3 = _run(run, CMD_START_ES)
        if rc3 != 0:
            log.error("dualscreen_keys: could not restart essway.service (rc %s): %s", rc3, out3)
