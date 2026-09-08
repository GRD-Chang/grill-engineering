PYTHON ?= python
PYTEST_ARGS ?= -q
TEST_WORKERS ?= 6
TEST_WHEELHOUSE ?= .test-wheels
.DEFAULT_GOAL := test

# Groups are starting points; shared changes also need their callers' tests.
TESTS_policy = tests/test_delivery_policy.py tests/test_review_budget.py
TESTS_state = tests/test_state_store.py tests/test_scope_revision.py
TESTS_prompts = tests/test_codex_prompt_contract.py
TESTS_locator = tests/test_cli.py tests/test_state_store.py tests/test_run_locator*.py tests/test_test_environment.py -k 'locator or selector or test_pytest_isolates'
TESTS_github = tests/test_github*.py tests/test_required_checks_observation.py tests/test_external_supervision.py
TESTS_delivery = tests/test_delivery.py tests/test_cli_delivery.py tests/test_cli_mvp.py tests/test_review_budget.py
TESTS_run = tests/test_run_*.py tests/test_ticket_*.py
TESTS_executor = tests/test_cli_run*.py tests/test_run_lifecycle.py tests/test_runner_lease.py tests/test_systemd_executor_host.py

.PHONY: test test-full test-prepare test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor typecheck

# Network access belongs to dependency preparation, never a pytest fixture.
test-prepare:
	$(PYTHON) -m pip download --only-binary=:all: --no-deps --require-hashes -r tests/build-requirements.txt --dest "$(TEST_WHEELHOUSE)"

# Local feedback; run the changed module's tests as well.
test:
	@echo "本地快速回归：Policy / State / Prompts / Locator；完整套件请运行 make test-full"
	$(MAKE) test-policy
	$(MAKE) test-state
	$(MAKE) test-prompts
	$(MAKE) test-locator

# Complete suite for CI and final validation, with a fixed process limit.
test-full:
	$(PYTHON) -c 'import os, pip, sys; sys.exit(0 if hasattr(os, "memfd_create") else "完整测试需要支持 os.memfd_create 的 Linux Python")'
	AGENT_RUN_TEST_WHEELHOUSE="$(abspath $(TEST_WHEELHOUSE))" $(PYTHON) -m pytest -n $(TEST_WORKERS) --dist worksteal $(PYTEST_ARGS)

test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor: test-%:
	$(PYTHON) -m pytest $(TESTS_$*) $(PYTEST_ARGS)

typecheck:
	$(PYTHON) -m mypy src/agent_run
