# RP5 Dual Screen Command Center (rp5deck)

A touch app for the Retroid Pocket 5's built-in (bottom) screen when the
Retroid Dual Screen add-on provides the top screen, on ROCKNIX. The bottom
screen shows art or video for the game selected (or running) in
EmulationStation, and a pull-down Command Center with a volume slider and
per-app mixer, a device-info HUD, a browser, a YouTube app, Discord's web
app, a hotkey cheat sheet, stick lights, Clean state, Sleep, a safe-charge
limit, screen swap, colour themes and settings. It is touch-only: the
gamepad always stays with EmulationStation or the running game.

Alongside the app, this repo ships two small system-level scripts that keep
the dual-screen layout working across a reboot and a suspend/resume cycle
(see `scripts/`).

## Requirements

- A Retroid Pocket 5
- The official Retroid Dual Screen add-on
- ROCKNIX (this was built and tested against a specific ROCKNIX/kernel build
  on the RP5 - see "Status and known limitations" below)
- Python 3 (device default; no extra Python packages needed on-device)
- To use `scripts/deploy_rp5deck.py`/`scripts/rk.py` from a PC: Python 3 with
  `paramiko` installed

## What's included

- `app/` - the rp5deck application. It installs to `/storage/rp5deck` and
  is started at boot by its supervisor, `094-rp5deck` (installed to
  `/storage/.config/autostart/`). Also its test suite (`app/tests/`) and the
  device-side verify/install scripts (`app/testday/td1-verify-build.sh`,
  `td3-install.sh`, `td-common.sh`).
- `scripts/092-dual-screen-persist` - a ROCKNIX autostart script that keeps
  the add-on's screen enabled, correctly positioned, and pointed at the
  right output across a reboot (see the script's own header for the full
  "why" - three separate stock mechanisms undo this layout on every boot).
- `scripts/dp-sleep-guard.sh` + `dp-sleep-guard.service` - a systemd oneshot
  unit that turns the add-on's DisplayPort output off before suspend and
  back on at resume (on the tested kernel, suspending with DP lit resets the
  device instead of resuming).
- `scripts/install.sh` - installs the two scripts above on the device.
- `scripts/deploy_rp5deck.py` + `scripts/rk.py` - optional PC-side tooling
  that does the app install below over SSH in one step (needs Python 3 with
  `paramiko`, and this repo cloned with git, since it lists the files with
  `git ls-files`).

## Install

Experienced users: [docs/QUICK-INSTALL.md](docs/QUICK-INSTALL.md) has just the
commands. The full walkthrough is in [docs/COMMAND-CENTER-GUIDE.md](docs/COMMAND-CENTER-GUIDE.md).
Both installers refuse to run unless the device is a Retroid Pocket 5 with
the add-on attached and showing a picture. With SSH enabled on the device:

1. Copy `app/` to a new folder on the device, for example
   `/storage/rp5deck-new/rp5deck` (not `/storage/rp5deck` itself).
2. On the device, in that folder, write the checksum list the installer
   checks every file against:
   `find . -type f ! -name MANIFEST.md5 ! -path '*/__pycache__/*' -print0 | xargs -0 md5sum > MANIFEST.md5`
3. Run `sh testday/td1-verify-build.sh`, then `sh testday/td3-install.sh install`.
   This backs up any existing install to `/storage/rp5deck-backups/`, keeps
   your `config.json`, installs `094-rp5deck` and starts the app.
4. Run `sh /storage/rp5deck/testday/td3-install.sh guard-live` to switch the
   focus guard from its first-install observe-only mode to normal.
5. Run `sh /storage/rp5deck/tools/install-es-hooks.sh`, then
   `systemctl restart essway`, so the Companion view follows the selected
   game.
6. Copy `scripts/` to the device and run `sh install.sh` there once, to
   install `092-dual-screen-persist` and `dp-sleep-guard`.

`scripts/deploy_rp5deck.py <name> --install` does steps 1-4 from a PC.
To roll back: `sh /storage/rp5deck/testday/td3-install.sh rollback <backup dir>`.

## Status and known limitations

- Built for, and tested on, one Retroid Pocket 5 running one specific
  ROCKNIX/kernel build. Other ROCKNIX versions, kernels, or other
  Retroid-Dual-Screen-capable devices have not been tried and may need
  changes (sysfs paths, sway/wlroots behaviour, and EmulationStation's HTTP
  API can all differ or move between releases).
- `092-dual-screen-persist` and `dp-sleep-guard` work around specific stock
  ROCKNIX/kernel behaviour observed on that one build; a future ROCKNIX or
  kernel update could change or remove the underlying issue, or need a
  different fix.
- Per-game `system.cfg` overrides (`app/dualscreen_keys.py`, e.g. a DS title
  that needs its own screen-layout key) are configured by hand in
  `config.json`'s `dualscreen.extra_keys` - there is no in-app editor for
  this yet.
- Known device issues this app can only work around, not fix: after the
  handheld has powered the add-on, a charger plugged into the add-on can
  fail to charge (the Command Center shows a warning with what to do), and
  booting with a charger already connected can leave the add-on's screen
  dark until the charger is unplugged. Kernel fixes are being worked on
  upstream.
- Tests that relied on captures from the developer's own device are not
  included here; the remaining suite (`cd app && python3 -m unittest
  discover -s tests -p "test_*.py"`) runs on a PC.

## Licence

GNU General Public License v2.0 - see [LICENSE](LICENSE).

## AI assistance

This app's code and documentation were written with AI assistance (Claude),
under the maintainer's direction and review, and tested on the maintainer's
own RP5 device. Flagging this up front in line with ROCKNIX's own request
that contributors be transparent about AI-assisted work.
