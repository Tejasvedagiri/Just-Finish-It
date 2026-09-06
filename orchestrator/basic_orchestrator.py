class BaseOrchestrator:
    def __init__(self, system_prompt= None):
        default_system_prompt = """
        You are a generic AI agent who sole job is it complete the task assigned
        from the user.
        """
        self.system_prompt = system_prompt or default_system_prompt
        self.message = [{"role": "system", "content": self.system_prompt}]

    def add_message(self, role, message: str):
        self.message.append({"role": role, "content": message})

    def get_message(self):
        return self.message