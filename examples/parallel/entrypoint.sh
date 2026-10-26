#!/bin/sh
# examples/parallel/entrypoint.sh
# Start a private X display (Xvfb) and a window manager (openbox), run the
# worker on it, and tear the display down when the worker ends or the
# container is stopped. Arguments are passed to worker.py unchanged.
set -u

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1920x1080x24}"
export DISPLAY=":${DISPLAY_NUM}"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"

xvfb_pid=""
wm_pid=""
worker_pid=""

stop_display() {
    if [ -n "$wm_pid" ]; then kill "$wm_pid" 2>/dev/null; fi
    if [ -n "$xvfb_pid" ]; then
        kill "$xvfb_pid" 2>/dev/null
        wait "$xvfb_pid" 2>/dev/null
    fi
}

# docker stop sends TERM (through tini with `init: true`): let the worker
# close its browser first, then stop the display, then exit with its status.
on_signal() {
    echo "entrypoint: $1 received, stopping the worker"
    status=143
    if [ -n "$worker_pid" ]; then
        kill -TERM "$worker_pid" 2>/dev/null
        wait "$worker_pid"
        status=$?
    fi
    stop_display
    exit "$status"
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!

# Wait up to 10 s for the display socket; fail fast if Xvfb died.
tries=0
while [ ! -S "$SOCKET" ]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "entrypoint: Xvfb exited during startup" >&2
        cat /tmp/xvfb.log >&2
        exit 1
    fi
    tries=$((tries + 1))
    if [ "$tries" -gt 100 ]; then
        echo "entrypoint: display $DISPLAY did not come up in 10 s" >&2
        cat /tmp/xvfb.log >&2
        stop_display
        exit 1
    fi
    sleep 0.1
done

openbox >/dev/null 2>&1 &
wm_pid=$!
sleep 1
echo "entrypoint: display $DISPLAY ($SCREEN_GEOMETRY) with openbox is up"

python /app/worker.py "$@" &
worker_pid=$!
wait "$worker_pid"
status=$?
worker_pid=""
stop_display
exit "$status"
