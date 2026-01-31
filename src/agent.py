"""
SWE-bench Green Agent - Orchestrates evaluation of code-fixing agents.

This agent:
1. Receives evaluation requests via A2A protocol
2. Loads tasks from SWE-bench Verified dataset
3. Manages Docker containers for secure bash execution
4. Coordinates multi-turn conversations with solver (Purple) agents
5. Validates patches and reports results
"""

import json
import re
import time
from typing import Any
from pydantic import BaseModel, HttpUrl, ValidationError
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, TaskState, Part, TextPart, DataPart
from a2a.utils import get_message_text, new_agent_text_message

from messenger import Messenger
from swebench import SWEBenchDataset, SWEBenchTask
from docker_validator import DockerValidator
from container_executor import ContainerExecutor, BashResult, PatchResult


# Default timeout and turn limits
DEFAULT_BASH_TIMEOUT = 30  # seconds per bash command
DEFAULT_MAX_TURNS = 10     # max conversation turns before forcing patch
DEFAULT_TASK_TIMEOUT = 600  # overall timeout per task in seconds
DEFAULT_MAX_PATCH_RETRIES = 3  # max patch retry attempts


def parse_solver_response(response: dict) -> tuple[str | None, str | None]:
    """
    Parse the solver response to extract action and content.

    The solver should respond with JSON: {"action": "bash"|"patch"|"debug", "content": "..."}

    Returns:
        tuple[action, content]: The action type and content, or (None, None) if parsing fails
    """
    VALID_ACTIONS = ("bash", "patch", "debug")

    content = response.get("content", "")

    # First, try to parse from the artifact action field
    action = response.get("action", "")
    if action in VALID_ACTIONS:
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
                if action in VALID_ACTIONS:
                    return action, content
        except json.JSONDecodeError:
            pass

        # Try to find JSON in the content (LLM might add extra text)
        json_match = re.search(r'\{[^{}]*"action"\s*:\s*"(bash|patch|debug)"[^{}]*\}', content, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                action = parsed.get("action", "")
                content = parsed.get("content", "")
                if action in VALID_ACTIONS:
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
    """
    Message format sent to the solver agent.

    Note: This contains ONLY the raw issue data. The Purple Agent is responsible
    for all prompting and LLM-specific formatting. This ensures fairness across
    different model implementations.
    """

    cwd: str
    problem_statement: str
    hints_text: str
    python_version: str
    fail_to_pass: list[str]

    @classmethod
    def from_task_and_container(cls, task: SWEBenchTask, container: ContainerExecutor) -> "TaskMessage":
        return cls(
            cwd=container.cwd,
            problem_statement=task.problem_statement,
            hints_text=task.hints_text,
            python_version=container.python_version,
            fail_to_pass=task.fail_to_pass,
        )


class Agent:
    required_roles: list[str] = ["solver"]
    required_config_keys: list[str] = []

    def __init__(self):
        self.messenger = Messenger()
        self.dataset = SWEBenchDataset()
        self.container: ContainerExecutor | None = None

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
        """Get tasks based on config filters (AND logic)."""
        # instance_id is exclusive - returns only that task
        if instance_id := config.get("instance_id"):
            task = self.dataset.get_task_by_id(instance_id)
            return [task] if task else []

        # Start with all tasks
        tasks = list(self.dataset.iter_tasks())

        # Apply repo filter (AND)
        if repo := config.get("repo"):
            tasks = [t for t in tasks if t.repo == repo]

        # Apply difficulty filter (AND)
        if difficulty := config.get("difficulty"):
            difficulty_ids = {t.instance_id for t in self.dataset.get_tasks_by_difficulty(difficulty)}
            tasks = [t for t in tasks if t.instance_id in difficulty_ids]

        # Apply max_tasks limit
        max_tasks = config.get("max_tasks", 1000) # get all by default
        return tasks[:max_tasks]

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
        match = re.search(r"```(?:diff)?\s*(diff --git[\s\S]*?)```", solver_response)
        if match:
            return match.group(1).strip()

        return None
    
    # Helper function to calculate pass@k
    def calculate_pass_at_k(self, results_by_instances, k):
        """Check if at least one of first k attempts succeeded (score == 1.0)"""
        passed = 0
        for _, attempts in results_by_instances.items():
            attempts_sorted = sorted(attempts, key=lambda x: x["attempt"])
            if any(att.get("score", 0.0) == 1.0 for att in attempts_sorted[:k]):
                passed += 1
        return passed / len(results_by_instances) if results_by_instances else 0.0

    async def validate_patch(
        self, task: SWEBenchTask, updater: TaskUpdater
    ) -> dict[str, Any]:
        """Validate a patch by running tests in the existing container.

        The patch should already be applied in self.container.
        This just runs the tests - no cloning, installing, or re-patching.
        """
        if not self.container or not self.container.container_id:
            return {
                "patch_applied": False,
                "install_success": False,
                "tests_passed": 0,
                "tests_failed": 0,
                "score": 0.0,
                "errors": ["No container available for validation"],
                "test_details": {},
            }

        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"Validating patch for {task.instance_id}..."),
        )

        # Create validator with the existing container
        validator = DockerValidator(container_id=self.container.container_id)
        result = validator.validate_task(task)

        return {
            "patch_applied": result.patch_applied,
            "install_success": result.install_success,
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "score": result.score,
            "errors": result.errors,
            "test_details": result.test_results,
        }

    def _format_bash_result(self, result: BashResult) -> dict:
        """Format bash result as structured output for the solver."""
        return {
            "cwd": result.cwd,
            "stdout": result.stdout,
            "stderr": result.stderr
        }

    def _format_patch_failure(self, result: PatchResult) -> dict:
        """Format patch failure for retry."""
        return {
            "patch_failed": True,
            "cwd": result.cwd,
            "stderr": result.stderr,
            "message": "Patch application failed. Please review the error and try again."
        }

    async def run_multi_turn_conversation(
        self,
        task: SWEBenchTask,
        solver_url: str,
        updater: TaskUpdater,
        max_turns: int = DEFAULT_MAX_TURNS,
        bash_timeout: int = DEFAULT_BASH_TIMEOUT,
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
        max_patch_retries: int = DEFAULT_MAX_PATCH_RETRIES,
    ) -> dict[str, Any]:
        """
        Run a multi-turn conversation with the solver agent.

        The solver can:
        1. Issue bash commands to explore the codebase (executed in Docker)
        2. Submit patches (with retry on failure)

        Args:
            task: The SWEBenchTask to solve
            solver_url: URL of the solver agent
            updater: TaskUpdater for status updates
            max_turns: Maximum number of conversation turns
            bash_timeout: Timeout for each bash command
            task_timeout: Overall timeout for the entire task
            max_patch_retries: Maximum patch retry attempts

        Returns:
            dict with keys: patch, turns, conversation_history, error
        """

        conversation_history = []
        patch = None
        turn = 0
        patch_attempts = 0
        start_time = time.time()
        bash_stdout_chars = 0  # Track total stdout characters sent to solver

        # Start container for this task
        self.container = ContainerExecutor()
        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"Starting container for {task.instance_id}..."),
        )

        task.apply_test_patch = True # Apply test patch if available when setting up task
        task.run_tests = True # Get baseline tests run in container
        success, error = await self.container.start(task)
        if not success:
            return {
                "patch": None,
                "turns": 0,
                "conversation_history": [],
                "bash_stdout_chars": 0,
                "error": f"Failed to start container: {error}"
            }
        
        # Create task message with raw issue data only
        # Purple Agent handles all prompting
        task_message = TaskMessage.from_task_and_container(task, self.container)
        initial_message = task_message.model_dump_json()

        try:
            # Send initial task data (new conversation)
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(f"[Turn {turn + 1}] Sending task to solver..."),
            )

            try:
                response = await self.messenger.talk_to_agent(
                    initial_message, solver_url, new_conversation=True, timeout=120
                )
            except Exception as e:
                return {
                    "patch": None,
                    "turns": turn,
                    "conversation_history": conversation_history,
                    "bash_stdout_chars": bash_stdout_chars,
                    "error": f"Failed to send initial message: {e}"
                }

            conversation_history.append({
                "turn": turn,
                "role": "green",
                "content": "[task data sent]",
            })

            while turn < max_turns:
                # Check overall timeout
                elapsed = time.time() - start_time
                if elapsed > task_timeout:
                    return {
                        "patch": None,
                        "turns": turn,
                        "conversation_history": conversation_history,
                        "bash_stdout_chars": bash_stdout_chars,
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
                    # Solver submitted a patch - try to apply it
                    patch_attempts += 1
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(f"[Turn {turn}] Applying patch (attempt {patch_attempts})..."),
                    )

                    patch_result = await self.container.apply_patch(content)

                    if patch_result.success:
                        # Patch applied successfully
                        patch = content
                        print(f"✅ PATCH APPLIED SUCCESSFULLY (Turn {turn})")
                        print(f"{'='*60}")
                        print(f"Patch content preview:\n{content[:500]}...")
                        print(f"{'='*60}\n")
                        await updater.update_status(
                            TaskState.working,
                            new_agent_text_message(f"[Turn {turn}] Patch applied successfully"),
                        )

                        # Run validation NOW while container is still alive
                        validation = await self.validate_patch(task, updater)

                        return {
                            "patch": patch,
                            "turns": turn,
                            "conversation_history": conversation_history,
                            "bash_stdout_chars": bash_stdout_chars,
                            "validation": validation,
                            "error": None
                        }
                    else:
                        # Patch failed - check if we can retry
                        if patch_attempts >= max_patch_retries:
                            # Max retries reached
                            return {
                                "patch": None,
                                "turns": turn,
                                "conversation_history": conversation_history,
                                "bash_stdout_chars": bash_stdout_chars,
                                "error": f"Patch failed after {patch_attempts} attempts: {patch_result.stderr}"
                            }

                        # Send failure feedback to solver for retry
                        feedback = self._format_patch_failure(patch_result)
                        conversation_history.append({
                            "turn": turn,
                            "role": "green",
                            "type": "patch_failure",
                            "content": f"Patch failed: {patch_result.stderr[:200]}",
                        })

                        try:
                            response = await self.messenger.talk_to_agent(
                                json.dumps(feedback), solver_url, new_conversation=False, timeout=120
                            )
                        except Exception as e:
                            return {
                                "patch": None,
                                "turns": turn,
                                "conversation_history": conversation_history,
                                "bash_stdout_chars": bash_stdout_chars,
                                "error": f"Failed to send patch failure feedback: {e}"
                            }

                elif action == "bash":
                    # Execute the bash command in container
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(f"[Turn {turn}] Executing: {content[:50]}..."),
                    )

                    bash_result = await self.container.execute_bash(content, timeout=bash_timeout)

                    # Format structured response
                    bash_output = self._format_bash_result(bash_result)

                    # Track stdout characters sent to solver
                    bash_stdout_chars += len(bash_result.stdout) if bash_result.stdout else 0

                    conversation_history.append({
                        "turn": turn,
                        "role": "green",
                        "type": "bash_result",
                        "cwd": bash_result.cwd,
                        "content": bash_result.stdout[:200] if bash_result.stdout else bash_result.stderr[:200],
                    })

                    # Send bash output back to solver
                    try:
                        response = await self.messenger.talk_to_agent(
                            json.dumps(bash_output), solver_url, new_conversation=False, timeout=120
                        )
                    except Exception as e:
                        return {
                            "patch": None,
                            "turns": turn,
                            "conversation_history": conversation_history,
                            "bash_stdout_chars": bash_stdout_chars,
                            "error": f"Failed to send bash result: {e}"
                        }

                elif action == "debug":
                    # Debug action: run bash commands in an isolated container with write access
                    # Content is the bash command to run (can modify files, changes are rolled back)
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(f"[Turn {turn}] Running debug session..."),
                    )

                    debug_command = content if content else "echo 'No command specified'"

                    debug_result = await self.container.execute_debug(
                        command=debug_command,
                        timeout=bash_timeout
                    )

                    # Track stdout characters sent to solver (same counter for bash and debug)
                    bash_stdout_chars += len(debug_result.stdout) if debug_result.stdout else 0

                    # Format response
                    debug_output = {
                        "debug_result": True,
                        "cwd": debug_result.cwd,
                        "stdout": debug_result.stdout,
                        "stderr": debug_result.stderr,
                        "success": debug_result.success,
                        "note": "This was a debug session. Changes were NOT applied to the main environment."
                    }

                    conversation_history.append({
                        "turn": turn,
                        "role": "green",
                        "type": "debug_result",
                        "cwd": debug_result.cwd,
                        "content": debug_result.stdout[:200] if debug_result.stdout else debug_result.stderr[:200],
                    })

                    # Send debug output back to solver
                    try:
                        response = await self.messenger.talk_to_agent(
                            json.dumps(debug_output), solver_url, new_conversation=False, timeout=120
                        )
                    except Exception as e:
                        return {
                            "patch": None,
                            "turns": turn,
                            "conversation_history": conversation_history,
                            "bash_stdout_chars": bash_stdout_chars,
                            "error": f"Failed to send debug result: {e}"
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

                    error_feedback = {
                        "error": "Invalid response format",
                        "message": "Please respond with JSON: {\"action\": \"bash\"|\"patch\"|\"debug\", \"content\": \"...\"}",
                        "cwd": self.container.cwd
                    }

                    try:
                        response = await self.messenger.talk_to_agent(
                            json.dumps(error_feedback), solver_url, new_conversation=False, timeout=120
                        )
                    except Exception as e:
                        return {
                            "patch": None,
                            "turns": turn,
                            "conversation_history": conversation_history,
                            "bash_stdout_chars": bash_stdout_chars,
                            "error": f"Failed to send error feedback: {e}"
                        }

            # If we exhausted turns without getting a patch
            # (patch is always None here since successful patches return early with validation)
            return {
                "patch": None,
                "turns": turn,
                "conversation_history": conversation_history,
                "bash_stdout_chars": bash_stdout_chars,
                "error": f"Max turns ({max_turns}) reached without successful patch"
            }

        finally:
            # Always clean up container
            if self.container:
                await self.container.stop()
                self.container = None

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
                "task_timeout": 600,  # optional: overall timeout per task (default: 600s)
                "max_patch_retries": 3,  # optional: max patch retry attempts (default: 3)
                "max_attempts": 1  # optional: max attempts per task for pass@k (default: 1 for pass@1, use 3 for pass@3)
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
        max_patch_retries = request.config.get("max_patch_retries", DEFAULT_MAX_PATCH_RETRIES)
        max_attempts = request.config.get("max_attempts", 1)  # pass@k: 1 for pass@1, 3 for pass@3

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

            # Run up to max_attempts for pass@k evaluation
            for attempt in range(1, max_attempts + 1):
                attempt_str = f" (attempt {attempt}/{max_attempts})" if max_attempts > 1 else ""
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(
                        f"[{i+1}/{len(tasks)}] Starting evaluation for {task.instance_id}{attempt_str}..."
                    ),
                )

                result_entry = {
                    "instance_id": task.instance_id,
                    "repo": task.repo,
                    "fail_to_pass": task.fail_to_pass,
                    "attempt": attempt,
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
                        max_patch_retries=max_patch_retries,
                    )

                    result_entry["turns"] = conversation_result["turns"]
                    result_entry["conversation_history"] = conversation_result["conversation_history"]
                    result_entry["bash_stdout_chars"] = conversation_result.get("bash_stdout_chars", 0)

                    patch = conversation_result["patch"]
                    validation = conversation_result.get("validation")
                    error = conversation_result.get("error")

                    # Conditional messaging based on success/failure
                    if patch and validation:
                        score = validation.get("score", 0.0)
                        if score == 1.0:
                            print(f"[{task.instance_id}] Successfully resolved after {conversation_result['turns']} turns")
                        else:
                            print(f"[{task.instance_id}] Patch applied (score: {score:.1%}) after {conversation_result['turns']} turns")
                    elif patch:
                        print(f"[{task.instance_id}] Patch extracted but validation failed after {conversation_result['turns']} turns")
                    elif error:
                        print(f"[{task.instance_id}] Failed after {conversation_result['turns']} turns: {error}")
                    else:
                        print(f"[{task.instance_id}] No patch extracted after {conversation_result['turns']} turns")

                    if patch and validation:
                        # Validation was done inside run_multi_turn_conversation
                        result_entry["patch"] = patch
                        result_entry["validation"] = validation
                        result_entry["status"] = "validated"
                        result_entry["score"] = validation.get("score", 0.0)

                        # Log validation results
                        print(f"🧪 VALIDATION RESULTS for {task.instance_id}{attempt_str}")
                        print(f"{'='*60}")
                        print(f"Score: {validation.get('score', 0.0):.2%}")
                        print(f"Tests passed: {validation.get('tests_passed', 0)}")
                        print(f"Tests failed: {validation.get('tests_failed', 0)}")
                        if validation.get('score', 0.0) == 1.0:
                            print(f"🎉 ALL TESTS PASSED!")
                        print(f"{'='*60}\n")
                    else:
                        result_entry["status"] = "no_patch"
                        result_entry["score"] = 0.0
                        result_entry["error"] = conversation_result.get("error", "No patch extracted")

                except Exception as e:
                    result_entry["status"] = "error"
                    result_entry["score"] = 0.0
                    result_entry["error"] = str(e)

                # Store all independent attempts for pass@k
                results.append(result_entry)

                # Reset messenger context for next attempt
                self.messenger.reset()

            # Reset messenger context for next task
            self.messenger.reset()

        # ============== PASS@K CALCULATION ==============       
        # Group results by instance
        results_by_instance = {}
        for r in results:
            instance_id = r["instance_id"]
            if instance_id not in results_by_instance:
                results_by_instance[instance_id] = []
            results_by_instance[instance_id].append(r)

        # Calculate pass@k for all k from 1 to max_attempts
        pass_at_k_results = {}
        for k in range(1, max_attempts + 1):
            pass_at_k_results[f"pass@{k}"] = self.calculate_pass_at_k(results_by_instance, k)
        
        # pass@1 is the official resolve rate (SWE-bench metric)
        resolve_rate = pass_at_k_results.get("pass@1", 0.0)
        resolved = int(resolve_rate * len(results_by_instance))
        total_instances = len(results_by_instance)
        
        # Get best attempt per instance for aggregate metrics
        best_results = []
        for instance_id, attempts in results_by_instance.items():
            best = max(attempts, key=lambda x: x.get("score", 0.0))
            best_results.append(best)
        
        # Summary stats from best results per instance
        validated = sum(1 for r in best_results if r["status"] == "validated")
        no_patch = sum(1 for r in best_results if r["status"] == "no_patch")
        errors = sum(1 for r in best_results if r["status"] == "error")
        total_score = sum(r.get("score", 0.0) for r in best_results)
        avg_score = total_score / len(best_results) if best_results else 0.0
        avg_turns = sum(r.get("turns", 0) for r in best_results) / len(best_results) if best_results else 0

        # Count tests passed across all validated results
        tests_passed = sum(
            r.get("validation", {}).get("tests_passed", 0)
            for r in best_results
            if r["status"] == "validated"
        )
        tests_failed = sum(
            r.get("validation", {}).get("tests_failed", 0)
            for r in best_results
            if r["status"] == "validated"
        )

        # Total bash stdout characters sent to solver (proxy for token usage)
        total_bash_stdout_chars = sum(r.get("bash_stdout_chars", 0) for r in best_results)
        avg_bash_stdout_chars = total_bash_stdout_chars / len(results) if best_results else 0

        pass_at_k_str = ", ".join([f"pass@{k}: {pass_at_k_results[f'pass@{k}']:.2%}" for k in range(1, max_attempts + 1)])

        summary_text = (
            f"Evaluation complete (pass@{max_attempts}):\n"
            f"- Tasks: {len(best_results)} total, {validated} validated, {no_patch} no patch, {errors} errors\n"
            f"- Tests: {tests_passed} passed, {tests_failed} failed\n"
            f"- Average Best-of-{max_attempts} Score: {avg_score:.2%}\n"
            f"- Resolve Rate (pass@1): {resolve_rate:.2%} ({resolved}/{total_instances} instances fully resolved)\n"
            f"- Pass@k metrics: {pass_at_k_str}\n"
            f"- Average Best-of-{max_attempts} turns: {avg_turns:.1f}\n"
            f"- Avg Best-of-{max_attempts} bash stdout chars per task: {avg_bash_stdout_chars:,.0f}"
        )

        # Print final summary to console
        print(f"\n{'#'*60}")
        print(f"{'#'*60}")
        print(f"##  FINAL EVALUATION SUMMARY")
        print(f"{'#'*60}")
        print(summary_text)
        print(f"{'#'*60}")
        print(f"{'#'*60}\n")

        await updater.add_artifact(
            parts=[
                Part(root=TextPart(text=summary_text)),
                Part(
                    root=DataPart(
                        data={
                            "total_tasks": len(best_results),
                            "validated": validated,
                            "no_patch": no_patch,
                            "errors": errors,
                            "tests_passed": tests_passed,
                            "tests_failed": tests_failed,
                            "average_best_of_k_score": avg_score,
                            "average_turns": avg_turns,
                            "resolved": resolved,
                            "resolve_rate": resolve_rate,
                            "pass_at_k": pass_at_k_results,
                            "max_attempts": max_attempts,
                            "avg_bash_stdout_chars": avg_bash_stdout_chars,
                            "results": results,
                        }
                    )
                ),
            ],
            name="SWE-bench Evaluation Results",
        )
