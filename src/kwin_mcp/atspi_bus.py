"""Resolve and check the selected session's accessibility bus before libatspi.

libatspi treats a failed connection as a fatal GLib error, terminating the
worker with SIGABRT. Use dbus-python's recoverable errors for this preflight;
never search other sessions or restart the user's accessibility services.
"""

import json
import subprocess
import sys
from collections.abc import Mapping

_PROBE_TIMEOUT_S = 8.0


def _probe(env: Mapping[str, str]) -> str:
    """Return a reachable bus address, or an actionable, recoverable error."""
    import dbus.bus

    address = env.get("AT_SPI_BUS_ADDRESS", "")
    explicit = bool(address)
    source = "AT_SPI_BUS_ADDRESS" if explicit else "org.a11y.Bus.GetAddress"
    try:
        if not address:
            session = dbus.bus.BusConnection(env.get("DBUS_SESSION_BUS_ADDRESS", ""))
            try:
                address = str(
                    session.get_object("org.a11y.Bus", "/org/a11y/bus").GetAddress(
                        dbus_interface="org.a11y.Bus", timeout=5
                    )
                )
            finally:
                session.close()
        if not address:
            raise ValueError("empty accessibility bus address")
        connection = dbus.bus.BusConnection(address)
        try:
            bus_id = str(
                connection.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus").GetId(
                    dbus_interface="org.freedesktop.DBus", timeout=5
                )
            )
        finally:
            connection.close()
    except Exception as exc:
        # Do not include arbitrary environment values or remote error messages.
        # The source and error type suffice to distinguish stale overrides from
        # a broken launcher without exposing addresses belonging to other users.
        remedy = (
            "Remove or correct AT_SPI_BUS_ADDRESS in the caller's environment."
            if explicit
            else "Inspect `journalctl --user -u at-spi-dbus-bus.service` and the socket "
            "returned by org.a11y.Bus.GetAddress. When safe, the desktop owner can run "
            "`systemctl --user restart at-spi-dbus-bus.service`; applications may need "
            "to reconnect. For an isolated session, recreate only that session."
        )
        name = exc.get_dbus_name() if hasattr(exc, "get_dbus_name") else type(exc).__name__
        raise RuntimeError(
            f"AT-SPI2 bus unavailable: {source} could not be resolved or connected "
            f"({name}). Request refused before starting an accessibility worker. {remedy}"
        ) from exc
    return address if "guid=" in address else f"{address},guid={bus_id}"


def checked_address(env: Mapping[str, str]) -> str:
    """Bound even D-Bus authentication, which has no Python-level timeout."""
    selected = {
        key: env[key] for key in ("DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS") if key in env
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "kwin_mcp.atspi_bus"],
            input=json.dumps(selected),
            text=True,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "AT-SPI2 bus preflight timed out during address lookup, connection or authentication. "
            "Check AT_SPI_BUS_ADDRESS and journalctl --user -u at-spi-dbus-bus.service. "
            "No accessibility worker was started; no desktop service was restarted."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            "AT-SPI2 bus preflight process failed; inspect the Python/dbus installation."
        )
    response = json.loads(result.stdout)
    if "error" in response:
        raise RuntimeError(response["error"])
    return str(response["address"])


if __name__ == "__main__":
    try:
        payload = {"address": _probe(json.load(sys.stdin))}
    except RuntimeError as error:
        payload = {"error": str(error)}
    print(json.dumps(payload))
