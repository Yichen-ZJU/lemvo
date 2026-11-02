# Windows research decisions and SSH experiments

When running on Windows, use the installed Lemvo desktop client for unattended SSH research and Windows/server handoff. The executable entrypoint is `%USERPROFILE%/.lemvo-desktop/lemvo.ps1`; its installation.json identifies the Python/client paths. The registered project names and explicit host/path mappings are in registry.json. Read those mappings before choosing a target.

Use normal local research skills for reasoning and writing. Execute Linux training/runtime helpers on the registered server. Preserve the server's research-state.yaml, STATE.md, job ledger, results, guidance queue and resource accounting as the authoritative project; local files are scratchpads or downloaded artifacts.

- `start-worker` starts the ordinary Windows background supervisor. It does not start experiments by itself.
- `register NAME --host USER@HOST --port PORT --project ABS_PATH --runtime ABS_LEMVO_RUN_PATH` opts an idle/new project into the bridge. Default decision provider is Windows Codex. Existing live controllers are not silently taken over.
- Server permissions default to preservation. With the user's explicit authorization, add `--server-approval auto-review` for that project to route Codex host-level launch requests through automatic approval review while retaining workspace-write sandboxing. Do not enable this based only on an assumption or change global settings.
- `steer NAME --message TEXT`, `start NAME --mode guided|unattended`, `mode NAME --mode ...`, `advance NAME`, and `status NAME` reuse the server's existing runtime semantics. Start a campaign only within the user's requested research scope.
- `handoff NAME|all --to server|windows --wait SECONDS` requests a safe model-turn boundary. Report actual readiness; queued requests are not completed handoffs. Before planned Windows shutdown, require a successful server handoff receipt for the projects to continue there.

Do not route commands to every server when the user selected one project. Multiple registered projects can run on different hosts; each remains separately identified. Keep existing Windows GPU-reserve behavior and unrelated skills intact.

The visible desktop chat is an interface; persistent decisions run in local Codex CLI sessions. Remote-controller waiting is ordinary code with no model calls. Do not claim local skills alone create perpetual execution or that an unacknowledged handoff survived shutdown. Provider switching does not change guided/unattended mode, clear budget totals, or terminate training.

The user can still SSH into the server and use its ordinary Lemvo CLI. For the same registered project, switch to guided mode and wait for the active model turn to end before a separate interactive session makes edits; do not create a competing unattended controller. Unregistered projects keep their existing workflows.

For desktop worker turns, follow the supplied per-turn remote exec/put/get helper commands. These bind changes to the current claim and run in the correct remote project. Read current remote state at each resumed turn because the server provider may have advanced it since the last Windows turn.
