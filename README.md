# YAKKI Game Polygon

> **LLM-driven automated game testing for Android educational apps**

Game Polygon is a Python tool that connects a large language model (Claude or Gemini) to an Android device via ADB and plays through a game autonomously — making decisions from screenshots, validating its own actions, and escalating failures to the right level of review.

Built for [YakkiEdu](https://github.com/YakkiEdu), an AI-powered English learning platform for Israeli schools.

---

## How it works

```
+--------------+     screenshot      +--------------+
|   Android    | ------------------> |  LLM Vision  |
|   Device     | <------------------ |  (Claude /   |
|   (via ADB)  |    action to take   |   Gemini)    |
+--------------+                     +--------------+
       |                                    |
       |         after action               |
       +------------------------------------+
                    reflection:
              "did that actually work?"
```

Each test step:

1. **Screenshot** — capture the current screen via ADB
2. **Decide** — send screenshot + UI element list to the LLM, ask "what should I do?"
3. **Validate** — run the proposed action through `ActionSpaceFilter` (clamp coords, snap to nearest button, truncate oversized input)
4. **Execute** — send tap / swipe / type / back to the device
5. **Reflect** — take another screenshot and ask the LLM "did that action succeed?"
6. **Repeat** — until the LLM says `done`, an anomaly is detected, or max steps is reached
7. **Analyse** — on failure, read the relevant Kotlin source files and ask the LLM for a root-cause + fix suggestion
8. **Escalate** — classify the problem (`simple -> medium -> hard -> critical`) and decide whether to auto-fix, open a committee review, or stop immediately

---

## Features

### Vision-based game play
The LLM sees the actual screen (not just element names) and can handle dynamic layouts, animations, and edge cases that rule-based scripts miss.

### Reflection engine
After every action the tool asks *"did that work?"* using a two-tier check:
- **Fast local check** — compare MD5 hashes of the element tree before/after (free, instant)
- **Deep vision check** — compare before/after screenshots with the LLM if the fast check is inconclusive

If an action failed, the engine retries up to 2 times before moving on.

### Action space filter
Prevents the LLM from doing dangerous or nonsensical things:
- Tapping outside the physical screen — coordinates are clamped
- Tapping near (< 50 px) a clickable element but missing — snapped to the element centre
- Sending a very long text input — truncated to 500 chars
- Swiping without all four coordinates — missing values filled with the screen centre

### Stuck & loop detection
Every screen is fingerprinted with MD5 of the top-20 element texts/positions. The tracker raises anomalies when:
- The same screen is seen N times in a row (`STUCK`)
- A previously seen screen reappears (`LOOP`)
- No progress for N steps (`NO_PROGRESS`)
- The test has been running for more than 10 minutes (`TIMEOUT`)

### Focus recovery
If the app loses foreground (home button pressed, system dialog appeared, etc.) the tool automatically:
1. Presses Back to dismiss any overlay
2. Relaunches the app
3. Verifies the package is back in the hierarchy before continuing

Up to 3 recovery attempts per test run.

### Complexity escalation
After a failure the root cause is classified into four levels:

| Level | Trigger keywords / conditions | Action |
|-------|------------------------------|--------|
| `simple` | None of the below | Auto-fix allowed |
| `medium` | state, lifecycle, async, loop detected | Needs human review |
| `hard` | architectural, refactor, timeout, low-confidence LLM | Spawn committee |
| `critical` | crash, ANR, security, exception | Stop immediately + incident report |

### Screenshot lifecycle management
- On **success**: all screenshots are deleted (nothing to debug)
- On **failure**: only the failure screenshot is kept; the rest are deleted
- `--keep-screenshots` disables deletion entirely

### Code-context-aware failure analysis
When a test fails, the tool reads the relevant Kotlin source files (contracts, ViewModels, UI screens) and passes them to the LLM together with the failure screenshot and UI hierarchy. This gives the LLM enough context to produce accurate, file-specific fix suggestions.

---

## Requirements

- Python 3.10 or newer
- `adb` on your PATH (from [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools))
- Android device connected via USB or ADB-over-WiFi / ADB-over-TCP
- `ANTHROPIC_API_KEY` **or** `GEMINI_API_KEY` environment variable

```bash
pip install -r requirements.txt
```

---

## Quick start

```bash
# 1. Set your API key
export ANTHROPIC_API_KEY=sk-ant-...          # macOS / Linux
set ANTHROPIC_API_KEY=sk-ant-...             # Windows CMD

# 2. Connect your device
adb connect 192.168.1.42:5555               # or plug in USB

# 3. Run a test
python game_polygon.py --game scrambler
```

---

## Usage

```
python game_polygon.py [options]

Required:
  --game GAME           Game to test (scrambler | cloze | quiz | guest_day)

Optional:
  --objective TEXT      Natural-language test goal  [default: "Complete one round"]
  --max-steps N         Maximum steps before declaring failure  [default: 30]
  --auto-fix            Apply simple fixes automatically and rebuild
  --analyze-only        Skip playing; only analyse an existing failure
  --llm {claude,gemini} LLM backend  [default: claude]
  --device ADDR         ADB device address  [overrides YAKKI_DEVICE env-var]
  --keep-screenshots    Keep all screenshots even after a successful run
  --list-games          Print available game names and exit
```

### Examples

```bash
# Test with Gemini instead of Claude
python game_polygon.py --game cloze --llm gemini

# Increase step budget for a long game
python game_polygon.py --game guest_day --max-steps 60

# Auto-fix simple failures and retest
python game_polygon.py --game quiz --auto-fix

# Keep all screenshots for debugging
python game_polygon.py --game scrambler --keep-screenshots

# Use a specific device when multiple are connected
python game_polygon.py --game cloze --device emulator-5554

# List all registered games
python game_polygon.py --list-games
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required when using `--llm claude` (default) |
| `GEMINI_API_KEY` | — | Required when using `--llm gemini` |
| `YAKKI_DEVICE` | `localhost:5555` | ADB device address (overridden by `--device`) |
| `YAKKI_ROOT` | `.` (current directory) | Root of the Android project (used to locate source files) |

All secrets are read exclusively from environment variables — nothing is hard-coded.

---

## Output

Every test run creates a timestamped folder under `REPORTS/game_polygon/<game>_<timestamp>/`:

```
REPORTS/game_polygon/
+-- scrambler_20260508_143022/
    +-- step_001.png          <- only present with --keep-screenshots or on failure
    +-- step_002.png
    +-- step_015.png          <- failure screenshot (always kept on failure)
```

For failures above the `simple` complexity threshold additional reports are written:

- `INCIDENT_<game>_<timestamp>.md` — critical incident report
- `COMMITTEE_REQUEST_<game>_<timestamp>.md` — committee review request with full context

---

## Architecture

```
game_polygon.py
|
+-- GamePolygon                  Top-level controller
|   +-- run_test()               Single test run
|   +-- analyze_failure()        LLM root-cause analysis
|   +-- run_cycle()              test -> analyse -> fix -> retest loop
|   +-- _save_incident_report()  Critical failure report
|   +-- _request_committee()     Hard/critical escalation
|
+-- GameTester                   Step-by-step test runner
|   +-- ScreenStateTracker       MD5 fingerprinting, loop/stuck/timeout
|   +-- FocusRecovery            App relaunch on foreground loss
|   +-- ReflectionEngine         Post-action outcome evaluation
|   +-- ActionSpaceFilter        LLM action validation & correction
|
+-- ADBController                ADB wrapper (all commands have timeouts)
+-- UIParser                     XML hierarchy -> flat element list
+-- CodeContextBuilder           Reads .kt source files for analysis context
|
+-- ClaudeClient   (LLMClient)   Anthropic API: analyze_screen, analyze_failure, reflect_on_action
+-- GeminiClient   (LLMClient)   Google Gemini API
|
+-- ComplexityAssessor           simple / medium / hard / critical classification
+-- BuildManager                 Gradle assembleDebug + ADB install
```

---

## Adding a new game

1. Register the game's source paths in `GAME_CONTRACTS` at the top of `game_polygon.py`:

```python
GAME_CONTRACTS = {
    ...
    "my_game": [
        "yakkiedu/libraries/my-game/src/main/java/com/yakki/edu/mygame",
        "yakkiedu/domain/src/main/java/com/yakki/edu/domain/games/MyGameContract.kt",
    ],
}
```

2. Run: `python game_polygon.py --game my_game`

The LLM will use the registered source files as context when analysing failures.

---

## Design decisions

**Why MD5 for screen hashing?**
Python's built-in `hash()` is randomised across runs (PYTHONHASHSEED). MD5 gives a stable, reproducible fingerprint so stuck/loop detection works correctly even after a restart.

**Why validate LLM actions?**
LLMs occasionally propose coordinates outside the screen, off-centre taps, or very long text inputs. The `ActionSpaceFilter` corrects these before they reach the device, preventing wasted steps and confusing feedback loops.

**Why reflect after every action?**
A tap that "succeeds" at the ADB level but doesn't change the UI is a silent failure. Without reflection the LLM would keep tapping the same element. The reflection engine catches this immediately and either retries or moves on.

**Why complexity-based escalation?**
Not all failures are equal. A missing null-check is safe to auto-fix; an architectural race condition is not. The `ComplexityAssessor` ensures auto-fix is only attempted for problems where the LLM has high confidence and the scope is narrow.

---

## License

MIT
