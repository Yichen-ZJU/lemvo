<p align="center"><a href="https://yichen-zju.github.io/lemvo/"><img src="claude/assets/research-cover.png?v=lemvo-agent-3" alt="Lemvo — A research agent that thinks and acts." width="100%"></a></p>

<h1 align="center">Lemvo</h1>
<p align="center"><strong>会思考、会行动的科研 Agent。<br>A research agent that thinks and acts.</strong><br>一套科研体系，两种 CLI 引擎。 / One research system. Two CLI engines.</p>
<p align="center"><a href="https://yichen-zju.github.io/lemvo/?lang=zh">Lemvo 中文主页</a> · <a href="https://yichen-zju.github.io/lemvo/?lang=en">Lemvo in English</a> · <a href="#quickstart">Quickstart</a> · <a href="https://github.com/Yichen-ZJU/lemvo/stargazers">⭐ Star Lemvo</a></p>
<p align="center"><img src="https://img.shields.io/badge/skills-114-B7F7D4?style=flat-square&amp;labelColor=101312" alt="114 skills"> <img src="https://img.shields.io/badge/engines-Claude%20Code%20%7C%20Codex%20CLI-B7F7D4?style=flat-square&amp;labelColor=101312" alt="Claude Code | Codex CLI"> <img src="https://img.shields.io/badge/workflow-research%20%E2%86%92%20experiments%20%E2%86%92%20papers-B7F7D4?style=flat-square&amp;labelColor=101312" alt="Research to experiments to papers"></p>

**Lemvo** 把研究思考与实际行动接在一起：理解问题、读透文献、构建方法、选择实验，再根据结果修订下一步。**Research Orchestrator** 协调 **114 个科研技能**，通过内外双循环推进从构想到发现、再到论文的研究过程。

**Lemvo** connects research reasoning with action: understand a question, investigate the literature, build methods, choose experiments, and learn what to do next. **Research Orchestrator** coordinates **114 research skills** through two nested loops, carrying ideas into findings and findings into papers.

本仓包含同一套科研体系的两个引擎目录，内容与各自原仓库逐字节一致：

This repository carries one research system as two engine directories, byte-identical to the original standalone repositories:

| 目录 / Directory | 引擎 / Engine | 安装 / Install | 说明 / Docs |
|---|---|---|---|
| [`claude/`](claude/) | Claude Code | `cd claude && ./install.sh` | [claude/README.md](claude/README.md)（中英） |
| [`codex/`](codex/) | Codex CLI | `cd codex && ./setup.sh install` | [codex/README.md](codex/README.md)（中英） |

两个目录各自保持原仓的技能、脚本、约定文件与安装器（Claude 端 `CLAUDE.md`，Codex 端 `AGENTS.md`）；同一套 114 技能名称集合，工具调用与续接机制按宿主适配。

Each directory keeps its original skills, scripts, convention files, and installer (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex). The 114-skill catalogue is shared by name; tool use and continuation adapt to each host CLI.

<a id="quickstart"></a>
## 快速开始 / Quickstart

安装并登录所选引擎的 CLI，在 Linux / WSL2 的 Bash 中运行： / Install and sign in to your engine's CLI, then run in Bash on Linux / WSL2:

```bash
git clone https://github.com/Yichen-ZJU/lemvo.git
cd lemvo

# Claude Code 端 / Claude Code side:
cd claude && ./install.sh            # 可加 --with-mcp 配置论文检索 MCP
cd ..

# Codex CLI 端 / Codex side:
cd codex && ./setup.sh install
```

安装器会备份同名技能、代理/约定文件后落装，可回退（各端说明见其 README）。全文检索的 ArXiv MCP 配置见各端 README：Claude 端 `./install.sh --with-mcp` 或手动 `claude mcp add`，Codex 端安装时自动注册。

The installers back up matching skills, agents, and convention files before installing, and can be reverted (see each side's README). For full-text retrieval, configure the ArXiv MCP as described there: `./install.sh --with-mcp` (or manual `claude mcp add`) on the Claude side; the Codex side registers it automatically during setup.

在你的项目目录里打开引擎 CLI，直接说： / Open your engine's CLI in a project folder and say:

> 我要研究 X 方向。
>
> I want to research X.

无需记住技能名。Lemvo 会调用 **Research Orchestrator**，梳理目标与可用资源、建立 `research-state.yaml`，再协调内外双循环：内循环构建与验证方法，外循环综合结果并修订下一步。已有项目则读取原状态继续。

No skill name to memorize. Lemvo brings in **Research Orchestrator** to clarify the goal and available resources, create `research-state.yaml`, and coordinate two loops: build and test methods inside; interpret results and revise the next step outside. Existing projects resume from saved state.

### 选择运行模式 / Choose your mode

交互会话内：说"每个实验先给我确认"即半自动；说"自主推进，卡住再找我"即全自动。跨轮次无人值守由各端自带的常驻控制器（`lemvo-run.sh`）承担，交互会话不会因此自行变成常驻进程；详见各端 README 的运行模式说明。

In an interactive session: ask "confirm each experiment with me first" for guided mode, or "run autonomously, come back when stuck" for unattended. Cross-turn unattended runs are handled by each side's resident controller (`lemvo-run.sh`) — an interactive session does not become a resident process by itself. See each side's README for run modes.

GPU 小时与模型费用默认用于统计，不是自动停机上限；用户明确设置的限额与资源权限仍然遵守。

GPU hours and model costs are tracked by default, not used as automatic spending caps. Explicit user limits and resource permissions still apply.

## 许可证 / License

本仓原创内容以 **MIT** 发布（各目录内 [LICENSE](claude/LICENSE)）。第三方组件以各自许可证发布：`academic-research-suite`（vendored ARS，上游 © Cheng-I Wu）为 **CC BY-NC 4.0（仅限非商业使用）**，许可全文见其目录内 LICENSE；其余来源见各端 README 致谢。 / Original content is released under **MIT** (see [LICENSE](claude/LICENSE) in each directory). Third-party components keep their own licenses: `academic-research-suite` (vendored ARS, upstream © Cheng-I Wu) is **CC BY-NC 4.0 (non-commercial only)**; other sources are acknowledged in each side's README.

<sub>感谢开源仓库 / Thanks to [autoresearch](https://github.com/karpathy/autoresearch), [Feynman](https://github.com/Companion-Inc/feynman), and [AI Research Skills](https://github.com/Orchestra-Research/AI-Research-SKILLs).</sub>
