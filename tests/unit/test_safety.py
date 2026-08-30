from forge_agent.safety import PolicyEngine, READ_ONLY_TOOLS, RiskLevel
from forge_agent.types import RunMode


def test_policy_classifies_read_write_and_destructive_command() -> None:
    policy = PolicyEngine()
    read = policy.evaluate("read_file", {"path": "a.txt"})
    write = policy.evaluate("write_file", {"path": "a.txt"})
    destructive = policy.evaluate("run_command", {"command": "git reset --hard"})
    removed = policy.evaluate(
        "run_command",
        {"command": "python -c \"import os; os.remove('a.py')\""},
    )

    assert read.risk == RiskLevel.LOW
    assert not read.requires_approval
    assert write.risk == RiskLevel.MEDIUM
    assert write.requires_approval
    assert destructive.risk == RiskLevel.HIGH
    assert removed.risk == RiskLevel.HIGH
    assert not removed.allowed


def test_policy_blocks_mutation_in_plan_mode_and_is_conservative() -> None:
    policy = PolicyEngine(mode=RunMode.PLAN)
    decision = policy.evaluate("replace_in_file", {"path": "a.txt"})
    unknown = policy.evaluate("unregistered", {})

    assert not decision.allowed
    assert not decision.requires_approval
    assert unknown.risk == RiskLevel.HIGH
    assert not PolicyEngine(mode=RunMode.PLAN).evaluate("delete_file", {"path": "a.txt"}).allowed


def test_planning_pass_blocks_writes_without_calling_it_plan_mode() -> None:
    policy = PolicyEngine(mode=RunMode.PLAN, planning_pass=True)
    decision = policy.evaluate("write_file", {"path": ".gitignore"})

    assert not decision.allowed
    assert "confirm the plan" in decision.reason
    assert "plan mode" not in decision.reason


def test_policy_treats_repo_outline_and_git_status_as_read_only() -> None:
    policy = PolicyEngine()

    outline = policy.evaluate("repo_outline", {"path": "."})
    status = policy.evaluate("git_status", {"path": "."})

    assert outline.risk is RiskLevel.LOW
    assert status.risk is RiskLevel.LOW
    assert not outline.requires_approval
    assert not status.requires_approval
    for name in READ_ONLY_TOOLS:
        assert policy.evaluate(name, {"path": ".", "query": "x"}).risk is RiskLevel.LOW


def test_policy_flags_install_and_network_commands_as_medium() -> None:
    policy = PolicyEngine()
    install = policy.evaluate("run_command", {"command": "pip install requests"})
    network = policy.evaluate("run_command", {"command": "curl https://example.com"})

    assert install.risk is RiskLevel.MEDIUM
    assert "installing dependencies" in install.reason
    assert network.risk is RiskLevel.MEDIUM
    assert "network commands" in network.reason


def test_policy_allows_tests_and_verify_without_approval() -> None:
    policy = PolicyEngine()
    pytest_cmd = policy.evaluate("run_command", {"command": "python -m pytest -q"})
    npm_test = policy.evaluate("run_command", {"command": "npm test"})
    verify = policy.evaluate("verify_changes", {"command": "python -m pytest -q"})
    verify_default = policy.evaluate("verify_changes", {})

    assert pytest_cmd.risk is RiskLevel.LOW
    assert not pytest_cmd.requires_approval
    assert npm_test.risk is RiskLevel.LOW
    assert verify.risk is RiskLevel.LOW
    assert not verify.requires_approval
    assert verify_default.risk is RiskLevel.LOW
    assert not verify_default.requires_approval


def test_policy_flags_direct_git_metadata_write() -> None:
    decision = PolicyEngine(auto_approve=True).evaluate(
        "write_file", {"path": ".git/config"}
    )
    assert decision.risk == RiskLevel.HIGH
    assert not decision.requires_approval
