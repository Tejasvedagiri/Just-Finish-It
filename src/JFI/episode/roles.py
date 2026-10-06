"""v2 roles: which env prefixes configure their model, and which tools each
gets (laya_plan.md G1.2, G15).

Core sets are fixed in code and always sent -- they replace v1's
session-wide load_tool unlocking, where an unlocked tool stayed in every
request for the rest of the session (all 32 deferred schemas together are
~5,900 tokens: over a quarter of a 20k-token episode). Tools a role rarely needs
sit in OPTIONAL_POOL: Laya may pre-pick 0-3 per node (phase 8), and an
episode can load_tool one for itself; either way it lasts that episode only.

A name that isn't implemented yet (the phase 4 code tools) is skipped by
EpisodeTools, so the sets can name the final design today.
"""

ROLES = ("architect", "lead", "task", "dev", "reviewer", "cleanup")

#: Model settings per role, most specific first (laya_plan.md G15): e.g.
#: ARCHITECT_MODEL, else PLANNER_MODEL, else MODEL -- via phase_env's chain.
ROLE_ENV_PREFIXES = {
    "architect": ("ARCHITECT", "PLANNER"),
    "lead": ("LEAD", "PLANNER"),
    "task": ("TASK", "PLANNER"),
    "dev": ("DEV", "IMP"),
    "reviewer": ("REVIEWER",),
    "cleanup": ("CLEANUP",),
}

ROLE_CORE_TOOLS = {
    "architect": ["get_plan", "get_node", "add_node", "update_node", "delete_node", "design_set", "design_get",
                  "runbook_set", "runbook_get", "list_dir", "outline_file", "read_file", "search_code",
                  "execute_command", "capture_evidence", "finish"],
    "lead": ["get_node", "list_nodes", "add_node", "update_node", "delete_node", "design_get", "design_set",
             "runbook_get", "list_dir", "outline_file", "read_file", "read_symbol", "scaffold_file", "unscaffold_file",
             "mark_change", "capture_evidence", "list_evidence", "escalate", "finish"],
    "task": ["get_node", "list_nodes", "add_node", "update_node", "delete_node", "outline_file", "list_symbols",
             "read_file", "read_symbol", "find_references",
             "scaffold_file", "design_get", "list_evidence", "escalate", "finish"],
    "dev": ["outline_file", "read_symbol", "replace_symbol", "list_symbols", "read_file", "copy_lines", "write_file",
            "replace_in_file", "apply_patch", "find_references",
            "search_code", "design_get", "runbook_get", "runbook_set", "execute_command", "add_reviewer_note",
            "compare_evidence", "mark_leaf_done"],
    "reviewer": ["runbook_get", "runbook_set", "start_background_process", "stop_background_process",
                 "check_page", "execute_command", "read_file", "search_code", "list_dir", "get_plan",
                 "get_reviewer_notes", "leaf_diff", "compare_evidence", "list_evidence",
                 "write_review_report",
                 "reopen_leaf", "finish"],
    "cleanup": ["execute_command", "list_dir", "finish"],
}

#: The tool that ends each role's episode.
ROLE_FINISH_TOOL = {role: ("mark_leaf_done" if role == "dev" else "finish") for role in ROLES}

#: Tools most nodes don't need, available through load_tool (or a Laya pick).
OPTIONAL_POOL = [
    "capture_screenshot", "view_image", "fetch_webpage_images", "browse_webpage", "check_page", "browser", "http_request",
    "extract_video_frames",
    "ask_llm", "start_background_process", "stop_background_process", "list_processes",
    "clear_finished_processes", "context_save", "context_lookup", "append_to_file",
]
