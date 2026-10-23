#!/bin/sh
# Commit gates (repo-gates kit). Usage: run.sh staged|all
#   staged: content gates on the index snapshot, language gates if relevant files are staged
#   all:    every gate on all tracked files (CI, or after installing the gates)
# Language gates run on the working tree; stash unstaged changes for an exact check.
set -eu
mode="${1:-staged}"
cd "$(git rev-parse --show-toplevel)"
gates=.gates
failed=""

# Licenses that allow commercial use (Node and Python; Rust uses deny.toml).
# LGPL is allowed: Python/Node packages are used unmodified as separate modules (e.g. PyGObject).
NODE_LICENSES="MIT;MIT-0;Apache-2.0;BSD-2-Clause;BSD-3-Clause;ISC;0BSD;Zlib;CC0-1.0;Unlicense;BlueOak-1.0.0;Python-2.0;MPL-2.0;CC-BY-4.0;LGPL-2.1-or-later;LGPL-3.0-or-later"
PY_LICENSES="MIT;Apache;BSD;ISC;PSF;Python Software Foundation;MPL-2.0;Mozilla Public License 2.0;Zlib;Unlicense;CC0;0BSD;Historical Permission;GNU Lesser General Public License;LGPL"

step() { printf '== %s\n' "$1"; }
bad() { printf 'GATE FAILED: %s\n' "$1" >&2; failed="$failed
  - $1"; }
has() { command -v "$1" >/dev/null 2>&1; }
need() { has "$1" || { bad "$1 not found ($2)"; return 1; }; }
touched() { [ "$mode" = all ] || tr '\0' '\n' <"$tmp/list" | grep -Eq "$1"; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
if [ "$mode" = staged ]; then
    git diff --cached --name-only --diff-filter=ACMR -z >"$tmp/list"
    mkdir "$tmp/tree"
    git checkout-index --stdin -z --prefix="$tmp/tree/" <"$tmp/list"
    base="$tmp/tree"
elif [ "$mode" = all ]; then
    git ls-files -z >"$tmp/list"
    base=.
else
    echo "usage: run.sh staged|all" >&2
    exit 2
fi

if [ -s "$tmp/list" ]; then
    step "no CJK outside i18n files"
    python3 "$gates/cjk.py" --base "$base" --list "$tmp/list" --allow "$gates/cjk-allow" || bad "CJK characters"
    step "token size limits"
    python3 "$gates/tokens.py" --base "$base" --list "$tmp/list" --allow "$gates/tokens-allow" || bad "files too large; split them"
fi

rust_gate() {
    need cargo "install Rust with rustup" || return 0
    cargo fmt --all --check || bad "cargo fmt (fix: cargo fmt --all)"
    cargo clippy --all-targets --locked -q -- -D warnings || bad "cargo clippy"
    cargo test --locked -q || bad "cargo test"
    need cargo-deny "install: mise use -g cargo-deny, or cargo install --locked cargo-deny" || return 0
    cargo deny --log-level error check advisories bans licenses sources ||
        bad "cargo deny (advisories, or a dependency license that does not allow commercial use)"
}

python_gate() {
    need uv "install uv: https://docs.astral.sh/uv/" || return 0
    # The kit's own helpers under .gates/ follow the kit's formatting, not the host repo's ruff config.
    uvx ruff check --exclude .gates . || bad "ruff check"
    uvx ruff format --check --exclude .gates . || bad "ruff format (fix: uvx ruff format .)"
    if [ ! -f uv.lock ]; then
        bad "Python dependencies must be locked with uv (uv.lock) for audit and license checks"
        return 0
    fi
    uv export --frozen --no-hashes --no-emit-project -q -o "$tmp/requirements.txt" || bad "uv export"
    uvx pip-audit --progress-spinner off -r "$tmp/requirements.txt" || bad "pip-audit"
    uv run --frozen -q --with pip-licenses pip-licenses --partial-match --allow-only "$PY_LICENSES" >/dev/null ||
        bad "Python dependency licenses (must allow commercial use)"
    if [ -d tests ] || [ -d test ]; then
        # .gates/pytest-args (optional, kept on reinstall) narrows the run, e.g. "tests/unit"
        # when the full suite needs an environment the commit host does not have.
        pytest_args=""
        [ -f "$gates/pytest-args" ] && pytest_args=$(tr '\n' ' ' <"$gates/pytest-args")
        # shellcheck disable=SC2086
        uv run --frozen -q --with pytest pytest -q $pytest_args || bad "pytest"
    fi
}

node_gate() {
    need npm "install Node.js" || return 0
    [ -d node_modules ] || { bad "node_modules missing; run npm ci first"; return 0; }
    npm audit --audit-level=high || bad "npm audit"
    npx --yes license-checker-rseidelsohn@5.0.1 --production --excludePrivatePackages \
        --onlyAllow "$NODE_LICENSES" >/dev/null || bad "npm dependency licenses (must allow commercial use)"
    npm run --if-present lint || bad "npm run lint"
    npm run --if-present test || bad "npm test"
}

go_gate() {
    need go "install Go" || return 0
    [ -z "$(gofmt -l .)" ] || bad "gofmt (fix: gofmt -w .)"
    go vet ./... || bad "go vet"
    go test ./... || bad "go test"
    go run golang.org/x/vuln/cmd/govulncheck@v1.1.4 ./... || bad "govulncheck"
    go run github.com/google/go-licenses@v1.6.0 check ./... \
        --disallowed_types=forbidden,restricted,reciprocal,unknown || bad "Go dependency licenses"
}

if [ -f Cargo.toml ] && touched '(\.rs|Cargo\.(toml|lock)|deny\.toml|rustfmt\.toml|\.yml)$'; then
    step "rust: fmt, clippy, test, cargo-deny"
    rust_gate
fi
if [ -f pyproject.toml ] && touched '(\.py|pyproject\.toml|uv\.lock)$'; then
    step "python: ruff, pip-audit, licenses, pytest"
    python_gate
fi
if [ -f package.json ] && touched '(\.[cm]?[jt]sx?|\.vue|\.svelte|package(-lock)?\.json)$'; then
    step "node: audit, licenses, lint, test"
    node_gate
fi
if [ -f go.mod ] && touched '(\.go|go\.(mod|sum))$'; then
    step "go: gofmt, vet, test, govulncheck, licenses"
    go_gate
fi

if [ -n "$failed" ]; then
    printf '\nCommit blocked by repo gates:%s\n' "$failed" >&2
    exit 1
fi
echo "repo gates passed"
