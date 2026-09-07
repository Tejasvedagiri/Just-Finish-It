import subprocess


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