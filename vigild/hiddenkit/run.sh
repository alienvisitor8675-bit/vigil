#!/bin/sh
# The client of vigild's subject broker (broker.sh), run by vigild's helper as the
# test process's uid. Usage:
#   run.sh CWD TIMEOUT_SECONDS FD3_TO -- ARGV...
# stdin is the subject's stdin; the subject's stdout and stderr come out on ours;
# our exit status is its. FD3_TO is a file for what the subject wrote to fd 3
# ("-" for none). The subject's environment is the lines of $VIGIL_SUBJ_ENV.
# CWD is relative to nothing: it must be inside /subject.
IPC=${VIGIL_BROKER:-}
[ -n "$IPC" ] || { echo "no broker" >&2; exit 125; }
cwd=$1
to=$2
fd3=$3
[ "$4" = "--" ] || exit 125
shift 4
id=req-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
d=$IPC/$id
mkdir -m 700 "$d" || exit 125
printf '%s\n' "$cwd" > "$d/cwd"
printf '%s\n' "$to" > "$d/timeout"
: > "$d/argv"
for a in "$@"; do
	case $a in
		*'
'*) echo "an argument contains a newline" >&2; exit 125 ;;
	esac
	printf '%s\n' "$a" >> "$d/argv"
done
printf '%s\n' "${VIGIL_SUBJ_ENV:-}" > "$d/env"
cat > "$d/stdin"
printf '%s\n' "$id" > "$IPC/queue"
# The broker serves one request at a time, so a call may wait its turn: the
# queue wait is generous and separate, and the call's own timeout is counted from
# the moment the broker starts it (it writes `started`), never from when we queued.
waited=0
queue_limit=$((${VIGIL_QUEUE_SECONDS:-1800} * 20))
while [ ! -f "$d/started" ] && [ ! -f "$d/rc" ]; do
	sleep 0.05
	waited=$((waited + 1))
	if [ "$waited" -gt "$queue_limit" ]; then
		echo "the broker did not start the call" >&2
		exit 124
	fi
done
waited=0
limit=$((to * 20 + 1200))
while [ ! -f "$d/rc" ]; do
	sleep 0.05
	waited=$((waited + 1))
	if [ "$waited" -gt "$limit" ]; then
		echo "the broker did not finish the call" >&2
		exit 124
	fi
done
# a refusal is not the subject's exit: say so out of band (the caller reads this file)
if [ -f "$d/refused" ] && [ -n "${VIGIL_REFUSED:-}" ]; then
	cat "$d/err" > "$VIGIL_REFUSED"
fi
cat "$d/out"
cat "$d/err" >&2
if [ "$fd3" != "-" ] && [ -f "$d/fd3" ]; then
	cp "$d/fd3" "$fd3"
fi
rc=$(cat "$d/rc")
rm -rf "$d"
exit "$rc"
