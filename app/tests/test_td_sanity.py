"""testday/td-common.sh td_sanity, the install's safety check, run verbatim under dash: the RP5 keeps its add-on check, a
built-in dual-screen handheld the app's profiles name is let through (it says it is untested), any other model is refused
unless TD_ALLOW_ANY_DEVICE=1."""
import os
import re
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_sw1_092 as sw1  # noqa: E402

APP = os.path.dirname(HERE)
COMMON = os.path.join(APP, "testday", "td-common.sh")


def function_text():
    with open(COMMON, encoding="utf-8") as f:
        s = f.read()
    return re.search(r"(td_sanity\(\) \{\n.*?\n\}\n)", s, re.S).group(1)


@unittest.skipUnless(sw1.DASH, "dash needed")
class TestSanity(sw1.TmpDir):
    def setUp(self):
        t = self.mk()
        self.build = os.path.join(t, "build")
        os.makedirs(self.build)
        for f in ("screen_map.py", "device.py"):
            shutil.copy(os.path.join(APP, f), self.build)
        self.model = os.path.join(t, "model")

    def run_script(self, model, extra_env=""):
        with open(self.model, "wb") as f:
            f.write(model.encode("utf-8") + b"\0")
        path = os.path.join(os.path.dirname(self.model), "run.sh")
        with open(path, "w", newline="\n") as f:
            f.write("export TD_MODEL_FILE=%s RP5DECK_DEVICE=%s %s\nBUILD=%s\n%s\ntd_sanity\necho reached-the-end\n"
                    % (sw1.sh_quote(sw1.to_sh_path(self.model)), sw1.sh_quote(model), extra_env,
                       sw1.sh_quote(sw1.to_sh_path(self.build)), function_text()))
        r = sw1.run_dash("sh %s; echo rc=$?" % sw1.sh_quote(sw1.to_sh_path(path)))
        return r.stdout

    def test_a_built_in_dual_screen_handheld_is_let_through_and_told_it_is_untested(self):
        for model in ("AYN Thor", "AYN Thor Lite", "AYANEO Pocket DS", "Anbernic RG DS", "Anbernic RG DS Plus"):
            out = self.run_script(model)
            self.assertIn("reached-the-end", out, model)
            self.assertIn("has not been tried on this model", out, model)
            self.assertIn("rc=0", out, model)

    def test_an_unknown_model_is_refused(self):
        out = self.run_script("Some Other Handheld")
        self.assertIn("HARD STOP", out)
        self.assertIn("rc=90", out)
        self.assertNotIn("reached-the-end", out)

    def test_the_pocket_duo_lite_is_refused_like_any_unknown_model(self):
        self.assertIn("rc=90", self.run_script("Retroid Pocket Duo Lite"))

    def test_an_unknown_model_goes_on_when_the_installer_says_so(self):
        out = self.run_script("Some Other Handheld", "TD_ALLOW_ANY_DEVICE=1")
        self.assertIn("reached-the-end", out)
        self.assertIn("TD_ALLOW_ANY_DEVICE=1", out)
        self.assertIn("rc=0", out)

    def test_the_rp5_keeps_its_add_on_check(self):
        out = self.run_script("Retroid Pocket 5")
        self.assertIn("rc=91", out)                         # no add-on lit here, as before
        self.assertIn("Attach the Dual Screen add-on", out)

    def test_the_rp5_is_not_let_through_by_the_override(self):
        self.assertIn("rc=91", self.run_script("Retroid Pocket 5", "TD_ALLOW_ANY_DEVICE=1"))

    def test_a_missing_model_file_counts_as_unknown(self):
        path = os.path.join(os.path.dirname(self.model), "run2.sh")
        with open(path, "w", newline="\n") as f:
            f.write("export TD_MODEL_FILE=/no/such/file RP5DECK_DEVICE=\nBUILD=%s\n%s\ntd_sanity\necho reached-the-end\n"
                    % (sw1.sh_quote(sw1.to_sh_path(self.build)), function_text()))
        r = sw1.run_dash("sh %s; echo rc=$?" % sw1.sh_quote(sw1.to_sh_path(path)))
        self.assertIn("rc=90", r.stdout)

    def test_a_build_without_the_profile_module_is_refused(self):
        os.remove(os.path.join(self.build, "screen_map.py"))
        self.assertIn("rc=90", self.run_script("AYN Thor"))


if __name__ == "__main__":
    unittest.main()
