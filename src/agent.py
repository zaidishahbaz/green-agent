import json
import re
import shlex
import subprocess
import time
from typing import Any
from pydantic import BaseModel, HttpUrl, ValidationError
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, TaskState, Part, TextPart, DataPart
from a2a.utils import get_message_text, new_agent_text_message

from messenger import Messenger
from swebench import SWEBenchDataset, SWEBenchTask
from docker_validator import DockerValidator


# =============================================================================
# Safe Bash Execution Configuration
# =============================================================================

# Allowed read-only commands (whitelist approach)
ALLOWED_COMMANDS = {
    # File listing and navigation
    "ls", "pwd", "tree", "find", "locate", "which", "whereis", "file", "stat",
    # File reading (read-only)
    "cat", "head", "tail", "less", "more", "bat",
    # Text searching
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    # Text processing (read-only, outputs to stdout)
    "wc", "sort", "uniq", "cut", "awk", "sed", "tr", "diff", "comm",
    # Git commands (read-only)
    "git",
    # Python/code inspection
    "python", "python3",
    # Directory info
    "du", "df",
    # Environment
    "env", "printenv", "echo",
}

# Git subcommands that are allowed (read-only operations)
ALLOWED_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "tag", "describe",
    "ls-files", "ls-tree", "cat-file", "rev-parse", "rev-list",
    "blame", "grep", "shortlog", "config", "remote",
}

# Python flags that are safe (read-only inspection)
ALLOWED_PYTHON_FLAGS = {"-c", "-m", "--version", "-V"}

# Patterns that indicate dangerous operations (block these)
DANGEROUS_PATTERNS = [
    r">\s*[^&]",      # Output redirection (but not >>)
    r">>",            # Append redirection
    r"\|.*(?:rm|mv|cp|chmod|chown|dd|mkfs|wget|curl.*-o)",  # Pipe to dangerous commands
    r";\s*(?:rm|mv|cp|chmod|chown)",  # Command chaining with dangerous commands
    r"`[^`]+`",       # Command substitution with backticks
    r"\$\([^)]+\)",   # Command substitution with $()
    r"&&\s*(?:rm|mv|cp|chmod|chown)",  # AND chaining with dangerous commands
    r"\|\|\s*(?:rm|mv|cp|chmod|chown)",  # OR chaining with dangerous commands
]

# Default timeout and turn limits
DEFAULT_BASH_TIMEOUT = 30  # seconds per bash command
DEFAULT_MAX_TURNS = 10     # max conversation turns before forcing patch
DEFAULT_TASK_TIMEOUT = 600  # overall timeout per task in seconds


def is_safe_bash_command(command: str) -> tuple[bool, str]:
    """
    Validate that a bash command is safe to execute (read-only/execute mode).

    Returns:
        tuple[bool, str]: (is_safe, error_message)
    """
    if not command or not command.strip():
        return False, "Empty command"

    command = command.strip()

    # Check for dangerous patterns first
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return False, f"Command contains dangerous pattern: {pattern}"

    # Parse the command to get the base command
    try:
        # Handle simple command parsing
        parts = shlex.split(command)
        if not parts:
            return False, "Could not parse command"
        base_cmd = parts[0]
    except ValueError as e:
        return False, f"Command parsing error: {e}"

    # Check if base command is in whitelist
    if base_cmd not in ALLOWED_COMMANDS:
        return False, f"Command '{base_cmd}' is not in the allowed list. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"

    # Special handling for git commands
    if base_cmd == "git":
        if len(parts) < 2:
            return True, ""  # Just "git" is fine
        git_subcmd = parts[1]
        if git_subcmd not in ALLOWED_GIT_SUBCOMMANDS:
            return False, f"Git subcommand '{git_subcmd}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}"

    # Special handling for python commands - only allow inspection
    if base_cmd in ("python", "python3"):
        if len(parts) < 2:
            return False, "Python command requires arguments"
        # Check if it's a safe flag or just reading a file
        flag = parts[1]
        if flag.startswith("-"):
            if flag not in ALLOWED_PYTHON_FLAGS:
                return False, f"Python flag '{flag}' is not allowed"
            # For -c, ensure it's not doing anything dangerous
            if flag == "-c" and len(parts) > 2:
                code = parts[2]
                # Only allow simple inspection operations
                if any(kw in code.lower() for kw in ["open(", "write", "os.", "subprocess", "import os", "exec", "eval"]):
                    return False, "Python -c command contains potentially dangerous operations"

    return True, ""


def execute_bash_command(command: str, cwd: str = None, timeout: int = DEFAULT_BASH_TIMEOUT) -> dict:
    """
    Execute a safe bash command and return the result.

    Args:
        command: The bash command to execute
        cwd: Working directory for the command
        timeout: Timeout in seconds

    Returns:
        dict with keys: success, stdout, stderr, error
    """
    # Validate command first
    is_safe, error_msg = is_safe_bash_command(command)
    if not is_safe:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "error": f"Command blocked: {error_msg}"
        }

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:10000] if result.stdout else "",  # Limit output size
            "stderr": result.stderr[:2000] if result.stderr else "",
            "error": None
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "error": f"Command timed out after {timeout} seconds"
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "error": str(e)
        }


def parse_solver_response(response: dict) -> tuple[str | None, str | None]:
    """
    Parse the solver response to extract action and content.

    The solver should respond with JSON: {"action": "bash"|"patch", "content": "..."}

    Returns:
        tuple[action, content]: The action type and content, or (None, None) if parsing fails
    """
    content = response.get("content", "")

    # First, try to parse from the artifact action field
    action = response.get("action", "")
    if action in ("bash", "patch"):
        return action, content

    # Try to parse content as JSON
    if isinstance(content, str):
        # Try to extract JSON from the content
        try:
            # Check if the content itself is JSON
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                action = parsed.get("action", "")
                content = parsed.get("content", "")
                if action in ("bash", "patch"):
                    return action, content
        except json.JSONDecodeError:
            pass

        # Try to find JSON in the content (LLM might add extra text)
        json_match = re.search(r'\{[^{}]*"action"\s*:\s*"(bash|patch)"[^{}]*\}', content, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                action = parsed.get("action", "")
                content = parsed.get("content", "")
                if action in ("bash", "patch"):
                    return action, content
            except json.JSONDecodeError:
                pass

        # Check if it's a raw diff (fallback)
        if content.strip().startswith("diff --git") or content.strip().startswith("--- "):
            return "patch", content.strip()

    return None, content


class EvalRequest(BaseModel):
    """Request format sent by the AgentBeats platform to green agents."""

    participants: dict[str, HttpUrl]  # role -> agent URL
    config: dict[str, Any]


class TaskMessage(BaseModel):
    """Message format sent to the solver agent."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    version: str
    fail_to_pass: list[str]

    @classmethod
    def from_task(cls, task: SWEBenchTask) -> "TaskMessage":
        return cls(
            instance_id=task.instance_id,
            repo=task.repo,
            base_commit=task.base_commit,
            problem_statement=task.problem_statement,
            hints_text=task.hints_text,
            version=task.version,
            fail_to_pass=task.fail_to_pass,
        )

    @property
    def agent_prompt(self) -> str:
        # Building minimal, fair and objective system prompt. Currently, 0 shot.
        # TODO: Add support for bash commands. Add prompt for json output

        return f"""You are a software-fixing agent. You are working on a repository to fix a bug.

You may respond in one of two ways:

1. Bash commands to explore or fetch context:
   - Format: {{"action": "bash", "content": "<your shell command>"}}
   - Example: {{"action": "bash", "content": "ls sklearn/metrics"}}
   - Outputs from the command will be returned to you.
   - Only read-only commands are allowed; do not modify files yet.

2. Final patch to submit a fix:
   - Format: {{"action": "patch", "content": "<unified diff>"}}
   - Example: {{"action": "patch", "content": "--- a/foo.py\n+++ b/foo.py\n@@ ..."}}
   - You may generate the patch as a minimal diff; it will be executed for you.

You must output exactly ONE JSON object per response.
The JSON object must contain exactly ONE action.
Do not return arrays, multiple JSON objects, or additional text.
If you need to take multiple steps, wait for the next turn.

Task:
You may alternate between Bash commands and reading outputs as needed. 
When you are confident in your fix, submit a patch. 
Do not perform arbitrary operations outside these formats.
Do not include any explanations.

Here are the Issue Details:

Issue:
{self.problem_statement}

Additional context from issue discussion:
{'N/A' if not self.hints_text else self.hints_text}
"""


class Agent:
    required_roles: list[str] = ["solver"]
    required_config_keys: list[str] = []

    def __init__(self):
        self.messenger = Messenger()
        self.dataset = SWEBenchDataset()
        self.docker_validator = DockerValidator()

    def validate_request(self, request: EvalRequest) -> tuple[bool, str]:
        missing_roles = set(self.required_roles) - set(request.participants.keys())
        if missing_roles:
            return False, f"Missing roles: {missing_roles}"

        missing_config_keys = set(self.required_config_keys) - set(
            request.config.keys()
        )
        if missing_config_keys:
            return False, f"Missing config keys: {missing_config_keys}"

        return True, "ok"

    def get_tasks(self, config: dict[str, Any]) -> list[SWEBenchTask]:
        """Get tasks based on config filters."""
        # If specific instance_id requested, return just that task
        if instance_id := config.get("instance_id"):
            task = self.dataset.get_task_by_id(instance_id)
            return [task] if task else []

        # If specific repo requested, filter by repo
        if repo := config.get("repo"):
            tasks = self.dataset.get_tasks_by_repo(repo)
        # If difficulty filter
        elif difficulty := config.get("difficulty"):
            tasks = self.dataset.get_tasks_by_difficulty(difficulty)
        else:
            tasks = list(self.dataset.iter_tasks())

        # Apply max_tasks limit
        max_tasks = config.get("max_tasks", 1)
        return tasks[:max_tasks]

    # TODO: Remove the keyword 'patch' and instead communicate through
    # the a2a level names, such as 'respond' or 'input-requested' when
    # requesting tool use via bash scripts
    def extract_patch(self, solver_response: str) -> str | None:
        """Extract patch from solver response.

        The solver response may contain:
        1. Raw diff content
        2. JSON with a 'patch' field
        3. Markdown code block with diff
        """

        if not solver_response:
            return None

        # Try parsing as JSON first
        try:
            if isinstance(solver_response, dict) and "action" in solver_response and solver_response["action"] == "patch":
                return solver_response["content"]
        except json.JSONDecodeError:
            pass

        # Check if it's a raw diff
        if solver_response.strip().startswith("diff --git"):
            return solver_response.strip()

        # Try to extract from markdown code block
        import re

        match = re.search(r"```(?:diff)?\s*(diff --git[\s\S]*?)```", solver_response)
        if match:
            return match.group(1).strip()

        return None

    async def validate_patch(
        self, task: SWEBenchTask, patch: str, updater: TaskUpdater
    ) -> dict[str, Any]:
        """Validate a patch using Docker container."""
        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"Validating patch for {task.instance_id}..."),
        )

        result = self.docker_validator.validate_task(task, patch)

        return {
            "patch_applied": result.patch_applied,
            "install_success": result.install_success,
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "score": result.score,
            "errors": result.errors,
            "test_details": result.test_results,
        }

    async def run_multi_turn_conversation(
        self,
        task: SWEBenchTask,
        solver_url: str,
        updater: TaskUpdater,
        max_turns: int = DEFAULT_MAX_TURNS,
        bash_timeout: int = DEFAULT_BASH_TIMEOUT,
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
    ) -> dict[str, Any]:
        """
        Run a multi-turn conversation with the solver agent.

        The solver can:
        1. Issue bash commands (read-only) to explore the codebase
        2. Submit a final patch when ready

        Args:
            task: The SWEBenchTask to solve
            solver_url: URL of the solver agent
            updater: TaskUpdater for status updates
            max_turns: Maximum number of conversation turns
            bash_timeout: Timeout for each bash command
            task_timeout: Overall timeout for the entire task

        Returns:
            dict with keys: patch, turns, conversation_history, error
        """
        task_message = TaskMessage.from_task(task)
        initial_prompt = task_message.agent_prompt

        conversation_history = []
        patch = None
        turn = 0
        start_time = time.time()

        # Send initial prompt (new conversation)
        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"[Turn {turn + 1}] Sending task to solver..."),
        )

        try:
            response = await self.messenger.talk_to_agent(
                initial_prompt, solver_url, new_conversation=True, timeout=120
            )
        except Exception as e:
            return {
                "patch": None,
                "turns": turn,
                "conversation_history": conversation_history,
                "error": f"Failed to send initial message: {e}"
            }

        conversation_history.append({
            "turn": turn,
            "role": "green",
            "content": "[initial prompt]",
        })

        while turn < max_turns:
            # Check overall timeout
            elapsed = time.time() - start_time
            if elapsed > task_timeout:
                return {
                    "patch": None,
                    "turns": turn,
                    "conversation_history": conversation_history,
                    "error": f"Task timed out after {task_timeout} seconds"
                }

            turn += 1

            # Parse the solver's response
            action, content = parse_solver_response(response)

            conversation_history.append({
                "turn": turn,
                "role": "solver",
                "action": action,
                "content": content[:500] if content else None,  # Truncate for history
            })

            print(f"[Turn {turn}] Solver action: {action}")

            if action == "patch":
                # Solver submitted a patch - we're done
                patch = content
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(f"[Turn {turn}] Solver submitted patch"),
                )
                break

            elif action == "bash":
                # Execute the bash command safely
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(f"[Turn {turn}] Executing bash: {content[:50]}..."),
                )

                bash_result = execute_bash_command(content, timeout=bash_timeout)

                # Prepare response to send back to solver
                if bash_result["error"]:
                    bash_response = f"Error: {bash_result['error']}"
                elif bash_result["success"]:
                    bash_response = bash_result["stdout"]
                    if bash_result["stderr"]:
                        bash_response += f"\n[stderr]: {bash_result['stderr']}"
                else:
                    bash_response = f"Command failed (exit code non-zero)\nstdout: {bash_result['stdout']}\nstderr: {bash_result['stderr']}"

                # Truncate response if too long
                if len(bash_response) > 8000:
                    bash_response = bash_response[:8000] + "\n... [output truncated]"

                conversation_history.append({
                    "turn": turn,
                    "role": "green",
                    "type": "bash_result",
                    "content": bash_response[:500],  # Truncate for history
                })

                # Send bash output back to solver (continue conversation)
                feedback_message = f"""Here is the output of your bash command:

```
{bash_response}
```

Continue exploring or submit your patch when ready. Remember:
- Use {{"action": "bash", "content": "<command>"}} for more exploration
- Use {{"action": "patch", "content": "<unified diff>"}} to submit your fix"""

                try:
                    response = await self.messenger.talk_to_agent(
                        feedback_message, solver_url, new_conversation=False, timeout=120
                    )
                except Exception as e:
                    return {
                        "patch": None,
                        "turns": turn,
                        "conversation_history": conversation_history,
                        "error": f"Failed to send bash result: {e}"
                    }

            else:
                # Unknown or no action - prompt solver to respond properly
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(f"[Turn {turn}] Invalid response, prompting solver..."),
                )

                conversation_history.append({
                    "turn": turn,
                    "role": "green",
                    "type": "error_feedback",
                    "content": "Invalid response format",
                })

                feedback_message = f"""Your response was not in the expected format. Please respond with exactly ONE JSON object:

- For exploration: {{"action": "bash", "content": "<command>"}}
- For submitting fix: {{"action": "patch", "content": "<unified diff>"}}

Do not include any other text. Just the JSON object."""

                try:
                    response = await self.messenger.talk_to_agent(
                        feedback_message, solver_url, new_conversation=False, timeout=120
                    )
                except Exception as e:
                    return {
                        "patch": None,
                        "turns": turn,
                        "conversation_history": conversation_history,
                        "error": f"Failed to send error feedback: {e}"
                    }

        # If we exhausted turns without getting a patch
        if patch is None:
            return {
                "patch": None,
                "turns": turn,
                "conversation_history": conversation_history,
                "error": f"Max turns ({max_turns}) reached without patch submission"
            }

        return {
            "patch": patch,
            "turns": turn,
            "conversation_history": conversation_history,
            "error": None
        }

    async def run(self, message: Message, updater: TaskUpdater) -> None:
        """Run SWE-bench evaluation with multi-turn support.

        Expected request format:
        {
            "participants": {"solver": "http://purple-agent:9010/"},
            "config": {
                "instance_id": "astropy__astropy-12907",  # optional: specific task
                "repo": "astropy/astropy",  # optional: filter by repo
                "difficulty": "easy",  # optional: filter by difficulty
                "max_tasks": 10,  # optional: limit number of tasks (default: 1)
                "max_turns": 10,  # optional: max conversation turns per task (default: 10)
                "bash_timeout": 30,  # optional: timeout per bash command (default: 30s)
                "task_timeout": 600  # optional: overall timeout per task (default: 600s)
            }
        }
        """
        input_text = get_message_text(message)

        try:
            request: EvalRequest = EvalRequest.model_validate_json(input_text)
            ok, msg = self.validate_request(request)
            if not ok:
                await updater.reject(new_agent_text_message(msg))
                return
        except ValidationError as e:
            await updater.reject(new_agent_text_message(f"Invalid request: {e}"))
            return

        solver_url = str(request.participants["solver"])

        # Extract config options with defaults
        max_turns = request.config.get("max_turns", DEFAULT_MAX_TURNS)
        bash_timeout = request.config.get("bash_timeout", DEFAULT_BASH_TIMEOUT)
        task_timeout = request.config.get("task_timeout", DEFAULT_TASK_TIMEOUT)

        # Load dataset
        await updater.update_status(
            TaskState.working,
            new_agent_text_message("Loading SWE-bench Verified dataset..."),
        )

        try:
            self.dataset.load()
        except Exception as e:
            await updater.failed(new_agent_text_message(f"Failed to load dataset: {e}"))
            return

        # Get tasks based on config
        tasks = self.get_tasks(request.config)
        if not tasks:
            await updater.failed(
                new_agent_text_message("No tasks found matching criteria")
            )
            return

        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"Found {len(tasks)} task(s) to evaluate"),
        )

        results = []

        for i, task in enumerate(tasks):
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    f"[{i+1}/{len(tasks)}] Starting multi-turn conversation for {task.instance_id}..."
                ),
            )

            result_entry = {
                "instance_id": task.instance_id,
                "repo": task.repo,
                "fail_to_pass": task.fail_to_pass,
            }

            try:
                # Run multi-turn conversation with solver
                conversation_result = await self.run_multi_turn_conversation(
                    task=task,
                    solver_url=solver_url,
                    updater=updater,
                    max_turns=max_turns,
                    bash_timeout=bash_timeout,
                    task_timeout=task_timeout,
                )

                result_entry["turns"] = conversation_result["turns"]
                result_entry["conversation_history"] = conversation_result["conversation_history"]

                patch = conversation_result["patch"]
                print(f"[{task.instance_id}] Extracted patch after {conversation_result['turns']} turns")

                if patch:
                    # Validate the patch using Docker
                    validation = await self.validate_patch(task, patch, updater)
                    result_entry["patch"] = patch
                    result_entry["validation"] = validation
                    result_entry["status"] = "validated"
                    result_entry["score"] = validation.get("score", 0.0)
                else:
                    result_entry["status"] = "no_patch"
                    result_entry["score"] = 0.0
                    result_entry["error"] = conversation_result.get("error", "No patch extracted")

            except Exception as e:
                result_entry["status"] = "error"
                result_entry["score"] = 0.0
                result_entry["error"] = str(e)

            results.append(result_entry)

            # Reset messenger context for next task
            self.messenger.reset()

        # Summary
        validated = sum(1 for r in results if r["status"] == "validated")
        no_patch = sum(1 for r in results if r["status"] == "no_patch")
        errors = sum(1 for r in results if r["status"] == "error")
        total_score = sum(r.get("score", 0.0) for r in results)
        avg_score = total_score / len(results) if results else 0.0
        avg_turns = sum(r.get("turns", 0) for r in results) / len(results) if results else 0

        # Count tests passed across all validated results
        tests_passed = sum(
            r.get("validation", {}).get("tests_passed", 0)
            for r in results
            if r["status"] == "validated"
        )
        tests_failed = sum(
            r.get("validation", {}).get("tests_failed", 0)
            for r in results
            if r["status"] == "validated"
        )

        summary_text = (
            f"Evaluation complete:\n"
            f"- Tasks: {len(results)} total, {validated} validated, {no_patch} no patch, {errors} errors\n"
            f"- Tests: {tests_passed} passed, {tests_failed} failed\n"
            f"- Average score: {avg_score:.2%}\n"
            f"- Average turns: {avg_turns:.1f}"
        )

        await updater.add_artifact(
            parts=[
                Part(root=TextPart(text=summary_text)),
                Part(
                    root=DataPart(
                        data={
                            "total_tasks": len(results),
                            "validated": validated,
                            "no_patch": no_patch,
                            "errors": errors,
                            "tests_passed": tests_passed,
                            "tests_failed": tests_failed,
                            "average_score": avg_score,
                            "average_turns": avg_turns,
                            "results": results,
                        }
                    )
                ),
            ],
            name="SWE-bench Evaluation Results",
        )
