"""SWE-bench Verified dataset loader."""

from dataclasses import dataclass
from datasets import load_dataset


DATASET_NAME = "princeton-nlp/SWE-bench_Verified"


@dataclass
class SWEBenchTask:
    """A single task from SWE-bench Verified."""
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str
    test_patch: str
    hints_text: str
    created_at: str
    version: str
    environment_setup_commit: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    difficulty: str

    @classmethod
    def from_dict(cls, data: dict) -> "SWEBenchTask":
        """Create a SWEBenchTask from a dataset row."""
        import json
        return cls(
            instance_id=data["instance_id"],
            repo=data["repo"],
            base_commit=data["base_commit"],
            problem_statement=data["problem_statement"],
            patch=data["patch"],
            test_patch=data["test_patch"],
            hints_text=data.get("hints_text", ""),
            created_at=data.get("created_at", ""),
            version=data.get("version", ""),
            environment_setup_commit=data.get("environment_setup_commit", ""),
            fail_to_pass=json.loads(data.get("FAIL_TO_PASS", "[]")),
            pass_to_pass=json.loads(data.get("PASS_TO_PASS", "[]")),
            difficulty=data.get("difficulty", ""),
        )


class SWEBenchDataset:
    """Loader for SWE-bench Verified dataset."""

    def __init__(self):
        self._dataset = None

    def load(self) -> None:
        """Load the dataset from HuggingFace."""
        if self._dataset is None:
            self._dataset = load_dataset(DATASET_NAME, split="test")

    @property
    def dataset(self):
        """Get the raw dataset, loading if necessary."""
        if self._dataset is None:
            self.load()
        return self._dataset

    def __len__(self) -> int:
        """Return the number of tasks in the dataset."""
        return len(self.dataset)

    def get_task(self, index: int) -> SWEBenchTask:
        """Get a task by index."""
        return SWEBenchTask.from_dict(self.dataset[index])

    def get_task_by_id(self, instance_id: str) -> SWEBenchTask | None:
        """Get a task by its instance_id."""
        for row in self.dataset:
            if row["instance_id"] == instance_id:
                return SWEBenchTask.from_dict(row)
        return None

    def iter_tasks(self):
        """Iterate over all tasks."""
        for row in self.dataset:
            yield SWEBenchTask.from_dict(row)

    def get_repos(self) -> list[str]:
        """Get list of unique repositories in the dataset."""
        return list(set(row["repo"] for row in self.dataset))

    def get_tasks_by_repo(self, repo: str) -> list[SWEBenchTask]:
        """Get all tasks for a specific repository."""
        return [
            SWEBenchTask.from_dict(row)
            for row in self.dataset
            if row["repo"] == repo
        ]

    def get_tasks_by_difficulty(self, difficulty: str) -> list[SWEBenchTask]:
        """Get all tasks with a specific difficulty."""
        return [
            SWEBenchTask.from_dict(row)
            for row in self.dataset
            if row.get("difficulty") == difficulty
        ]
