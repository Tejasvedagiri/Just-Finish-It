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
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Seconds to wait before giving up on this command. Defaults to 300 -- "
                            "raise it (e.g. 600-1200) for anything that legitimately takes even "
                            "longer, like installing large packages or running a slow build/test "
                            "suite."
                        )
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
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": (
                "Appends content to the end of a file, creating it if needed. Use this to build "
                "up a long document in several smaller calls instead of one oversized write_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file, e.g., 'plan.md'"
                    },
                    "content": {
                        "type": "string",
                        "description": "The chunk of content to add at the end of the file."
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": (
                "Replaces one exact substring in a file, leaving the rest untouched. This is the "
                "correct way to tick a checkbox (e.g. '- [ ] 1.1 Foo' -> '- [x] 1.1 Foo') or to "
                "patch a few lines. Prefer this over rewriting a whole file with write_file. "
                "old_string must match exactly once, including whitespace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file to edit."
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact existing text to replace. Must occur exactly once."
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to put in its place."
                    }
                },
                "required": ["file_path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screenshot",
            "description": (
                "Captures the primary monitor and saves it as an auto-numbered "
                "screen-N.png inside `directory`. Does NOT show you the image — it only "
                "writes the file. Call view_image on the returned path afterward to "
                "actually see it. Fails cleanly (with a message telling you to skip the "
                "step and continue) in a headless environment with no display."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": (
                            "Where to save the screenshot — pass your session's JFI/<session> "
                            "folder (the same directory your plan file lives in)."
                        )
                    }
                },
                "required": ["directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": (
                "Reads an image file (png, jpg, jpeg, gif, or webp) and attaches it to the "
                "conversation so you can see it on your next turn — e.g. a screenshot from "
                "capture_screenshot, or an image already in the project. Unlike read_file, "
                "this shows you the actual picture, not text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the image file."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage_images",
            "description": (
                "Fetches a web page (http/https only) and downloads the images it references "
                "— the Open Graph/Twitter preview image first, then every <img> tag, in page "
                "order — into `directory` as auto-numbered files (web-1.png, web-2.jpg, ...). "
                "Does NOT show you the image — like capture_screenshot, it only writes files "
                "and reports where. Call view_image on one of the returned paths afterward to "
                "actually see it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The page URL to fetch, e.g. 'https://example.com/article'."
                    },
                    "directory": {
                        "type": "string",
                        "description": (
                            "Where to save downloaded images — pass your session's "
                            "JFI/<session> folder (the same directory your plan file lives in)."
                        )
                    },
                    "max_images": {
                        "type": "integer",
                        "description": "Maximum number of images to download (default 5, capped at 20)."
                    }
                },
                "required": ["url", "directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context_save",
            "description": (
                "Saves one fact to your persistent context cache in a single call — merges "
                "it in without disturbing any other key already there. This is the correct "
                "way to add or update a context-cache fact; do NOT read_file + write_file "
                "the whole cache by hand, that risks dropping other keys you didn't retype."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short identifier for this fact, e.g. 'db_schema'."
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact itself, e.g. 'users table: id, email, created_at'."
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context_lookup",
            "description": (
                "Searches your persistent context cache instead of reading the whole file. "
                "Call with no keyword first to list every saved key plus a short preview — "
                "then call again with a keyword (matched against keys and values, case-"
                "insensitive) to get the full text of just what's relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Term to search for. Omit or leave blank to list all saved keys."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_llm",
            "description": (
                "Asks a fresh, single-turn LLM call anything — write a description, brainstorm "
                "names, clarify an ambiguous requirement, summarize a chunk of text, or handle "
                "any other one-off text task that doesn't need a dedicated tool. This call is "
                "STATELESS: it has NO access to your conversation, the plan file, or any files "
                "on disk — put everything it needs directly in the prompt. Do NOT use this for "
                "file operations, running commands, or anything another tool already does "
                "directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The full, self-contained question or instruction to send."
                    }
                },
                "required": ["prompt"]
            }
        }
    },
]
