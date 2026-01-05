import httpx
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--agent-url",
        default="http://localhost:9009",
        help="Agent URL (default: http://localhost:9009)",
    )
    parser.addoption(
        "--solver-url",
        default="http://localhost:9010",
        help="Solver/Purple Agent URL (default: http://localhost:9010)",
    )


@pytest.fixture(scope="session")
def agent(request):
    """Agent URL fixture. Agent must be running before tests start."""
    url = request.config.getoption("--agent-url")

    try:
        response = httpx.get(f"{url}/.well-known/agent-card.json", timeout=2)
        if response.status_code != 200:
            pytest.exit(f"Agent at {url} returned status {response.status_code}", returncode=1)
    except Exception as e:
        pytest.exit(f"Could not connect to agent at {url}: {e}", returncode=1)

    return url


@pytest.fixture(scope="session")
def agent_card(agent):
    """Fetch and return the agent card."""
    response = httpx.get(f"{agent}/.well-known/agent-card.json", timeout=5)
    return response.json()


@pytest.fixture(scope="session")
def solver(request):
    """Solver/Purple Agent URL fixture. Returns None if not available."""
    url = request.config.getoption("--solver-url")

    try:
        response = httpx.get(f"{url}/.well-known/agent-card.json", timeout=2)
        if response.status_code == 200:
            return url
    except Exception:
        pass

    return None


@pytest.fixture(scope="session")
def solver_card(solver):
    """Fetch and return the solver agent card. Returns None if solver not available."""
    if solver is None:
        return None
    response = httpx.get(f"{solver}/.well-known/agent-card.json", timeout=5)
    return response.json()
