# Quick install (experienced users)

> **Not an official ROCKNIX project.** The ROCKNIX team didnt make this, review it or endorse it, so please dont ask
> them for help with it. If something breaks, tell me (Xenocideend) by opening an issue on this repo, and not them.

For users comfortable with SSH on ROCKNIX. The full walkthrough, with what
each step does and how to undo it, is in
[COMMAND-CENTER-GUIDE.md](COMMAND-CENTER-GUIDE.md#2-installing-updating-removing).

**You need:** a Retroid Pocket 5 on ROCKNIX, the Retroid Dual Screen add-on
attached in display mode and showing a picture, and SSH enabled
(START > Network Settings > Enable SSH). The install scripts refuse to run
unless they detect an RP5 with the add-on's display connected. Without the
add-on (single-screen use), set `TD_ALLOW_UNDOCKED=1` on the device or pass
`--undocked` to `deploy_rp5deck.py`.

**A handheld with two built-in screens** (AYN Thor and Thor Lite, AYANEO Pocket DS, Anbernic RG DS and DS Plus) is let
through too, and `install.sh` then installs `dual-screen-builtin-layout` instead of the add-on scripts. That support is
untested: follow [TESTER-GUIDE.md](TESTER-GUIDE.md), which also writes the log to send back. Any other model needs
`TD_ALLOW_ANY_DEVICE=1` in front of the install commands.

## From a PC

```bash
RP5=root@<device-ip>
ssh $RP5 mkdir -p /storage/rp5deck-new
scp -r app $RP5:/storage/rp5deck-new/rp5deck
scp -r scripts $RP5:/storage/rp5deck-scripts
ssh $RP5
```

## On the device

Coming from the first release (25 Sep)? Run this first, it stops the old
`092-dual-screen-persist` and `094-rp5deck` and moves their files aside:

```bash
sh /storage/rp5deck-scripts/migrate-old-daemon-names.sh
```

Then:

```bash
cd /storage/rp5deck-new/rp5deck
find . -type f ! -name MANIFEST.md5 ! -path '*/__pycache__/*' -print0 | xargs -0 md5sum > MANIFEST.md5
sh testday/td1-verify-build.sh          # must end: RESULT: build verified
sh testday/td3-install.sh install       # backs up any old install, keeps config.json, starts the app
sh /storage/rp5deck/testday/td3-install.sh guard-live   # focus guard out of observe-only mode
sh /storage/rp5deck/tools/install-es-hooks.sh           # add --force only if you accept its name warnings
systemctl restart essway                # ES reads hooks only at startup; never restart sway.service
sh /storage/rp5deck-scripts/install.sh  # dual-screen-layout-and-power + dp-sleep-guard
sh /storage/rp5deck/steam/install-steam-nested.sh install   # optional: Steam beside the Command Center
reboot                                  # the layout daemon starts from autostart at boot
```

Update the layout daemon together with the app: a single screen needs its newer rules (a web app's
workspace, ES back on its own).

Instead of the first four device commands, `scripts/deploy_rp5deck.py <name> --install`
run from a clone of this repo does the copy, checksum, verify, install and
guard-live steps over SSH (Python 3 with `paramiko`; connection details from
`RK_HOST`/`RK_USER`/`RK_PASS` or a local `rk_local.json`, see `scripts/rk.py`).

## After installing

- Dual-screen emulators need their own settings (3DS layout, Wii U GamePad,
  optional per-game DS layouts): see
  [Dual-screen emulators](COMMAND-CENTER-GUIDE.md#9-dual-screen-emulators).
- Per-game keys the Command Center should watch for go in
  `/storage/rp5deck/config.json` under `dualscreen.extra_keys`, e.g.
  `{"key": "nds[\"Game.zip\"].screen_layout", "line": "nds[\"Game.zip\"].screen_layout=6", "rom": "nds/Game.zip"}`.
- Steam: with nested Steam installed, the Steam tab, the Companion card and Clean state's Steam items appear
  while Steam is open ([Steam](COMMAND-CENTER-GUIDE.md#steam)).
- Logs: `/storage/rp5deck/log/`. Settings: `/storage/rp5deck/config.json`.

## Turn off or roll back

| Action | Command |
|---|---|
| Disable the app | `touch /storage/.disable-rp5deck` (delete the file to re-enable) |
| Disable the layout daemon | `touch /storage/.disable-dualscreen` |
| Roll back the app | `sh /storage/rp5deck/testday/td3-install.sh rollback /storage/rp5deck-backups/<timestamp>` |
| Remove the ES hooks | `sh /storage/rp5deck/tools/install-es-hooks.sh --remove && systemctl restart essway` |
| Remove nested Steam | `sh /storage/rp5deck/steam/install-steam-nested.sh remove` |
| Remove the sleep guard | `systemctl disable dp-sleep-guard.service`, then delete `/storage/.config/dp-sleep-guard.sh` and `/storage/.config/system.d/dp-sleep-guard.service` |

Known issues (charging through the add-on, the top screen after a boot with
a charger connected) and their workarounds are in
[Known limitations](COMMAND-CENTER-GUIDE.md#12-known-limitations-and-troubleshooting).

## Coming soon

TV mode (gamepad, keyboard or mouse control on a TV), hibernate, a one-script installer, and the ROCKNIX
charger and USB fixes once they are merged upstream. See the guide's
[Coming soon](COMMAND-CENTER-GUIDE.md#15-coming-soon).
