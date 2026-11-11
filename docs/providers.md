# Engines and providers

bot-inator runs three engines: **main** (event summaries, chat, the agent
loop), **analysis** (judging ideas) and **drafting** (writing tool code).
Each one runs either on your Mac or on a provider you bring a key for.

## First run

On a fresh install (no `~/.bot-inator/settings.json`) the daemon starts
with no engine loaded and downloads nothing. The app opens **Set up
bot-inator** and asks you to pick one:

- **Local**: every engine runs on your Mac. Nothing leaves it.
- **Cloud key**: every engine runs on a provider you pick, with your own
  key. Faster and stronger models.
- **Mixed**: main and analysis run on your Mac; only tool drafts go to the
  provider.

The screen reads your Mac's memory and marks one choice **Recommended for
this Mac**: Cloud key under 24 GB, Local at 24 GB and up (the stock local
models need about 13 GB of memory). It is only a recommendation. Nothing
is selected for you.

Local models can come from:

- **Download the recommended set**: the stock MLX models (about 13.7 GB /
  12.7 GiB). Starts only when you click the button, with progress.
- **A folder of MLX models**: one folder per engine, each with
  `config.json` and `.safetensors` weights. GGUF folders (Ollama, most LM
  Studio downloads) are refused, since MLX cannot load them; use the next
  option for those.
- **Local server**: Ollama (`http://localhost:11434/v1`) or LM Studio
  (`http://localhost:1234/v1`). Must be on this Mac. Keep the server running.

After you choose, the app restarts the daemon on that choice and shows the
macOS permissions checklist. An existing install never sees this screen.
Set `AGENT_SKIP_FIRST_RUN=1` to turn it off for scripted setups.

## Providers (Settings)

Every provider except Local speaks the OpenAI chat-completions format, so
there is one integration with presets:

| Preset | Base URL | Default models | Status |
| :--- | :--- | :--- | :--- |
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` (main, drafting), `openai/gpt-oss-20b` (analysis) | run end to end (Developer-plan key; free plan not verified) |
| OpenAI | `https://api.openai.com/v1` | none: pick from List models | untested |
| Anthropic | `https://api.anthropic.com/v1/` (its OpenAI-compatibility layer) | `claude-sonnet-5-5`, `claude-haiku-5-5` (analysis) | untested |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | none: pick from List models | untested |
| xAI | `https://api.x.ai/v1` | none: pick from List models | untested |
| OpenRouter | `https://openrouter.ai/api/v1` | none: pick from List models | untested |
| Local server | `http://localhost:11434/v1` | none | run against Ollama |
| Custom | any OpenAI-compatible URL | none | untested |

In **Settings… (⌘,)**: pick the provider, paste your key, click **List
models** and choose a model for each engine, **Test**, **Save**, then
**Restart Daemon**. Each engine can use a different provider.

Keys are saved in `~/.bot-inator/settings.json` (readable only by you).
They are never logged, never put in a prompt and never given to generated
tools.

## What leaves your Mac

Privacy is decided by the base URL's host, not the provider's name. A
`localhost` URL is local. Any other host is a cloud engine: event
summaries, chat and tool drafts go to it, and message and email text is
replaced before it is sent unless you turn that on in Settings. Every call
to a cloud engine is listed in **Settings → Privacy ledger**.
