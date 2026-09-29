"""One episode's tool set (laya_plan.md G1.2, G1.3).

active = the role's core tools + any tools Laya picked for this node,
filtered to what's actually implemented. The optional pool is reachable via
this episode's own load_tool, and a loaded tool lasts this episode only --
unlike v1's session-wide UnlockedTool. Picks and loads can only ADD tools;
nothing removes a core tool.
"""

from typing import Any, Callable, Dict, List, Sequence

from JFI.episode.roles import OPTIONAL_POOL, ROLE_CORE_TOOLS
from JFI.planner.nodes import NODE_TOOL_SCHEMAS
from JFI.tool.code_tools import CODE_TOOL_SCHEMAS
from JFI.tool.design_tools import DESIGN_TOOL_SCHEMAS
from JFI.tool.runbook_tools import RUNBOOK_TOOL_SCHEMAS
from JFI.tool.schemas import CORE_TOOLS, DEFERRED_TOOLS

FINISH_SCHEMA = {"type": "function", "function": {
    "name": "finish",
    "description": ("End this conversation: call it once the node in SCOPE is complete. Pass the node's id "
                    "and a one-line summary of what you did."),
    "parameters": {"type": "object", "properties": {
        "node_id": {"type": "integer"},
        "summary": {"type": "string"},
    }, "required": ["node_id", "summary"]},
}}


def _known_schemas(extra: Sequence[dict]) -> Dict[str, dict]:
    schemas = {t["function"]["name"]: t for t in CORE_TOOLS + DEFERRED_TOOLS if t["function"]["name"] != "load_tool"}
    for t in [*RUNBOOK_TOOL_SCHEMAS, *DESIGN_TOOL_SCHEMAS, *CODE_TOOL_SCHEMAS, *NODE_TOOL_SCHEMAS, FINISH_SCHEMA,
              *extra]:
        schemas[t["function"]["name"]] = t
    return schemas


class EpisodeTools:
    def __init__(self, role: str, implementations: Dict[str, Callable[..., Any]],
                 picked: Sequence[str] = (), extra_schemas: Sequence[dict] = ()):
        self.role = role
        self._impl = dict(implementations)
        self._schemas = _known_schemas(extra_schemas)
        available = [n for n in self._impl if n in self._schemas]
        core = [n for n in ROLE_CORE_TOOLS[role] if n in available]
        picks = [n for n in picked if n in OPTIONAL_POOL and n in available and n not in core]
        self.active: List[str] = core + picks
        self.pool: List[str] = [n for n in OPTIONAL_POOL if n in available and n not in self.active]
        self.loaded: List[str] = []
        self.used: List[str] = []

    def schemas(self) -> List[dict]:
        schemas = [self._schemas[n] for n in self.active]
        if self.pool:
            schemas.append(self._load_tool_schema())
        return schemas

    def _load_tool_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": "load_tool",
            "description": ("Add one optional tool to this conversation (it lasts only this conversation). "
                            "Available: " + ", ".join(self.pool)),
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "enum": list(self.pool)},
            }, "required": ["name"]},
        }}

    def load(self, name: str) -> str:
        if name in self.active:
            return f"{name} is already available."
        if name not in self.pool:
            return f"Error: {name!r} can't be loaded here. Loadable: {', '.join(self.pool) or 'none'}."
        self.pool.remove(name)
        self.active.append(name)
        self.loaded.append(name)
        return f"Loaded {name}; it's available from your next call."

    def has(self, name: str) -> bool:
        return name == "load_tool" or name in self.active

    def call(self, name: str, args: Dict[str, Any]):
        if name == "load_tool":
            return self.load(args.get("name", ""))
        if name not in self.active:
            hint = f" Call load_tool('{name}') first." if name in self.pool else ""
            return f"Error: there is no tool called {name!r} here.{hint} Available: {', '.join(self.active)}."
        if name not in self.used:
            self.used.append(name)
        return self._impl[name](**args)
