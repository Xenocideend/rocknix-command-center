#!/usr/bin/env python3
r"""Run commands on the ROCKNIX device over SSH.

Usage:
    python rk.py "command"            run one command, print stdout/stderr/exit
    python rk.py -f cmds.txt          run each non-comment line in turn
    python rk.py --put LOCAL REMOTE   upload a file (LF endings preserved)

Connection details come ONLY from, in order of priority:
  1. Environment variables RK_HOST / RK_USER / RK_PASS / RK_PORT.
  2. rk_local.json next to this file - gitignored, never commit it.

There is no built-in host/user/password default: without one of the two
sources above, this refuses to run rather than guessing.

rk_local.json shape (RK_HOST/USER/PASS/PORT env vars override any field
that is present):
    {"host": "192.0.2.10", "user": "root", "password": "...", "port": 22}

("192.0.2.10" above is a documentation-only example address (RFC 5737) -
put your own device's real LAN IP there, never a placeholder that looks
like a working default.)
"""
import json
import sys
import os

import paramiko


def _load_config():
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rk_local.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    host = os.environ.get("RK_HOST", cfg.get("host"))
    user = os.environ.get("RK_USER", cfg.get("user", "root"))
    pw = os.environ.get("RK_PASS", cfg.get("password"))
    port = int(os.environ.get("RK_PORT", cfg.get("port", 22)))
    if not host or pw is None:
        sys.exit("rk.py: no device credentials. Create %s (see this file's docstring) "
                 "or set RK_HOST and RK_PASS." % path)
    return host, user, pw, port


HOST, USER, PASS, PORT = _load_config()


# Device output contains box-drawing and other non-cp1252 characters (busctl
# trees, ES logs). Windows' default console codec raises on those and kills the
# whole run, so force UTF-8 with replacement rather than losing the output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=20, banner_timeout=30, auth_timeout=30,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, quiet=False):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=180)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if not quiet:
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr] " + err.rstrip())
        if rc != 0:
            print("[exit %d]" % rc)
    return rc, out, err


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    c = connect()
    try:
        if args[0] in ("--put", "--put-text"):
            local, remote = args[1], args[2]
            raw = open(local, "rb").read()
            # --put is BINARY EXACT. Only --put-text normalises CRLF -> LF, which
            # matters for shell scripts (a stray CR in a shebang gives "bad
            # interpreter") and CORRUPTS anything else. Normalising a binary file
            # here (e.g. a .tar) silently corrupts it - and hashing the mangled
            # buffer would make verification confirm the corruption, so don't.
            data = raw.replace(b"\r\n", b"\n") if args[0] == "--put-text" else raw

            # Try SFTP first, but some ROCKNIX SSH servers refuse SFTP writes even
            # where the directory exists, so fall back to streaming the bytes
            # through a shell redirect instead.
            try:
                sftp = c.open_sftp()
                with sftp.open(remote, "wb") as fh:
                    fh.write(data)
                size = sftp.stat(remote).st_size
                sftp.close()
                print("uploaded via sftp: %s -> %s (%d bytes)" % (local, remote, size))
            except Exception as e:
                print("sftp unavailable (%s) - streaming over the shell instead" % e)
                import base64
                b64 = base64.b64encode(data).decode("ascii")
                # chunk it: a single enormous command line can exceed limits
                run(c, "rm -f %s.b64 %s" % (remote, remote), quiet=True)
                CH = 32000
                for i in range(0, len(b64), CH):
                    rc, _, err = run(c, "printf '%%s' '%s' >> %s.b64"
                                     % (b64[i:i + CH], remote), quiet=True)
                    if rc != 0:
                        print("chunk write failed: " + err)
                        return 1
                rc, _, err = run(c, "base64 -d %s.b64 > %s && rm -f %s.b64"
                                 % (remote, remote, remote), quiet=True)
                if rc != 0:
                    print("decode failed: " + err)
                    return 1
                print("uploaded via shell: %s -> %s" % (local, remote))

            # verify what landed, byte-count and hash, rather than trusting it
            import hashlib
            want = hashlib.md5(data).hexdigest()
            rc, out, _ = run(c, "md5sum %s 2>/dev/null | awk '{print $1}'; "
                                "wc -c < %s" % (remote, remote), quiet=True)
            parts = out.split()
            got = parts[0] if parts else "-"
            nbytes = parts[1] if len(parts) > 1 else "-"
            print("  local md5 %s (%d bytes)" % (want, len(data)))
            print("  remote md5 %s (%s bytes)" % (got, nbytes))
            print("  VERIFIED" if got == want else "  MISMATCH - transfer is not trustworthy")
            return 0 if got == want else 1

        if args[0] == "--get":
            remote, local = args[1], args[2]
            sftp = c.open_sftp()
            data = sftp.open(remote, "rb").read()
            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            with open(local, "wb") as fh:
                fh.write(data)
            sftp.close()
            print("downloaded %s -> %s (%d bytes)" % (remote, local, len(data)))
            return 0

        if args[0] == "-f":
            for line in open(args[1], encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                print("$ " + line)
                run(c, line)
                print()
            return 0

        rc, _, _ = run(c, " ".join(args))
        return rc
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
