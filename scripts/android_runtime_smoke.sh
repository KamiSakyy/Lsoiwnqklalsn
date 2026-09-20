#!/usr/bin/env bash
set -euo pipefail

ROOT="${GITHUB_WORKSPACE:-$PWD}"
cd "$ROOT"
SMOKE_LOG="$ROOT/runtime-smoke.log"
: > "$SMOKE_LOG"

log() {
  printf '%s\n' "$*" | tee -a "$SMOKE_LOG"
}

failure_dump() {
  rc=$?
  printf 'SMOKE_SCRIPT_FAILURE rc=%s pwd=%s\n' "$rc" "$(pwd)" >> "$SMOKE_LOG"
  adb logcat -d -v brief 2>/dev/null | tail -n 600 >> "$SMOKE_LOG" || true
  exit "$rc"
}
trap failure_dump ERR

log "smoke_pwd=$(pwd) smoke_root=$ROOT smoke_log=$SMOKE_LOG"
APK="yoru-android/app/build/outputs/apk/tsuyu/release/app-tsuyu-release.apk"
test -s "$APK"
if ! wait_output="$(adb wait-for-device 2>&1)"; then
  log "ADB_WAIT_FAILURE $wait_output"
  exit 1
fi
if ! install_output="$(adb install -r "$APK" 2>&1)"; then
  log "ADB_INSTALL_FAILURE $install_output"
  exit 1
fi
log "ADB_INSTALL_OK $install_output"

cat > /tmp/tsuyu-smoke.py <<'PY'
import json

titles = [
    ("slime4", 59970),
    ("onepiece", 21),
    ("solo2", 58567),
    ("frieren", 52991),
]
for key, mal in titles:
    # Resolve by stable MAL/Shikimori id; short fields keep adb's shell argv safe.
    value = {
        "id": "shikimori:%d" % mal,
        "provider": "shikimori",
        "nativeId": str(mal),
        "title": key,
        "english": key,
        "mId": mal,
        "episodesTotal": 24,
        "episodesAired": 1,
    }
    print(key + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":")))
PY

while IFS='=' read -r key json; do
  log "=== runtime $key ==="
  adb shell am force-stop com.tsuyu.line
  adb logcat -c
  if ! start_output="$(adb shell am start -n com.tsuyu.line/.PlayerActivity --es anime "$json" --ed episode 1 2>&1)"; then
    log "$start_output"
    log "AM_START_FAILURE_$key"
    adb logcat -d -v brief | tail -n 600 | tee -a "$SMOKE_LOG" >&2 || true
    exit 1
  fi
  printf '%s\n' "$start_output" | tee -a "$SMOKE_LOG"

  pass=0
  for n in $(seq 1 90); do
    sleep 1
    runtime_log="$(adb logcat -d -s TsuyuRuntime:I '*:S' 2>/dev/null || true)"
    printf '%s\n' "$runtime_log" >> "$SMOKE_LOG"
    if echo "$runtime_log" | grep -Eq 'PLAYBACK_READY .*episodes=[1-9][0-9]* .*voices_ep1=[1-9][0-9]*'; then
      if echo "$runtime_log" | grep -q 'MEDIA_READY'; then
        pass=1
        break
      fi
    fi
    if adb shell dumpsys activity activities 2>/dev/null | grep -q 'com.tsuyu.line/.PlayerActivity'; then
      :
    else
      log "Activity exited before runtime-ready for $key"
    fi
  done

  if [ "$pass" -ne 1 ]; then
    log "RUNTIME_FAILURE_$key"
    adb logcat -d -v brief | tail -n 600 | tee -a "$SMOKE_LOG" >&2 || true
    exit 1
  fi
  log "RUNTIME_PASS_$key"
done < <(python3 /tmp/tsuyu-smoke.py)

log "RUNTIME_ALL_PASS"
