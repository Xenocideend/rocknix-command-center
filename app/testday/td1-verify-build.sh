#!/bin/sh
# Step 1: verify the deployed build byte for byte against the PC's manifest,
# then that everything parses. Changes nothing outside /tmp.
#   sh /storage/rp5deck-i2/rp5deck/testday/td1-verify-build.sh
. "$(dirname "$0")/td-common.sh"
td_sanity
cd "$BUILD" || exit 1
[ -f MANIFEST.md5 ] || { echo "FAIL: no MANIFEST.md5 in $BUILD"; exit 2; }
n=$(wc -l < MANIFEST.md5)
if md5sum -c MANIFEST.md5 > /tmp/td1-md5.txt 2>&1; then
    echo "md5: all $n files match the PC build"
else
    grep -v ': OK$' /tmp/td1-md5.txt
    echo "FAIL: md5 mismatch - do not use this build"
    exit 3
fi
extra=$(find . -type f ! -name MANIFEST.md5 ! -path '*/__pycache__/*' | wc -l)
[ "$extra" -eq "$n" ] || { echo "FAIL: $extra files on disk, $n in the manifest"; exit 3; }
if python3 -B -m py_compile ./*.py ./tools/*.py ./tests/*.py; then
    echo "py_compile: OK"
else
    echo "FAIL: py_compile"; exit 4
fi
for f in command-center-app tools/install-es-hooks.sh es-hooks/*.sh testday/*.sh; do
    sh -n "$f" || { echo "FAIL: sh -n $f"; exit 5; }
done
echo "sh -n: OK"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
echo "RESULT: build verified ($n files) in $BUILD"
