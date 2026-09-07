from abc import ABC, abstractmethod
import os



class BaseLLMStream(ABC):
    def __init__(self):
        self.model = os.environ.get("MODEL", "glm-5.3-flash-colibri")
        self.temperature = os.environ.get("TEMPERATURE", "0.7")
        self.stream = True
        self.stream_service = None

    @abstractmethod
    def send_message(self, message):
        pass

    @abstractmethod
    def close(self):
        pass

    def check_user_approval(self, user_input: str) -> bool:
        # A strict system prompt forces the LLM to output only YES or NO
        eval_messages = [
            {
                "role": "system",
                "content": "You are an intent classifier. Evaluate if the user is approving the proposed plan or indicating they are ready to proceed. Reply with exactly 'YES' if they are approving, or 'NO' if they want changes/more planning. Say nothing else."
            },
            {"role": "user", "content": user_input}
        ]

        response_stream = self.send_message(eval_messages)

        # Consume the stream silently (no console updates)
        evaluation = ""
        for chunk in response_stream:
            if chunk.choices[0].delta.content is not None:
                evaluation += chunk.choices[0].delta.content

        # Return True if the LLM said YES
        return "YES" in evaluation.strip().upper()