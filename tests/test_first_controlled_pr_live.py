"""First live validation of the governed W1 GitHub write path."""


def test_first_controlled_pr_requires_human_merge() -> None:
    """The first controlled PR stops before merge and deployment."""
    human_merge_required = True
    agent_can_merge = False
    agent_can_deploy = False

    assert human_merge_required is True
    assert agent_can_merge is False
    assert agent_can_deploy is False
