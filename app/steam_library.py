"""steam_library: what Steam has on this device, read from its own files, never from the network.

    games(root)            installed games (appmanifest_*.acf), tools and runtimes left out
    art(root, appid)       the artwork Steam cached for a game: header, capsule, hero, hero_blur
    running_appid(proc)    the appid of the Steam game running now, from /proc (works without sway)
    info(root, appid)      name, size, last played, art and running state in one dict
    library(root, now)     what the library tab lists: each game with its card art and one short line
    client_process(proc)   (pid, argv, environ) of the running Steam client, None when Steam is not open
    launch(appid, proc)    asks the running client to start a game (steam://rungameid/N)

root is Steam's folder (ROCKNIX keeps it at /storage/roms/steam, linked from ~/.local/share/Steam).
"""
import os
import re

STEAMAPPS = "steamapps"
LIBRARY_CACHE = os.path.join("appcache", "librarycache")

# Steam's own plumbing shows up as installed apps: shared redistributables, the Linux runtimes, Proton.
NOT_GAMES = re.compile(r"^(Steamworks Common Redistributables|Steam Linux Runtime.*|Proton( .*)?|"
                       r"Steam Controller Configs.*|SteamVR.*)$", re.I)

# the file names Steam uses inside librarycache/<appid>/, best first
ART_FILES = {
    "header": ("header.jpg", "library_header.jpg"),
    "capsule": ("library_600x900.jpg",),
    "hero": ("library_hero.jpg",),
    "hero_blur": ("library_hero_blur.jpg",),
}

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text):
    """Valve's key/value text format to nested dicts. Keys are kept as written (Steam is inconsistent
    about case). Unbalanced input raises ValueError."""
    stack = [{}]
    key = None
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            if key is None:
                raise ValueError("block without a key")
            child = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) == 1:
                raise ValueError("unbalanced }")
            stack.pop()
            key = None
        elif key is None:
            key = s.replace("\\\\", "\\").replace('\\"', '"')
        else:
            stack[-1][key] = s.replace("\\\\", "\\").replace('\\"', '"')
            key = None
    if len(stack) != 1:
        raise ValueError("unbalanced {")
    return stack[0]


def _library_folders(root):
    """The steamapps folders to read: the main one, plus any other library listed in libraryfolders.vdf."""
    out = [os.path.join(root, STEAMAPPS)]
    try:
        with open(os.path.join(root, STEAMAPPS, "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            data = parse_vdf(f.read())
    except (OSError, ValueError):
        return out
    for entry in (data.get("libraryfolders") or {}).values():
        path = entry.get("path") if isinstance(entry, dict) else None
        if path:
            p = os.path.join(path, STEAMAPPS)
            if p not in out and os.path.isdir(p):
                out.append(p)
    return out


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def games(root):
    """Installed games, newest played first then by name. A manifest that cannot be read is skipped."""
    found = {}
    for folder in _library_folders(root):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            if not (n.startswith("appmanifest_") and n.endswith(".acf")):
                continue
            try:
                with open(os.path.join(folder, n), encoding="utf-8", errors="replace") as f:
                    state = parse_vdf(f.read()).get("AppState") or {}
            except (OSError, ValueError):
                continue
            appid = _int(state.get("appid"))
            name = str(state.get("name") or "")
            if not appid or not name or NOT_GAMES.match(name):
                continue
            found[appid] = {"appid": appid, "name": name, "installdir": state.get("installdir") or "",
                            "size_bytes": _int(state.get("SizeOnDisk")),
                            "last_played": _int(state.get("LastPlayed")),
                            "installed": bool(_int(state.get("StateFlags")) & 4)}
    return sorted(found.values(), key=lambda g: (-g["last_played"], g["name"].lower()))


def art(root, appid):
    """{"header": path, ...} for the artwork that exists, never a path that is not there."""
    base = os.path.join(root, LIBRARY_CACHE, str(int(appid)))
    out = {}
    for kind, names in ART_FILES.items():
        for n in names:
            p = os.path.join(base, n)
            if os.path.isfile(p):
                out[kind] = p
                break
    return out


_APPID_ARG = re.compile(rb"(?:SteamLaunch\0AppId=|AppId=)(\d+)")


def running_appid(proc="/proc"):
    """The appid of the Steam game that is running, or None. Steam starts every game through
    `reaper SteamLaunch AppId=<id> -- ...`, so the id is on that process's command line. Reads
    /proc only, so it works while sway is gone."""
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    for pid in sorted(pids, key=int):
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                cmd = f.read(4096)
        except OSError:
            continue
        if b"SteamLaunch" not in cmd:
            continue
        m = _APPID_ARG.search(cmd)
        if m:
            return int(m.group(1))
    return None


_RUN_URI = re.compile(r"steam://rungameid/(\d+)|-applaunch\s+(\d+)")
# where ROCKNIX's Steam lives: the real folder first, then the link that points at it
ROOTS = ("/storage/roms/steam", "/storage/.local/share/Steam")


def default_root(isdir=os.path.isdir):
    """The first Steam folder that has a steamapps folder, else the first one (so a caller always has a path)."""
    for r in ROOTS:
        if isdir(os.path.join(r, STEAMAPPS)):
            return r
    return ROOTS[0]


def appid_from_desktop(path):
    """The appid in a Steam shortcut (Exec=steam steam://rungameid/<id>), or None. Steam.desktop, the Big Picture
    launcher, has none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("Exec="):
                    m = _RUN_URI.search(line)
                    return int(m.group(1) or m.group(2)) if m else None
    except OSError:
        pass
    return None


def fmt_size(n):
    """12345678901 -> '11.5 GB'. Under a megabyte it says nothing (a manifest with no size)."""
    if n < 1 << 20:
        return ""
    for unit, shift in (("GB", 30), ("MB", 20)):
        if n >= 1 << shift:
            v = n / float(1 << shift)
            return ("%.1f %s" % (v, unit)) if v < 100 else ("%d %s" % (round(v), unit))
    return ""


def fmt_last_played(ts, now, months=("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                     "Nov", "Dec")):
    """Epoch seconds -> 'Last played today' / 'yesterday' / '5 days ago' / '12 Sep' / '' when never."""
    import time as _t
    if not ts:
        return ""
    days = int((now - ts) // 86400)
    if days <= 0:
        return "Last played today"
    if days == 1:
        return "Last played yesterday"
    if days < 14:
        return "Last played %d days ago" % days
    t = _t.localtime(ts)
    return "Last played %d %s" % (t.tm_mday, months[t.tm_mon - 1])


def fmt_played(minutes):
    """3217 -> '54 h played', 95 -> '1.6 h played', 20 -> '20 min played', 0 -> ''."""
    if minutes <= 0:
        return ""
    if minutes < 60:
        return "%d min played" % minutes
    h = minutes / 60.0
    return ("%.1f h played" % h) if h < 10 else ("%d h played" % round(h))


def fmt_duration(seconds):
    """4320 -> '1 h 12 min', 90 -> '1 min', under a minute -> ''."""
    m = int(seconds // 60)
    if m < 1:
        return ""
    return ("%d h %02d min" % (m // 60, m % 60)) if m >= 60 else ("%d min" % m)


def facts(i, now):
    """The short lines the screen shows under a game's title, empty ones left out."""
    return [f for f in (fmt_played(i.get("playtime_min") or 0),
                        fmt_last_played(i.get("last_played") or 0, now),
                        fmt_size(i.get("size_bytes") or 0)) if f]


_CONFIG_CACHE = {}


def _local_config(root):
    """The per-game table of the library's localconfig.vdf (appid -> dict), parsed again only when the file
    changed. {} when there is none or it cannot be read. Steam rewrites it while running."""
    import glob
    for p in sorted(glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf"))):
        try:
            st = os.stat(p)
            key = (p, st.st_mtime_ns, st.st_size)
            if key not in _CONFIG_CACHE:
                with open(p, encoding="utf-8", errors="replace") as f:
                    data = parse_vdf(f.read())
                apps = {}

                def find(n):
                    for k, v in n.items():
                        if isinstance(v, dict):
                            if k.lower() == "apps":
                                apps.update(v)
                            else:
                                find(v)
                find(data)
                _CONFIG_CACHE.clear()
                _CONFIG_CACHE[key] = apps
            return _CONFIG_CACHE[key]
        except (OSError, ValueError):
            continue
    return {}


def playtime(root, appid):
    """{"total_min": N, "two_weeks_min": N} from Steam's local config, zeros when it has nothing."""
    a = _local_config(root).get(str(int(appid)))
    a = a if isinstance(a, dict) else {}
    return {"total_min": _int(a.get("Playtime")), "two_weeks_min": _int(a.get("Playtime2wks"))}


def running_since(proc="/proc", clk=100):
    """(appid, seconds the game has been running) for the Steam game that is up, else (None, 0). The age is the
    launcher process's start time (clock ticks since boot, field 22 of its stat) against the machine's uptime."""
    appid = running_appid(proc)
    if appid is None:
        return None, 0
    try:
        with open(os.path.join(proc, "uptime"), encoding="utf-8") as f:
            up = float(f.read().split()[0])
        for pid in sorted((p for p in os.listdir(proc) if p.isdigit()), key=int):
            try:
                with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                    if b"SteamLaunch" not in f.read(4096):
                        continue
                with open(os.path.join(proc, pid, "stat"), encoding="utf-8", errors="replace") as f:
                    start = int(f.read().rsplit(")", 1)[1].split()[19])
            except (OSError, ValueError, IndexError):
                continue
            return appid, max(0.0, up - start / float(clk))
    except (OSError, ValueError):
        pass
    return appid, 0


def poll(root, proc="/proc", clk=100):
    """What the screen watches while Steam runs, in one call: {"appid", "seconds", "downloads"}."""
    appid, seconds = running_since(proc, clk)
    return {"appid": appid, "seconds": seconds, "downloads": downloads(root)}


# StateFlags bits Steam sets while it is fetching an update or an install
_UPDATING = 256 | 1024


def downloads(root):
    """Games Steam is downloading or updating right now: [{"appid", "name", "percent"}]."""
    out = []
    for folder in _library_folders(root):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            if not (n.startswith("appmanifest_") and n.endswith(".acf")):
                continue
            try:
                with open(os.path.join(folder, n), encoding="utf-8", errors="replace") as f:
                    s = parse_vdf(f.read()).get("AppState") or {}
            except (OSError, ValueError):
                continue
            want, got = _int(s.get("BytesToDownload")), _int(s.get("BytesDownloaded"))
            if _int(s.get("StateFlags")) & _UPDATING and want > 0 and got < want:
                out.append({"appid": _int(s.get("appid")), "name": str(s.get("name") or ""),
                            "percent": int(100 * got / want)})
    return out


def info(root, appid, proc="/proc"):
    """Everything the screen shows about one game. An appid with no manifest still gets a dict
    (name None), so a running game that is not installed here does not crash the screen."""
    appid = int(appid)
    g = next((x for x in games(root) if x["appid"] == appid), None)
    out = {"appid": appid, "name": g["name"] if g else None, "installed": bool(g),
           "size_bytes": g["size_bytes"] if g else 0, "last_played": g["last_played"] if g else 0,
           "art": art(root, appid), "running": running_appid(proc) == appid}
    p = playtime(root, appid)
    out["playtime_min"], out["two_weeks_min"] = p["total_min"], p["two_weeks_min"]
    return out


# -- the library tab ---------------------------------------------------------------------------
CARD_ART = ("header", "capsule", "hero")        # best card picture first: the wide header suits a grid


def library(root, now):
    """Every installed game for the library tab, last played first: appid, name, card (the art file to show, or
    None), and line (hours played, else last played, else size)."""
    out = []
    for g in games(root):
        if not g["installed"]:
            continue
        pics = art(root, g["appid"])
        card = next((pics[k] for k in CARD_ART if k in pics), None)
        i = {"playtime_min": playtime(root, g["appid"])["total_min"], "last_played": g["last_played"],
             "size_bytes": g["size_bytes"]}
        line = (facts(i, now) or [""])[0]
        out.append({"appid": g["appid"], "name": g["name"], "card": card, "line": line,
                    "last_played": g["last_played"], "playtime_min": i["playtime_min"]})
    return out


SORTS = ("recent", "az", "played")      # the library tab's sort button cycles these, in this order


def sort_games(games, mode):
    """The library in a sort order: recent (last played first, never played by name), az, or played (most
    played first). Ties are by name so the order never jumps around."""
    if mode == "az":
        return sorted(games, key=lambda g: g["name"].lower())
    if mode == "played":
        return sorted(games, key=lambda g: (-g["playtime_min"], g["name"].lower()))
    return sorted(games, key=lambda g: (-g["last_played"], g["name"].lower()))


def _read(path, limit=1 << 16):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def client_process(proc="/proc"):
    """(pid, argv, environ) of the Steam client (steamrtarm64/steam, not its web helper), or None."""
    try:
        pids = sorted((p for p in os.listdir(proc) if p.isdigit()), key=int)
    except OSError:
        return None
    for pid in pids:
        argv = [a for a in _read(os.path.join(proc, pid, "cmdline")).split(b"\x00") if a]
        if not argv or not argv[0].endswith(b"/steamrtarm64/steam"):
            continue
        env = {}
        for item in _read(os.path.join(proc, pid, "environ")).split(b"\x00"):
            k, sep, v = item.partition(b"=")
            if sep and k:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return int(pid), [a.decode("utf-8", "replace") for a in argv], env
    return None


def launch_url(appid):
    return "steam://rungameid/%d" % int(appid)


def launch(appid, proc="/proc", popen=None):
    """Hands the running Steam client a game to start. The client binary given a steam:// URL passes it on to
    the instance that is already open, with that instance's own environment so it finds the same display and
    home. False when Steam is not open (nothing is started, a second client is never launched)."""
    import subprocess
    found = client_process(proc)
    if found is None:
        return False
    _pid, argv, env = found
    (popen or subprocess.Popen)([argv[0], launch_url(appid)], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
