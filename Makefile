PYTHON ?= python
PYTEST_ARGS ?= -q
TEST_WORKERS ?= 6
TEST_WHEELHOUSE ?= .test-wheels
TEST_RESULTS ?= .test-results
# Indirection keeps make -n from executing the diagnostic pipe as a recursion.
REPORT_MAKE = $(MAKE)
.DEFAULT_GOAL := test

# One current inventory; shared changes also need their callers' tests.
TEST_SCOPE = $(PYTHON) .github/ci/test_scope.py
test_scope = $(shell $(TEST_SCOPE) $(1) || echo .invalid-test-selection)

.PHONY: test test-full test-report test-bootstrap test-prepare test-native-capabilities test-native-contracts test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor typecheck

# Shared dependency preparation for local validation and CI.
test-bootstrap:
	$(PYTHON) -m pip install --only-binary=:all: --require-hashes -r tests/dev-requirements.txt
	# setuptools reuses build/lib, including modules removed from src.
	rm -rf -- build
	$(PYTHON) -m pip install --no-build-isolation --no-deps .
	$(MAKE) test-prepare

# Network access belongs to dependency preparation, never a pytest fixture.
test-prepare:
	$(PYTHON) -m pip download --only-binary=:all: --no-deps --require-hashes -r tests/build-requirements.txt --dest "$(TEST_WHEELHOUSE)"

# Local feedback; run the changed module's tests as well.
test:
	@echo "本地快速回归：Policy / State / Prompts / Locator；完整套件请运行 make test-full"
	$(PYTHON) -m pytest $(call test_scope,fast) $(PYTEST_ARGS)

# Missing native evidence is a failing precondition, never a pytest skip.
test-native-capabilities:
	$(PYTHON) tests/support/report_environment.py --require-native

test-native-contracts: test-native-capabilities
	$(PYTHON) -m pytest $(call test_scope,native-contract) $(PYTEST_ARGS)

# Complete suite for CI and final validation, with a fixed process limit.
test-full:
	$(PYTHON) -c 'import os, pip, sys; all(hasattr(os, name) for name in ("memfd_create", "pidfd_open")) or sys.exit("完整测试需要支持 os.memfd_create 和 os.pidfd_open 的 Linux Python；请切换解释器，不得以跳过关键进程测试代替完整验收"); os.close(os.memfd_create("agent-run-tests", os.MFD_CLOEXEC)); os.close(os.pidfd_open(os.getpid()))'
	@test_tmpdir="$${TMPDIR:-/tmp}"; runtime_dir="/run/user/$$(id -u)"; \
	if [ -z "$${TMPDIR:-}" ] && [ -d "$$runtime_dir" ] && [ ! -L "$$runtime_dir" ] \
		&& [ -w "$$runtime_dir" ] && [ "$$(stat -f -c %T "$$runtime_dir")" = tmpfs ]; then \
		test_tmpdir="$$runtime_dir"; \
	fi; \
	printf '完整测试临时目录：%s\n' "$$test_tmpdir"; \
	TMPDIR="$$test_tmpdir" AGENT_RUN_TEST_WHEELHOUSE="$(abspath $(TEST_WHEELHOUSE))" \
		$(PYTHON) -m pytest -n $(TEST_WORKERS) --dist worksteal $(PYTEST_ARGS)

# Preserve diagnostics on failure; pipefail retains the test command's failure.
test-report: SHELL := /bin/bash
test-report: .SHELLFLAGS := -o pipefail -c
test-report:
	@mkdir -p "$(TEST_RESULTS)"
	@rm -f "$(TEST_RESULTS)/junit.xml" "$(TEST_RESULTS)/resources.txt" "$(TEST_RESULTS)/environment.json" "$(TEST_RESULTS)/pytest.log"
	$(PYTHON) tests/support/report_environment.py > "$(TEST_RESULTS)/environment.json"
	@/usr/bin/time -v -o "$(TEST_RESULTS)/resources.txt" $(REPORT_MAKE) test-full PYTEST_ARGS="$(PYTEST_ARGS) --durations=50 --durations-min=0 --junitxml='$(TEST_RESULTS)/junit.xml'" 2>&1 | tee "$(TEST_RESULTS)/pytest.log"

test-policy test-state test-prompts test-locator test-github test-delivery test-run test-executor: test-%:
	$(PYTHON) -m pytest $(call test_scope,$*) $(PYTEST_ARGS)

typecheck:
	$(PYTHON) -m mypy src/agent_run
