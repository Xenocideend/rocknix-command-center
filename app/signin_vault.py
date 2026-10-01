"""signin_vault: keeps the Browser/Discord/YouTube sign-ins encrypted on disk.

Sign-in features keep working, but nothing is stored in the app or in plain text. Firefox never
saves a password (policies.json/user.js), so what keeps you signed in is each site's session
data, cookies (YouTube, Google) and site storage (Discord keeps its login token in
localStorage), plus history and open tabs. Those files are the secret, and this keeps them only
as an encrypted vault file:

  prepare(profile)  vault -> a RAM folder (/run, tmpfs) Firefox starts on, with the big
                    non-personal cache folders linked back to the disk profile
  checkpoint()      seals again while Firefox runs (every CHECKPOINT_S), so a crash or a flat
                    battery loses minutes, not the sign-in
  release(profile)  seals once more after Firefox quits, then removes the RAM folder

First use moves an existing plain profile in: sealed, the vault opened again and compared file
by file, and only then are the plain files overwritten and removed.

Encryption is AES-256-CBC by openssl (PBKDF2), then HMAC-SHA256 over the ciphertext
(encrypt-then-MAC, so a changed or cut off vault gets refused, never half opened). The key is 32
random bytes in a root-only file outside every network share, mixed with this device's
machine-id. That protects every copy of the sign-ins (the SD card, a backup, a network share, a
file pulled off the device) but not the running device, since whoever is root on it can read
what Firefox has open, like any browser.
"""
import hashlib
import hmac
import io
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import time

log = logging.getLogger("rp5deck.signin_vault")

KEY_PATH = "/storage/rp5deck/.signin-vault-key"      # /storage/rp5deck: not shared
RUN_ROOT = "/run/rp5deck/signins"                     # tmpfs: gone at power-off
MACHINE_ID = "/etc/machine-id"
MAGIC = b"RP5SIGNIN1\n"
CHECKPOINT_S = 300
# regenerable, non-personal folders: stay on disk, linked into the RAM profile
CACHE_DIRS = ("cache2", "startupCache", "security_state", "remote-settings", "safebrowsing",
              "thumbnails", "crashes", "minidumps", "datareporting", "saved-telemetry-pings",
              "shader-cache")
CACHE_PREFIXES = ("gmp",)                               # downloaded media plugins
_PASS_ENV = "RP5DECK_VAULT_PASS"


def is_cache(name):
    return name in CACHE_DIRS or name.startswith(CACHE_PREFIXES)


def available():
    """On the device (POSIX with openssl), unless RP5DECK_SIGNIN_VAULT=0."""
    return (os.name == "posix" and os.environ.get("RP5DECK_SIGNIN_VAULT", "1") != "0"
            and shutil.which("openssl") is not None)


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, key_path=KEY_PATH, run_root=RUN_ROOT, machine_id_path=MACHINE_ID,
                 openssl="openssl", checkpoint_s=CHECKPOINT_S, link=None):
        self.key_path = key_path
        self.run_root = run_root
        self.machine_id_path = machine_id_path
        self.openssl = openssl
        self.checkpoint_s = checkpoint_s
        self.link = link or os.symlink
        self._threads = {}
        self._lock = threading.Lock()
        # serialises seal(): the checkpoint thread and release() can both seal the same profile at
        # once, and they used to share one fixed .tmp path - one os.replace would consume it and the
        # other got ENOENT ("profile.signins.tmp -> profile.signins").
        self._seal_lock = threading.Lock()

    # -- keys ---------------------------------------------------------------------
    def _key(self):
        try:
            with open(self.key_path, "rb") as f:
                key = f.read()
            if len(key) == 32:
                return key
        except OSError:
            pass
        os.makedirs(os.path.dirname(self.key_path), exist_ok=True)
        key = os.urandom(32)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def _derived(self):
        try:
            with open(self.machine_id_path, "rb") as f:
                mid = f.read().strip()
        except OSError:
            mid = b""
        key = self._key()
        enc = hmac.new(key, b"encrypt|" + mid, hashlib.sha256).hexdigest()
        mac = hmac.new(key, b"authenticate|" + mid, hashlib.sha256).digest()
        return enc, mac

    def _openssl(self, args, data, passphrase):
        env = dict(os.environ)
        env[_PASS_ENV] = passphrase
        r = subprocess.run([self.openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "20000",
                            "-pass", "env:" + _PASS_ENV] + args,
                           input=data, capture_output=True, env=env, timeout=120)
        if r.returncode != 0:
            raise VaultError("openssl failed (%d)" % r.returncode)
        return r.stdout

    # -- sealing --------------------------------------------------------------------
    def encrypt(self, plain):
        enc, mac = self._derived()
        body = self._openssl(["-salt"], plain, enc)
        return MAGIC + hmac.new(mac, body, hashlib.sha256).digest() + body

    def decrypt(self, sealed):
        enc, mac = self._derived()
        if not sealed.startswith(MAGIC) or len(sealed) < len(MAGIC) + 32:
            raise VaultError("not a sign-in vault")
        tag, body = sealed[len(MAGIC):len(MAGIC) + 32], sealed[len(MAGIC) + 32:]
        if not hmac.compare_digest(tag, hmac.new(mac, body, hashlib.sha256).digest()):
            raise VaultError("the vault was changed or damaged; not opened")
        return self._openssl(["-d"], body, enc)

    @staticmethod
    def _tar(src):
        """Every private file under src: symlinks (the cache links) and caches skipped."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in sorted(os.listdir(src)):
                p = os.path.join(src, name)
                if is_cache(name) or os.path.islink(p):
                    continue
                tar.add(p, arcname=name, filter=lambda ti: None if ti.issym() else ti)
        return buf.getvalue()

    @staticmethod
    def _untar(data, dest, only=None):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            for m in tar.getmembers():
                parts = m.name.replace("\\", "/").split("/")
                if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                    continue                            # nothing outside dest, no links
                if only is not None and m.name not in only:
                    continue
                target = os.path.join(dest, *parts)
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(m) as f, open(target, "wb") as out:
                    shutil.copyfileobj(f, out)
                os.chmod(target, 0o600)

    def vault_path(self, profile):
        return profile.rstrip("/\\") + ".signins"

    def run_dir(self, profile):
        return os.path.join(self.run_root, os.path.basename(profile.rstrip("/\\")))

    def seal(self, src, profile):
        with self._seal_lock:
            data = self.encrypt(self._tar(src))
            path = self.vault_path(profile)
            # a temp name per process, so a seal in another process (or a crash mid-seal) never
            # shares this one's .tmp
            tmp = "%s.%d.tmp" % (path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return path

    def open_into(self, profile, dest, only=None):
        with open(self.vault_path(profile), "rb") as f:
            self._untar(self.decrypt(f.read()), dest, only)

    # -- the RAM profile ------------------------------------------------------------------
    def prepare(self, profile):
        """The folder Firefox runs on. A leftover RAM folder (the app restarted while
        Firefox was up) is re-sealed and reused as it is."""
        run = self.run_dir(profile)
        if os.path.isdir(run):
            self.seal(run, profile)
        else:
            os.makedirs(run, mode=0o700)
            if os.path.exists(self.vault_path(profile)):
                self.open_into(profile, run)
            elif os.path.isdir(profile):
                self._migrate(profile, run)
        os.chmod(run, 0o700)
        os.makedirs(profile, exist_ok=True)
        for name in CACHE_DIRS:
            disk, link = os.path.join(profile, name), os.path.join(run, name)
            os.makedirs(disk, exist_ok=True)
            if not os.path.lexists(link):
                self.link(disk, link)
        for name in os.listdir(profile):
            link = os.path.join(run, name)
            if name.startswith(CACHE_PREFIXES) and not os.path.lexists(link):
                self.link(os.path.join(profile, name), link)
        self._start_checkpoints(profile, run)
        return run

    def _migrate(self, profile, run):
        """A plain profile from before the vault: into RAM, sealed, checked, wiped."""
        for name in os.listdir(profile):
            if is_cache(name):
                continue
            src, dst = os.path.join(profile, name), os.path.join(run, name)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=False)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        self.seal(run, profile)
        check = run + ".check"
        shutil.rmtree(check, True)
        os.makedirs(check, mode=0o700)
        try:
            self.open_into(profile, check)
            if _listing(check) != _listing(run, skip_links=True):
                raise VaultError("the sealed copy does not match; the plain profile is kept")
        finally:
            shutil.rmtree(check, True)
        for name in os.listdir(profile):
            if not is_cache(name):
                wipe(os.path.join(profile, name))
        log.info("sign-ins moved into %s; plain copies removed", self.vault_path(profile))

    def release(self, profile):
        """After Firefox quit: seal, then remove the RAM folder."""
        self._stop_checkpoints(profile)
        run = self.run_dir(profile)
        if not os.path.isdir(run):
            return False
        self.seal(run, profile)
        shutil.rmtree(run, True)
        return True

    # -- checkpoints ---------------------------------------------------------------------
    def _start_checkpoints(self, profile, run):
        if not self.checkpoint_s:
            return
        with self._lock:
            if profile in self._threads:
                return
            stop = threading.Event()

            def loop():
                while not stop.wait(self.checkpoint_s):
                    try:
                        if os.path.isdir(run):
                            self.seal(run, profile)
                    except Exception as e:          # noqa: BLE001 - next one retries
                        log.warning("sign-in checkpoint failed: %s", e)
            t = threading.Thread(target=loop, name="signin-checkpoint", daemon=True)
            self._threads[profile] = stop
            t.start()

    def _stop_checkpoints(self, profile):
        with self._lock:
            stop = self._threads.pop(profile, None)
        if stop is not None:
            stop.set()


def _listing(root, skip_links=False):
    out = {}
    for dp, dirs, files in os.walk(root):
        if skip_links:
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dp, d))]
        for f in files:
            p = os.path.join(dp, f)
            if skip_links and os.path.islink(p):
                continue
            out[os.path.relpath(p, root).replace("\\", "/")] = os.path.getsize(p)
    return out


def wipe(path):
    """Overwrite a file (or every file in a folder) with zeros, then remove it."""
    if os.path.islink(path):
        os.unlink(path)
        return
    if os.path.isdir(path):
        for dp, _dirs, files in os.walk(path):
            for f in files:
                wipe(os.path.join(dp, f))
        shutil.rmtree(path, True)
        return
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            while n > 0:
                k = min(n, 1 << 20)
                f.write(b"\0" * k)
                n -= k
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


_DEFAULT = None


def default():
    """The shared Vault on the device, or None where it is not available (tests, PCs)."""
    global _DEFAULT
    if _DEFAULT is None and available():
        _DEFAULT = Vault()
    return _DEFAULT
