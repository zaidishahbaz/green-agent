from typing import Any
import pytest
import httpx
from uuid import uuid4
import json
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart


# A2A validation helpers - adapted from https://github.com/a2aproject/a2a-inspector/blob/main/backend/validators.py

def validate_agent_card(card_data: dict[str, Any]) -> list[str]:
    """Validate the structure and fields of an agent card."""
    errors: list[str] = []

    # Use a frozenset for efficient checking and to indicate immutability.
    required_fields = frozenset(
        [
            'name',
            'description',
            'url',
            'version',
            'capabilities',
            'defaultInputModes',
            'defaultOutputModes',
            'skills',
        ]
    )

    # Check for the presence of all required fields
    for field in required_fields:
        if field not in card_data:
            errors.append(f"Required field is missing: '{field}'.")

    # Check if 'url' is an absolute URL (basic check)
    if 'url' in card_data and not (
        card_data['url'].startswith('http://')
        or card_data['url'].startswith('https://')
    ):
        errors.append(
            "Field 'url' must be an absolute URL starting with http:// or https://."
        )

    # Check if capabilities is a dictionary
    if 'capabilities' in card_data and not isinstance(
        card_data['capabilities'], dict
    ):
        errors.append("Field 'capabilities' must be an object.")

    # Check if defaultInputModes and defaultOutputModes are arrays of strings
    for field in ['defaultInputModes', 'defaultOutputModes']:
        if field in card_data:
            if not isinstance(card_data[field], list):
                errors.append(f"Field '{field}' must be an array of strings.")
            elif not all(isinstance(item, str) for item in card_data[field]):
                errors.append(f"All items in '{field}' must be strings.")

    # Check skills array
    if 'skills' in card_data:
        if not isinstance(card_data['skills'], list):
            errors.append(
                "Field 'skills' must be an array of AgentSkill objects."
            )
        elif not card_data['skills']:
            errors.append(
                "Field 'skills' array is empty. Agent must have at least one skill if it performs actions."
            )

    return errors


def _validate_task(data: dict[str, Any]) -> list[str]:
    errors = []
    if 'id' not in data:
        errors.append("Task object missing required field: 'id'.")
    if 'status' not in data or 'state' not in data.get('status', {}):
        errors.append("Task object missing required field: 'status.state'.")
    return errors


def _validate_status_update(data: dict[str, Any]) -> list[str]:
    errors = []
    if 'status' not in data or 'state' not in data.get('status', {}):
        errors.append(
            "StatusUpdate object missing required field: 'status.state'."
        )
    return errors


def _validate_artifact_update(data: dict[str, Any]) -> list[str]:
    errors = []
    if 'artifact' not in data:
        errors.append(
            "ArtifactUpdate object missing required field: 'artifact'."
        )
    elif (
        'parts' not in data.get('artifact', {})
        or not isinstance(data.get('artifact', {}).get('parts'), list)
        or not data.get('artifact', {}).get('parts')
    ):
        errors.append("Artifact object must have a non-empty 'parts' array.")
    return errors


def _validate_message(data: dict[str, Any]) -> list[str]:
    errors = []
    if (
        'parts' not in data
        or not isinstance(data.get('parts'), list)
        or not data.get('parts')
    ):
        errors.append("Message object must have a non-empty 'parts' array.")
    if 'role' not in data or data.get('role') != 'agent':
        errors.append("Message from agent must have 'role' set to 'agent'.")
    return errors


def validate_event(data: dict[str, Any]) -> list[str]:
    """Validate an incoming event from the agent based on its kind."""
    if 'kind' not in data:
        return ["Response from agent is missing required 'kind' field."]

    kind = data.get('kind')
    validators = {
        'task': _validate_task,
        'status-update': _validate_status_update,
        'artifact-update': _validate_artifact_update,
        'message': _validate_message,
    }

    validator = validators.get(str(kind))
    if validator:
        return validator(data)

    return [f"Unknown message kind received: '{kind}'."]


# A2A messaging helpers

async def send_text_message(text: str, url: str, context_id: str | None = None, streaming: bool = False):
    async with httpx.AsyncClient(timeout=10) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=url)
        agent_card = await resolver.get_agent_card()
        config = ClientConfig(httpx_client=httpx_client, streaming=streaming)
        factory = ClientFactory(config)
        client = factory.create(agent_card)

        msg = Message(
            kind="message",
            role=Role.user,
            parts=[Part(TextPart(text=text))],
            message_id=uuid4().hex,
            context_id=context_id,
        )

        events = [event async for event in client.send_message(msg)]

    return events


# A2A conformance tests

def test_agent_card(agent):
    """Validate agent card structure and required fields."""
    response = httpx.get(f"{agent}/.well-known/agent-card.json")
    assert response.status_code == 200, "Agent card endpoint must return 200"

    card_data = response.json()
    errors = validate_agent_card(card_data)

    assert not errors, f"Agent card validation failed:\n" + "\n".join(errors)

@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_message(agent, streaming):
    """Test that agent returns valid A2A message format."""
    events = await send_text_message("Hello", agent, streaming=streaming)

    all_errors = []
    for event in events:
        match event:
            case Message() as msg:
                errors = validate_event(msg.model_dump())
                all_errors.extend(errors)

            case (task, update):
                errors = validate_event(task.model_dump())
                all_errors.extend(errors)
                if update:
                    errors = validate_event(update.model_dump())
                    all_errors.extend(errors)

            case _:
                pytest.fail(f"Unexpected event type: {type(event)}")

    assert events, "Agent should respond with at least one event"
    assert not all_errors, f"Message validation failed:\n" + "\n".join(all_errors)

# SWE-bench specific tests



def get_eval_request(solver_url: str, config: dict | None = None) -> str:
    """Create an EvalRequest JSON string."""
    request = {
        "participants": {"solver": solver_url},
        "config": config or {"max_tasks": 1}
    }
    return json.dumps(request)


@pytest.mark.asyncio
async def test_swebench_with_solver(agent, solver, agent_card, solver_card):
    """Test SWE-bench evaluation with a solver agent."""
    if solver is None:
        pytest.skip("Solver agent not available (start Purple Agent on port 9010)")

    # Verify agent card has SWE-bench skill
    skill_ids = [s.get("id") for s in agent_card.get("skills", [])]
    assert "swebench_evaluator" in skill_ids, "Agent should have swebench_evaluator skill"

    # Send evaluation request
    request = get_eval_request(solver, {"max_tasks": 1})
    events = await send_text_message(request, agent, streaming=False)

    assert events, "Agent should respond with events"

    # Check for successful completion
    for event in events:
        match event:
            case (task, update):
                status = task.status.state.value
                assert status in ["completed", "working"], f"Task status should be completed or working, got {status}"


@pytest.mark.asyncio
async def test_swebench_missing_solver(agent):
    """Test that agent properly rejects request without solver participant."""
    request = json.dumps({
        "participants": {},  # Missing solver
        "config": {"max_tasks": 1}
    })

    events = await send_text_message(request, agent, streaming=False)

    assert events, "Agent should respond with events"

    # Should be rejected due to missing solver
    for event in events:
        match event:
            case (task, update):
                status = task.status.state.value
                assert status == "rejected", f"Task should be rejected without solver, got {status}"


@pytest.mark.asyncio
async def test_swebench_invalid_request(agent):
    """Test that agent properly rejects invalid request format."""
    events = await send_text_message("not a valid json request", agent, streaming=False)

    assert events, "Agent should respond with events"

    # Should be rejected due to invalid format
    for event in events:
        match event:
            case (task, update):
                status = task.status.state.value
                assert status == "rejected", f"Task should be rejected for invalid request, got {status}"


@pytest.mark.asyncio
async def test_swebench_specific_task(agent, solver):
    """Test evaluation of a specific SWE-bench task."""
    if solver is None:
        pytest.skip("Solver agent not available")

    # Request a specific task by instance_id
    request = get_eval_request(solver, {
        "instance_id": "django__django-11099",
        "max_tasks": 1
    })

    events = await send_text_message(request, agent, streaming=False)

    assert events, "Agent should respond with events"

    # Verify we got results
    found_artifact = False
    for event in events:
        match event:
            case (task, update):
                if task.artifacts:
                    found_artifact = True
                    # Check artifact has results
                    for artifact in task.artifacts:
                        assert artifact.parts, "Artifact should have parts"

    assert found_artifact, "Response should include result artifacts"
