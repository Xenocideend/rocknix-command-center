"""CC1 break proofs: each mutation of a safety rule in cleanstate.py /
es_health.py / cleanstate_view.py must turn its tests RED, and the restored
file must turn them GREEN again. Sources are hashed before and after (they
must come back byte-identical). Always -B: a stale .pyc can fake a result.
Run from rp5deck/:  python -B tools/cc1_break_tests.py
LINUX_ONLY breaks need /proc (WSL / the device); elsewhere they are SKIPPED,
never counted as proven."""
import hashlib
import subprocess
import sys

T_CS = "tests.test_cleanstate."
T_EH = "tests.test_es_health."
T_CV = "tests.test_cleanstate_view."
LINUX = sys.platform.startswith("linux")

BREAKS = [
    # --- cleanstate: allow-lists -------------------------------------------
    ("cleanstate.py", "any action name accepted",
     "    if name not in ACTIONS:\n        raise Refused(\"cleanstate refuses action",
     "    if False:\n        raise Refused(\"cleanstate refuses action",
     [T_CS + "TestGuards.test_action_allow_list",
      T_CS + "TestConfirmAndDryRun.test_unknown_action_refuses_the_whole_call"]),
    ("cleanstate.py", "any command accepted",
     "    if t not in MUTATING_COMMANDS and t not in READ_COMMANDS:",
     "    if False:",
     [T_CS + "TestGuards.test_command_allow_list_is_exact"]),
    ("cleanstate.py", "the never-sway belt removed",
     "    if t and t[0] == \"systemctl\" and any(a in (\"sway\", \"sway.service\") for a in lowered):",
     "    if False:",
     [T_CS + "TestGuards.test_sway_is_refused_even_if_someone_adds_it_to_the_list"]),
    ("cleanstate.py", "restart command pointed at sway",
     "CMD_RESTART_ES = (\"systemctl\", \"restart\", \"essway.service\")",
     "CMD_RESTART_ES = (\"systemctl\", \"restart\", \"sway.service\")",
     [T_CS + "TestRestart.test_frozen_restarts_essway_only"]),
    ("cleanstate.py", "kill-name guard returns at once (ES / python3 killable)",
     "    if not _NAME_RE.match(name or \"\"):\n        raise Refused(\"kill target",
     "    return name\n    if not _NAME_RE.match(name or \"\"):\n        raise Refused(\"kill target",
     [T_CS + "TestKillEmulator.test_es_named_by_the_kill_file_during_a_game_is_never_hit",
      T_CS + "TestKillEmulator.test_python3_target_is_refused",
      T_CS + "TestGuards.test_kill_names"]),
    ("cleanstate.py", "unknown names killable (KILLABLE check off)",
     "    if name not in KILLABLE_NAMES:",
     "    if False:",
     [T_CS + "TestKillEmulator.test_unknown_target_is_refused"]),
    ("cleanstate.py", "pid re-check before the signal removed",
     "        if self.probe.comm(pid) != name[:15]:",
     "        if False:",
     [T_CS + "TestKillEmulator.test_a_reused_pid_is_not_hit"]),
    ("cleanstate.py", "port subtree scan keeps protected names (ra_proxy)",
     "                if comm in _SHELLS or comm in PROTECTED_NAMES:",
     "                if comm in _SHELLS:",
     [T_CS + "TestKillEmulator.test_port_processes_lists_only_the_ports_programs"]),
    ("cleanstate.py", "port signal gate lets protected names through",
     "        if not _NAME_RE.match(comm or \"\") or comm in PROTECTED_NAMES or comm in _SHELLS:",
     "        if not _NAME_RE.match(comm or \"\") or comm in _SHELLS:",
     [T_CS + "TestKillEmulator.test_port_signal_gate_refuses_protected_names"]),
    # --- cleanstate: confirmation, dry run ---------------------------------
    ("cleanstate.py", "execute without confirmation",
     "        if not confirmed and not self.dry_run:\n            raise Refused(",
     "        if False:\n            raise Refused(",
     [T_CS + "TestConfirmAndDryRun.test_execute_needs_confirmation"]),
    ("cleanstate.py", "dry run signals for real",
     "        if self.dry_run:\n            self.audit(action, \"would signal\"",
     "        if False:\n            self.audit(action, \"would signal\"",
     [T_CS + "TestConfirmAndDryRun.test_dry_run_touches_nothing_and_logs_everything"]),
    ("cleanstate.py", "dry run runs mutating commands",
     "        if mutating and self.dry_run:",
     "        if False:",
     [T_CS + "TestConfirmAndDryRun.test_dry_run_touches_nothing_and_logs_everything"]),
    ("cleanstate.py", "emukill() accepts any URL",
     "        if url != EMUKILL_URL:",
     "        if False:",
     [T_CS + "TestEmukillGuard.test_only_the_constant_url_and_only_confirmed"]),
    ("cleanstate.py", "emukill() without confirmation",
     "        if not confirmed:\n            raise Refused(\"emukill() needs",
     "        if False:\n            raise Refused(\"emukill() needs",
     [T_CS + "TestEmukillGuard.test_only_the_constant_url_and_only_confirmed"]),
    # --- cleanstate: restart / touch gates ---------------------------------
    ("cleanstate.py", "restart regardless of the verdict",
     "        if not (h.offer_restart or (force and h.allow_force)):",
     "        if False:",
     [T_CS + "TestRestart.test_healthy_es_is_not_restarted",
      T_CS + "TestRestart.test_suspect_needs_force",
      T_CS + "TestRestart.test_force_does_not_override_a_healthy_verdict"]),
    ("cleanstate.py", "restart from inside essway's cgroup",
     "        if \"essway\" in self.own_cgroup():",
     "        if False:",
     [T_CS + "TestRestart.test_refused_from_inside_essway"]),
    ("cleanstate.py", "touch command sent although touch is on",
     "        if st != \"disabled\":",
     "        if False:",
     [T_CS + "TestChildrenAndTouch.test_touch_only_when_disabled"]),
    # --- es_health: the frozen rule ----------------------------------------
    ("es_health.py", "one signal is enough for FROZEN",
     "    if len(signals) >= 2:",
     "    if len(signals) >= 1:",
     [T_EH + "TestJudge.test_http_down_alone_is_never_frozen",
      T_EH + "TestJudge.test_stall_alone_is_suspect_not_frozen"]),
    ("es_health.py", "main-thread progress never measured (always 0)",
     "        ev[\"vol_delta\"] = last[0][0] - first[0][0]",
     "        ev[\"vol_delta\"] = 0",
     [T_EH + "TestCheck.test_healthy"]),
    ("es_health.py", "health judged while a game runs",
     "    if ev.get(\"game\"):\n        return Health(GAME",
     "    if False:\n        return Health(GAME",
     [T_EH + "TestJudge.test_game_running_is_never_judged",
      T_EH + "TestCheck.test_game_running_via_kill_target"]),
    ("es_health.py", "judged with ES's screen off",
     "    if ev.get(\"output_on\") is False:",
     "    if False:",
     [T_EH + "TestJudge.test_screen_off_is_unknown"]),
    ("es_health.py", "an ES event during the check does not veto",
     "    if ev.get(\"events_during\"):",
     "    if False:",
     [T_EH + "TestJudge.test_an_es_event_during_the_check_vetoes"]),
    ("es_health.py", "one failed /caps counts (instead of both)",
     "    ev[\"http_fail\"] = http1[0] != \"ok\" and http2[0] != \"ok\"",
     "    ev[\"http_fail\"] = http1[0] != \"ok\" or http2[0] != \"ok\"",
     [T_EH + "TestCheck.test_one_slow_http_answer_is_not_a_signal"]),
    ("es_health.py", "HTTP allow-list removed",
     "        if path not in ALLOWED_HTTP_PATHS:",
     "        if False:",
     [T_EH + "TestHttpAllowList.test_only_caps_and_running_game"]),
    ("es_health.py", "PowerSaverMode enhanced does not excuse idling",
     "    elif ps in (\"instant\", \"enhanced\"):",
     "    elif False:",
     [T_EH + "TestJudge.test_powersaver_enhanced_makes_idle_legitimate"]),
    ("es_health.py", "no startup grace",
     "    if age is not None and age < STARTUP_GRACE_S:",
     "    if False:",
     [T_EH + "TestJudge.test_young_process_is_starting"]),
    ("es_health.py", "missing window not watched for 30 s",
     "                extend = \"ES's window is missing; watching up to %d s\" % no_window_s",
     "                extend = None",
     [T_EH + "TestCheck.test_window_missing_is_watched_for_30s"]),
    # --- cleanstate_view ----------------------------------------------------
    ("cleanstate_view.py", "Restart anyway without the second confirm",
     "                self.phase = CONFIRM_FORCE\n",
     "                return self._execute(self.plan.actions() + [cleanstate.RESTART_ES],"
     " force=True)\n",
     [T_CV + "TestFlow.test_suspect_needs_a_second_confirm_and_forces"]),
    ("cleanstate_view.py", "live web session not ended before the stop",
     "                web.end_session(\"clean state\")      # the UI-side stop (web_tiles)",
     "                pass",
     [T_CV + "TestFlow.test_clean_up_runs_the_plans_actions_and_ends_the_web_session_first"]),
    # --- real processes (Linux) ---------------------------------------------
    ("es_health.py", "zombies count as running (LINUX_ONLY)",
     "            if st and st[\"state\"] in (\"Z\", \"X\"):",
     "            if False:",
     [T_CS + "TestRealProcess.test_closes_a_real_retroarch_by_name_and_nothing_else"]),
    ("es_health.py", "stall read from the wrong thread's status (LINUX_ONLY)",
     "        t = _read(\"%s/%d/task/%d/status\" % (self.proc, pid, pid))",
     "        t = _read(\"%s/self/task/%d/status\" % (self.proc, os.getpid()))",
     [T_EH + "TestRealProcess.test_live_then_sigstop_then_sigcont"]),
]

FILES = sorted({b[0] for b in BREAKS})


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(tests):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest"] + tests,
                       capture_output=True, text=True)
    last = [ln for ln in r.stderr.splitlines() if ln.startswith(("OK", "FAILED", "Ran"))]
    skipped = "skipped" in " ".join(last)
    return r.returncode, " ".join(last), skipped


def main():
    before = {p: sha(p) for p in FILES}
    bad = skipped_n = 0
    for i, (path, what, old, new, tests) in enumerate(BREAKS, 1):
        if "LINUX_ONLY" in what and not LINUX:
            print("%2d SKIPPED     %-58s (needs Linux /proc)" % (i, what[:58]))
            skipped_n += 1
            continue
        with open(path, "rb") as f:
            orig = f.read()
        text = orig.decode("utf-8")
        if text.count(old) != 1:
            print("%2d SETUP-FAIL %s: anchor found %d times" % (i, what, text.count(old)))
            bad += 1
            continue
        try:
            with open(path, "wb") as f:
                f.write(text.replace(old, new).encode("utf-8"))
            rc_b, out_b, _ = run(tests)
        finally:
            with open(path, "wb") as f:
                f.write(orig)
        rc_g, out_g, sk = run(tests)
        ok = rc_b != 0 and rc_g == 0 and not sk
        bad += not ok
        print("%2d %s  %-58s broken: %-24s restored: %s" % (
            i, "PROVEN    " if ok else "NOT-PROVEN", what[:58], out_b[-24:], out_g))
    after = {p: sha(p) for p in FILES}
    same = before == after
    print("sources byte-identical after: %s (%s)" % (same, ", ".join(FILES)))
    print("%d/%d proven, %d skipped" % (len(BREAKS) - bad - skipped_n, len(BREAKS), skipped_n))
    return 0 if (bad == 0 and same) else 1


if __name__ == "__main__":
    sys.exit(main())
