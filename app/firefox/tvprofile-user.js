// rp5deck's YouTube TV tile - its OWN Firefox profile (browser.TV_PROFILE_DIR
// = /storage/rp5deck-firefox/tvprofile), SEPARATE from the normal Browser/
// Discord profile (firefox/user.js -> /storage/rp5deck-firefox/profile).
// Copied verbatim to the runtime profile by web_tiles.YtAppSession's
// browser.Browser instance the same way Browser.open() copies the other
// profile's user.js. Kept here under version control as the source of truth.
//
// Why a whole second profile just for one pref: youtube.com/tv only serves
// its TV/leanback UI to a recognised TV/console user agent, and Firefox has
// not honoured a PER-DOMAIN override (general.useragent.override.<domain>)
// since Firefox 71 (Mozilla bug 1513574, "Remove UserAgentOverrides.jsm";
// bug 1589607 tracks the resulting "stopped working" reports) - confirmed on
// this device, 24 Sep evening, when the per-domain pref silently did nothing
// and youtube.com/tv served the normal desktop sign-in page instead. Only
// the GLOBAL general.useragent.override (below) still works, and a global
// override would change Browser/Discord's UA too if it lived in their
// shared profile - hence this tile gets its own profile, process, app_id
// (browser.TV_APP_ID) and Marionette port (browser.TV_MARIONETTE_PORT), so
// the override can never leak into normal browsing.
//
// Sourcing for the UA string itself: browser.py's YOUTUBE_TV_USER_AGENT
// comment (Samsung's own documented Tizen Smart-TV UA format, cross-checked
// against a Jan 2026 report of a near-identical string still unlocking
// youtube.com/tv). Kept in sync with that constant by
// tests/test_browser.py::TestTvProfileUserAgent.
// PS4 Leanback (24 Sep): Tizen 2.3 showed "This device no longer fully
// supports YouTube"; see browser.YOUTUBE_TV_USER_AGENT for the probe results.
user_pref("general.useragent.override",
         "Mozilla/5.0 (PS4; Leanback Shell) Gecko/20100101 Firefox/65.0 LeanbackShell/01.00.01.75 Sony PS4/ (PS4, , no, CH)");

// --- No page fullscreen (FOC2, same reasoning as the main profile) ---------
// A fullscreen leanback page would cover rp5deck's BAR strip (the D-pad),
// leaving no way to get out short of the hardware Back button.
user_pref("full-screen-api.enabled", false);

// --- Touch / scrolling (same as the main profile - see its own comments) ---
user_pref("dom.w3c_touch_events.enabled", 1);
user_pref("apz.allow_zooming", true);
user_pref("apz.touch_start_tolerance", "0.06");
user_pref("general.smoothScroll", true);
user_pref("general.smoothScroll.msdPhysics.enabled", true);
user_pref("apz.overscroll.enabled", true);

// --- UI density/scale (same panel as the main profile) ----------------------
user_pref("layout.css.devPixelsPerPx", "2.0");

// --- No first-run / welcome / onboarding / default-browser prompt ----------
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url_additional", "");
user_pref("browser.aboutwelcome.enabled", false);
// Firefox's first-run "Welcome to Firefox" Terms of Use modal covered the
// YouTube page on a fresh profile (device, 24 Sep 20:04). The owner chose to
// skip it the same way the main Browser profile already does (its prefs.js
// carries these two).
user_pref("browser.preonboarding.enabled", false);
user_pref("termsofuse.bypassNotification", true);
// chrome/userChrome.css (tvprofile-userChrome.css) hides the tab strip and
// address bar so YouTube's TV interface gets the whole window.
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
user_pref("browser.startup.upgradeDialog.enabled", false);
user_pref("browser.uitour.enabled", false);
user_pref("browser.disableResetPrompt", true);
user_pref("browser.rights.3.shown", true);
user_pref("browser.laterrun.enabled", false);
user_pref("browser.newtabpage.activity-stream.asrouter.userprefs.cfr.addons", false);
user_pref("browser.newtabpage.activity-stream.asrouter.userprefs.cfr.features", false);
user_pref("browser.newtabpage.activity-stream.feeds.snippets", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.shell.didSkipDefaultBrowserCheckOnFirstRun", true);

// --- Session restore: off in this profile ------------------------------------
// This tile always launches straight to browser.YOUTUBE_TV_URL (an explicit
// navigate() after Marionette's session starts, not this pref) - login
// (cookies/localStorage) persists in the profile regardless of whether the
// LAST TAB is restored, so there is nothing to gain from page=3 here, unlike
// the main Browser/Discord profile (firefox/user.js).
user_pref("browser.startup.page", 1);
user_pref("browser.sessionstore.resume_from_crash", false);

// --- Marionette (still needed here: the D-pad drives the page over
// WebDriver:PerformActions) - same reasoning as firefox/user.js: without
// this, Marionette's own "recommended prefs" bundle would silently override
// browser.startup.page/telemetry/etc. above on every start.
user_pref("remote.prefs.recommended", false);
// --marionette takes no port argument: Firefox listens on marionette.port
// (default 2828, the Browser/Discord instance's port). Without this the TV
// Firefox listened on 2828 while rp5deck waited on 2829 and gave up ("Firefox
// did not start", device 24 Sep 19:50). Must equal browser.TV_MARIONETTE_PORT.
user_pref("marionette.port", 2829);

// --- Downloads: ask every time (same as the main profile) -------------------
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
user_pref("extensions.autoDisableScopes", 0);

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

// --- Disk cache capped ---------------------------------------------------------
// Firefox sizes its disk cache from the free space and let each profile here grow to ~150 MB of flash writes
// for two web apps. 64 MB is plenty, and smart sizing has to be off or the capacity is ignored.
user_pref("browser.cache.disk.smart_size.enabled", false);
user_pref("browser.cache.disk.capacity", 65536);    // KiB
