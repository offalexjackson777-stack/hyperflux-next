# Shutdown Hang With OpenRazer

With the Full package installed and OpenRazer running, shutting down or rebooting could stop
for about 90 seconds. Two separate ordering mistakes in the generated systemd units caused
it, and both were fixed in 2.0.1.

Issues:
[#30](https://github.com/offalexjackson777-stack/hyperflux-next/issues/30) (shutdown waits
for OpenRazer) and
[#31](https://github.com/offalexjackson777-stack/hyperflux-next/issues/31) (ordering cycle in
the desktop socket).

## What People Saw

On 2.0.0, the desktop could show a stop job for the user session that ran for up to two
minutes before the machine powered off. The wait came from `openrazer-daemon`, which hit
its 90-second stop timeout.

## How It Was Tracked Down

The daemon was still running its `resume_devices()` teardown when the virtual devices it
was talking to disappeared. The relay had already destroyed them, so the daemon read a
sysfs file that no longer existed and never reached its main-loop quit. It was stopped only
when its 90-second timeout ran out. The question was therefore not why the daemon was slow,
but why the relay was allowed to stop first.

A second fault in the same shutdown path had nothing to do with OpenRazer. The per-user
bridge socket, `hyperflux-next-client@.socket`, sat in an ordering loop that systemd can
only break by dropping stop jobs.

## The Cause

**#30, relay stopped too early.** At shutdown systemd stops units in the reverse of their
start order. Nothing ordered the relay against user sessions, so the relay could destroy
its virtual devices while the per-user OpenRazer daemon still used them.

**#31, ordering cycle.** The socket unit inherited systemd's default
`Before=sockets.target`. It also ordered itself after `user-runtime-dir@%i.service` and
after the bridge, and both of those come after `sockets.target` through `basic.target`. The
socket therefore had to start both before and after `sockets.target`. At shutdown systemd
resolved the loop by deleting stop jobs it could not order.

## The Fix

The relay unit now carries `Before=systemd-user-sessions.service`. User sessions, and the
OpenRazer daemon inside them, start after `systemd-user-sessions.service`, so at shutdown
they stop before the relay removes its devices. We chose an ordering fix over a shorter
timeout or a forced kill because the daemon then finishes its own teardown normally.
There are no sleeps, timeout changes, or forced kills.

[3a3a61d](https://github.com/offalexjackson777-stack/hyperflux-next/commit/3a3a61d856f463c1d5711edadf70657e66062901)

The socket unit now sets `DefaultDependencies=no` with explicit `Conflicts=shutdown.target`
and `Before=shutdown.target`, so it no longer joins `sockets.target` at all. The socket
path, owner, mode `0600`, `RemoveOnStop`, and the `user-runtime-dir` dependency are
unchanged.

[38588e1](https://github.com/offalexjackson777-stack/hyperflux-next/commit/38588e1aafa2e51d9be4f10350444cf2fb949b86)

Both units are generated, so both changes were made in the unit generator under
`tools/hfxdev/` and the files were regenerated with `./hfx generate`.

## How It Was Checked

- Generator tests assert the new relay ordering, the new socket ordering, and the absence
  of any `sockets.target` edge on the socket.
- `systemd-analyze verify` is clean for the bridge, preflight, relay, socket, and socket
  proxy units.
- The fast verification lane passed 25 of 25 checks in the pinned container.

## What Is Still Open

A timed shutdown on real hardware is part of the 2.0.1 release checklist,
[#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36): five normal
reboots and one poweroff with no long stop job and no ordering-cycle messages. #30 is done
when OpenRazer exits normally in under two seconds at shutdown across that series. #31 is
done when there are no ordering-cycle messages at boot or shutdown and on-demand socket
activation still works. Both issues stay open until those checks are recorded.
