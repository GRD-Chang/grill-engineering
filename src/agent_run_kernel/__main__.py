"""Public native entry point; only genuinely connected commands are routed."""
from .query_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
