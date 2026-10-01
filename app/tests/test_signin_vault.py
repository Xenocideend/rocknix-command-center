"""Owner, 26 Sep: sign-ins keep working, but nothing that keeps you signed in is stored
in plain text. signin_vault seals the Firefox profile's private files (cookies, site
storage, sessions) with openssl + HMAC; Firefox runs on a RAM copy."""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import browser  # noqa: E402
import signin_vault as sv  # noqa: E402

SECRET = b"SAPISID=do-not-leak-this-cookie-value"
TOKEN = b'{"token":"discord-login-token-value"}'


def fake_link(target, link):
    os.makedirs(link, exist_ok=True)        # no symlink rights on Windows; a folder stands in


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class VaultCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vault-")
        self.addCleanup(shutil.rmtree, self.d, True)
        mid = os.path.join(self.d, "machine-id")
        with open(mid, "w") as f:
            f.write("0123456789abcdef\n")
        self.v = sv.Vault(key_path=os.path.join(self.d, "key"), run_root=os.path.join(self.d, "run"),
                          machine_id_path=mid, checkpoint_s=0, link=fake_link)
        self.profile = os.path.join(self.d, "profile")
        os.makedirs(os.path.join(self.profile, "storage", "default", "https+++discord.com", "ls"))
        os.makedirs(os.path.join(self.profile, "cache2"))
        self.write("cookies.sqlite", SECRET)
        self.write("storage/default/https+++discord.com/ls/data.sqlite", TOKEN)
        self.write("cache2/entry", b"cached page")

    def write(self, rel, data, root=None):
        p = os.path.join(root or self.profile, *rel.split("/"))
        with open(p, "wb") as f:
            f.write(data)

    def read(self, root, rel):
        with open(os.path.join(root, *rel.split("/")), "rb") as f:
            return f.read()

    def everything_on_disk(self):
        blob = b""
        for dp, _d, files in os.walk(self.d):
            if os.path.join(self.d, "run") in dp:
                continue                            # the RAM folder is tmpfs on the device
            for f in files:
                with open(os.path.join(dp, f), "rb") as fh:
                    blob += fh.read()
        return blob


class Sealing(VaultCase):
    def test_round_trip(self):
        sealed = self.v.encrypt(SECRET)
        self.assertNotIn(SECRET, sealed)
        self.assertEqual(self.v.decrypt(sealed), SECRET)

    def test_a_changed_vault_is_refused(self):
        # a flip in the middle keeps the padding valid: only the MAC can notice it
        sealed = bytearray(self.v.encrypt(SECRET * 40))
        sealed[len(sv.MAGIC) + 32 + 16 + 200] ^= 1
        with self.assertRaises(sv.VaultError) as e:
            self.v.decrypt(bytes(sealed))
        self.assertIn("changed or damaged", str(e.exception))

    def test_another_device_cannot_open_it(self):
        sealed = self.v.encrypt(SECRET)
        enc_here, mac_here = self.v._derived()
        with open(self.v.machine_id_path, "w") as f:
            f.write("another-device\n")
        enc_there, mac_there = self.v._derived()
        self.assertNotEqual(enc_here, enc_there)           # both keys depend on the device
        self.assertNotEqual(mac_here, mac_there)
        with self.assertRaises(sv.VaultError):
            self.v.decrypt(sealed)

    def test_the_key_file_is_owner_only(self):
        self.v.encrypt(b"x")
        if os.name == "posix":
            self.assertEqual(os.stat(self.v.key_path).st_mode & 0o777, 0o600)


class Profile(VaultCase):
    def test_first_use_moves_the_plain_profile_in_and_wipes_it(self):
        run = self.v.prepare(self.profile)
        self.assertEqual(self.read(run, "cookies.sqlite"), SECRET)          # Firefox sees it
        self.assertFalse(os.path.exists(os.path.join(self.profile, "cookies.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.profile, "storage")))
        self.assertTrue(os.path.exists(os.path.join(self.profile, "cache2", "entry")))  # cache stays
        self.assertTrue(os.path.exists(self.v.vault_path(self.profile)))
        disk = self.everything_on_disk()
        self.assertNotIn(SECRET, disk)
        self.assertNotIn(TOKEN, disk)

    def test_close_seals_changes_and_removes_the_ram_copy(self):
        run = self.v.prepare(self.profile)
        self.write("cookies.sqlite", SECRET + b"-new-sign-in", root=run)
        self.assertTrue(self.v.release(self.profile))
        self.assertFalse(os.path.exists(run))
        run2 = self.v.prepare(self.profile)
        self.assertEqual(self.read(run2, "cookies.sqlite"), SECRET + b"-new-sign-in")
        self.assertNotIn(b"-new-sign-in", self.everything_on_disk())

    def test_a_leftover_ram_copy_is_sealed_and_reused(self):
        run = self.v.prepare(self.profile)
        self.write("cookies.sqlite", b"changed before the app restarted", root=run)
        self.v._stop_checkpoints(self.profile)
        run2 = self.v.prepare(self.profile)                 # app restarted, Firefox gone
        self.assertEqual(run2, run)
        self.v.release(self.profile)
        run3 = self.v.prepare(self.profile)
        self.assertEqual(self.read(run3, "cookies.sqlite"), b"changed before the app restarted")

    def test_migration_keeps_the_plain_profile_when_the_check_fails(self):
        with mock.patch.object(sv, "_listing", side_effect=[{"a": 1}, {"b": 2}]):
            with self.assertRaises(sv.VaultError):
                self.v.prepare(self.profile)
        self.assertEqual(self.read(self.profile, "cookies.sqlite"), SECRET)

    def test_yt_dlp_gets_a_cookie_copy_that_is_removed_after(self):
        self.v.prepare(self.profile)
        self.v.release(self.profile)
        with self.v.cookie_profile(self.profile) as ram:
            self.assertEqual(self.read(ram, "cookies.sqlite"), SECRET)
            self.assertFalse(os.path.exists(os.path.join(ram, "storage")))   # cookies only
        self.assertFalse(os.path.exists(ram))

    def test_yt_dlp_uses_the_live_copy_while_firefox_runs(self):
        run = self.v.prepare(self.profile)
        with self.v.cookie_profile(self.profile) as ram:
            self.assertEqual(ram, run)
        self.assertTrue(os.path.exists(run))


class BrowserUsesTheVault(VaultCase):
    def test_firefox_starts_on_the_ram_copy_and_close_seals_it(self):
        b = browser.Browser(profile_dir=self.profile, vault=self.v)
        launched = []

        class Proc:
            def poll(self):
                return 0
        real_popen = browser.subprocess.Popen

        def popen(cmd, *a, **kw):
            if "--marionette" not in cmd:
                return real_popen(cmd, *a, **kw)       # the vault's own openssl calls
            launched.append(cmd)
            return Proc()
        with mock.patch.object(browser.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(b, "_connect_marionette", return_value=False):
            b.open()
        run = self.v.run_dir(self.profile)
        self.assertEqual(launched[0][launched[0].index("--profile") + 1], run)
        self.assertFalse(os.path.exists(run))              # open failed -> close() sealed it
        self.assertTrue(os.path.exists(self.v.vault_path(self.profile)))
        self.assertNotIn(SECRET, self.everything_on_disk())

    def test_no_vault_keeps_the_old_plain_behaviour(self):
        b = browser.Browser(profile_dir=self.profile, vault=False)
        self.assertEqual(b.build_command()[b.build_command().index("--profile") + 1], self.profile)


class Wipe(unittest.TestCase):
    def test_wipe_overwrites_before_removing(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "f")
        with open(p, "wb") as f:
            f.write(SECRET)
        seen = []
        real_open = open

        def spy(path, mode="r", *a, **kw):
            fh = real_open(path, mode, *a, **kw)
            if mode == "r+b":
                orig = fh.write
                fh.write = lambda data: (seen.append(data), orig(data))[1]
            return fh
        with mock.patch("builtins.open", side_effect=spy):
            sv.wipe(p)
        self.assertFalse(os.path.exists(p))
        self.assertEqual(b"".join(seen), b"\0" * len(SECRET))


class SealRace(unittest.TestCase):
    """The checkpoint thread and release() can seal one profile at once. With one shared .tmp path a
    second os.replace found it gone (ENOENT, seen on the device). openssl is stubbed out so the
    threads reach the write and replace together, as they did there."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vault-race-")
        self.addCleanup(shutil.rmtree, self.d, True)
        mid = os.path.join(self.d, "machine-id")
        with open(mid, "w") as f:
            f.write("0123456789abcdef\n")
        self.v = sv.Vault(key_path=os.path.join(self.d, "key"), run_root=os.path.join(self.d, "run"),
                          machine_id_path=mid, checkpoint_s=0, link=fake_link)
        self.v._openssl = lambda args, data, passphrase: data
        self.profile = os.path.join(self.d, "profile")
        os.makedirs(self.profile)
        with open(os.path.join(self.profile, "cookies.sqlite"), "wb") as f:
            f.write(b"x" * 1000)

    def seal_together(self, n):
        errors = []
        barrier = threading.Barrier(n)

        def worker():
            try:
                barrier.wait()
                self.v.seal(self.profile, self.profile)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_sealing_the_same_profile_from_many_threads_never_fails(self):
        for n in (8, 16):
            self.assertEqual(self.seal_together(n), [], "%d threads" % n)

    def test_no_temp_file_is_left_behind(self):
        self.seal_together(8)
        left = [f for f in os.listdir(os.path.dirname(self.v.vault_path(self.profile))) if f.endswith(".tmp")]
        self.assertEqual(left, [])
        self.assertTrue(os.path.exists(self.v.vault_path(self.profile)))


if __name__ == "__main__":
    unittest.main()
