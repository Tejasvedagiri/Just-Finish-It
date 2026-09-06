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

        if not output:
            return f"Success: Command '{command}' executed silently (Exit code {result.returncode})"

        return "\n\n".join(output)

    except subprocess.TimeoutExpired:
        return f"Error: Command '{command}' timed out after {timeout} seconds."
    except Exception as e:
        return f"Error executing '{command}': {str(e)}"