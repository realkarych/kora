# kora

Orchestrate coding-agent sessions as a graph.

kora is an open-source (Apache-2.0) control plane for graphs whose nodes are full coding-agent sessions (Claude Code, Codex, agents speaking the Agent Client Protocol). Graphs are written in Python and compiled to a versioned YAML document; the control plane adds triggers, retries of nodes and sub-graphs, typed artifacts, approvals bound to the exact content under review, cost accounting, and protocols through which an organisation plugs in its own runtime, version control, secrets and observability.

The project is in the design stage; there is no code yet.

## Documentation

- [Technical introduction](docs/technical-introduction.md): start here to understand the core components, their connections and one complete run.
- [docs/](docs/README.md): positioning, technical introduction, requirements, decision records, proposals and the roadmap, written for people.
- [knowledge/](knowledge/README.md): normative specifications, detailed diagrams, research, analyses and review records, loaded section by section by engineers and coding agents.
- [AGENTS.md](AGENTS.md): rules for contributors, human or agent.
