# Lemvo desktop dispatch

Run research decisions with Codex on Windows while experiments run on SSH Linux hosts. A lightweight bridge reuses the server's existing Lemvo controller, job ledger, guidance queue, mode selection and accounting. Switching the decision provider does not move training or copy research state into a second authoritative workspace.

This first version uses **Codex on both sides**. The Windows app's current chat is the control interface; unattended decisions run in separate, persistent local Codex CLI sessions. This is not automatic continuation of the visible desktop chat.

## Install on Windows

Requirements: Python 3.9+, Windows OpenSSH, a signed-in Windows Codex CLI supporting `exec --approve-for-me`, and SSH access to a Linux host with Lemvo and Codex. The remote bridge needs Python 3.9+. Keep any existing host-specific Python/CLI configuration.

From the unified repository:

```powershell
python desktop/install.py
& "$HOME/.lemvo-desktop/lemvo.ps1" --help
```

For a Pro checkout use the same installer: it discovers the root `skills/` directory. It installs portable skill files, shared references and a versioned desktop client. Existing unmanaged or modified skills are preserved and reported, including Windows-specific GPU reservation. It does not overwrite global AGENTS.md, model configuration, authentication or power settings.

## Register a project and start

The remote project directory must exist. For a new project, create that directory over SSH first and place the task instructions there or send them with `steer` below. Register projects by explicit host and absolute project path; the client does not select all machines automatically.

```powershell
$Lemvo = "$HOME/.lemvo-desktop/lemvo.ps1"
& $Lemvo register project-a --host user@host --port 22 `
  --project /home/user/project-a `
  --runtime /home/user/.codex/skills/research-orchestrator/templates/lemvo-run.sh
& $Lemvo start-worker
& $Lemvo steer project-a --message "Research X. Code and data are in ...; proceed within the agreed scope."
& $Lemvo start project-a --mode unattended
& $Lemvo status project-a
```

Add another name/host/project for another server; one Windows worker supervises registered projects concurrently. Each campaign retains a single authoritative server project. Cross-server distributed training within one campaign is not introduced by this feature.

`register` preserves an already registered project's provider. It refuses first adoption while a legacy controller/turn is active; finish that controller at a safe boundary before registering. Do not kill training to register a project. Once registered, Windows/server switching is live and needs no controller restart.

This release activates the bridge through new registrations. Updating the Windows client does not migrate an already pinned remote bridge or its permission policy; existing registrations need an explicit version migration at a safe boundary. Automatic Codex rediscovery applies to registrations made without `--codex`; an explicitly supplied executable remains pinned. No existing campaign is migrated by installation.

Registration preserves server permissions by default. If you explicitly authorize Codex's automatic approval reviewer for this project, add `--server-approval auto-review` when registering. This replaces the standard workspace sandbox flag with the equivalent `--approve-for-me` mode (the two flags cannot be combined). It retains workspace-write sandboxing and routes host-level execution requests through approval review. It never changes global configuration, and refuses to override an explicitly different sandbox such as read-only. Existing projects and unregistered server workflows are unaffected.

## Modes and guidance

```powershell
& $Lemvo mode project-a --mode guided
& $Lemvo advance project-a
& $Lemvo steer project-a --message "Try changing the sampling schedule first."
& $Lemvo mode project-a --mode unattended
```

These invoke the existing server mode and guidance queue. Switching provider is a separate choice from guided/unattended mode, and does not reset budgets or scientific results.

## Continue working directly over SSH

Normal server-only Claude/Codex workflows remain available. Projects that were not registered with this bridge work exactly as before.

For a registered project, you can manage the same controller from the server using its existing guidance, status and mode commands. To edit through a separate interactive CLI, first switch the project to guided mode and wait until `status` shows no active model turn (`busy: false`) and a user-waiting handoff. Then use the server CLI in the authoritative project directory. Training already launched can keep running. Do not start a second unattended controller or make simultaneous model edits to the same project. Save changes to the existing research state before resuming the controller.

## Before closing Windows

```powershell
& $Lemvo handoff all --to server --wait 600
```

Wait for **`ready: true`**. The current Windows model turn finishes, all output bytes are acknowledged, and subsequent decisions run with server Codex. Running training continues throughout. If one host is unreachable or a local turn is unfinished, the command does not claim readiness. It does not terminate that turn to meet the wait timeout.

To bring decisions back to Windows:

```powershell
& $Lemvo start-worker
& $Lemvo handoff all --to windows --wait 600
```

Each location keeps its own real Codex session ID. Both read the same remote research files after handoff. There is no cross-machine auth/session-file copying.

## Persistence and recovery

- Worker and model runners are background processes; closing the visible chat does not stop them. The worker prevents idle system sleep while it supervises Windows-owned work, without changing persistent power settings. Explicit shutdown/sleep still ends local availability.
- Output is spooled locally, transferred with byte offsets and hashes, and acknowledged before a turn is complete. A network interruption retries transfer; it does not restart the model or training.
- Restarting the worker uses the same worker ID and durable request claim to collect already running local runners. A second worker is excluded by an OS file lock.
- Planned handoff is supported. Unexpected loss of Windows during an unfinished model turn is **not** silently treated as a completed handoff; the server retains that claim and training continues. Bring Windows back to finish/collect the turn, or inspect an unrecoverable runner before an explicit recovery. Automatically starting a second model in this ambiguous state would risk duplicate experiments.
- Existing Lemvo budgets, stop conditions and watchdog still apply. This feature adds no scientific selection gate or GPU-hours limit.
- Server decisions preserve existing permissions unless this project was registered with the explicit automatic-review option. Background experiments require an authorized durable host job launcher and host process identities; ordinary `nohup` inside a temporary sandbox does not guarantee persistence. A denied launch is reported rather than bypassed.

State locations: Windows `~/.lemvo-desktop/registry.json`, `worker-status.json`, `projects/<project-id>/requests/`; server `<project>/.lemvo-desktop/`. Use `status` plus the existing Lemvo console for project state. Do not edit queue claims or copy session IDs manually.

`handoff` also checks controller ownership. A missing controller is not reported as ready. A deliberately paused or completed campaign is identified as such; handoff does not override that stop. Windows handoff additionally requires a live worker heartbeat. After restarting Windows, run `start-worker` again; installation does not create a login startup task.

The project bridge uses SSH only. No public listener or password/token copies are required. Local Codex uses the existing automatic approval-review mode, not a global sandbox bypass. If a command lacks authorization, its error is surfaced rather than interpreted as a failed research idea.
