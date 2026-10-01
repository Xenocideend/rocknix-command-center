"""dualscreen_keys: warns when the dual-screen settings are missing from system.cfg, with a one
tap Restore. Only a warning at boot, the fix is always a tap.

ROCKNIX's /usr/bin/chksysconfig runs `verify` every boot, and if system.cfg looks binary (NUL
blocks from an unclean reset) it copies system.cfg.backup over it. That backup is only
refreshed at a clean shutdown (save-sysconfig.service -> `chksysconfig backup`), so a crash
in between quietly drops the dual-screen keys, and 3DS, Wii U or a per-game DS title go back
to one screen with no error anywhere. system.cfg never gets written while ES is starting, so
this never edits it from a background poll. It just says so, and the button does the fix once
you've confirmed no game is running and that ES restarts for a few seconds.

Two keys are always checked, from BASE_KEYS / KEY_LINES (never worked out or guessed):
    3ds.screen_layout=5
    wiiu.gamepad_enabled=true
A DS title can need its own override (`nds["<rom filename>"].screen_layout=<value>`), but
which title and value depends on your own ROM collection, so it's never hard-coded. It comes
from config.json's dualscreen.extra_keys (empty by default), a list of {"key": the exact
system.cfg key, "line": the exact "key=value" text restore() appends as is, "rom": optional, a
path relative to ROMS_DIR}. An entry with "rom" only counts while that file exists, so without
the game it never shows as missing. One with no "rom" always counts once it's set up.

A key is present if some line starts with "<key>=", any value, since you might have changed it
yourself in ES. This only ever reports a missing name, never a value it disagrees with. The
"=" has to follow the key right away, so "3ds.screen_layout_x=..." never passes for
"3ds.screen_layout=...".

Three parts, like charge_limit.py, system_sleep.py and cleanstate.py:
  missing_keys()   pure: file text + the list of extra key names to expect -> the missing
                   key names. No filesystem, no clock.
  check()          reads system.cfg and resolves dualscreen.extra_keys against roms_dir. An
                   absent, unreadable or undecodable file is "cant check", never "missing",
                   so a corrupt file isnt counted as both chksysconfig's problem and this one.
  restore()        the privileged fix. Refuses while a game runs (the same check es_health
                   uses), stops essway.service, waits for ES to exit (bounded, and if it
                   doesnt it gives up and still restarts essway), backs up system.cfg,
                   appends only the keys still missing (checked again right before writing),
                   tells chksysconfig to refresh its own boot snapshot so a crash later cant
                   undo the fix, and always restarts essway in a finally, so a half done edit
                   never leaves you without ES.

system.cfg never gets printed in full anywhere, logs included, only key names show up in a
log line or a returned detail. The whole thing sits behind a confirm, nothing runs by itself
from a background poll.
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
# The two keys' own lines, as is. restore() never builds a "key=value" string any other way,
# and any per-game key/line comes from config.json's dualscreen.extra_keys (see _key_lines()).
KEY_LINES = {
    KEY_3DS: "3ds.screen_layout=5",
    KEY_WIIU: "wiiu.gamepad_enabled=true",
}

CMD_STOP_ES = ("systemctl", "stop", "essway.service")
CMD_START_ES = ("systemctl", "start", "essway.service")
CMD_CHKSYSCONFIG = ("/usr/bin/chksysconfig", "backup")

STOP_WAIT_S = 15.0  # how long to wait for ES to exit after the stop
POLL_S = 0.25


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------
def _present(lines, key):
    prefix = key + "="
    return any(ln.startswith(prefix) for ln in lines)


def missing_keys(text, expected_extra=()):
    """text (system.cfg's raw content, any line ending, trailing newline or not) -> the expected key
    names not in it, BASE_KEYS order first, then expected_extra's in the order given. Pure,
    expected_extra is the plain list of key names the caller wants checked (check() builds it
    from dualscreen.extra_keys, filtered by whether each entry's ROM exists).
    """
    lines = text.splitlines()
    keys = list(BASE_KEYS) + list(expected_extra)
    return [k for k in keys if not _present(lines, k)]


def _default_extra_specs():
    """Your own per-game keys from config.json (dualscreen.extra_keys, RP5DECK_CONFIG override
    honoured like every other setting). Empty by default, so a fresh install only checks and
    restores the two BASE_KEYS.
    """
    cfg, _note = config.load()
    return config.get_value(cfg, ("dualscreen", "extra_keys"))


def _expected_extra_keys(specs, roms_dir):
    """specs (dualscreen.extra_keys, already validated by config.py) -> the key names expected right
    now. An entry with no "rom" always is once it's set up, one with a "rom" only while that file
    exists under roms_dir.
    """
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
    """BASE_KEYS' own KEY_LINES plus a "key" -> "line" entry for every extra spec that has both,
    exactly the text restore() appends for that key, from config.json as is.
    """
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
    """{"available", "missing", "error"}. available False means cant check (the file is missing,
    unreadable or not valid UTF-8, like the NUL block state chksysconfig watches for), and the
    caller must never turn that into "keys are missing", only its own "couldnt check" note. Never
    raises and never logs the file's content. extra_key_specs defaults to dualscreen.extra_keys
    (None means look it up, tests pass a list instead of touching a real config).
    """
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
# The restore (privileged, touch only, behind a confirm)
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
    """Works out from the file as it is right now, right before the append, which of the requested
    keys are still missing, so a key you added yourself in ES since check() (or since the confirm
    opened) never gets appended twice. Only trusts the caller's list if the file cant be read here
    (it was readable a moment ago at backup time).
    """
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
    """Byte level append: a newline only if the file doesnt already end with one, then exactly
    `key_lines`'s text for `keys`. Opened for append, never rewritten, so a crash mid-write can
    only lose the new lines, never cut the file short (a truncated or NUL padded system.cfg is what
    makes chksysconfig restore a stale backup next boot). fsynced before returning since
    chksysconfig backup copies the file right after.
    """
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
    """Fixes exactly the keys in `missing`, each has to be a known name (BASE_KEYS or a configured
    dualscreen.extra_keys entry), an unknown one refuses instead of guessing a line. Returns
    {"ok", "detail", "restored", "backup"}, and detail and log lines only ever name keys, never a
    "key=value" line or anything else from system.cfg. extra_key_specs defaults to
    dualscreen.extra_keys (None means look it up, tests pass a list). Order: refuse while a game
    runs -> stop essway -> wait for ES to exit (bounded) -> back up -> append only what's still
    missing -> chksysconfig backup -> finally restart essway even if anything above failed.
    """
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
