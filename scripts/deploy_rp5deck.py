#!/usr/bin/env python3
r"""Stage the Command Center (../app) on the device as a verified build, then
optionally install it.

    python deploy_rp5deck.py BUILD_NAME            upload + verify only
    python deploy_rp5deck.py BUILD_NAME --install  ... then td3-install.sh install
                                                    and guard-live (the focus
                                                    guard's normal mode)

BUILD_NAME is the device folder: /storage/rp5deck-<BUILD_NAME>/rp5deck (e.g.
"test1"). It must not exist yet, so an earlier build is never overwritten.

The file set is every file this repo's ../app/ directory tracks in git (or
would track - not gitignored), minus screenshots/ and proto/, plus a
MANIFEST.md5 that testday/td1-verify-build.sh checks byte for byte.
testday/td3-install.sh backs up the previously-installed app and its
autostart entry before replacing them, and carries over config.json; its
rollback command is printed at the end.

Requires: this repo initialised as a git repo (app_files() below runs `git
ls-files` against ../app), and rk.py's own connection details set up - see
rk.py's docstring (env vars, or a local rk_local.json next to it - never
commit real device credentials).
"""
import hashlib
import io
import os
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, HERE)
import rk  # noqa: E402

SKIP_TOP = ("screenshots/", "proto/")


def app_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "."],
                         cwd=APP, capture_output=True, check=True).stdout.decode("utf-8")
    files = sorted(f for f in out.split("\0") if f and not f.startswith(SKIP_TOP)
                   and "__pycache__" not in f and os.path.isfile(os.path.join(APP, f)))
    return files


def sh(c, cmd, timeout=600):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return o.channel.recv_exit_status(), out + err


def main():
    if len(sys.argv) < 2 or not sys.argv[1].replace("-", "").isalnum():
        sys.exit(__doc__)
    name, install = sys.argv[1], "--install" in sys.argv
    base = "/storage/rp5deck-%s" % name
    build = base + "/rp5deck"
    files = app_files()
    manifest = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            p = os.path.join(APP, f)
            with open(p, "rb") as fh:
                data = fh.read()
            manifest.append("%s  ./%s\n" % (hashlib.md5(data).hexdigest(), f))
            ti = tarfile.TarInfo("rp5deck/" + f)
            ti.size = len(data)
            ti.mode = 0o755 if (f.endswith(".sh") or f == "094-rp5deck") else 0o644
            tar.addfile(ti, io.BytesIO(data))
        m = "".join(manifest).encode()
        ti = tarfile.TarInfo("rp5deck/MANIFEST.md5")
        ti.size = len(m)
        tar.addfile(ti, io.BytesIO(m))
    print("staged %d files (%d KiB compressed)" % (len(files), len(buf.getvalue()) // 1024))

    c = rk.connect()
    try:
        rc, out = sh(c, "[ -e %s ] && echo EXISTS; true" % base)
        if "EXISTS" in out:
            sys.exit("%s already exists - pick a new BUILD_NAME" % base)
        tmp = "/tmp/rp5deck-%s.tgz" % name
        sftp = c.open_sftp()
        sftp.putfo(io.BytesIO(buf.getvalue()), tmp)
        sftp.close()
        rc, out = sh(c, "mkdir -p %s && tar -xzf %s -C %s && rm -f %s" % (base, tmp, base, tmp))
        if rc:
            sys.exit("extract failed: " + out)
        rc, out = sh(c, "sh %s/testday/td1-verify-build.sh" % build)
        print(out.strip())
        if rc or "RESULT: build verified" not in out:
            sys.exit("verify FAILED - not installing")
        if not install:
            print("verified; re-run with --install to install it")
            return 0
        rc, out = sh(c, "cd %s && sh testday/td3-install.sh install" % build)
        print(out.strip())
        if rc:
            sys.exit("install FAILED (rc %d)" % rc)
        rc, out = sh(c, "sh /storage/rp5deck/testday/td3-install.sh guard-live")
        print(out.strip())
        return rc
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
