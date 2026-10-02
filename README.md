# ROCKNIX Command Center (rp5deck)

> **Not an official ROCKNIX project.** The ROCKNIX team didnt make this, review it or endorse it, so please dont ask
> them for help with it. If something breaks, tell me (Xenocideend) by opening an issue on this repo, and not them.

*Formerly the Dual Screen Command Center.* Version 1.9.0, patch notes in
[app/CHANGELOG.md](app/CHANGELOG.md).

A touch app for the Retroid Pocket 5's built-in (bottom) screen when the
Retroid Dual Screen add-on provides the top screen, on ROCKNIX. The bottom
screen shows art or video for the game selected (or running) in
EmulationStation, and a pull-down Command Center with a volume slider and
per-app mixer, a device-info HUD, a browser, a YouTube app, Discord's web
app, a hotkey cheat sheet, stick lights, Clean state, Sleep, a safe-charge
limit, screen swap, colour themes and settings. With Steam open it adds a
Steam tab (your installed games as cover art, tap to start one), the running
game's details on the Companion and stop controls in Clean state. On a single
screen (the add-on unplugged, or another device) Single-screen mode runs the
Command Center and the web apps over EmulationStation or Steam. It is
touch-only: the gamepad always stays with EmulationStation or the running
game.

Alongside the app, this repo ships two small system-level scripts that keep
the dual-screen layout working across a reboot and a suspend/resume cycle
(see `scripts/`).

**Other handhelds.** Since 1.8.0 the app also has profiles for the ROCKNIX handhelds with two built-in screens
(AYN Thor and Thor Lite, AYANEO Pocket DS, Anbernic RG DS and RG DS Plus), written from ROCKNIX's own scripts, with a
small helper that keeps the second screen on for the Command Center. **None of this has been run on a real one.**
If you have one and want to help, [docs/TESTER-GUIDE.md](docs/TESTER-GUIDE.md) walks through a ten minute test that
writes a log you can send back.

## Requirements

- A Retroid Pocket 5 and the official Retroid Dual Screen add-on (the tested setup), or one of the built-in
  two-screen handhelds above (untested)
- ROCKNIX (this was built and tested against a specific ROCKNIX/kernel build
  on the RP5 - see "Status and known limitations" below)
- Python 3 (device default; no extra Python packages needed on-device)
- To use `scripts/deploy_rp5deck.py`/`scripts/rk.py` from a PC: Python 3 with
  `paramiko` installed

## What's included

- `app/` - the rp5deck application. It installs to `/storage/rp5deck` and
  is started at boot by its supervisor, `command-center-app` (installed to
  `/storage/.config/autostart/`). Also its test suite (`app/tests/`) and the
  device-side verify/install scripts (`app/testday/td1-verify-build.sh`,
  `td3-install.sh`, `td-common.sh`).
- `scripts/dual-screen-layout-and-power` - a ROCKNIX autostart script that keeps
  the add-on's screen enabled, correctly positioned, and pointed at the
  right output across a reboot (see the script's own header for the full
  "why" - three separate stock mechanisms undo this layout on every boot).
- `scripts/dp-sleep-guard.sh` + `dp-sleep-guard.service` - a systemd oneshot
  unit that turns the add-on's DisplayPort output off before suspend and
  back on at resume (on the tested kernel, suspending with DP lit resets the
  device instead of resuming).
- `scripts/install.sh` - installs the two scripts above on the device. On a handheld with two built-in screens it
  hands over to `install-layout-daemon.sh` instead.
- `scripts/dual-screen-builtin-layout` - the helper for the built-in two-screen handhelds: keeps the second screen
  on, turns its touch screen on if ROCKNIX left it off, and switches ROCKNIX's own bottom-screen app (lowerdeck)
  off through its setting. It never moves windows or turns a screen off. Untested on real devices.
- `scripts/install-layout-daemon.sh` - installs whichever of the two layout scripts the device's profile calls for
  and never leaves both installed.
- `app/tools/device-probe.sh` - a read-only report of a device's screens, touch screens and seats.
  `app/tools/tester-run.sh` - the guided test the tester guide describes.
- `scripts/migrate-old-daemon-names.sh` - for an install from the first
  release (25 Sep): stops and moves aside the old `092-dual-screen-persist`
  and `094-rp5deck` autostart files before the new ones go in.
- `app/steam/` - nested Steam (`install-steam-nested.sh`), optional, so the
  Command Center stays up while Steam is on screen. See
  [app/steam/INSTALL.md](app/steam/INSTALL.md).
- `scripts/deploy_rp5deck.py` + `scripts/rk.py` - optional PC-side tooling
  that does the app install below over SSH in one step (needs Python 3 with
  `paramiko`, and this repo cloned with git, since it lists the files with
  `git ls-files`).

## Install

Experienced users: [docs/QUICK-INSTALL.md](docs/QUICK-INSTALL.md) has just the
commands. The full walkthrough is in [docs/COMMAND-CENTER-GUIDE.md](docs/COMMAND-CENTER-GUIDE.md).
Both installers refuse to run unless the device is a Retroid Pocket 5 with
the add-on attached and showing a picture (set `TD_ALLOW_UNDOCKED=1`, or pass
`--undocked` to `deploy_rp5deck.py`, to install without the add-on), or one of the built-in two-screen handhelds
the app knows (it says it is untested there; `TD_ALLOW_ANY_DEVICE=1` tries any other model at your own risk). With SSH
enabled on the device:

0. Upgrading from the first release (25 Sep)? Copy `scripts/` to the device and
   run `sh migrate-old-daemon-names.sh` first. It stops the old
   `092-dual-screen-persist` and `094-rp5deck` and moves their files aside,
   so two copies never run.
1. Copy `app/` to a new folder on the device, for example
   `/storage/rp5deck-new/rp5deck` (not `/storage/rp5deck` itself).
2. On the device, in that folder, write the checksum list the installer
   checks every file against:
   `find . -type f ! -name MANIFEST.md5 ! -path '*/__pycache__/*' -print0 | xargs -0 md5sum > MANIFEST.md5`
3. Run `sh testday/td1-verify-build.sh`, then `sh testday/td3-install.sh install`.
   This backs up any existing install to `/storage/rp5deck-backups/` (the newest five are kept), keeps
   your `config.json`, installs `command-center-app` and starts the app.
4. Run `sh /storage/rp5deck/testday/td3-install.sh guard-live` to switch the
   focus guard from its first-install observe-only mode to normal.
5. Run `sh /storage/rp5deck/tools/install-es-hooks.sh`, then
   `systemctl restart essway`, so the Companion view follows the selected
   game.
6. Copy `scripts/` to the device and run `sh install.sh` there once, to
   install `dual-screen-layout-and-power` and `dp-sleep-guard` (on a built-in two-screen handheld it installs
   `dual-screen-builtin-layout` instead).
7. Optional, for Steam: `sh /storage/rp5deck/steam/install-steam-nested.sh install`
   then `systemctl restart essway`. It runs Steam inside the desktop so the
   Command Center stays up (details in `app/steam/INSTALL.md`).

`scripts/deploy_rp5deck.py <name> --install` does steps 1-4 from a PC.
To roll back: `sh /storage/rp5deck/testday/td3-install.sh rollback <backup dir>`.

## Status and known limitations

- Built for, and tested on, one Retroid Pocket 5 running one specific
  ROCKNIX/kernel build. Other ROCKNIX versions, kernels, or other
  Retroid-Dual-Screen-capable devices have not been tried and may need
  changes (sysfs paths, sway/wlroots behaviour, and EmulationStation's HTTP
  API can all differ or move between releases). The built-in two-screen handhelds are supported from ROCKNIX's
  scripts only, and the ES hooks, focus guard and game-screen overlay have not been checked on them.
- `dual-screen-layout-and-power` and `dp-sleep-guard` work around specific stock
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

## Coming soon

Not in this version:

- **TV mode**: the Command Center on a TV with a gamepad, keyboard or mouse for control, and a mouse mode for the
  web apps. Designed, not built.
- **Hibernate**: shown greyed out in the Power sheet for now, suspend is the low-power mode.
- **A one-script installer**: installing is still copy, verify, install, hooks, scripts and an optional Steam step.
- **Steam library paging** tried on a library bigger than twelve games (covered by tests, not yet tried on a device).
- **Testing on the built-in two-screen handhelds**: the profiles, the helper and the brightness controls need someone
  with the device ([tester guide](docs/TESTER-GUIDE.md)). Single-screen handhelds come after that.
- **ROCKNIX fixes**: the charger follow-ups and a USB suspend crash fix are going to ROCKNIX as pull requests after
  its code freeze. Until they are merged, the charging behaviour in the guide needs the project's kernel.

## Licence

GNU General Public License v2.0 - see [LICENSE](LICENSE).

## AI assistance

This app's code and documentation were written with AI assistance (Claude),
under the maintainer's direction and review, and tested on the maintainer's
own RP5 device. Flagging this up front in line with ROCKNIX's own request
that contributors be transparent about AI-assisted work.
