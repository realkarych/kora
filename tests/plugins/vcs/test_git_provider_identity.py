from kora.plugins.vcs.git import GitVcs


def test_git_name_is_available_before_configuration() -> None:
    provider_type = GitVcs

    name = provider_type.name

    assert name == "git"
