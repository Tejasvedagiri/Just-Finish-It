#Take user first input
#Create a plan and reviwer with user till he accepts it.
#implement the plan and then create.
#Then sping up a reviewer agent to test the work done.
#I

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
from orchestrator.basic_orchestrator import BaseOrchestrator

# Load environment variables
load_dotenv()

console = Console()
stream = ColibriLLMStream()

console.print(Panel.fit("[bold blue]Welcome to Just Finish It CLI v1.0[/bold blue]\nType 'exit' or 'quit' to close."))
console.print(Panel.fit("[bold blue]Enter the system message (Agent type).[/bold blue]\nType 'exit' or 'quit' to close."))
user_input = Prompt.ask("\n[bold green]You")
bmo = BaseOrchestrator(user_input)
response = stream.generate_llm_response_stream(console, bmo.get_message())
while True:
    bmo.add_message("assistant", response)
    user_input = Prompt.ask("\n[bold green]You")

    if user_input.lower() in ['exit', 'quit']:
        break

    orchestrator.add_message("user", user_input)
    response = stream.generate_llm_response_stream(console, bmo.get_message())

    pass
