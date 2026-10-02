# Testing the Command Center on your handheld

> **Not an official ROCKNIX project.** The ROCKNIX team did not make this, review it or endorse it, so please do not ask
> them for help with it. If something breaks don't yell at the ROCKNIX team, yell at me.

Thank you for trying this. The Command Center was built and tested on a Retroid Pocket 5 only. This guide is for
handhelds with **two built-in screens** running ROCKNIX: the AYN Thor and Thor Lite, the AYANEO Pocket DS and the Anbernic
RG DS and RG DS Plus. Support for them was written from ROCKNIX's own scripts, so it **has not been tried on a real one**.
Your test log is how it gets fixed.

Anything can go wrong on a first try. Nothing here touches the kernel, the boot files or your games, and everything below can
be undone (see "Undo").

## What it changes on your device

- Copies the app to `/storage/rp5deck` and adds one start-up entry (`command-center-app`).
- Adds a small helper (`dual-screen-builtin-layout`) that keeps the **second (bottom) screen switched on**, turns its touch
  screen on if ROCKNIX left it off, and **switches off ROCKNIX's own bottom-screen app (lowerdeck)**, because it would sit on the
  same screen. It does this through ROCKNIX's own setting `rocknix.bottomscreen.type`. It never moves windows, never turns a
  screen off and never closes a program.
- Installs the EmulationStation hooks the app uses to notice games starting (this restarts EmulationStation once).
- Does **not** change ROCKNIX's dual-screen setting. It does not touch `/flash`, the kernel or your ROMs.

## What you need

- A handheld from the list above on a current ROCKNIX.
- SSH switched on (START > Network Settings > Enable SSH, the address is shown there).
- A computer on the same Wi-Fi with `ssh` and `scp` (Windows 10 and 11 have them in PowerShell).
- About 15 minutes.

Just want to help without installing anything? Copy the pack as in step 1 and run only
`sh /storage/command-center-pack/app/tools/device-probe.sh`. It is read-only and prints what I need to know about your
screens. Send me what it prints.

## 1. Copy the pack to the handheld

On your computer, in the folder where you saved the zip (replace <address> with the one shown in the Network Settings menu):

```
scp command-center-tester-pack-*.zip root@<address>:/storage/
ssh root@<address>
```

(The ROCKNIX default password is `rocknix`.) On the handheld:

```
cd /storage
unzip command-center-tester-pack-*.zip
mv command-center-tester-pack-* command-center-pack
```

## 2. Install

Still on the handheld:

```
cd /storage/command-center-pack/app
sh testday/td1-verify-build.sh           # must end with: RESULT: build verified
sh testday/td3-install.sh install        # copies the app, backs up what it replaces, starts it
sh /storage/rp5deck/testday/td3-install.sh guard-live
sh /storage/rp5deck/tools/install-es-hooks.sh
systemctl restart essway                 # EmulationStation restarts once
sh /storage/command-center-pack/scripts/install-layout-daemon.sh
```

If the first command says `HARD STOP: model is '...'`, your model is not on the list. Say so in your message, and you can
still try with `TD_ALLOW_ANY_DEVICE=1 sh testday/td1-verify-build.sh` (and the same in front of the install line).

The last command says which helper it installed. It must say `dual-screen-builtin-layout`. If it says
`dual-screen-layout-and-power`, stop and send me the line: that helper is for the Retroid add-on.

Then restart the handheld: `reboot`.

## 3. Run the test

After the reboot, once EmulationStation is up:

```
sh /storage/rp5deck/tools/tester-run.sh --scripts /storage/command-center-pack/scripts
```

It takes about 10 minutes. It collects what the device reports, then asks you to look at the screens and answer:

| Question | What to look at |
|---|---|
| Command Center shown on the second screen | a grid of tiles with a tab strip across the top |
| Touch works / lands where you touch | tap a tile, open Settings, drag a slider |
| EmulationStation on the other screen | use the buttons to move around |
| Swap screens and Top screen tiles missing | they belong to the Retroid add-on |
| On-screen keyboard and its down arrow | the Keyboard tile |
| ROCKNIX's bottom-screen app stays away in a RetroArch game | start any RetroArch game |
| A DS game's second screen | optional, only if you have a DS game |
| Which brightness slider moves which screen | Settings > Screens, two sliders |

Answer `y`, `n` or `s` (skip). After each answer you can type a note, for example "touch is mirrored left to right". The notes
are the most useful part. `--auto` collects everything without asking anything, if you cannot look at the screens now.

## 4. Send me the log

The script prints the log's name, `/storage/rp5deck-test-log-DATE-TIME.txt`. From your computer:

```
scp root@<address>:/storage/rp5deck-test-log-*.txt .
```

Please read it before you send it. It contains the model and ROCKNIX version, the screens and touch screens ROCKNIX reports,
brightness controls, the app's recent log lines, kernel messages about displays and touch, and your answers. Game and ROM
names, serial numbers, MAC addresses and IP addresses are removed. It contains no passwords, sign-ins, saves or settings
files. Send it with: your device model, the ROCKNIX version, and anything you noticed that the questions did not ask.

## Undo

| What | Command |
|---|---|
| Stop the app | `touch /storage/.disable-rp5deck` (delete the file to bring it back) |
| Stop the helper | `touch /storage/.disable-dualscreen` then reboot |
| Give ROCKNIX's bottom-screen app back | `sh /storage/.config/autostart/dual-screen-builtin-layout --restore-lowerdeck` |
| Leave the bottom screen's power to ROCKNIX | `touch /storage/.bottom-screen-power-rocknix` |
| Leave ROCKNIX's bottom-screen app alone from the start | `touch /storage/.keep-lowerdeck` before installing the helper |
| Roll the app back | `sh /storage/rp5deck/testday/td3-install.sh rollback /storage/rp5deck-backups/<newest folder>` |
| Remove the EmulationStation hooks | `sh /storage/rp5deck/tools/install-es-hooks.sh --remove && systemctl restart essway` |
| Remove everything | the three lines above, then delete `/storage/.config/autostart/command-center-app`, `/storage/.config/autostart/dual-screen-builtin-layout` and `/storage/rp5deck`, and reboot |

If a screen stays black after the install, reboot first. If it is still black, connect over SSH and run the "Stop the helper"
and "Stop the app" lines above, then reboot.

## What I most want to learn

1. The real output names and which screen is physically the bottom one (the log shows both).
2. Whether touch lands where you touch, on which screen, and whether it works at all.
3. Whether the second screen stays on, and whether ROCKNIX's bottom-screen app stays away.
4. Which brightness slider moves which backlight.
5. Anything that crashes or logs errors (the log has the app's last lines).

Things I could not check at all: how the Command Center looks over a DS or 3DS game's second screen on your device, whether
the game-screen overlay works there, and how long the battery lasts with the second screen kept on.
