#!/bin/sh
# vigild's service guard for hidden runs. Usage: guard.sh TEST_UID [wait]
#
# The test process (TEST_UID) and the subject (VIGIL_SUBJ_UID, default 10002) share
# one loopback, so anything started as the test uid that accepts requests is a way
# for the subject to act as the test uid. This exits 1, naming what it found, when
# a service owned by the test uid is reachable by the subject:
#   - a TCP listener, or a bound UDP socket, owned by the test uid;
#   - a unix socket file owned by the test uid that the subject can write;
#   - any listening abstract unix socket (it has no owner or permissions to check).
# With `wait` it retries for a few seconds, for processes that are still exiting.
# It reads /proc only; run it as root (setpriv checks the subject's access).

t=${1:?test uid}
subj=${VIGIL_SUBJ_UID:-10002}

found() {
	for f in /proc/net/tcp /proc/net/tcp6; do
		[ -r "$f" ] && awk -v u="$t" 'NR > 1 && $4 == "0A" && $8 == u { print "a TCP listener (" $2 ")" }' "$f"
	done
	for f in /proc/net/udp /proc/net/udp6; do
		[ -r "$f" ] && awk -v u="$t" 'NR > 1 && $4 == "07" && $3 ~ /^0+:0000$/ && $8 == u { print "a UDP socket (" $2 ")" }' "$f"
	done
	if [ -r /proc/net/unix ]; then
		awk 'NR > 1 && NF >= 8 { print $4, $8 }' /proc/net/unix | while read -r flags path; do
			case $path in
				@*) [ "$flags" = 00010000 ] && echo "a listening abstract unix socket ($path)" ;;
				/*)
					[ -S "$path" ] || continue
					[ "$(stat -c %u "$path")" = "$t" ] || continue
					setpriv --reuid="$subj" --regid="$subj" --clear-groups test -w "$path" 2> /dev/null &&
						echo "a unix socket the subject can write ($path)"
					;;
			esac
		done
	fi
}

tries=1
[ "${2:-}" = wait ] && tries=50
while :; do
	out=$(found)
	[ -z "$out" ] && exit 0
	tries=$((tries - 1))
	[ "$tries" -le 0 ] && break
	sleep 0.1
done
echo "a service owned by the test uid ($t) is reachable by the subject: $(echo "$out" | tr '\n' ';')" >&2
exit 1
