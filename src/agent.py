import json
from typing import Any
from pydantic import BaseModel, HttpUrl, ValidationError
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, TaskState, Part, TextPart, DataPart
from a2a.utils import get_message_text, new_agent_text_message

from messenger import Messenger
from swebench import SWEBenchDataset, SWEBenchTask


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


class Agent:
    required_roles: list[str] = ["solver"]
    required_config_keys: list[str] = []

    def __init__(self):
        self.messenger = Messenger()
        self.dataset = SWEBenchDataset()

    def validate_request(self, request: EvalRequest) -> tuple[bool, str]:
        missing_roles = set(self.required_roles) - set(request.participants.keys())
        if missing_roles:
            return False, f"Missing roles: {missing_roles}"

        missing_config_keys = set(self.required_config_keys) - set(request.config.keys())
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
            new_agent_text_message("Loading SWE-bench Verified dataset...")
        )

        try:
            self.dataset.load()
        except Exception as e:
            await updater.failed(new_agent_text_message(f"Failed to load dataset: {e}"))
            return

        # Get tasks based on config
        tasks = self.get_tasks(request.config)
        if not tasks:
            await updater.failed(new_agent_text_message("No tasks found matching criteria"))
            return

        await updater.update_status(
            TaskState.working,
            new_agent_text_message(f"Found {len(tasks)} task(s) to evaluate")
        )

        results = []

        for i, task in enumerate(tasks):
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(f"[{i+1}/{len(tasks)}] Sending task {task.instance_id} to solver...")
            )

            # Create message for solver
            task_message = TaskMessage.from_task(task)

            try:
                # Send task to solver agent
                response = await self.messenger.talk_to_agent(
                    task_message.model_dump_json(),
                    solver_url
                )

                results.append({
                    "instance_id": task.instance_id,
                    "repo": task.repo,
                    "status": "completed",
                    "solver_response": response,
                    "expected_patch": task.patch,
                    "fail_to_pass": task.fail_to_pass,
                })
            except Exception as e:
                results.append({
                    "instance_id": task.instance_id,
                    "repo": task.repo,
                    "status": "error",
                    "error": str(e),
                })

        # Summary
        completed = sum(1 for r in results if r["status"] == "completed")
        failed = sum(1 for r in results if r["status"] == "error")

        await updater.add_artifact(
            parts=[
                Part(root=TextPart(text=f"Evaluation complete: {completed}/{len(results)} tasks completed, {failed} errors")),
                Part(root=DataPart(data={
                    "total_tasks": len(results),
                    "completed": completed,
                    "failed": failed,
                    "results": results,
                }))
            ],
            name="SWE-bench Evaluation Results",
        )
