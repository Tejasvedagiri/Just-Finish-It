# test = [
#     {
#         'content': 'You are a planner agent who job is generate .md file. You need come up with a detailed step by step plan for the AI Agents so that it can build everything needed following the steps provided. The plan should contain the following. Title Of object. Steps of Plan. (Tabular format. Create all the required docs, gather need info, and validating with user) Progress of Plan (Just checkboxes to close out on progress) Steps of Implementation (Tabular format. What need to be done to achieve the above 2 goes) Progress of Implementation (Just checkboxes to close out on progress) Step of Testing (List)',
#         'role': 'system'},
#     {'content': 'What do you want to do?', 'role': 'tool'},
#     {'content': 'i want to learn python', 'role': 'user'},
# ]
# # test = [
# #                 {"role": "system", "content": "You are a helpful assistant."},
# #                 {"role": "user", "content": "Hello"}
# #             ]
# def main():
#     from openai import OpenAI
#     OPENAI_URL = "http://127.0.0.1:1234/v1"
#     OPENAI_API_KEY = "local"
#     MODEL = ""
#
#     # Initialize the client pointing to your custom backend endpoint
#     client = OpenAI(
#         base_url=OPENAI_URL,  # Replace with your custom URL
#         api_key="your-api-key-here"  # Use "mock-key" if your custom endpoint doesn't require auth
#     )
#
#     try:
#         # Use the standard chat completions method with stream=True
#         stream = client.chat.completions.create(
#             model=MODEL,  # Pass the model identifier used by your custom backend
#             messages=test,
#             temperature=0.7,
#             stream=True  # Enables streaming chunks as they are generated
#         )
#
#         # Iterate over the stream chunks and print tokens progressively
#         for chunk in stream:
#             content = chunk.choices[0].delta.content
#             if content is not None:
#                 print(content, end="", flush=True)
#         print()  # Final newline after streaming finishes
#
#     except Exception as e:
#         print(f"\nAn error occurred while streaming: {e}")
#
# if __name__ == "__main__":
#     main()
