PYTHON ?= python
PYTEST_ARGS ?= -q
TEST_WORKERS ?= 6
TEST_WHEELHOUSE ?= .test-wheels
TEST_RESULTS ?= .test-results
# Indirection keeps make -n from executing the diagnostic pipe as a recursion.
REPORT_MAKE = $(MAKE)
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

.PHONY: test test-full test-report test-bootstrap test-prepare test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor typecheck

# Shared dependency preparation for local validation and CI.
test-bootstrap:
	$(PYTHON) -m pip install --only-binary=:all: --require-hashes -r tests/dev-requirements.txt
	$(PYTHON) -m pip install --no-build-isolation --no-deps .
	$(MAKE) test-prepare

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

# Preserve diagnostics on failure; pipefail retains the test command's failure.
test-report: SHELL := /bin/bash
test-report: .SHELLFLAGS := -o pipefail -c
test-report:
	@mkdir -p "$(TEST_RESULTS)"
	@rm -f "$(TEST_RESULTS)/junit.xml" "$(TEST_RESULTS)/resources.txt" "$(TEST_RESULTS)/environment.json" "$(TEST_RESULTS)/pytest.log"
	$(PYTHON) tests/support/report_environment.py > "$(TEST_RESULTS)/environment.json"
	@/usr/bin/time -v -o "$(TEST_RESULTS)/resources.txt" $(REPORT_MAKE) test-full PYTEST_ARGS="$(PYTEST_ARGS) --durations=50 --durations-min=0 --junitxml='$(TEST_RESULTS)/junit.xml'" 2>&1 | tee "$(TEST_RESULTS)/pytest.log"

test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor: test-%:
	$(PYTHON) -m pytest $(TESTS_$*) $(PYTEST_ARGS)

typecheck:
	$(PYTHON) -m mypy src/agent_run
