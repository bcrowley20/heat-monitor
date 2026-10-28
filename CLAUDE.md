# Heating System Monitor

Monitor and visyalize the performance of a residential radiant heatiing system.

## General
- This will be an app that runs continously taking in informaiton from various data sources, making calculations and updating a browser based webpage as needed
- The app will run on a local computer and serve the UI on a local network
- The app will be written in Python and run using uv.
- FastAPI and NiceGUI along with various data visualization packages should be considered

## Testing
Run `uv run pytest`; no credentials or network needed. A change is done when the suite passes. If a test fails for a reason unrelated to your change, report it instead of editing the test to pass. There is no need to run tests for only documentation changes.

## Working style
- Minimum code that solves the problem; nothing speculative. Touch only what you must, reuse what exists, and remove code your change leaves unused.
- Don't assume. Say when you're unsure, and surface tradeoffs.

## Documentation
- When updating documentation, always use simple, jargon-free English. Never use phrases like 'it’s important to note,' 'at the end of the day,' 'game-changer,' 'delve,' or 'not only... but also'.
- No Punctuation Crutches: Avoid using em-dashes (—) and excessive bulleted lists unless strictly necessary.
- Vary Sentence Length: Mix very short, punchy sentences with longer, conversational ones. Avoid uniform paragraph lengths.
- Active Voice: Write with direct active verbs and drop empty intensifiers like 'very,' 'really,' or 'truly'.
- Tone: Keep it clear, grounded, and practical. Cut any sentence that does not add new information.
