import json
import os
import shlex
import subprocess

from JFI.tool.context_tools import get_context_value, set_context_value

APPROVED_CMD_KEY = "approved-cmd"


def auto_approve_enabled() -> bool:
    """AUTO_APPROVE_COMMANDS=1: every execute_command call runs immediately,
    with no approval prompt at all -- as if "Yes, for the rest of this
    session" had been answered every time, without anything needing to be
    there to answer it. Meant for unattended runs (CI, a scripted end-to-end
    test) where nothing can respond to an interactive prompt; off by
    default; approvals exist precisely because execute_command can do
    anything a shell command can, so only turn this on for a run you already
    trust completely."""
    return os.environ.get("AUTO_APPROVE_COMMANDS", "").strip().lower() in ("1", "true", "yes", "on")


def execute_command(command: str, timeout: int = 300) -> str:
    """Executes a shell command and returns the output."""
    try:
        # shell=True allows for piped commands like 'ls -la | grep src'.
        # stdin=DEVNULL is deliberate: this subprocess shares our controlling
        # tty, so any invoked CLI that isatty()-detects it (npm create,
        # create-next-app, etc.) will launch an interactive prompt instead of
        # picking a non-interactive default -- and nothing will ever answer
        # it, so it hangs until `timeout` instead of failing fast. Closing
        # stdin makes those tools see a non-interactive session immediately,
        # same as CI, so they either use their default or error out clearly.
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL
        )

        # Combine stdout and stderr for the LLM to read
        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr.strip()}")
        body = "\n\n".join(output)

        # The exit code has to be unmistakable: a failing command that only
        # writes to stderr used to look indistinguishable from a chatty
        # successful one, so nothing downstream could tell it needed fixing.
        if result.returncode != 0:
            return (
                f"Error: Command '{command}' failed with exit code "
                f"{result.returncode}.\n\n{body}".rstrip()
            )

        if not body:
            return f"Success: Command '{command}' executed silently (exit code 0)."
        return f"Success: Command '{command}' exited 0.\n\n{body}"

    except subprocess.TimeoutExpired:
        return f"Error: Command '{command}' timed out after {timeout} seconds."
    except Exception as e:
        return f"Error executing '{command}': {str(e)}"


# ------------------------------------------------------------- approval gate
#
# Before a shell command from the LLM actually runs, the human is asked:
#   [Y]es       — run this one command, ask again next time
#   [S]ave      — run it, and remember its prefix in context.json so future
#                 commands starting with that prefix skip the prompt
#   [A]ll       — run it, and stop asking for the rest of this session
#                 (in-memory only; a fresh run asks again)
#   [N]o        — don't run it
#
# "Save" persists to the session's own context store (JFI.models.ContextEntry)
# under the APPROVED_CMD_KEY row -- the same store the LLM uses as its own
# scratchpad via the context_save/context_lookup tools (context_tools.py,
# CONTEXT_CACHE_RULES in simple_session_manager.py), just a row the model's
# own context_lookup never sees (see context_tools._INTERNAL_KEYS). One row
# per session, JSON-encoded list as its value -- get/set_context_value
# (context_tools.py) already handle one key atomically, so there is no
# whole-cache round trip to accidentally clobber another key with.

def get_approved_cmd_prefixes(engine, session_id: str) -> list:
    raw = get_context_value(engine, session_id, APPROVED_CMD_KEY)
    if not raw:
        return []
    try:
        prefixes = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return prefixes if isinstance(prefixes, list) else []


def save_approved_cmd_prefix(prefix: str, engine, session_id: str) -> None:
    prefixes = get_approved_cmd_prefixes(engine, session_id)
    if prefix not in prefixes:
        prefixes.append(prefix)
    set_context_value(engine, session_id, APPROVED_CMD_KEY, json.dumps(prefixes))


def _command_prefix(command: str) -> str:
    """The leading word(s) of `command`, used as the prefix a Save answer
    remembers. Falls back to a plain whitespace split (and then the raw
    string) so an unbalanced quote in the command can't crash the gate."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return tokens[0] if tokens else command


class CmdApprovalGate:
    """
    Per-session approval state for `execute_command`.

    Holds the in-memory "Yes for all" flag (deliberately not persisted —
    approving every command for a session is a one-time, in-the-moment call,
    not something a future session should silently inherit) alongside the
    DB engine/session_id used to read/write persisted "Save" prefixes.
    """

    def __init__(self, console, engine, session_id: str):
        self.console = console
        self.engine = engine
        self.session_id = session_id
        self.approve_all = False

    def request(self, command: str) -> bool:
        """Returns True if `command` may run, prompting the user if needed."""
        if auto_approve_enabled():
            self.console.display_system(f"✅ Auto-approved (AUTO_APPROVE_COMMANDS=1): {command}")
            return True
        if self.approve_all:
            return True
        if any(command.startswith(prefix) for prefix in get_approved_cmd_prefixes(self.engine, self.session_id)):
            return True

        prefix = _command_prefix(command)
        self.console.display_system(f"⚠️  Command pending approval: {command}")
        answer = self.console.get_user_choice("Run this command?", [
            ("y", "Yes, run once"),
            ("s", f"Yes, save '{prefix}' & run"),
            ("a", "Yes, for the rest of this session"),
            ("n", "No, don't run"),
        ])
        if answer == "s":
            save_approved_cmd_prefix(prefix, self.engine, self.session_id)
            return True
        if answer == "a":
            self.approve_all = True
            return True
        return answer == "y"


def request_cmd_approval(command: str, console, engine, session_id: str) -> bool:
    """One-shot approval check (no 'Yes for all' memory across calls) — a thin
    wrapper over CmdApprovalGate for callers that don't need session state."""
    return CmdApprovalGate(console, engine, session_id).request(command)


def make_gated_execute_command(console, engine, session_id: str):
    """Wraps execute_command behind a CmdApprovalGate for `console`/session.

    Bare `execute_command` (e.g. TOOL_MAP's default, or a script calling it
    directly) stays ungated — this is opt-in, wired in by the caller that
    actually has a console and a session to gate.
    """
    gate = CmdApprovalGate(console, engine, session_id)

    def gated_execute_command(command: str, timeout: int = 300) -> str:
        if not gate.request(command):
            return f"Command not executed (user answered No): '{command}'"
        return execute_command(command, timeout)

    return gated_execute_command
