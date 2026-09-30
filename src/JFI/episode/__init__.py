"""The v2 episode engine (laya_plan.md §0, §4.1, §4.8; phase 3).

An episode is one small, scoped LLM conversation about one node: a planner
role breaking it down or redoing it, Dev implementing one leaf, the reviewer
running the e2e, cleanup. It starts from a small brief (a never-trimmed scope
anchor, the role prompt, one-line runbook/design indexes), pulls everything
else with tools, and ends when the role's finish tool is called -- or is
stopped by the token budget or the turn cap. Nothing is shared with any other
episode's conversation.

Modules:
- roles:   the roles, their env prefixes, core tool sets and the optional pool.
- budget:  the per-episode token budget and the request-size estimate.
- brief:   the scope anchor and the system/kickoff messages.
- tools:   one episode's tool set (core + picked + load_tool'd), with schemas.
- directives: user directives forced mid-run, delivered to a node's next episode.
- engine:  run_episode() -- the turn loop, persistence, end conditions.
"""
