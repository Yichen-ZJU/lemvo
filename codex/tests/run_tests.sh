#!/usr/bin/env bash
# One-command regression suite (T01): queue state machine, runtime/idle suites, companion & installer scenarios.
set -e
cd "$(dirname "$0")/.."
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -d "$REPO_ROOT/console" ] && [ -d "$REPO_ROOT/../console" ]; then
  REPO_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
fi

# Screen-based tests run sequentially so screen sessions never collide.
python3 tests/queue_state_test.py
python3 tests/queue_integration_test.py  # real screen/process chain (E04 guard), skips if no screen
python3 tests/queue_namespace_test.py     # real screen namespace coexistence (E05), skips if no screen

# Drift / syntax verification before parallel runtime tests.
if [ -f "$REPO_ROOT/scripts/check-runtime-drift.py" ]; then
  python3 "$REPO_ROOT/scripts/check-runtime-drift.py"
fi

TESTS=(
  tests/ideation_blindtest_fixture_test.py
  tests/construction_context_test.py
  tests/runtime_policy_test.py
  tests/runtime_liveness_test.py
  tests/runtime_scalability_test.py
  tests/runtime_inflight_test.py
  tests/runtime_contract_test.py
  tests/runtime_workunit_test.py
  tests/runtime_dispatch_test.py
  tests/runtime_handoff_order_test.py
  tests/runtime_job_events_test.py
  tests/event_runtime_install_test.py
  tests/runtime_event_adapter_test.py
  tests/execution_owner_lock_test.py
  tests/native_event_runtime_test.py
  tests/runtime_idle_settlement_test.py
)
for opt in tests/arbor_construction_test.py tests/pro_context_test.py tests/team_classification_test.py tests/gpu_pool_test.py; do
  [ -f "$opt" ] && TESTS+=("$opt")
done
if [ -f "$REPO_ROOT/console/tests/test_console.py" ]; then
  TESTS+=("$REPO_ROOT/console/tests/test_console.py")
fi
if [ -f "$REPO_ROOT/desktop/tests/test_dispatch.py" ]; then
  TESTS+=("$REPO_ROOT/desktop/tests/test_dispatch.py")
fi
if [ -f "$REPO_ROOT/desktop/tests/test_install.py" ]; then
  TESTS+=("$REPO_ROOT/desktop/tests/test_install.py")
fi

JOBS="${LEMVO_TEST_JOBS:-4}"
python3 - "$JOBS" "${TESTS[@]}" <<'PYRUN'
import concurrent.futures, subprocess, sys, time

workers = max(1, int(sys.argv[1]))
tests = sys.argv[2:]
failures = []
t0 = time.monotonic()

def run_one(path):
    s = time.monotonic()
    res = subprocess.run([sys.executable, path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return path, res.returncode, time.monotonic() - s, res.stdout

with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
    futs = [pool.submit(run_one, p) for p in tests]
    for fut in concurrent.futures.as_completed(futs):
        path, rc, dt, out = fut.result()
        status = 'PASS' if rc == 0 else f'FAIL(rc={rc})'
        print(f'[{status}] {path} ({dt:.2f}s)', flush=True)
        if rc != 0:
            failures.append((path, rc, out))

if failures:
    for path, rc, out in failures:
        print(f'=== FAILURE OUTPUT: {path} (rc={rc}) ===', file=sys.stderr)
        print(out, file=sys.stderr)
    raise SystemExit(1)
print(f'Parallel Python tests passed ({len(tests)} suites in {time.monotonic() - t0:.2f}s, workers={workers})')
PYRUN

bash tests/installer_test.sh
echo "== ALL TEST SUITES PASSED =="
