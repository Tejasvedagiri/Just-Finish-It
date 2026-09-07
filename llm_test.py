import re
import io
import contextlib
from dotenv import load_dotenv

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.live import Live

# Import your custom LLM stream class
from llm.colibri_llm_stream import ColibriLLMStream
from main import OpenCodeOrchestrator
from orchestrator.basic_orchestrator import BaseOrchestrator

# Load environment variables
load_dotenv()

console = Console()
stream = ColibriLLMStream()
orchestrator = OpenCodeOrchestrator()

console.print(Panel.fit("[bold blue]OpenCode CLI v1.0[/bold blue]\nType 'exit' or 'quit' to close."))
ser_input = Prompt.ask("\n[bold green]You")
orchestrator.add_message("user", ser_input)
a = stream.generate_llm_response_stream(console, orchestrator.history)

print(a)