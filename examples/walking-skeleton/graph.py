from kora.artifacts import StructuredOutput

from kora import approval, graph, node


@graph(namespace="example", repo="", rev="main", runtime="local", vcs="git")
def skeleton() -> None:
    prepare = node.agent(
        "agent",
        harness="claude",
        prompt="Summarize the repository.",
        output={"summary": StructuredOutput},
    )
    check = node.command("gate", cmd=["printf", "checked"])
    prepare >> check
    approval("approve", channel="cli", timeout="1h", on_expiry="fail")(gate=check)
