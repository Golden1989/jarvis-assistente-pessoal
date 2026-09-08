# Jarvis — personal assistant

An AI assistant to talk with, debate ideas, and think things through. Built in
layers, loosely inspired by JARVIS.

**Level 1 (current):** terminal chatbot with conversation history, using the
Anthropic API (Claude Sonnet).

## Requirements

- Python 3.12+
- An Anthropic API key (https://platform.claude.com/settings/keys)

## Setup

1. Create the environment and install dependencies:

   ```
   conda create -n jarvis python=3.12
   conda activate jarvis
   pip install -r requirements.txt
   ```

2. Set the API key as an environment variable (never put the key in the code):

   - **Windows:** Control Panel → "Edit environment variables for your account"
     → new user variable `ANTHROPIC_API_KEY` with the value `sk-ant-...`
   - **Linux/macOS:** `export ANTHROPIC_API_KEY="sk-ant-..."`

   Close and reopen the terminal (and the editor) after creating the variable.

## Usage

```
python jarvis.py
```

Type `quit` to exit. The running session cost is shown after every reply.

### Optional personal profile

Create a `profile.txt` file in the project root with anything you want the
assistant to know about you (name, pronouns, interests, how you like to be
addressed). It is loaded into the system prompt at startup. The file is
git-ignored, so your personal details never reach the repository.

### Helper scripts

- `smoke_test.py` — a single API call, to check that the environment works.
- `list_models.py` — lists the models available to your account.

## Roadmap

- **Level 2:** tool use — access to the web, files, and code.
- **Level 3:** fixed persona and memory that persists across sessions.
