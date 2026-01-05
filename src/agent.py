import json
from typing import Any
from pydantic import BaseModel, HttpUrl, ValidationError
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, TaskState, Part, TextPart, DataPart
from a2a.utils import get_message_text, new_agent_text_message

from messenger import Messenger
from swebench import SWEBenchDataset, SWEBenchTask
from docker_validator import DockerValidator


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

        return f"""Issue:
{self.problem_statement}

Additional context from issue discussion:
{'N/A' if not self.hints_text else self.hints_text}

Task:
Provide a unified diff that fixes the issue.
The diff must apply cleanly to the current codebase.
Output only the diff. Do not include explanations.
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
            data = json.loads(solver_response)
            if isinstance(data, dict) and "patch" in data:
                return data["patch"]
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

    async def run(self, message: Message, updater: TaskUpdater) -> None:
        """Run SWE-bench evaluation.

        Expected request format:
        {
            "participants": {"solver": "http://purple-agent:9010/"},
            "config": {
                "instance_id": "astropy__astropy-12907",  # optional: specific task
                "repo": "astropy/astropy",  # optional: filter by repo
                "difficulty": "easy",  # optional: filter by difficulty
                "max_tasks": 10  # optional: limit number of tasks (default: 1)
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
                    f"[{i+1}/{len(tasks)}] Sending task {task.instance_id} to solver..."
                ),
            )

            # Create message for solver
            task_message = TaskMessage.from_task(task)
            prompt_for_purple_agent = task_message.agent_prompt

            result_entry = {
                "instance_id": task.instance_id,
                "repo": task.repo,
                "fail_to_pass": task.fail_to_pass,
            }

            try:
                # Send task to solver agent
                # response = await self.messenger.talk_to_agent(
                #     task_message.model_dump_json(), solver_url
                # )
                response = await self.messenger.talk_to_agent(
                    prompt_for_purple_agent, solver_url
                )
                result_entry["solver_response"] = response

                # Extract patch from solver response
                patch = self.extract_patch(response)
                print("extracted patch > ", patch)

                if patch:
                    # Validate the patch using Docker
                    validation = await self.validate_patch(task, patch, updater)
                    result_entry["patch"] = patch
                    result_entry["validation"] = validation
                    result_entry["status"] = "validated"
                    result_entry["score"] = validation["score"] if "score" in validation else 0.0
                else:
                    result_entry["status"] = "no_patch"
                    result_entry["score"] = 0.0
                    result_entry["error"] = (
                        "Could not extract patch from solver response"
                    )

            except Exception as e:
                result_entry["status"] = "error"
                result_entry["score"] = 0.0
                result_entry["error"] = str(e)

            results.append(result_entry)

        # Summary
        validated = sum(1 for r in results if r["status"] == "validated")
        no_patch = sum(1 for r in results if r["status"] == "no_patch")
        errors = sum(1 for r in results if r["status"] == "error")
        total_score = sum(r.get("score", 0.0) for r in results)
        avg_score = total_score / len(results) if results else 0.0

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
            f"- Average score: {avg_score:.2%}"
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
                            "results": results,
                        }
                    )
                ),
            ],
            name="SWE-bench Evaluation Results",
        )
