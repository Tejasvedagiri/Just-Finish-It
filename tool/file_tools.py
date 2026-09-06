import os
from pathlib import Path


def _get_safe_path(file_path: str) -> Path:
    """
    Validates and resolves the path to ensure it stays strictly
    within the current working directory. Prevents path traversal attacks (like ../../).
    """
    cwd = Path.cwd().resolve()

    # Resolve the target path relative to the current working directory
    target_path = (cwd / file_path).resolve()

    # Check if the target path is a subpath of the current working directory
    if not target_path.is_relative_to(cwd):
        raise PermissionError(
            f"Security Error: Access denied. Path '{file_path}' resolves outside the current working directory.")

    return target_path


def write_file(file_path: str, content: str) -> str:
    """Writes content to a file, restricted to the current working directory."""
    try:
        path = _get_safe_path(file_path)
        # Create directories if they don't exist (e.g., output/ subfolders)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

        return f"Success: Wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error writing to {file_path}: {str(e)}"


def read_file(file_path: str) -> str:
    """Reads the content of a file, restricted to the current working directory."""
    try:
        path = _get_safe_path(file_path)
        if not path.exists():
            return f"Error: File {file_path} does not exist."

        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading {file_path}: {str(e)}"


def append_to_file(file_path: str, content: str) -> str:
    """Appends content to the end of a file, restricted to the current working directory."""
    try:
        path = _get_safe_path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'a', encoding='utf-8') as f:
            f.write("\n" + content)

        return f"Success: Appended {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error appending to {file_path}: {str(e)}"