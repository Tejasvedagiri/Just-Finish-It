from pathlib import Path

from JFI.text_sanitize import strip_leaked_special_tokens


def _get_safe_path(file_path: str) -> Path:
    """
    Validates and resolves the path to ensure it stays strictly
    within the current working directory. Prevents path traversal attacks (like ../../).
    """
    # Some models occasionally fail to cleanly terminate a tool call's JSON
    # arguments (e.g. a stray chat-template/reasoning-channel token bleeding
    # into the stream) -- past this point file_path is one that came from
    # such a broken call. Reject it outright rather than silently creating a
    # garbage file on disk with control characters or raw template tokens in
    # its name.
    if "\x00" in file_path or "\n" in file_path or "\r" in file_path:
        raise ValueError(
            f"file_path contains a control/newline character — this looks like a malformed "
            f"tool call, not a real path: {file_path!r}"
        )
    if "<|" in file_path or "|>" in file_path:
        raise ValueError(
            f"file_path contains what looks like a leaked chat-template token, not a real "
            f"path: {file_path!r}"
        )

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
        # Unlike a malformed file_path (rejected outright, above), a leaked
        # token inside otherwise-legitimate content is scrubbed rather than
        # failing the whole write -- the content around it is still real and
        # worth keeping.
        content = strip_leaked_special_tokens(content)

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

        # Only add a separator when the file doesn't already end with one,
        # otherwise chunked writes accumulate blank lines.
        prefix = ""
        if path.exists() and path.stat().st_size:
            with open(path, 'r', encoding='utf-8') as f:
                f.seek(max(0, path.stat().st_size - 1))
                prefix = "" if f.read().endswith("\n") else "\n"

        content = strip_leaked_special_tokens(content)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(prefix + content)

        return f"Success: Appended {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error appending to {file_path}: {str(e)}"

def _replace_ignoring_whitespace(content: str, old_string: str, new_string: str):
    """
    Retries a single-line replacement with whitespace normalised.

    Returns the new file content, or None if the relaxed match is absent or
    ambiguous. Multi-line old_strings are left alone: guessing there is unsafe.
    """
    needle = old_string.strip()
    if not needle or "\n" in old_string.strip("\n"):
        return None

    lines = content.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip() == needle]
    if len(hits) != 1:
        return None

    index = hits[0]
    original = lines[index]
    indent = original[:len(original) - len(original.lstrip())]
    newline = "\n" if original.endswith("\n") else ""
    lines[index] = indent + new_string.strip() + newline
    return "".join(lines)


def replace_in_file(file_path: str, old_string: str, new_string: str) -> str:
    """
    Swaps an exact substring inside a file, restricted to the current working
    directory.

    This is the cheap way to tick a checkbox: rewriting a whole plan through a
    JSON tool argument is what truncates on long documents.
    """
    try:
        path = _get_safe_path(file_path)
        if not path.exists():
            return f"Error: File {file_path} does not exist."

        old_string = strip_leaked_special_tokens(old_string)
        new_string = strip_leaked_special_tokens(new_string)

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        occurrences = content.count(old_string)

        if occurrences == 0:
            # Auto-rectify the common near-miss: the right line, but the
            # indentation or trailing spaces were not reproduced exactly.
            # Only applied when exactly one line matches once whitespace is
            # normalised, so it can never touch the wrong line.
            repaired = _replace_ignoring_whitespace(content, old_string, new_string)
            if repaired is not None:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(repaired)
                return (
                    f"Success: Replaced 1 occurrence in {file_path} "
                    "(matched ignoring surrounding whitespace)."
                )
            return (
                f"Error: old_string was not found in {file_path}. Nothing was changed. "
                "Read the file again and copy the exact text, including whitespace."
            )
        if occurrences > 1:
            return (
                f"Error: old_string appears {occurrences} times in {file_path}. Nothing was "
                "changed. Include surrounding text to make it match exactly one place."
            )

        with open(path, 'w', encoding='utf-8') as f:
            f.write(content.replace(old_string, new_string, 1))

        return f"Success: Replaced 1 occurrence in {file_path}"
    except Exception as e:
        return f"Error replacing in {file_path}: {str(e)}"
