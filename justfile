import '.gates/gates.just'

# Reinstall the kwin-mcp tool from this checkout's main branch through mise
# (the lefthook post-commit hook runs this on main; mise cannot pin a commit).
install:
    mise install -f 'pipx:git+file://{{justfile_directory()}}@main'
    mise reshim
    mcporter daemon restart >/dev/null 2>&1 || true
