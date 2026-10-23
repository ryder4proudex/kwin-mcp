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
    mcporter daemon restart >/dev/null 2>&1 || true
