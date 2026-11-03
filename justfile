import '.gates/gates.just'

# Reinstall the kwin-mcp tool through mise from this checkout's main branch
# (the lefthook post-commit hook runs this on main; mise cannot pin a commit).
# The tool key must match the one activated in the mise config, which may spell
# the checkout path differently (symlinks), so it is looked up there first.
install:
    #!/bin/sh
    set -eu
    tool=$(mise ls --json 2>/dev/null | python3 -c 'import json,sys; print(next((k for k in json.load(sys.stdin) if k.startswith("pipx:git+file://") and k.endswith("/kwin-mcp")), ""))')
    [ -n "$tool" ] || tool="pipx:git+file://{{justfile_directory()}}"
    mise install -f "$tool@main"
    mise reshim
    # The mcporter daemon keeps kwin-mcp alive; restarting it ends every live
    # desktop session. Queue the restart in a transient user unit that waits until
    # mcp-restart-when-idle (workstation-mcp) sees the desktop MCP stack idle, so
    # this never blocks the commit or cuts off an agent. The new daemon gets its
    # own scope, so it outlives the queue unit.
    helper=$(command -v mcp-restart-when-idle || true)
    unit=kwin-mcp-deferred-restart
    if [ -z "$helper" ] || ! command -v systemd-run >/dev/null 2>&1; then
        echo "kwin-mcp installed; mcp-restart-when-idle or systemd-run missing:"
        echo "run 'mcporter daemon restart' yourself once no agent uses kwin-mcp"
    elif systemctl --user is-active --quiet "$unit"; then
        echo "kwin-mcp installed; restart already queued ($unit), it will load this install"
    else
        systemd-run --user --quiet --collect --unit="$unit" \
            --description="Restart mcporter once the desktop MCP stack is idle" \
            "$helper" --idle 300 --timeout 7200 -- \
            systemd-run --user --scope --quiet --collect --unit="mcporter-daemon-$(date +%s)" \
            mcporter daemon restart
        echo "kwin-mcp installed; mcporter restart queued until idle: journalctl --user -u $unit"
    fi
