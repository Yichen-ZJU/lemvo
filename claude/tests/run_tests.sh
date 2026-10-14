#!/usr/bin/env bash
# One-command regression suite (T01): queue state machine + installer scenarios.
set -e
cd "$(dirname "$0")/.."
python3 tests/queue_state_test.py
python3 tests/queue_integration_test.py  # real screen/process chain (E04 guard), skips if no screen
python3 tests/queue_namespace_test.py     # real screen namespace coexistence (E05), skips if no screen
python3 tests/ideation_blindtest_fixture_test.py  # grounded-ideation blindtest fixture (no model)
python3 tests/runtime_policy_test.py  # real adapter, isolated CPU stubs
if [ -f tests/team_classification_test.py ]; then
  python3 tests/team_classification_test.py  # optional installed backend consumer
fi
bash tests/installer_test.sh
echo "== ALL TEST SUITES PASSED =="
