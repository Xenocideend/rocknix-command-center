#!/bin/sh
# screen-size-reset.sh - put the Command Center's screen size preset back to Auto from
# outside the app (owner, 26 Sep: a preset left Settings unreachable). The ES Ports entry
# "Command Center Screen Size Reset.sh" runs it. Writes config.json, then stops the app
# process; command-center-app starts it again at the panel's own size.
CFG="${RP5DECK_CONFIG:-/storage/rp5deck/config.json}"
STATE=/run/rp5deck/state.json
python3 - "$CFG" <<'EOF' || exit 1
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {"schema_version": 1}
cfg.setdefault("screens", {})["ui_resolution"] = "auto"
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
print("screen size preset: auto")
EOF
pid=$(python3 -c "import json; print(json.load(open('$STATE')).get('pid') or '')" 2>/dev/null)
[ -n "$pid" ] && [ -d "/proc/$pid" ] && kill -TERM "$pid"
exit 0
