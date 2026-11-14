# Live accessibility bus connection failure

`element_query` checks the selected session's accessibility bus before starting
libatspi. The probe uses recoverable D-Bus errors in a separate process with an
8-second deadline, including authentication. It never restarts desktop services
or falls back to another session's bus. The worker receives the address exactly
as the bus advertised it; the bus id (`org.freedesktop.DBus.GetId`) only keys
the worker, so a bus restarted behind the same path gets a fresh worker.

If the error names `AT_SPI_BUS_ADDRESS`, remove or correct that override in the
caller's environment. If it names `org.a11y.Bus.GetAddress`, inspect:

```sh
journalctl --user -u at-spi-dbus-bus.service
busctl --user call org.a11y.Bus /org/a11y/bus org.a11y.Bus GetAddress
ss -xlpn
```

A running launcher and a listening socket in `ss` do not prove that the current
filesystem socket path reaches that listener. A removed or replaced Unix socket
can leave a listener alive but unreachable through its advertised pathname.

When the owner can tolerate disrupting accessibility connections, they can run:

```sh
systemctl --user restart at-spi-dbus-bus.service
```

Applications may need to reconnect or be restarted by their owner afterwards.
For an isolated desktop, recreate only that session instead. Restarting
kwin-mcp alone cannot repair a dead socket advertised by the desktop launcher.
Use the repository's `just install` deferred restart via
`mcp-restart-when-idle` for kwin-mcp's own MCP stack.

## How the socket gets replaced

`at-spi-bus-launcher` binds `$XDG_RUNTIME_DIR/at-spi/bus$DISPLAY` (`:` becomes
`_`) and unlinks whatever is at that path first. Two launchers that share
`XDG_RUNTIME_DIR` and `DISPLAY` therefore fight over one pathname: the second
replaces the first one's socket, the first session's clients reach the second
bus, and after the second launcher exits the advertised path is dead
(`dbus-broker` leaves the file behind, so connections are refused). libatspi
treats that as a fatal error and the process aborts with SIGABRT.

Virtual sessions used to start their launcher in the host's runtime dir, so
every virtual session (they run without `DISPLAY`) shared `at-spi/bus` and
replaced one another's sockets; any other process starting a launcher with the
desktop's `DISPLAY` would replace the desktop's `bus_0` the same way. A virtual
session now activates its launcher with `XDG_RUNTIME_DIR` set to a private
`kwin-mcp-a11y-*` directory (mode `0700`, removed on `session_stop`) and
restores the host value before any other service is activated. The services the
launcher starts (the AT-SPI registry, gvfsd) inherit the private directory;
gvfsd's FUSE mount is disabled with `GVFS_DISABLE_FUSE=1`, as no mount point
could be shared with the host anyway.

## The guid pitfall

A D-Bus address may carry `guid=`, which libdbus compares with the server's
guid during the handshake. `org.freedesktop.DBus.GetId` returns the bus id,
which is a different value on both `dbus-daemon` and `dbus-broker`. Putting the
bus id into the address as `guid=` makes every connection fail, so libatspi
aborts even on a healthy bus. The address is therefore passed on unchanged.

## Issue #6 evidence

- Worker cores aborted in libatspi `_atspi_bus` / `_atspi_ref_accessible`. The
  worker's stderr reported `bus_0: Connection refused`, followed by libatspi's
  fatal connection error. The desktop broker was still listening on a socket
  named `bus_0`, but the file at that path was newer than the broker: the
  pathname had been replaced. Logs from that time had rotated, so the process
  that replaced it was not identified.
- A reproduction in a private runtime dir showed that a second launcher with
  the same `XDG_RUNTIME_DIR`/`DISPLAY` replaces the first socket and that a
  libatspi client of the first session aborts once the second exits.
- After the desktop's accessibility bus was restarted, queries still aborted:
  the worker's environment held the advertised address plus `guid=<bus id>`,
  `dbus-send` with that address failed, and without the `guid` it succeeded.
- With both fixes a read-only live query returned elements on repeated
  requests with no new core, while the previous code aborted on the same bus.

Regression coverage:

- `tests/e2e/test_atspi_runtime_isolation.py` starts two real virtual sessions
  next to a foreign socket at the shared path and checks that each session gets
  its own private socket, that the foreign socket is untouched, and that the
  first session's bus survives the second session's exit.
- `tests/unit/test_atspi_runtime_dir.py` covers the private directory and the
  order of the wrapper script (switch, activation, restore).
- `tests/unit/test_atspi_bus.py` checks a real dead Unix socket, no worker
  spawn or retry, override isolation, stalled authentication, and that a real
  private D-Bus address is passed on unchanged and stays connectable.
