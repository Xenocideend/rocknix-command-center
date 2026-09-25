#!/bin/sh
# Stands in for `python3` in tests/test_supervisor.py: 094-rp5deck always
# runs its children as `python3 "$APP"`, so this shim just execs the given
# path directly (the "app"/"guard" fixture scripts below are themselves
# plain, executable POSIX shell scripts with their own shebang).
exec "$@"
