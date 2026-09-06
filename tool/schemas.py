# tools/schemas.py

AVAILABLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Writes code or text to a file. Automatically creates directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file, e.g., 'src/main.py'"
                    },
                    "content": {
                        "type": "string",
                        "description": "The exact content or code to write into the file."
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Executes a shell command on the terminal and returns the output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The terminal command to run, e.g., 'pip install requests' or 'python test.py'"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads the content of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file to read."
                    }
                },
                "required": ["file_path"]
            }
        }
    }
]