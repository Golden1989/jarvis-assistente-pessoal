"""List the models available to your account, straight from the API."""

import anthropic

client = anthropic.Anthropic()

for model in client.models.list():
    print(model.id, "-", model.display_name)

# Details for a specific model (context window, max output):
print()
info = client.models.retrieve("claude-sonnet-5")
print("ID:            ", info.id)
print("Name:          ", info.display_name)
print("Context (in):  ", getattr(info, "max_input_tokens", "n/a"))
print("Max output:    ", getattr(info, "max_tokens", "n/a"))
