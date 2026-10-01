// rp5deck Firefox profile prefs — touch handheld tuning for the RP5's bottom
// screen (DSI-1, 1920x1080, ~5.5in, ~400ppi). Copied verbatim to the runtime
// profile at /storage/rp5deck-firefox/profile/user.js by Browser.open(); kept
// here under version control as the source of truth. See BROWSER-NOTES.md for
// how each pref was chosen and its source.
//
// user.js is re-applied by Firefox on every startup (it is not a one-time
// migration like prefs.js), so editing this file and re-copying it is enough
// to change behaviour — no profile reset needed.

// --- No page fullscreen (FOC2) ----------------------------------------------
// The Fullscreen API lets a PAGE (e.g. a video site) cover the entire
// window with no chrome - on this panel that would also cover rp5deck's
// BAR strip (the Close/Back/keyboard controls), leaving no way to get out
// short of the hardware Back button. Disabling it keeps every page inside
// the tiled window the strip sits below; F11/the browser's own fullscreen
// is unaffected (this pref only gates the DOM Fullscreen API sites call).
user_pref("full-screen-api.enabled", false);

// --- Touch / scrolling -----------------------------------------------------
// dom.w3c_touch_events.enabled: 0=off, 1=always on, 2=autodetect (default).
// Force it on rather than trust autodetection through our layer-shell-hosted
// Wayland embedding.
user_pref("dom.w3c_touch_events.enabled", 1);
user_pref("apz.allow_zooming", true);              // pinch-to-zoom
user_pref("apz.touch_start_tolerance", "0.06");     // less slop before a touch begins panning (float prefs are strings: a bare 0.06 is a parse error, seen on the device)
user_pref("general.smoothScroll", true);            // smooth/kinetic scrolling
user_pref("general.smoothScroll.msdPhysics.enabled", true);
user_pref("apz.overscroll.enabled", true);          // mobile-style rubber-band edge feedback

// --- UI density/scale for a 1920x1080, ~5.5in (~400ppi) touch panel --------
// layout.css.devPixelsPerPx scales BOTH the browser chrome and page content
// globally (unlike page zoom, which only affects content). "-1" (default)
// computes from the OS's reported DPI, which is usually wrong/tiny for this
// panel. 2.0 approximates phone-browser scaling for a panel this dense;
// tune to taste.
user_pref("layout.css.devPixelsPerPx", "2.0");

// --- No first-run / welcome / onboarding screens ---------------------------
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url_additional", "");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.upgradeDialog.enabled", false);
user_pref("browser.uitour.enabled", false);
user_pref("browser.disableResetPrompt", true);
user_pref("browser.rights.3.shown", true);          // skip "know your rights" first-run notice
user_pref("browser.laterrun.enabled", false);
user_pref("browser.newtabpage.activity-stream.asrouter.userprefs.cfr.addons", false);
user_pref("browser.newtabpage.activity-stream.asrouter.userprefs.cfr.features", false);
user_pref("browser.newtabpage.activity-stream.feeds.snippets", false);

// --- No default-browser prompt ---------------------------------------------
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.shell.didSkipDefaultBrowserCheckOnFirstRun", true);

// --- Session restore: tabs survive Close (owner, 24 Sep) --------------------
// Reverses an earlier "kiosk: always start clean" decision (see git history)
// now that the owner explicitly asked for the opposite: "the browser needs
// to save tabs when closed". browser.startup.page: 0=blank, 1=homepage,
// 3=resume previous session.
user_pref("browser.startup.page", 3);
user_pref("browser.sessionstore.resume_from_crash", true);
user_pref("browser.sessionstore.resume_session_once", false);       // a one-shot flag Firefox
                                                                     // itself sets/clears; page=3
                                                                     // makes it moot either way
user_pref("browser.sessionstore.max_resumed_crashes", -1);          // -1 = unlimited (Mozilla's
                                                                     // own sentinel): a crash must
                                                                     // never stop tabs coming back
// Marionette (--marionette, browser.py) applies its own "recommended" prefs
// bundle at startup UNLESS this is false - and that bundle includes its own
// browser.startup.page=0, which would silently override the line above every
// time rp5deck's Firefox starts (this is a SEPARATE mechanism from
// dom.webdriver.enabled/navigator.webdriver, which Marionette sets for spec
// compliance whenever a WebDriver session is active and this pref does not
// affect - see browser.py's YOUTUBE_TV_USER_AGENT comment and W2-NOTES.md
// for why sign-in avoids that some other way instead).
user_pref("remote.prefs.recommended", false);

// --- YouTube TV tile: NOT here any more (W2b) -------------------------------
// W2a shipped general.useragent.override.youtube.com in THIS profile, on the
// wrong assumption that Firefox still read a per-domain UA override. It does
// not, and has not since Firefox 71 (Mozilla bug 1513574, "Remove
// UserAgentOverrides.jsm" - confirmed on the device, 24 Sep evening: the
// pref was silently ignored and youtube.com/tv served the normal desktop
// sign-in page). Only the GLOBAL general.useragent.override still works,
// which would also change Browser/Discord's own UA - so the YouTube TV tile
// now runs in its OWN profile (firefox/tvprofile-user.js) with the override
// there instead. See browser.py's YOUTUBE_TV_USER_AGENT comment and
// patches/W2-NOTES.md ("W2b" section) for the full story.

// --- Downloads: ask every time ----------------------------------------------
// No fixed download directory is assumed on this device, so prompt for a
// location each time rather than silently writing into a default folder.
user_pref("browser.download.useDownloadDir", false);

// --- Telemetry / data collection off ---------------------------------------
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("toolkit.telemetry.archive.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("app.shield.optoutstudies.enabled", false);
user_pref("browser.ping-centre.telemetry", false);
user_pref("browser.newtabpage.activity-stream.telemetry", false);

// --- Misc kiosk hygiene ------------------------------------------------------
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.warnOnQuit", false);
user_pref("extensions.pocket.enabled", false);
user_pref("extensions.autoDisableScopes", 0);        // irrelevant here: we never add extensions

// --- The gamepad stays a game control (owner rule; batch 1, 25 Sep) ----------
// YouTube's TV app navigates on the Gamepad API, so every controller press also
// moved YouTube while a game ran. No page in this profile may read the pad.
user_pref("dom.gamepad.enabled", false);

// --- Sign-ins stay private (owner, 26 Sep) ------------------------------------
// Never save a password or form data: a sign-in lives only as the site's session
// cookie in this profile, which rp5deck keeps owner-only (browser.lock_profile).
user_pref("signon.rememberSignons", false);
user_pref("signon.autofillForms", false);
user_pref("signon.generation.enabled", false);
user_pref("browser.formfill.enable", false);
