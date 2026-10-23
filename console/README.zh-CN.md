# Lemvo Console：一台服务器，多个研究会话

[English](README.md)

统一查看这台服务器上的 Claude / Codex 会话、项目实验与资源。需要 Linux + Python 3.9 或更新版本，无第三方依赖。监控不调用模型；关闭网页或控制台服务，实验照常运行。本版不含跨服务器汇总。

## 启动一次

在服务器的仓库根目录运行：

```bash
python3 console/install.py
~/.local/bin/lemvo-console start
~/.local/bin/lemvo-console url
```

`start` 后服务独立于 SSH 连接运行，仅监听 `127.0.0.1:8765`。重复启动保留原配置。`status` 查看服务；`stop` 仅停止控制台。状态和私有令牌位于 `~/.local/state/lemvo/console/`；版本放在独立安装目录，更新不覆盖运行中的旧版本。

在 Windows 运行仓库中的：

```powershell
./console/Open-LemvoConsole.ps1 -SshHost user@your-server -SshPort 22
```

脚本建立隐藏的 SSH 转发并打开浏览器。端口占用时指定 `-LocalPort`。也可以自己 `ssh -N -L 8765:127.0.0.1:8765 user@your-server`，再打开 `url` 输出的完整链接。浏览器的 localhost 指你的电脑，端口转发把它连接到服务器服务。

Windows 脚本使用真实主机名/IP，不使用 SSH 配置别名，避免继承已有转发造成端口冲突。非默认密钥可加 `-IdentityFile`；服务端用了其他端口时加 `-RemotePort`。

## 新会话与现有会话

控制台自动发现当前用户的 CLI 进程，通过明确的会话 ID 或打开的日志文件关联对话。同一个 Codex 后台进程里的多条会话分别展示。无法绑定日志时标为“仅进程，待绑定”，不会猜测目录中最新的记录。tmux 标签只读取默认 socket；不在 tmux 中的进程也可以发现。

新对话建议从已登记的入口启动，终端交互方式不变：

```bash
lemvo-console launch --engine claude --project /path/to/project -- claude
lemvo-console launch --engine codex --project /path/to/project -- codex
```

直接启动的新 Claude 对话会分配会话 UUID；resume / continue 参数保留。`claude-kimi` 等 shell 函数需要明确通过对应 shell 调用。已有对话可以只绑定，不向其发送任何消息：

```bash
lemvo-console attach --pid 12345 --engine claude --project /path/to/project \
  --transcript /absolute/path/to/session.jsonl
lemvo-console register /path/to/project
```

登记项目后，对话关闭也能继续看到其作业。同一目录多条对话并行时，未明确归属的作业显示在项目层，不擅自分配；需要精确关联时在作业记录中填写 `session_id`。

## 看懂训练、加载和等待

沿用项目 `.lemvo-jobs.json`：核对 PID 与启动身份，避免误认复用的 PID。结果 JSON 必须有对应 `run_id`、终态和退出码，半写入的文件不算成功；旧的文本哨兵在本页面标为未核实。

日志中的 `step 10/100`、`epoch 2/20`、`shard 3/8` 可以提供推断进度。更准确的阶段由任务启动器上报：

```bash
lemvo-console progress --project /path/to/project --run-id fit-1 --pid 12345 \
  --phase loading_model --current 3 --total 8 --unit shards
```

阶段支持 `loading_model`、`loading_data`、`training`、`evaluating`、`tool`。需要先在原作业表登记该任务。上报仅写控制台观测，不修改研究结论或预算；重复刷新时间、描述而不改变计数，不算新进展。

整机 GPU 利用率含他人任务和占卡进程，只作为资源背景。工具请求记录不证明命令已经执行。长时间没有阶段完成时显示需关注，默认 `--warn-seconds 1800`；这是提示时限，不是训练上限。控制台不会自动给研究进程发送 Esc / continue / TERM / KILL，也不会因为运行故障否定研究方向。等待用户和已完成的会话保持正常停留。

## 可选：项目控制

服务启动时加 `--allow-controls`，可以给已验证的 Lemvo 常驻控制器发送指导、切换半自动／全自动。更改控制台配置时仅需重启控制台。

模式沿用 `.lemvo-mode`，指导沿用现有 `runtime-contract.py` 队列。请求重复发送不会重复入队；页面“已排队”不等于模型已读。模式在安全交接点生效，当前训练继续。已完成课题不被重新打开；裸交互会话首版只读，不模拟终端按键。

## 验收与范围

```bash
python3 -m unittest discover -s console/tests -p 'test_*.py' -v
python3 console/tests/acceptance.py --output /tmp/lemvo-console-acceptance.json
```

隔离验收启动真实 CPU 子进程，使用 CLI 格式会话夹具，检查多对话、加载／训练进度、工具长等、等待用户、关闭控制台后实验继续、重启后对账。不消耗模型或 GPU；真实 CLI 记录的只读核对另行进行。

首版包含观测、告警和现有控制器操作；自动修复挂起、网页新建 CLI 会话留待后续。格式未知时明确标注；采集过期时显示最后更新时间并禁用控制。没有增加技能、科研门槛或资源预算。
