# Command Center patch notes

Version numbers are MAJOR.MINOR.PATCH: a new MINOR adds features, a new PATCH only
fixes things, a new MAJOR changes how something you rely on works. The newest version
is at the top. The Command Center's **Settings > About** shows the version you have
and the first lines of its notes.

## 1.6.2 - 1 Oct 2026

- Fixed touches landing in the wrong place until a restart. When sway had a second seat (it makes one when
  EmulationStation starts) every touch reached the app twice, once at the right spot and once without the
  panel's rotation, and the second one replaced the first. The app now listens to the first seat only.
- Installing now keeps the newest five backups of the app instead of every one (each is a full copy, about
  8 MB, and 63 had piled up).
- Firefox's disk cache is capped at 64 MB for the Browser, Discord and the YouTube App. Left alone it grew to
  about 150 MB per profile.
- Removed the "Use Firefox sign-in for YouTube" setting. It only switched code the app no longer runs (the old
  YouTube search player, with its yt-dlp and mpv calls), so it did nothing. That code is gone too.

## 1.6.1 - 1 Oct 2026

- The on-screen keyboard now has a way to close it. With ROCKNIX's keyboard up the Command Center shrinks to
  the space above it and had nothing left to close it with. A down arrow now sits on the keyboard's top edge,
  like Android's, and tapping it hides the keyboard.

## 1.6.0 - 1 Oct 2026

- Single-screen mode now runs the Browser, Discord and the YouTube App. Open one from the Command
  Center's tabs over EmulationStation (or Steam) and it fills the screen with a strip of controls at the
  bottom. The strip's Tabs button jumps to another app, Home goes back to what is underneath and keeps the
  app running, Close ends it. Opening an app again picks it up where it was, no restart.
- The strip now follows the app on screen: YouTube opened from Discord shows YouTube's controls, and the
  page never sits under the strip.
- New Steam tab, only while Steam is open: your installed games as cover art, tap one to start it in the
  open Steam, a button sorts them (Recent, A-Z, Most played). The game that is running is marked.
- The Companion shows a Steam game's name, art, hours played, last played, size, how long you have been
  playing and update progress.
- Clean state offers to stop the Steam game (Steam stays open) or to close Steam, only while Steam is open.
- New Settings > Steam page: what the bottom screen shows during a Steam game.
- Over Steam the Command Center now offers Settings too, along with Mixer, HUD, Hotkeys, Performance,
  Notes and Quit game.
- Steam runs inside the desktop so the Command Center stays up while Steam is on screen. Turn it on with
  `sh /storage/rp5deck/steam/install-steam-nested.sh install` (see steam/INSTALL.md).
- On a single screen, EmulationStation goes back to its own screen after Steam closes or ES restarts, even
  if a web app was on screen (needs the updated dual-screen-layout-and-power).

## 1.5.0 - 27 Sep 2026

- After you change button colours, Settings > Appearance has a button to restart ES so the new
  buttons show (tap twice). It waits while a game is running.
- If the Dual Screen add-on stops answering after several tries, Home says so and asks you to
  unplug it and plug it back in, instead of the screen just staying off.

## 1.4.0 - 27 Sep 2026

- Button colours now show on ES themes that bring their own button icons (like iconic-es):
  the colour you pick goes over the theme's buttons, and the theme's grey tint is turned off
  so the colours come through. The theme's other help icons (Menu, the d-pad) keep its grey.
- New "Use current theme" choice puts the theme's own buttons back exactly as they were.
- New Steam Deck button colours (dark grey caps).
- About has a Support page with the trans and lesbian pride flags. Want to support the
  project? Dont, give to Trans Lifeline instead (translifeline.org/donate).
- About's licence line now asks who hurt you if you paid for this.

## 1.3.3 - 27 Sep 2026

- A big full-screen warning when the charger is connected but not charging. It says the
  fix (unplug the charger and plug it back in) and stays until you unplug. A weak charger
  never sets it off.
- Single-screen mode is gone (Settings row and Ports entry). With one screen the Back
  button opens the Command Center anyway, and a floating button only shows if you pick a
  corner for it in Settings.

## 1.3.2 - 27 Sep 2026

- No volume icon in the top-right corner over the game when undocked. The Back button
  opens the Command Center, and the icon comes back if you turn single-screen mode on.

## 1.3.1 - 27 Sep 2026

- Undocked, the Notes, HUD and Companion tabs open again (they tried to move ES off
  the only screen and gave up). The Command Center also starts when you boot undocked.

## 1.3.0 - 26 Sep 2026

- Notes for each game: while a game runs, Notes opens that game's own notebook, and
  it opens again the next time you play it. Your usual notebook comes back after.
- A Notes tile in the game overlay, so game notes work over a DS game and undocked.
- With the dual screen disconnected, the Back button and the corner button open the
  Command Center without turning on single-screen mode first.

## 1.2.3 - 26 Sep 2026

- Notes: deleting a notebook no longer brings it straight back.

## 1.2.2 - 26 Sep 2026

- Notes: notebooks you can open, start and delete (File), pen width, an eraser, redo,
  and text colour and size.
- Settings fit every screen size preset, and a swipe up or down changes the page.
- Updates keep your notes; a Ports entry puts the screen size back to Auto.
- ROCKNIX's on-screen keyboard stays off the game screen.

Details:

- **Notes > File** lists your notebooks, newest first; tap one to open it. New notebook
  starts a fresh one. Delete asks for a second tap and moves the notebook to
  notes/.deleted, so it can be brought back. The notebook you had open opens again next
  time.
- **Drawing**: Pen picks the colour, Thin / Medium / Thick the width, Eraser removes the
  strokes it touches. Undo and Redo work on strokes.
- **Typing**: Text colour and Small / Medium / Large, kept per page.

## 1.2.1 - 26 Sep 2026

- Power: the greyed-out Hibernate button now says "Work in progress" - it saves to
  storage but does not resume after a restart yet.
- The bottom touchscreen stays on after the display system restarts.

## 1.2.0 - 26 Sep 2026

- Button colours: pick the colour of the A/B/X/Y pictures in EmulationStation.
- Screen size presets, so the Command Center fits smaller screens.
- Single-screen mode: open the Command Center over games without the add-on.
- Quit game has its own icon.
- PlayStation games start again (the default core could not run on the RP5).
- Settings show plain values, and About shows the version, credits and licence.

Details:

- **Button colours** (Settings > Appearance): the A, B, X and Y button pictures
  EmulationStation shows can match your colour theme or any of these: RP5 16 Bit,
  Black, White, GC, Yellow, Turquoise, Super Famicom, SNES (US), Xbox, PlayStation,
  Dreamcast, GameCube, Plain grey. Shows after EmulationStation restarts. Greyed out
  ("coming soon") on devices with 2, 3 or 6 face buttons.
- **Screen size preset** (Settings > Screens): Auto, 1920x1080, 1280x720, 1024x768,
  720x720, 640x480. A smaller screen gets the whole Command Center, shrunk to fit.
- **Single-screen mode** (Settings > Command Center, or Ports > Command Center Single
  Screen On-Off): with one screen, tap the floating button in the corner or press
  Back to open the Command Center over EmulationStation or the game. Off until you
  turn it on.
- The guide lists other ROCKNIX devices it should run on. Only the Retroid Pocket 5
  has been tested.
- **Plain wording**: every number in Settings has its unit (Stop charging at 85%,
  Video start delay 0.5 s), and the hotkey sheet, Clean state and Mixer no longer
  show program names or internal settings.
- **Sign-ins stay private**: signing in to the Browser, Discord and YouTube works as
  before, but nothing that keeps you signed in is stored as plain text. Firefox never
  saves a password, and the session data (cookies, site storage, open tabs, history)
  is kept in an encrypted file tied to this device. It is opened into memory only
  while the browser runs and sealed again when it closes (and every 5 minutes while
  it is open). The first start after the update moves your existing sign-ins in and
  removes the plain copies.
- **Settings > About**: version, credits, patch notes, and the licence (GNU GPL v2 -
  free; you should not have paid for it).

## 1.1.0 - 26 Sep 2026

- Power tile: Sleep, Restart and Shut down (Restart and Shut down ask first).
- Over a dual-screen game: Hotkeys, Performance and Quit game join Mixer and HUD.
- Notes tab: draw with a finger or type.
- Manual viewer: zoom, fit, single page and page jumps; manuals fit the screen.
- Brightness for both screens, with an option to keep them matched.
- Top screen off saves power; Performance switches Auto, Max and Saver.
- The bottom screen dims with EmulationStation's screensaver.
- YouTube can no longer take the controller away from a game.
- Edit which buttons Home shows, and in what order.
- Tiles that duplicated a tab (Browser, Discord, YouTube App, HUD) left Home.
- Games that opened on the Command Center's screen now move to the game screen.

## 1.0.0 - 25 Sep 2026

- First public release: the Command Center on the Retroid Pocket 5's own screen
  with the Dual Screen add-on - volume and mixer, game companion (art, video,
  manuals), browser, YouTube, Discord, hotkey cheat sheet, stick lights, clean
  state, screen swap, colour themes, safe charging and the overlay over DS/3DS
  screens.
