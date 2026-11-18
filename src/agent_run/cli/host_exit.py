"""Public module entry for the runtime Host exit hook."""
from agent_run.runtime.host_exit import main

if __name__ == "__main__":
    raise SystemExit(main())
