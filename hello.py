def main():
    from openai import OpenAI

    # Initialize the client pointing to your custom backend endpoint
    client = OpenAI(
        base_url="http://127.0.0.1:9999/v1",  # Replace with your custom URL
        api_key="your-api-key-here"  # Use "mock-key" if your custom endpoint doesn't require auth
    )

    try:
        # Use the standard chat completions method with stream=True
        stream = client.chat.completions.create(
            model="glm-5.3-flash-colibri",  # Pass the model identifier used by your custom backend
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"}
            ],
            temperature=0.7,
            stream=True  # Enables streaming chunks as they are generated
        )

        # Iterate over the stream chunks and print tokens progressively
        for chunk in stream:
            content = chunk.choices[0].delta.content
            if content is not None:
                print(content, end="", flush=True)
        print()  # Final newline after streaming finishes

    except Exception as e:
        print(f"\nAn error occurred while streaming: {e}")

if __name__ == "__main__":
    main()
