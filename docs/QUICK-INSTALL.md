# Quick install (experienced users)

For users comfortable with SSH on ROCKNIX. The full walkthrough, with what
each step does and how to undo it, is in
[COMMAND-CENTER-GUIDE.md](COMMAND-CENTER-GUIDE.md#2-installing-updating-removing).

**You need:** a Retroid Pocket 5 on ROCKNIX, the Retroid Dual Screen add-on
attached in display mode and showing a picture, and SSH enabled
(START > Network Settings > Enable SSH). The install scripts refuse to run
unless they detect an RP5 with the add-on's display connected.

## From a PC

```bash
RP5=root@<device-ip>
ssh $RP5 mkdir -p /storage/rp5deck-new
scp -r app $RP5:/storage/rp5deck-new/rp5deck
scp -r scripts $RP5:/storage/rp5deck-scripts
ssh $RP5
```

## On the device

```bash
cd /storage/rp5deck-new/rp5deck
find . -type f ! -name MANIFEST.md5 ! -path '*/__pycache__/*' -print0 | xargs -0 md5sum > MANIFEST.md5
sh testday/td1-verify-build.sh          # must end: RESULT: build verified
sh testday/td3-install.sh install       # backs up any old install, keeps config.json, starts the app
sh /storage/rp5deck/testday/td3-install.sh guard-live   # focus guard out of observe-only mode
sh /storage/rp5deck/tools/install-es-hooks.sh           # add --force only if you accept its name warnings
systemctl restart essway                # ES reads hooks only at startup; never restart sway.service
sh /storage/rp5deck-scripts/install.sh  # 092-dual-screen-persist + dp-sleep-guard
reboot                                  # 092 starts from autostart at boot
```

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
- Logs: `/storage/rp5deck/log/`. Settings: `/storage/rp5deck/config.json`.

## Turn off or roll back

| Action | Command |
|---|---|
| Disable the app | `touch /storage/.disable-rp5deck` (delete the file to re-enable) |
| Disable the layout daemon | `touch /storage/.disable-dualscreen` |
| Roll back the app | `sh /storage/rp5deck/testday/td3-install.sh rollback /storage/rp5deck-backups/<timestamp>` |
| Remove the ES hooks | `sh /storage/rp5deck/tools/install-es-hooks.sh --remove && systemctl restart essway` |
| Remove the sleep guard | `systemctl disable dp-sleep-guard.service`, then delete `/storage/.config/dp-sleep-guard.sh` and `/storage/.config/system.d/dp-sleep-guard.service` |

Known issues (charging through the add-on, the top screen after a boot with
a charger connected) and their workarounds are in
[Known limitations](COMMAND-CENTER-GUIDE.md#12-known-limitations-and-troubleshooting).
