# Live accessibility bus connection failure

`element_query` checks the selected session's accessibility bus before starting
libatspi. The probe uses recoverable D-Bus errors in a separate process with an
8-second deadline, including authentication. It never restarts desktop services
or falls back to another session's bus. A verified address and bus GUID are
passed explicitly to the worker so discovery and the native client agree.

If the error names `AT_SPI_BUS_ADDRESS`, remove or correct that override in the
caller's environment. If it names `org.a11y.Bus.GetAddress`, inspect:

```sh
journalctl --user -u at-spi-dbus-bus.service
busctl --user call org.a11y.Bus /org/a11y/bus org.a11y.Bus GetAddress
ss -xlpn
```

A running launcher and a listening socket in `ss` do not prove that the current
filesystem socket path reaches that listener. A removed/replaced Unix socket
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

## Issue #6 evidence (2026-11-09)

- Cores 922463/922542 and subsequent pairs aborted in libatspi `_atspi_bus` /
  `_atspi_ref_accessible`. The actual worker interpreter was Python 3.13.16.
- A direct read-only 10-node worker query reproduced SIGABRT. Its stderr reported
  `bus_0: Connection refused`, followed by libatspi's fatal connection error.
- Live session `GetAddress` advertised `/run/user/1000/at-spi/bus_0`.
  dbus-python independently reported `org.freedesktop.DBus.Error.NoServer`.
- Broker PID 4587 had been running since November 6 and still had a listening
  socket named `bus_0`; the filesystem socket's mtime was November 9 09:05.
  This is consistent with a replaced/unreachable pathname. The responsible
  process and replacement mechanism have not been established.
- The isolated session's own accessibility bus worked. After the fix, its
  read-only control queries returned 10 elements on each of two requests.
- The broken live bus was intentionally left untouched. The patched engine
  returned an actionable diagnosis on repeated queries without spawning a
  native worker. Restoring the live tree requires owner-controlled recovery.

Regression coverage: `uv run pytest -q tests/unit/test_atspi_bus.py` checks a
real dead Unix socket, no worker spawn/retry, override isolation, address
pinning, a real private D-Bus connection, and stalled authentication.
