# Commit gates from the repo-gates kit (.gates/). Managed by its install.sh.

# Gates for the staged changes (run by the lefthook pre-commit hook)
gate:
    sh .gates/run.sh staged

# All gates on every tracked file (CI, or after installing the gates)
gate-all:
    sh .gates/run.sh all
