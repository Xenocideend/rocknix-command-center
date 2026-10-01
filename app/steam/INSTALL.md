# Steam beside the Command Center (nested Steam)

ROCKNIX's own Steam launch stops the whole desktop (sway) while Steam runs, which takes the Command
Center with it. This runs Steam's gamescope **inside** the desktop instead, so the Command Center stays
up on the other screen (or over Steam on a single screen) and its Steam features work.

## Turn it on

The app has to be installed first (see the guide, section 2). Then, on the device:

```sh
sh /storage/rp5deck/steam/install-steam-nested.sh install
systemctl restart essway
```

It does three things:

- puts `steam-keep-command-center-alive` in `/storage/.config/autostart/`, which at every boot shadows
  ROCKNIX's `/usr/bin/start_steam_arm64.sh` with `start_steam_nested.sh` (a bind mount, the real file is
  never touched);
- puts `085-steam-display-priority` in `/storage/.config/profile.d/`: ROCKNIX's launcher passes
  `--prefer-output $SDL_VIDEO_DISPLAY_PRIORITY` to gamescope and nothing set it on the RP5, so gamescope
  took `-W` as the output name and tried to run `1080` as the game ("Failed to start process 1080");
- replaces the launcher for the current boot too, so you don't have to reboot.

Run it from `/storage/rp5deck/steam` (the installed copy), it refuses anywhere else. It is safe to run again.
`sh install-steam-nested.sh status` shows what is in place.

Steam itself has to be set up on ROCKNIX first. `/storage/.local/share/Steam` must be a real folder (on the
RP5 it links to `/storage/roms/steam`); the installer warns when it isn't.

## Turn it off

```sh
sh /storage/rp5deck/steam/install-steam-nested.sh remove
```

Puts ROCKNIX's own launcher back and removes the two files. A reboot without the boot script has the same
effect.

## What you get

- The Companion shows the Steam game's name, art, hours played, last played, size, "Playing for N min" and
  update progress.
- A **Steam** tab (only while Steam is open) lists your installed games as cover art, a tap starts one in
  the open Steam, a button sorts them.
- **Clean state** offers to stop the Steam game (Steam stays open) or close Steam, only while Steam is open.
- **Settings > Steam** chooses what the bottom screen shows during a Steam game.
- The summon button opens the Command Center over Steam, with Mixer, HUD, Hotkeys, Performance, Notes,
  Settings and Quit game.

## Costs and limits

- Every frame is composited twice (gamescope, then the desktop), a little GPU work and about a frame of
  latency that ROCKNIX's own launch doesn't pay. It is not measured here.
- The gamepad stays with Steam, the Command Center never reads it.
- When Steam closes while a web app's workspace is on screen on a single screen, the daemon puts
  EmulationStation back on its own workspace.
- It is all or nothing: every Steam launch is nested while it is on.
