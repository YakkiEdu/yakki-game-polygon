# YAKKI Game Polygon

**Automated LLM-driven game testing for Android educational apps.**

Game Polygon connects an LLM (Claude or Gemini) to an Android device via ADB and runs through a game autonomously, step by step:

1. Takes a screenshot
2. Asks the LLM "what action should I take to complete the objective?"
3. Validates and executes the action (tap, swipe, type, back …)
4. Reflects on whether the action succeeded ("did that work?")
5. On failure: sends the relevant source code to the LLM and generates a fix suggestion

---

## Features

| Feature | Description |
|---------|-------------|
| Multi-LLM | Claude (default) or Gemini — swap with `--llm gemini` |
| Reflection engine | After every action, asks the LLM "did that work?" — catches failures early |
| Action space filter | Validates LLM tap coordinates; clamps out-of-bounds, snaps to nearest element |
| Stuck / loop detection | MD5 screen fingerprinting; detects repeated screens and navigation loops |
| Focus recovery | Relaunches the app if it loses foreground focus |
| Complexity escalation | Classifies failures as simple / medium / hard / critical; stops auto-fix for critical issues |
| Screenshot cleanup | Keeps only the failure screenshot; deletes the rest to save disk space |

---

## Requirements

- Python 3.10+
- Android device connected via ADB (USB or network)
- `ANTHROPIC_API_KEY` **or** `GEMINI_API_KEY` environment variable

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
# Test the "Scrambler" game using Claude (default)
python game_polygon.py --game scrambler

# Test with Gemini, keep all screenshots
python game_polygon.py --game cloze --llm gemini --keep-screenshots

# Auto-fix simple issues and rebuild
python game_polygon.py --game quiz --auto-fix

# List available games
python game_polygon.py --list-games

# Custom device (or set YAKKI_DEVICE env-var)
python game_polygon.py --game scrambler --device 192.168.1.42:5555
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required for Claude backend |
| `GEMINI_API_KEY` | — | Required for Gemini backend |
| `YAKKI_DEVICE` | `localhost:5555` | ADB device address |
| `YAKKI_ROOT` | `.` | Project root directory |

---

## Architecture

```
game_polygon.py
├── GamePolygon           # Top-level controller (test → analyse → fix loop)
├── GameTester            # Step-by-step LLM-guided test runner
│   ├── ScreenStateTracker    # Loop / stuck / timeout detection
│   ├── FocusRecovery         # App relaunch on focus loss
│   ├── ReflectionEngine      # Post-action outcome evaluation
│   └── ActionSpaceFilter     # LLM action validation & correction
├── ADBController         # ADB wrapper with timeouts
├── UIParser              # XML hierarchy → element list
├── CodeContextBuilder    # Feeds relevant .kt files to the LLM
├── ClaudeClient          # Anthropic vision API
├── GeminiClient          # Google Gemini vision API
├── ComplexityAssessor    # simple / medium / hard / critical classification
└── BuildManager          # Gradle build + APK install
```

---

## Adding a new game

1. Add an entry to `GAME_CONTRACTS` in `game_polygon.py`:
   ```python
   "my_game": [
       "yakkiedu/libraries/my-game/src/main/java/com/yakki/edu/mygame",
   ],
   ```
2. Run: `python game_polygon.py --game my_game`

---

## License

MIT
