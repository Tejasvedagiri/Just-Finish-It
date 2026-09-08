import shlex
import subprocess

from JFI.tool.context_tools import load_context_cache, save_context_cache

APPROVED_CMD_KEY = "approved-cmd"


def execute_command(command: str, timeout: int = 120) -> str:
    """Executes a shell command and returns the output."""
    try:
        # shell=True allows for piped commands like 'ls -la | grep src'
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
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
# "Save" persists to the session's context.json — the same file the LLM uses
# as its own scratchpad via the context_save/context_lookup tools
# (context_tools.py, CONTEXT_CACHE_RULES in simple_session_manager.py).
# Sharing that file is deliberate (one place to look); load/save_context_cache
# (context_tools.py) always merge through the rest of the file so writing one
# key here can never clobber the model's own facts, or vice versa.

def get_approved_cmd_prefixes(cache_path: str) -> list:
    prefixes = load_context_cache(cache_path).get(APPROVED_CMD_KEY, [])
    return prefixes if isinstance(prefixes, list) else []


def save_approved_cmd_prefix(prefix: str, cache_path: str) -> None:
    data = load_context_cache(cache_path)
    prefixes = data.get(APPROVED_CMD_KEY, [])
    if not isinstance(prefixes, list):
        prefixes = []
    if prefix not in prefixes:
        prefixes.append(prefix)
    data[APPROVED_CMD_KEY] = prefixes
    save_context_cache(data, cache_path)


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
    cache_path used to read/write persisted "Save" prefixes. `cache_path`
    must be the owning session's own context.json (JFI/<session_id>/context.json)
    — there is no shared fallback location.
    """

    def __init__(self, console, cache_path: str):
        self.console = console
        self.cache_path = cache_path
        self.approve_all = False

    def request(self, command: str) -> bool:
        """Returns True if `command` may run, prompting the user if needed."""
        if self.approve_all:
            return True
        if any(command.startswith(prefix) for prefix in get_approved_cmd_prefixes(self.cache_path)):
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
            save_approved_cmd_prefix(prefix, self.cache_path)
            return True
        if answer == "a":
            self.approve_all = True
            return True
        return answer == "y"


def request_cmd_approval(command: str, console, cache_path: str) -> bool:
    """One-shot approval check (no 'Yes for all' memory across calls) — a thin
    wrapper over CmdApprovalGate for callers that don't need session state."""
    return CmdApprovalGate(console, cache_path).request(command)


def make_gated_execute_command(console, cache_path: str):
    """Wraps execute_command behind a CmdApprovalGate for `console`/`cache_path`.

    Bare `execute_command` (e.g. TOOL_MAP's default, or a script calling it
    directly) stays ungated — this is opt-in, wired in by the caller that
    actually has a console and a session to gate.
    """
    gate = CmdApprovalGate(console, cache_path)

    def gated_execute_command(command: str, timeout: int = 120) -> str:
        if not gate.request(command):
            return f"Command not executed (user answered No): '{command}'"
        return execute_command(command, timeout)

    return gated_execute_command
