#!/bin/bash
# Nested Steam (turned on by steam/install-steam-nested.sh): run Steam's gamescope session
# NESTED inside the already-running sway compositor, instead of ROCKNIX's
# stock DRM-backend path which stops sway for the whole session, which takes
# the Command Center with it.
#
# This is a drop-in REPLACEMENT for /usr/bin/start_steam_arm64.sh, the
# concrete launcher `/usr/bin/start_steam.sh`'s own entry-point dispatch execs
# into for ROCKNIX's default `steam_version=arm64` (verified: start_steam.sh's
# `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]` block at its tail does
# `exec /usr/bin/start_steam_arm64.sh "$@"` when STEAM_VERSION is unset/arm64).
# See steam/INSTALL.md for how to turn it on and off.
#
# ORIGIN (fetched fresh via `gh api`, 2026-09-24, no local clone):
# ROCKNIX/distribution, branch `next`, commit
# 19427e5d51ae4426c42f11e15fae4c3399326da8:
#   projects/ROCKNIX/packages/emulators/standalone/steam/scripts/start_steam_arm64.sh
#     (this file's stock counterpart - the whole call sequence below, minus
#     the steam_launch_bigpicture override, is copied from it verbatim)
#   projects/ROCKNIX/packages/emulators/standalone/steam/scripts/start_steam.sh
#     (the sourced function library - used UNMODIFIED via `. /usr/bin/start_steam.sh`,
#     so every helper except steam_launch_bigpicture stays byte-for-byte
#     upstream and inherits future ROCKNIX fixes automatically)
#
# Stock start_steam_arm64.sh, quoted in full for the diff:
#   STEAM_MAIN_SCRIPT=${0}
#   STEAM_FLAVOR=arm64
#   source /etc/profile
#   set_kill set "gamescope steam FEX"
#   . /usr/bin/start_steam.sh
#   steam_ensure_fex_config_template
#   steam_prepare_storage_and_vdf
#   steam_load_es_thunk_settings "$@"
#   steam_apply_lsfg_settings
#   steam_apply_fps_limit
#   steam_set_cpu_affinity
#   steam_debug_print
#   steam_arm64_binfmt_and_proton_prep
#   steam_read_sway_geometry
#   steam_setup_environment
#   steam_scope_reexec_if_needed "$@"
#   steam_dual_screen_begin
#   steam_launch_bigpicture "$@"
#   steam_dual_screen_end
#   systemctl restart systemd-binfmt
#
# ST1 (research/ST1-steam.md) traced the ONE line in the sourced library's
# own steam_launch_bigpicture (start_steam.sh) that breaks rp5deck - on the
# RP5 (HW_DEVICE=SM8250, so the DRM backend branch always applies):
#     unset WAYLAND_DISPLAY
#     systemctl stop sway
# ...and the script only ever calls `systemctl start essway` on exit, never
# `start sway` directly - `essway.service`'s own `Requires=sway.service` is
# what pulls sway back afterward. Every Steam session on this device is
# therefore a full sway restart, and for its whole duration there is NO
# compositor for rp5deck's layer-shell surface to live in. command-center-app's ST2
# fix (this project's `command-center-app`) makes recovery from that clean; it does
# not make rp5deck visible DURING the session. THIS file is the other half of
# ST2: make the compositor never go away in the first place.
#
# THE ONE THING THIS FILE CHANGES: `steam_launch_bigpicture` is redefined
# below (after sourcing the stock library, so this definition wins) to force
# gamescope's NESTED mode - an ordinary Wayland client of the
# ALREADY-RUNNING sway, exactly the way upstream's own script already runs
# things on `HW_DEVICE=SM4450` (start_steam.sh's `gamescope_backend` variable
# is "wayland" for that one device already; this file just takes that branch
# unconditionally instead of only for that hardware) - and the matching
# `unset WAYLAND_DISPLAY; systemctl stop sway` / `systemctl start essway`
# pair is dropped (nothing stops sway or ES, so nothing needs recovering).
# Every other helper - fex config, thunk settings, lsfg, fps limit, cpu
# affinity, the arm64 binfmt/proton prep, dual-screen begin/end (a no-op
# with this project's DEVICE_HAS_DUAL_SCREEN pinned false, per command-center-app's
# FLAG fix), and the systemd-scope re-exec - runs UNCHANGED from the sourced
# upstream file.
#
# Scope: covers ROCKNIX's DEFAULT `steam_version=arm64` flavor only (the one
# ST1's entire analysis is about, and the one every citation above is about).
# The legacy `steam_version=x86` flavor (start_steam_x86.sh, whole Steam
# client under FEX) is out of scope - see steam/INSTALL.md.

STEAM_MAIN_SCRIPT=${0}
STEAM_FLAVOR=arm64

# Fixed nested-window size (ST2 instruction): 1920x1080 regardless of which
# physical output ES/rp5deck/092 currently have DP-1 or DSI-1 set to - this
# matches DP-1's own scale-1 mode (TASKS.md, 23 Sep dual-screen session), so
# Steam's nested window is pixel-1:1 on the add-on and merely "windowed"
# (not cropped/scaled) if it ever lands somewhere else. Override with
# ROCKNIX_STEAM_NESTED_W/H for a different device profile; not read from
# swaymsg on purpose, so Steam's own size never depends on whatever rp5deck
# or 092 are doing to the outputs at that moment.
NESTED_W=${ROCKNIX_STEAM_NESTED_W:-1920}
NESTED_H=${ROCKNIX_STEAM_NESTED_H:-1080}

# The app_id gamescope's OWN nested Wayland backend gives its wl_surface -
# fixed by gamescope itself (upstream ValveSoftware/gamescope, at the exact
# commit ROCKNIX's package.mk pins - fa0b4d3342078f01eadff0193e09c3b561f40c03
# - `src/Backends/WaylandBackend.cpp:1420`:
# `libdecor_frame_set_app_id( m_pFrame, "gamescope" )`). There is no
# "--app-id"/"--name"/"--title" flag in gamescope's own `src/main.cpp`
# option table to override it (checked directly against that commit: the
# only "name"-shaped flags are `--vr-overlay-explicit-name` and
# `--vr-overlay-default-name`, both VR-only; the only "T" flag is
# `-T/--stats-path`, which writes a stats file, not a window property). So
# "the app_id you pick" is really "the one value gamescope offers" - this
# constant exists so a sway rule has ONE named place to read it from, not
# because this script can make gamescope emit something else.
STEAM_NESTED_APP_ID="gamescope"
# The window TITLE gamescope shows is dynamic
# (WaylandBackend.cpp:1254-1260, CWaylandConnector::SetTitle: "gamescope"
# until a client is tracked, then that client's own title) - expected to
# read "Steam" once Big Picture is up and then whatever the running game
# sets, but UNVERIFIED live (no device access this task - see the device
# test plan in steam/INSTALL.md). Match sway rules on app_id, not title.

source /etc/profile
set_kill set "gamescope steam FEX"

# shellcheck source=/dev/null
. /usr/bin/start_steam.sh

# --- Option A: nested gamescope instead of the DRM backend ------------------
# A close copy of the sourced steam_launch_bigpicture (start_steam.sh),
# minus the DRM-backend's "unset WAYLAND_DISPLAY; systemctl stop sway" block
# and the matching "systemctl start essway" recovery call, with the backend
# forced to "wayland" instead of branching on HW_DEVICE, and W/H pinned to
# NESTED_W/NESTED_H instead of the focused output's own current mode.
# Deliberately NOT using `set -u` (neither does upstream) - PREFER_OUTPUT,
# EMUPERF, mangoapp and rotate_clamp are all meant to expand to nothing when
# unset/empty, exactly as the sourced library already relies on.
steam_launch_bigpicture() {
  local game_uri=""
  local force_orientation="normal"
  local gamescope_mode_file="/storage/.config/gamescope/modes.cfg"
  local steam_exit_code=0
  local gamescope_exit_code=0
  local steam_exit_code_file=""
  local mangoapp=""
  if [ "${DEVICE_MANGOHUD_SUPPORT}" = "true" ] && [ "$(get_setting "rocknix.mangohud.enabled" "${PLATFORM}" "${GAME}")" = "1" ]; then
    mangoapp="--mangoapp"
  fi
  # Nested, sway rotates the window with its output, so gamescope renders upright. Rotating here as
  # well turns the picture sideways (the DRM backend needs the rotation, the nested one does not).
  # ST2: fixed size, not the focused output's own current_mode width/height.
  # steam_read_sway_geometry (called below, upstream, unedited) already set
  # TRANSFORM/REFRESH_HZ from swaymsg - sway is alive in nested mode, so that
  # part of upstream's own geometry read still works and is kept; only the
  # size is overridden.
  W="${NESTED_W}"
  H="${NESTED_H}"

  local rotate_clamp=""

  if [[ "$1" == *.desktop && -f "$1" && "$(basename "$1")" != "Steam.desktop" ]]; then
    local exec_line
    exec_line=$(grep -m1 '^Exec=' "$1" | cut -d'=' -f2-)
    game_uri="${exec_line#steam } -silent"
  fi

  # Nested, always - the whole point of Option A: gamescope is an ordinary
  # Wayland client of the ALREADY-RUNNING sway (the same mode upstream
  # already uses for HW_DEVICE=SM4450; this file takes that branch
  # unconditionally instead of only for that one hardware ID).
  local gamescope_backend="wayland"

  mkdir -p "$(dirname "$gamescope_mode_file")"
  touch "$gamescope_mode_file"
  unset MESA_LOADER_DRIVER_OVERRIDE

  # THE DELETION THAT MATTERS: upstream's DRM-backend branch here did
  #     unset WAYLAND_DISPLAY
  #     systemctl stop sway
  # - deliberately absent. sway keeps running throughout; rp5deck's layer
  # surface is never torn down, and WAYLAND_DISPLAY stays set so gamescope
  # itself can connect to it as a nested client.

  if [ "${STEAM_FLAVOR}" = "arm64" ]; then
    export STEAM_COMPAT_GRAPHICS_PROVIDER=/storage/.local/share/fex-emu/RootFS/ArchLinux/graphics_provider.json
    steam_exit_code_file=$(mktemp /tmp/steam-exit-code.XXXXXX)
    steam_touch_calibration_begin "${force_orientation}"
    trap steam_touch_calibration_end EXIT
    while true; do
      rm -f "${steam_exit_code_file}"
      GAMESCOPE_MODE_SAVE_FILE="${gamescope_mode_file}" GAMESCOPE_FAKE_OUTPUT_MM=508x286 \
      LD_LIBRARY_PATH=/storage/.local/share/Steam/lib/aarch64-linux-gnu/ ${EMUPERF} \
      gamescope -W "$W" -H "$H" -r "$REFRESH_HZ" --xwayland-count 2 ${mangoapp} --backend "${gamescope_backend}" --force-orientation "${force_orientation}" ${rotate_clamp} -e -- \
      /bin/bash -c '
        exit_file="$1"
        shift
        "$@"
        printf "%s\n" "$?" >"${exit_file}"
      ' _ "${steam_exit_code_file}" \
      /storage/.local/share/Steam/steamrtarm64/steam -deckard -steamos3 -gamepadui -noshaders ${game_uri:+"$game_uri"}
      gamescope_exit_code=$?
      if [ -f "${steam_exit_code_file}" ]; then
        steam_exit_code=$(cat "${steam_exit_code_file}")
      else
        steam_exit_code=${gamescope_exit_code}
      fi
      [ "${steam_exit_code}" = "42" ] || break
    done
    rm -f "${steam_exit_code_file}"
    steam_touch_calibration_end
    trap - EXIT
    # THE SECOND DELETION: upstream calls `systemctl start essway` here to
    # recover ES after the DRM backend's `stop sway` above. Nothing stopped
    # sway or ES in this file, so there is nothing to recover - calling it
    # anyway would be harmless (essway.service is already active, and
    # `systemctl start` on an already-active unit is a no-op), but is left
    # out to keep this diff an honest record of what actually changed.
    exit 0
  else
    # STEAM_FLAVOR is hardcoded "arm64" above, so this branch is dead code in
    # THIS file - kept only so a future copy of this pattern for
    # start_steam_x86.sh (out of scope here, see steam/INSTALL.md) has
    # something to start from without re-deriving it from scratch.
    FEX /usr/bin/steam -exitsteam
    steam_touch_calibration_begin "${force_orientation}"
    trap steam_touch_calibration_end EXIT
    GAMESCOPE_MODE_SAVE_FILE="${gamescope_mode_file}" GAMESCOPE_FAKE_OUTPUT_MM=508x286 ${EMUPERF} \
      gamescope -W "$W" -H "$H" -r "$REFRESH_HZ" --xwayland-count 2 --backend "${gamescope_backend}" --force-orientation "${force_orientation}" ${rotate_clamp} -- \
      FEX /usr/bin/steam -nobigpicture -noverifyfiles -nobootstrapupdate -skipinitialbootstrap -norepairfiles -noshaders ${game_uri:+"$game_uri"}
    steam_touch_calibration_end
    trap - EXIT
    exit 0
  fi
}

steam_ensure_fex_config_template
steam_prepare_storage_and_vdf
steam_load_es_thunk_settings "$@"
steam_apply_lsfg_settings
steam_apply_fps_limit
steam_set_cpu_affinity
steam_debug_print

steam_arm64_binfmt_and_proton_prep
steam_read_sway_geometry
steam_setup_environment
steam_scope_reexec_if_needed "$@"
steam_dual_screen_begin
steam_launch_bigpicture "$@"
steam_dual_screen_end
systemctl restart systemd-binfmt
