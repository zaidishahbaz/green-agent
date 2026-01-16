# AgentSWE  
**Agentifying SWE-bench-Verified**

---

## Overview

**AgentSWE** is a multi-agent re-architecture of **SWE-bench-Verified** that evaluates *software bug-fixing ability* using **Agent-to-Agent (A2A)** communication, while deliberately avoiding model-specific tool dependencies.

The core idea is simple:

> Measure **bug-fixing skill**, not **tool fluency**.

AgentSWE cleanly separates responsibilities between agents and constrains interaction to well-defined execution modes, enabling fairer, more interpretable evaluation of autonomous software engineering agents.

---

## Architecture Overview

![AgentSWE Architecture](docs/images/agentswe_architecture.png)

*Figure 1: High-level architecture showing Green and Purple agents communicating via A2A artifacts.*

---

## SWEAgent at a Glance

AgentSWE is built around three core principles:

- **Clean Separation of Concerns**
- **3 Interactive Execution Modes**
- **No Extra / Custom Tools Required**

We additionally introduce **token efficiency** as a first-class evaluation signal alongside resolution accuracy.

---

## Agent Roles & Responsibilities


There is a clear Separation of responsibilities between Green (benchmark) and Purple (solver) agents.

### Green Agent
- Acts as the **benchmark** and **orchestrator**
- Sends **issue details and repository metadata** to the Purple Agent via A2A
- Executes commands on behalf of the Purple Agent
- Communicates exclusively via **A2A artifacts**

### Purple Agent
- Acts as the **reasoning and decision-making agent**
- Generates:
  - Instruction prompts
  - Commands
  - Patch outputs
- Never directly mutates the repository outside controlled modes
- Communicates results back via **A2A artifacts**

This separation ensures:
- Deterministic artifacts sent to both green and purple agent
- Clear responsibility boundaries
- Reproducible evaluation behavior

---

## Interactive Execution Modes


![Execution Modes](docs/images/agent_interaction_flow.png)

*Figure 3: Controlled interaction modes used by the Purple Agent.*

The Purple Agent interacts with the repository through **three explicit modes**:

### 1. Bash Mode (Read-Only)
- Used for repository exploration
- Supports standard bash and git commands
- No filesystem mutation allowed

### 2. Debug Mode (Ephemeral Writes)
- Temporary write access for debugging
- Executed in an **isolated copy** of the repository
- All changes are discarded after execution

### 3. Patch Mode (Final Fix)
- Purple Agent submits a **git-style patch**
- Patch is applied once, deterministically
- Represents the agent’s final answer

This design enforces a clean progression:

> *Explore → Test → Fix*

---

## No Extra Tools Needed

Existing SWE-bench harnesses often rely on **custom tools** (search APIs, repo-specific helpers, etc.), which tightly couple benchmark performance to a model’s ability to use those tools.

AgentSWE removes this dependency.

- No custom search tools
- No bespoke file inspection APIs
- Only **standard bash and git commands**

As a result:
- Models are evaluated on **software reasoning**, not tool mastery
- Results are more comparable across agents
- Benchmark behavior is easier to interpret and reproduce

---

## Evaluation Metrics

AgentSWE tracks both **effectiveness** and **efficiency**.

Key Idea: A **preferred** agent is the purple agent that has a high Resolved Rate and requests fewer number of tokens.

Hence, following are the Key Metrics we track:

### Existing Metrics (from SWE-bench-Verified)
- **Resolved Rate**
  - pass@1
  - pass@3

### New Metric Introduced by AgentSWE
- **# Tokens Requested**
  - Total tokens requested by the Purple Agent
  - Includes tokens used in:
    - Bash mode
    - Debug mode


This captures the trade-off between **reasoning quality** and **computational efficiency**.

We also capture total number of FAIL_TO_PASS, PASS_TO_PASS, average number of turns.

---

## Why AgentSWE

AgentSWE provides:

- A cleaner abstraction for autonomous SWE agents
- A tool-agnostic benchmark design
- Better signals for real-world agent usefulness
- A natural fit for **A2A-based multi-agent systems**

It reframes SWE-bench-Verified from a *tool-centric* benchmark into an **agent-centric** one.

---

## License

This project is licensed under the **MIT License**.  
See the [`LICENSE`](LICENSE) file for details.

---


## Attribution

If you use or build on this work, please attribute:

**AgentSWE – Agentifying SWE-bench-Verified**  



# A2A Agent Template

A minimal template for building [A2A (Agent-to-Agent)](https://a2a-protocol.org/latest/) green agents compatible with the [AgentBeats](https://agentbeats.dev) platform.

## Project Structure

```
src/
├─ server.py      # Server setup and agent card configuration
├─ executor.py    # A2A request handling
├─ agent.py       # Your agent implementation goes here
└─ messenger.py   # A2A messaging utilities
tests/
└─ test_agent.py  # Agent tests
Dockerfile        # Docker configuration
pyproject.toml    # Python dependencies
.github/
└─ workflows/
   └─ test-and-publish.yml # CI workflow
```

## Getting Started

1. **Create your repository** - Click "Use this template" to create your own repository from this template

2. **Implement your agent** - Add your agent logic to [`src/agent.py`](src/agent.py)

3. **Configure your agent card** - Fill in your agent's metadata (name, skills, description) in [`src/server.py`](src/server.py)

4. **Write your tests** - Add custom tests for your agent in [`tests/test_agent.py`](tests/test_agent.py)

For a concrete example of implementing a green agent using this template, see this [draft PR](https://github.com/RDI-Foundation/green-agent-template/pull/3).

## Running Locally

```bash
# Install dependencies
uv sync

# Run the server
uv run src/server.py
```

## Running with Docker

```bash
# Build the image
docker build -t my-agent .

# Run the container
docker run -p 9009:9009 my-agent
```

## Testing

Run A2A conformance tests against your agent.

```bash
# Install test dependencies
uv sync --extra test

# Start your agent (uv or docker; see above)

# Run tests against your running agent URL
uv run pytest --agent-url http://localhost:9009
```

## Publishing

The repository includes a GitHub Actions workflow that automatically builds, tests, and publishes a Docker image of your agent to GitHub Container Registry.

If your agent needs API keys or other secrets, add them in Settings → Secrets and variables → Actions → Repository secrets. They'll be available as environment variables during CI tests.

- **Push to `main`** → publishes `latest` tag:
```
ghcr.io/<your-username>/<your-repo-name>:latest
```

- **Create a git tag** (e.g. `git tag v1.0.0 && git push origin v1.0.0`) → publishes version tags:
```
ghcr.io/<your-username>/<your-repo-name>:1.0.0
ghcr.io/<your-username>/<your-repo-name>:1
```

Once the workflow completes, find your Docker image in the Packages section (right sidebar of your repository). Configure the package visibility in package settings.

> **Note:** Organization repositories may need package write permissions enabled manually (Settings → Actions → General). Version tags must follow [semantic versioning](https://semver.org/) (e.g., `v1.0.0`).
