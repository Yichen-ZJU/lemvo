# Lemvo Console — one server, multiple research conversations

[中文](README.zh-CN.md)

A local dashboard for Claude Code / Codex conversations, project experiments, and host resources. Python 3.9+ on Linux, standard library only. The console does not call a model. Closing the browser or stopping the console does not stop experiments.

## Start

From the repository root on your research server:

```bash
python3 console/install.py
~/.local/bin/lemvo-console start
~/.local/bin/lemvo-console url
```

The service binds only `127.0.0.1:8765`. `start` runs it independently of the SSH connection. A second start keeps the existing service configuration. Logs, observations and the private access token are under `~/.local/state/lemvo/console/`. `status` inspects the service; `stop` stops **only the console**. Immutable installation releases allow returning `current` to a previous release without overwriting files in use.

On your computer, forward the port, then open the complete URL from `url` (including its token fragment):

```bash
ssh -N -L 8765:127.0.0.1:8765 user@your-server
```

Windows users can run `console/Open-LemvoConsole.ps1 -SshHost user@your-server -SshPort 22`. It opens a hidden SSH tunnel and the browser. Choose `-LocalPort` if 8765 is already in use. Remote browser `localhost` and your computer's `localhost` are different; forwarding connects them. No cross-server aggregation is included in this release.

The Windows helper uses a direct hostname/IP, not SSH-config aliases, to avoid inherited forwarding conflicts. Use `-IdentityFile` for a nondefault SSH key and `-RemotePort` when the console runs on a different server port.

## Conversations and projects

The observer discovers current-user CLI processes. It resolves an explicit session ID or an open transcript descriptor instead of assigning the newest log in a directory. A shared Codex backend can yield multiple conversation cards. If a binding cannot be established, the card says **Process only**, not “healthy”. Only the default tmux socket is used for pane labels; process discovery also works outside tmux.

For new terminal conversations, use the registered launcher:

```bash
lemvo-console launch --engine claude --project /path/to/project -- claude
lemvo-console launch --engine codex --project /path/to/project -- codex
```

The native terminal remains interactive. Fresh direct Claude launches receive a session UUID; resume/continue flags are preserved. Shell functions such as a custom `claude-kimi` wrapper require an explicit shell invocation; they are not executables. Existing unbound conversations can be attached without sending them input:

```bash
lemvo-console attach --pid 12345 --engine claude --project /path/to/project \
  --transcript /absolute/path/to/session.jsonl
lemvo-console register /path/to/project
```

Registration keeps a project's jobs visible even when its conversation has exited. A conversation in a directory with several sessions does not automatically own all project jobs. Add `session_id` to a job record when exact session attribution is needed; otherwise jobs remain visible at project level.

## Training, loading, and long tool calls

The console reads the existing `.lemvo-jobs.json`. Live jobs need a PID **and** its `/proc` start identity (`pid_start`); a reused PID is not adopted. Result JSON is validated using `run_id`, terminal status and exit code. A partial result never becomes successful just because a file exists. Legacy text sentinels remain unverified in this viewer.

Logs containing `step 10/100`, `epoch 2/20`, or `shard 3/8` can provide inferred progress. For precise loading/training stages, publish a small counter from your job wrapper:

```bash
lemvo-console progress --project /path/to/project --run-id fit-1 --pid 12345 \
  --phase loading_model --current 3 --total 8 --unit shards
```

Phases: `loading_model`, `loading_data`, `training`, `evaluating`, `tool`. This updates console observations, not research judgments or budgets. The job must already be registered in `.lemvo-jobs.json`. Updating a timestamp or description without changing the counter does not establish progress. GPU utilization is host-wide context, including reservations/other jobs, and is never proof that a particular experiment is advancing.

Tool requests are shown as **pending**, not assumed to have started. No log output can mean loading, computing, waiting for permission, or a stalled tool. After `--warn-seconds` (default 1800), an unresolved active phase is flagged for inspection. This is a warning threshold, **not a runtime cap**. The console never sends Esc, continue, TERM or KILL to a research process and never turns a runtime problem into an idea rejection. User-wait and completed sessions are not automatically awakened.

## Optional project controls

Start with `--allow-controls` to enable project mode changes and guidance. This is opt-in; changing a running console's configuration requires stopping/restarting the console alone.

Controls require a verified live Lemvo controller and its current process identity. Modes use the existing `.lemvo-mode`; guidance uses the installed `runtime-contract.py` queue with an idempotent request ID. **Queued** means publication accepted, not that the model read the instruction. A mode request takes effect at the controller's safe boundary. Completed campaigns are not reopened. Bare interactive conversations remain observation-only; no blind tmux keystroke injection is provided.

## Validation

```bash
python3 -m unittest discover -s console/tests -p 'test_*.py' -v
python3 console/tests/acceptance.py --output /tmp/lemvo-console-acceptance.json
```

The acceptance script creates isolated CPU processes and native-shaped transcript fixtures: concurrent conversations, loading/training counters, a long tool wait, a user-wait, console shutdown while work continues, and restart reconciliation. It makes no paid model or GPU calls. Observation of actual CLI transcripts is a separate check; fixture tests do not establish universal CLI-version compatibility. Unknown formats and stale snapshots stay visibly unknown.

The web server and collector are separate threads. A slow collector does not block the page; stale state disables controls. Sessions, permissions and training remain owned by their existing CLI/controller. This first release supplies observation, warnings and existing-controller controls; automatic hang recovery and browser-created CLI sessions are not implemented.
