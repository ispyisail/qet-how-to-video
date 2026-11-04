#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Smoke test: the probe self-test, then every story in stories/ in check-only
# mode (no video). Exit 1 if anything fails.
#   tests/smoke.sh <qelectrotech binary> [--qet-dir <checkout>]
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
B=${1:?usage: tests/smoke.sh <qelectrotech binary> [--qet-dir <checkout>]}; shift
OUT=$(mktemp -d "${HOME}/.cache/qet-how-to-video-smoke.XXXXXX")
fail=0
python3 "$HERE/probe.py" --binary "$B" "$@" || fail=1
for s in "$HERE"/stories/*.yaml; do
  python3 "$HERE/story.py" "$s" --binary "$B" --out "$OUT" --check-only "$@" || fail=1
done
rm -rf "$OUT"
if (( fail )); then echo "smoke: FAILED"; else echo "smoke: all passed"; fi
exit $fail
