"""A single API call, to check that the environment is set up correctly."""

import anthropic

# Creates the client. No arguments: it reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=200,
    messages=[
        {"role": "user", "content": "In one sentence, what is the Python language?"}
    ],
)

print(response.content[0].text)
print("---")
print("Input tokens: ", response.usage.input_tokens)
print("Output tokens:", response.usage.output_tokens)
