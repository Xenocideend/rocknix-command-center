"""26 Sep: every Command Center update wiped the owner's Notes - td3-install.sh replaced the
app folder and carried over only config.json. It now carries over every file the device
created (not in the old build's MANIFEST.md5) unless the new build ships that file. The
real carry-over block is cut out of td3-install.sh and run on two folders."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.join(HERE, "..", "testday", "td3-install.sh")


def carry_block():
    with open(INSTALL, encoding="utf-8") as f:
        s = f.read()
    m = re.search(r"<<'CARRY'\n(.*?)\nCARRY\n", s, re.S)
    return m.group(1)


class InstallKeepsUserData(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="td3-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old = os.path.join(self.d, "old")
        self.new = os.path.join(self.d, "new")
        for rel, text in (("main.py", "old code"), ("notes/notebook.json", '{"pages": [1]}'),
                          ("log/rp5deck.log", "log"), ("config.json", "{}"), ("env", "OLD=1"),
                          ("env.bak-1", "x"), ("__pycache__/main.cpython-312.pyc", "pyc"),
                          ("docs/shipped.md", "old doc")):
            self.write(self.old, rel, text)
        with open(os.path.join(self.old, "MANIFEST.md5"), "w") as f:
            f.write("aaaa  ./main.py\nbbbb  ./docs/shipped.md\n")
        self.write(self.new, "main.py", "new code")
        self.write(self.new, "env", "NEW=1")
        self.write(self.new, "config.json", "{new}")

    def write(self, root, rel, text):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.new, *rel.split("/"))) as f:
            return f.read()

    def run_carry(self):
        r = subprocess.run([sys.executable, "-", self.old, self.new], input=carry_block(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_notes_and_logs_are_carried_over(self):
        out = self.run_carry()
        self.assertEqual(self.read("notes/notebook.json"), '{"pages": [1]}')
        self.assertEqual(self.read("log/rp5deck.log"), "log")
        self.assertIn("carried over 2 file(s)", out)

    def test_new_build_files_win_and_env_is_not_carried(self):
        self.run_carry()
        self.assertEqual(self.read("main.py"), "new code")
        self.assertEqual(self.read("env"), "NEW=1")
        self.assertEqual(self.read("config.json"), "{new}")          # config.json copied earlier
        self.assertFalse(os.path.exists(os.path.join(self.new, "env.bak-1")))
        self.assertFalse(os.path.exists(os.path.join(self.new, "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.new, "docs", "shipped.md")))  # built, not user data


if __name__ == "__main__":
    unittest.main()
