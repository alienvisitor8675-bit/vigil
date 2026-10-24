#!/bin/sh
# vigild's subject broker for hidden runs. It runs as root in the verifier
# container, started by the container's PID 1 before the test command drops to
# uid 10001, and it is the only way the test process can run the code under
# test: that code runs as uid 10002, which owns nothing but its own fresh tmp
# dir, so it cannot write the JUnit report, the broker's state or any file of
# the test process, and cannot signal or ptrace it.
#
# Protocol (the client is run.sh, as uid 10001): a request is a directory
# /vigil-ipc/req-<32 hex> holding `cwd`, `timeout`, `argv` (one argument per
# line), optional `env` (KEY=VALUE lines) and `stdin`, and its name is written
# as one line to the FIFO /vigil-ipc/queue. The broker answers in the same
# directory with `out`, `err`, `fd3` and, last, `rc`. It refuses any request
# that is not a well-formed run spec (rc 126, the reason in `err`).
#
# Each subject runs under setpriv as uid/gid 10002 with no capabilities and no
# new privileges, in a new session and process group, under a timeout, in the
# read-only checkout at /subject, with no network (the container has none). When
# it returns or times out the whole process group is killed, and then every
# process of uid 10002 is, so nothing outlives a call.

IPC=/vigil-ipc
SUBJ=10002
PATH_SAFE=/usr/local/bin:/usr/bin:/bin

refuse() {
	printf '%s\n' "$1" > "$d/err"
	echo 126 > "$d/rc"
}

as_subject() {
	setpriv --reuid=$SUBJ --regid=$SUBJ --clear-groups --inh-caps=-all \
		--bounding-set=-all --no-new-privs -- "$@"
}

while :; do
	id=
	read -r id < "$IPC/queue"
	[ -n "$id" ] || continue
	case $id in
		req-????????????????????????????????) ;;
		*) continue ;;
	esac
	[ -z "$(printf %s "${id#req-}" | tr -d '0-9a-f')" ] || continue
	d=$IPC/$id
	[ -d "$d" ] && [ ! -L "$d" ] || continue
	[ -f "$d/cwd" ] && [ -f "$d/timeout" ] && [ -f "$d/argv" ] || { refuse "malformed request"; continue; }
	cwd=$(cat "$d/cwd")
	t=$(cat "$d/timeout")
	case $t in '' | *[!0-9]*) refuse "bad timeout"; continue ;; esac
	{ [ "$t" -ge 1 ] && [ "$t" -le 900 ]; } || { refuse "timeout out of range"; continue; }
	real=$(cd "$cwd" 2> /dev/null && pwd -P)
	case $real in
		/subject | /subject/*) ;;
		*) refuse "cwd is not inside /subject"; continue ;;
	esac
	tmp=$(mktemp -d /tmp/vigil-subj.XXXXXX) || { refuse "no tmp dir"; continue; }
	chown $SUBJ:$SUBJ "$tmp"
	set -- env -i PATH=$PATH_SAFE HOME="$tmp" TMPDIR="$tmp"
	ok=1
	if [ -f "$d/env" ]; then
		while IFS= read -r line; do
			case $line in
				[A-Za-z_]*=*) set -- "$@" "$line" ;;
				'') ;;
				*) ok= ;;
			esac
		done < "$d/env"
	fi
	n0=$#
	while IFS= read -r line; do
		set -- "$@" "$line"
	done < "$d/argv"
	if [ -z "$ok" ] || [ $# -le $n0 ] || [ -z "$(head -n 1 "$d/argv")" ]; then
		refuse "malformed env or argv"
		rm -rf "$tmp"
		continue
	fi
	[ -f "$d/stdin" ] || : > "$d/stdin"
	: > "$d/out"
	: > "$d/err"
	: > "$d/fd3"
	(
		cd "$real" || exit 126
		exec setsid timeout -s KILL "$t" setpriv --reuid=$SUBJ --regid=$SUBJ --clear-groups \
			--inh-caps=-all --bounding-set=-all --no-new-privs -- "$@"
	) < "$d/stdin" > "$d/out" 2> "$d/err" 3> "$d/fd3" &
	pid=$!
	wait $pid
	rc=$?
	kill -KILL -- "-$pid" 2> /dev/null
	as_subject sh -c 'kill -KILL -1' > /dev/null 2>&1
	rm -rf "$tmp"
	echo $rc > "$d/rc"
done
