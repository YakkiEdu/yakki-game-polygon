#!/usr/bin/env python3
"""
YAKKI Game Polygon - Intelligent Test-Fix-Build Cycle.

Automated game testing for Android apps using LLM vision + ADB:
1. Launch game on connected device via ADB
2. Screenshot each step and ask LLM "what action to take?"
3. Execute the action, reflect on outcome
4. On failure: analyze with codebase context, suggest fix
5. Optionally: apply fix, rebuild, retest (--auto-fix)

Usage:
    python game_polygon.py --game scrambler
    python game_polygon.py --game cloze --auto-fix
    python game_polygon.py --game quiz --analyze-only
    python game_polygon.py --list-games

Environment variables:
    ANTHROPIC_API_KEY   - Required when using --llm claude (default)
    GEMINI_API_KEY      - Required when using --llm gemini
    YAKKI_DEVICE        - ADB device address (default: localhost:5555)
    YAKKI_ROOT          - Root directory of the project (default: current dir)
"""

import subprocess
import base64
import json
import time
import argparse
import re
import os
from pathlib import Path
from typing import Optional, List, Dict, Any, TYPE_CHECKING
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Optional LLM dependencies (imported lazily so the script works without them
# until an actual test is run)
# ---------------------------------------------------------------------------
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

# =============================================================================
# Configuration — all sensitive values come from env-vars or CLI flags
# =============================================================================

# Project root: override with YAKKI_ROOT env-var (useful in CI or when the
# script is run from a directory other than the repo root).
YAKKI_ROOT = Path(os.environ.get("YAKKI_ROOT", "."))
YAKKIEDU_ROOT = YAKKI_ROOT / "yakkiedu"

# ADB device: override with YAKKI_DEVICE env-var or --device CLI flag.
# Default points to a local emulator; change for a real device.
DEFAULT_DEVICE = os.environ.get("YAKKI_DEVICE", "localhost:5555")

ADB = "adb"
OUTPUT_DIR = YAKKI_ROOT / "REPORTS" / "game_polygon"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Game contract locations — used to feed relevant source files to the LLM
# when analyzing a failure.
GAME_CONTRACTS: Dict[str, List[str]] = {
    "scrambler": [
        "yakkiedu/libraries/scrambler/src/main/java/com/yakki/edu/scrambler",
        "yakkiedu/domain/src/main/java/com/yakki/edu/domain/games/ScramblerContract.kt",
    ],
    "cloze": [
        "yakkiedu/app/src/main/java/com/yakki/edu/ui/cloze",
        "yakkiedu/domain/src/main/java/com/yakki/edu/domain/games/ClozeContract.kt",
    ],
    "quiz": [
        "yakkiedu/libraries/quiz-game/src/main/java/com/yakki/edu/quiz",
    ],
    "guest_day": [
        "yakkiedu/app/src/main/java/com/yakki/edu/ui/guest",
        "yakkiedu/domain/src/main/java/com/yakki/edu/domain/guest",
    ],
}

# =============================================================================
# Constants
# =============================================================================

# Loop / stuck detection
MAX_SAME_SCREEN_COUNT = 3   # Same screen fingerprint N times in a row → stuck
MAX_NO_PROGRESS_STEPS = 5   # No screen change for N steps → no progress
SCREEN_HASH_HISTORY = 10    # How many past hashes to remember for loop detection

# Problem complexity levels (drive escalation logic)
COMPLEXITY_SIMPLE   = "simple"    # Auto-fix is safe
COMPLEXITY_MEDIUM   = "medium"    # Needs human review
COMPLEXITY_HARD     = "hard"      # Needs multi-agent committee
COMPLEXITY_CRITICAL = "critical"  # Stop immediately

# Per-test timeouts
SCREEN_CHANGE_TIMEOUT = 10   # Seconds to wait after an action for the screen to change
ACTION_TIMEOUT        = 5    # Seconds allowed per action
MAX_TOTAL_TIME        = 600  # Hard cap: 10 minutes per full test run

# Reflection: after every action, ask the LLM "did that work?"
# ("measure seven times, cut once")
REFLECTION_ENABLED = True

# =============================================================================
# Screen State Tracker — detects stuck loops and focus loss
# =============================================================================

class ScreenStateTracker:
    """Tracks screen state across steps to detect stuck/loop/timeout conditions."""

    def __init__(self) -> None:
        self.screen_hashes: List[str] = []
        self.last_progress_step: int = 0
        self.start_time: float = time.time()
        self.same_screen_count: int = 0
        self.last_screen_hash: str = ""

    def compute_screen_hash(self, hierarchy: str, elements: List[Dict]) -> str:
        """Return a stable 16-char hex fingerprint of the current screen state.

        Uses element texts, descriptions and positions — MD5 for determinism
        across Python runs (Python's built-in hash() is randomised by default).
        """
        fingerprint = "|".join(
            f"{e.get('text', '')}{e.get('content_desc', '')}{e.get('center', ())}"
            for e in elements[:20]
        )
        return hashlib.md5(fingerprint.encode()).hexdigest()[:16]

    def update(self, hierarchy: str, elements: List[Dict], step: int) -> List[str]:
        """Update internal state and return a list of anomaly strings (may be empty)."""
        anomalies: List[str] = []
        current_hash = self.compute_screen_hash(hierarchy, elements)

        # Detect same screen repeated
        if current_hash == self.last_screen_hash:
            self.same_screen_count += 1
            if self.same_screen_count >= MAX_SAME_SCREEN_COUNT:
                anomalies.append(f"STUCK: Same screen for {self.same_screen_count} steps")
        else:
            self.same_screen_count = 0
            self.last_progress_step = step

        self.last_screen_hash = current_hash

        # Detect revisiting a screen (loop)
        recent = self.screen_hashes[-SCREEN_HASH_HISTORY:]
        if current_hash in recent:
            idx = recent.index(current_hash)
            anomalies.append(f"LOOP: Screen repeated from {SCREEN_HASH_HISTORY - idx} steps ago")

        self.screen_hashes.append(current_hash)
        # Trim history to avoid unbounded growth
        if len(self.screen_hashes) > SCREEN_HASH_HISTORY * 2:
            self.screen_hashes = self.screen_hashes[-SCREEN_HASH_HISTORY:]

        # Detect stale progress
        if step - self.last_progress_step >= MAX_NO_PROGRESS_STEPS:
            anomalies.append(
                f"NO_PROGRESS: No change for {step - self.last_progress_step} steps"
            )

        # Detect overall timeout
        elapsed = time.time() - self.start_time
        if elapsed > MAX_TOTAL_TIME:
            anomalies.append(
                f"TIMEOUT: Test exceeded {MAX_TOTAL_TIME}s ({elapsed:.0f}s elapsed)"
            )

        return anomalies

    def check_focus(self, hierarchy: str) -> Optional[str]:
        """Return an anomaly string if the target app is no longer in the foreground."""
        if "com.yakki.edu" not in hierarchy:
            return "FOCUS_LOST: App no longer in foreground"
        return None


# =============================================================================
# Complexity Assessor — decides when to escalate to a human / committee
# =============================================================================

class ComplexityAssessor:
    """Classifies problem complexity to decide the next escalation step."""

    CRITICAL_PATTERNS = [
        "crash", "exception", "fatal", "anr", "not responding",
        "security", "permission denied", "unauthorized",
    ]
    HARD_PATTERNS = [
        "architectural", "design", "refactor", "multiple files",
        "breaking change", "api change", "contract",
    ]
    MEDIUM_PATTERNS = [
        "state", "lifecycle", "navigation", "race condition",
        "timing", "async",
    ]

    def assess(
        self,
        failure_reason: str,
        analysis: Optional["AnalysisResult"] = None,
        anomalies: Optional[List[str]] = None,
    ) -> str:
        """Return a COMPLEXITY_* constant for the given failure context."""
        text = failure_reason.lower()
        if analysis:
            text += f" {analysis.root_cause.lower()} {analysis.suggested_fix.lower()}"
        if anomalies:
            text += " " + " ".join(anomalies).lower()

        for pattern in self.CRITICAL_PATTERNS:
            if pattern in text:
                return COMPLEXITY_CRITICAL

        for pattern in self.HARD_PATTERNS:
            if pattern in text:
                return COMPLEXITY_HARD

        for pattern in self.MEDIUM_PATTERNS:
            if pattern in text:
                return COMPLEXITY_MEDIUM

        if analysis and analysis.confidence == "low":
            return COMPLEXITY_HARD
        if analysis and analysis.confidence == "medium":
            return COMPLEXITY_MEDIUM

        if anomalies:
            if any("LOOP" in a for a in anomalies):
                return COMPLEXITY_MEDIUM
            if any("TIMEOUT" in a for a in anomalies):
                return COMPLEXITY_HARD

        return COMPLEXITY_SIMPLE

    def needs_committee(self, complexity: str) -> bool:
        """True when the complexity warrants multi-agent committee review."""
        return complexity in (COMPLEXITY_HARD, COMPLEXITY_CRITICAL)

    def should_stop(self, complexity: str) -> bool:
        """True when we must halt and wait for a human."""
        return complexity == COMPLEXITY_CRITICAL


# =============================================================================
# Focus Recovery — relaunches the app if it loses focus
# =============================================================================

class FocusRecovery:
    """Detects app focus loss and attempts to recover by relaunching."""

    def __init__(self, adb: "ADBController") -> None:
        self.adb = adb
        self.recovery_attempts: int = 0
        self.max_recovery_attempts: int = 3

    def check_and_recover(self, hierarchy: str) -> bool:
        """Return True if the app has focus (or was successfully recovered)."""
        if "com.yakki.edu" in hierarchy:
            self.recovery_attempts = 0
            return True

        if self.recovery_attempts >= self.max_recovery_attempts:
            return False

        self.recovery_attempts += 1
        print(f"  [RECOVERY] Attempt {self.recovery_attempts}: Relaunching app…")

        self.adb.press_back()
        time.sleep(0.5)
        self.adb.launch_app()
        time.sleep(2)

        # Verify recovery before returning success
        new_hierarchy = self.adb.get_ui_hierarchy()
        if "com.yakki.edu" not in new_hierarchy:
            print("  [RECOVERY] App still not in foreground after relaunch")
            return self.recovery_attempts < self.max_recovery_attempts

        return True


# =============================================================================
# Reflection Engine — evaluates each action after execution
# ("measure seven times, cut once")
# =============================================================================

@dataclass
class ReflectionResult:
    """Structured outcome of reflecting on a single action."""
    action_succeeded: bool          # Did the action achieve its intended effect?
    screen_changed: bool            # Did the screen visually change at all?
    progress_made: bool             # Are we closer to completing the objective?
    unexpected_state: Optional[str] = None   # Description of an unexpected UI state
    should_retry: bool = False               # Should we retry the same action?
    alternative_action: Optional[Dict] = None  # LLM-suggested alternative


class ReflectionEngine:
    """Reflects on each executed action to catch failures before they cascade.

    Two-tier approach:
    1. Fast local check: compare element hashes before/after (no LLM cost).
    2. Deep vision check: send before/after screenshots to the LLM for nuanced
       analysis (only when the fast check is inconclusive).
    """

    def __init__(self, llm_client: "LLMClient") -> None:
        self.llm = llm_client
        self.action_history: List[Dict] = []

    def reflect(
        self,
        before_screenshot: str,
        action: Dict,
        after_screenshot: str,
        before_elements: List[Dict],
        after_elements: List[Dict],
        game: str,
        objective: str,
    ) -> ReflectionResult:
        """Analyse action outcome; returns a ReflectionResult."""

        # Fast local check: did the element tree change?
        before_hash = hashlib.md5(str(before_elements[:10]).encode()).hexdigest()[:8]
        after_hash  = hashlib.md5(str(after_elements[:10]).encode()).hexdigest()[:8]
        screen_changed = before_hash != after_hash

        # A tap or swipe that doesn't change anything is suspicious
        if action.get("action") in ("tap", "swipe") and not screen_changed:
            return ReflectionResult(
                action_succeeded=False,
                screen_changed=False,
                progress_made=False,
                unexpected_state="Screen didn't change after tap/swipe",
                should_retry=True,
            )

        self.action_history.append({
            "action": action,
            "screen_changed": screen_changed,
            "timestamp": time.time(),
        })

        # Deep vision check via LLM (if the client supports it)
        if hasattr(self.llm, "reflect_on_action"):
            return self.llm.reflect_on_action(
                before_screenshot, action, after_screenshot,
                before_elements, after_elements, game, objective,
            )

        # Fallback: optimistically assume success when the screen changed
        return ReflectionResult(
            action_succeeded=screen_changed,
            screen_changed=screen_changed,
            progress_made=screen_changed,
        )


# =============================================================================
# Action Space Filter — validates / corrects LLM actions before execution
# =============================================================================

class ActionSpaceFilter:
    """Validates and corrects LLM-proposed actions before they are executed.

    Prevents the LLM from:
    - Tapping outside the physical screen bounds
    - Sending absurdly long type-text commands
    - Issuing a swipe with missing coordinates
    """

    def __init__(self, screen_width: int = 1080, screen_height: int = 2400) -> None:
        self.screen_width  = screen_width
        self.screen_height = screen_height

    def validate(self, action: Dict, elements: List[Dict]) -> Dict:
        """Return a (possibly corrected) copy of *action*."""
        action_type = action.get("action")
        if action_type == "tap":
            return self._validate_tap(action, elements)
        if action_type == "swipe":
            return self._validate_swipe(action)
        if action_type == "type":
            return self._validate_type(action)
        return action

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_tap(self, action: Dict, elements: List[Dict]) -> Dict:
        x, y = action.get("x", 0), action.get("y", 0)

        # Clamp out-of-bounds coordinates
        if not (0 <= x <= self.screen_width and 0 <= y <= self.screen_height):
            print(f"  [FILTER] Tap ({x},{y}) out of bounds — clamping")
            action = dict(action)
            action["x"] = max(0, min(x, self.screen_width))
            action["y"] = max(0, min(y, self.screen_height))
            return action

        # Snap to the nearest clickable element centre if it's close enough
        nearest = self._nearest_clickable(x, y, elements)
        if nearest and nearest["distance"] < 50:
            cx, cy = nearest["center"]
            if (cx, cy) != (x, y):
                print(f"  [FILTER] Snapping tap to element centre ({cx},{cy})")
                action = dict(action)
                action["x"] = cx
                action["y"] = cy
        return action

    def _validate_swipe(self, action: Dict) -> Dict:
        """Fill in any missing swipe coordinates with the screen centre."""
        for key in ("x1", "y1", "x2", "y2"):
            if key not in action:
                action = dict(action)
                action[key] = self.screen_width // 2 if "x" in key else self.screen_height // 2
        return action

    def _validate_type(self, action: Dict) -> Dict:
        """Truncate unreasonably long type-text payloads."""
        text = action.get("text", "")
        if len(text) > 500:
            print(f"  [FILTER] Truncating oversized type input ({len(text)} chars → 500)")
            action = dict(action)
            action["text"] = text[:500]
        return action

    def _nearest_clickable(self, x: int, y: int, elements: List[Dict]) -> Optional[Dict]:
        """Return the nearest clickable element and its pixel distance from (x, y)."""
        best: Optional[Dict] = None
        best_dist = float("inf")
        for el in elements:
            if not el.get("clickable"):
                continue
            cx, cy = el.get("center", (None, None))
            if cx is None:
                continue
            dist = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = {"center": (cx, cy), "distance": dist}
        return best


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TestResult:
    """Result of a single game test run."""
    success: bool
    game: str
    steps_taken: int
    failure_reason: Optional[str] = None
    failure_screenshot: Optional[Path] = None
    failure_hierarchy: Optional[str] = None
    all_screenshots: List[Path] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """LLM analysis of a test failure."""
    root_cause: str
    affected_files: List[str]
    suggested_fix: str
    confidence: str   # "high" | "medium" | "low"


@dataclass
class FixResult:
    """Outcome of an attempted auto-fix."""
    applied: bool
    files_modified: List[str]
    build_success: bool
    retest_success: Optional[bool] = None


# =============================================================================
# ADB Controller
# =============================================================================

class ADBController:
    """Wraps ADB commands with timeouts to prevent indefinite hangs."""

    def __init__(self, device: str = DEFAULT_DEVICE) -> None:
        self.device = device
        self._screen_size: Optional[tuple] = None

    # ------------------------------------------------------------------
    # Internal runners
    # ------------------------------------------------------------------

    def _run(self, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = [ADB, "-s", self.device] + args
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"  [WARN] ADB timeout: {' '.join(args[:2])}")
            return subprocess.CompletedProcess(cmd, 1, "", "timeout")

    def _run_bytes(self, args: List[str], timeout: int = 30) -> bytes:
        cmd = [ADB, "-s", self.device] + args
        try:
            return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
        except subprocess.TimeoutExpired:
            print(f"  [WARN] ADB timeout: {' '.join(args[:2])}")
            return b""

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        result = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
        return bool(re.search(rf"{re.escape(self.device)}" + r"\s+device", result.stdout))

    def connect(self) -> bool:
        subprocess.run([ADB, "connect", self.device])
        time.sleep(2)
        return self.is_connected()

    def get_screen_size(self) -> tuple:
        if self._screen_size:
            return self._screen_size
        result = self._run(["shell", "wm", "size"])
        match = re.search(r"(\d+)x(\d+)", result.stdout)
        self._screen_size = (int(match.group(1)), int(match.group(2))) if match else (1080, 2400)
        return self._screen_size

    # ------------------------------------------------------------------
    # Screen capture
    # ------------------------------------------------------------------

    def screenshot(self) -> bytes:
        return self._run_bytes(["exec-out", "screencap", "-p"])

    def screenshot_base64(self) -> str:
        return base64.standard_b64encode(self.screenshot()).decode()

    def save_screenshot(self, path: Path) -> bool:
        data = self.screenshot()
        if len(data) < 1000:   # Sanity-check: an empty PNG is ~67 bytes
            return False
        path.write_bytes(data)
        return True

    # ------------------------------------------------------------------
    # UI inspection
    # ------------------------------------------------------------------

    def get_ui_hierarchy(self) -> str:
        result = self._run_bytes(["exec-out", "uiautomator", "dump", "/dev/stdout"])
        return result.decode("utf-8", errors="ignore")

    # ------------------------------------------------------------------
    # Input actions
    # ------------------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)])
        time.sleep(0.3)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> None:
        self._run(["shell", "input", "swipe",
                   str(x1), str(y1), str(x2), str(y2), str(duration)])
        time.sleep(0.5)

    def type_text(self, text: str) -> None:
        escaped = text.replace(" ", "%s").replace("'", "\\'")
        self._run(["shell", "input", "text", escaped])
        time.sleep(0.2)

    def press_back(self) -> None:
        self._run(["shell", "input", "keyevent", "4"])
        time.sleep(0.3)

    def launch_app(self, package: str = "com.yakki.edu") -> None:
        self._run(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
        time.sleep(2)


# =============================================================================
# UI Parser — converts XML hierarchy into a flat list of element dicts
# =============================================================================

class UIParser:
    """Parses the XML hierarchy dumped by `uiautomator dump`."""

    @staticmethod
    def parse_bounds(bounds_str: str) -> tuple:
        """Parse '[x1,y1][x2,y2]' → (x1, y1, x2, y2)."""
        match = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
        if len(match) == 2:
            return (int(match[0][0]), int(match[0][1]),
                    int(match[1][0]), int(match[1][1]))
        return (0, 0, 0, 0)

    @staticmethod
    def get_center(bounds: tuple) -> tuple:
        x1, y1, x2, y2 = bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @classmethod
    def extract_elements(cls, hierarchy_xml: str) -> List[Dict]:
        """Return a list of element dicts from the UI hierarchy XML."""
        elements: List[Dict] = []
        try:
            # Trim trailing garbage that sometimes appears after </hierarchy>
            end = hierarchy_xml.find("</hierarchy>")
            if end > 0:
                hierarchy_xml = hierarchy_xml[: end + len("</hierarchy>")]
            root = ET.fromstring(hierarchy_xml)
            for node in root.iter("node"):
                text         = node.get("text", "")
                content_desc = node.get("content-desc", "")
                resource_id  = node.get("resource-id", "")
                clickable    = node.get("clickable") == "true"
                bounds       = cls.parse_bounds(node.get("bounds", ""))
                center       = cls.get_center(bounds)
                if text or content_desc or clickable:
                    elements.append({
                        "text":         text,
                        "content_desc": content_desc,
                        "resource_id":  resource_id,
                        "clickable":    clickable,
                        "center":       center,
                        "class":        node.get("class", "").split(".")[-1],
                    })
        except ET.ParseError:
            pass
        return elements


# =============================================================================
# Code Context Builder — feeds relevant Kotlin files to the LLM
# =============================================================================

class CodeContextBuilder:
    """Assembles a text snippet of relevant game source files for LLM analysis."""

    def __init__(self, yakki_root: Path = YAKKI_ROOT) -> None:
        self.root = yakki_root

    def get_game_files(self, game: str) -> List[Path]:
        """Collect up to 10 source files relevant to *game*."""
        files: List[Path] = []
        for p in GAME_CONTRACTS.get(game.lower(), []):
            full_path = self.root / p
            if full_path.is_dir():
                files.extend(full_path.rglob("*.kt"))
            elif full_path.exists():
                files.append(full_path)
        return files[:10]

    def read_file_summary(self, path: Path, max_lines: int = 100) -> str:
        try:
            content = path.read_text(encoding="utf-8")
            lines = content.split("\n")
            if len(lines) > max_lines:
                return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines)"
            return content
        except Exception as exc:
            return f"Error reading file: {exc}"

    def build_context(self, game: str) -> str:
        files = self.get_game_files(game)
        if not files:
            return "No game-specific files found."
        parts = []
        for f in files:
            rel = f.relative_to(self.root)
            parts.append(f"=== {rel} ===\n{self.read_file_summary(f)}")
        return "\n\n".join(parts)


# =============================================================================
# LLM Clients
# =============================================================================

class LLMClient:
    """Abstract base — concrete backends implement the two main methods."""

    def analyze_screen(
        self,
        screenshot_b64: str,
        hierarchy: str,
        elements: List[Dict],
        game: str,
        objective: str,
    ) -> Dict:
        raise NotImplementedError

    def analyze_failure(self, test_result: TestResult, code_context: str) -> AnalysisResult:
        raise NotImplementedError


class ClaudeClient(LLMClient):
    """LLM backend powered by Anthropic's Claude (vision-capable)."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        if not HAS_ANTHROPIC:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            )
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. "
                "Export it before running: export ANTHROPIC_API_KEY=sk-ant-..."
            )
        self.client = anthropic.Anthropic(api_key=resolved_key)

    def analyze_screen(
        self,
        screenshot_b64: str,
        hierarchy: str,
        elements: List[Dict],
        game: str,
        objective: str,
    ) -> Dict:
        """Ask Claude what action to take on the current screen."""
        elements_summary = "\n".join(
            f"- '{e['text'] or e['content_desc'] or e['resource_id']}' at {e['center']}"
            for e in elements[:15]
        )
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Game: {game}, Objective: {objective}\n\n"
                            f"Clickable elements:\n{elements_summary}\n\n"
                            "What action should I take? Respond with JSON only:\n"
                            '{"action": "tap|swipe|type|wait|back|done", '
                            '"x": 0, "y": 0, "reason": "...", "success": true/false}'
                        ),
                    },
                ],
            }],
        )
        try:
            return json.loads(response.content[0].text)
        except Exception:
            return {"action": "wait", "reason": "parse_error"}

    def analyze_failure(self, test_result: TestResult, code_context: str) -> AnalysisResult:
        """Ask Claude to diagnose a test failure given source context."""
        screenshot_b64 = ""
        if test_result.failure_screenshot and test_result.failure_screenshot.exists():
            screenshot_b64 = base64.standard_b64encode(
                test_result.failure_screenshot.read_bytes()
            ).decode()

        text_block = {
            "type": "text",
            "text": (
                f"Test failure analysis for {test_result.game}:\n\n"
                f"Failure reason: {test_result.failure_reason}\n"
                f"Steps taken: {test_result.steps_taken}\n\n"
                "UI Hierarchy at failure:\n"
                f"{test_result.failure_hierarchy[:2000] if test_result.failure_hierarchy else 'N/A'}\n\n"
                f"Relevant code context:\n{code_context[:8000]}\n\n"
                "Analyse the root cause and suggest a fix. Respond with JSON:\n"
                "{\n"
                '  "root_cause": "description",\n'
                '  "affected_files": ["path/to/file.kt"],\n'
                '  "suggested_fix": "code diff or description",\n'
                '  "confidence": "high|medium|low"\n'
                "}"
            ),
        }
        content = [text_block]
        if screenshot_b64:
            content.insert(0, {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            })

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
        try:
            data = json.loads(response.content[0].text)
            return AnalysisResult(
                root_cause=data.get("root_cause", "Unknown"),
                affected_files=data.get("affected_files", []),
                suggested_fix=data.get("suggested_fix", ""),
                confidence=data.get("confidence", "low"),
            )
        except Exception:
            return AnalysisResult(
                root_cause="Failed to parse LLM response",
                affected_files=[],
                suggested_fix=response.content[0].text,
                confidence="low",
            )

    def reflect_on_action(
        self,
        before_screenshot: str,
        action: Dict,
        after_screenshot: str,
        before_elements: List[Dict],
        after_elements: List[Dict],
        game: str,
        objective: str,
    ) -> ReflectionResult:
        """Deep vision reflection: compare before/after screenshots."""
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "BEFORE action:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": before_screenshot}},
                    {"type": "text", "text": f"ACTION: {json.dumps(action)}"},
                    {"type": "text", "text": "AFTER action:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": after_screenshot}},
                    {
                        "type": "text",
                        "text": (
                            f"Game: {game}, Objective: {objective}\n\n"
                            "Reflect: Did this action achieve its purpose? "
                            "Are we closer to the objective?\n"
                            "Respond JSON only:\n"
                            '{"action_succeeded": true/false, "progress_made": true/false, '
                            '"unexpected_state": "..." or null, "should_retry": true/false}'
                        ),
                    },
                ],
            }],
        )
        try:
            data = json.loads(response.content[0].text)
            return ReflectionResult(
                action_succeeded=data.get("action_succeeded", True),
                screen_changed=True,
                progress_made=data.get("progress_made", True),
                unexpected_state=data.get("unexpected_state"),
                should_retry=data.get("should_retry", False),
            )
        except Exception:
            return ReflectionResult(
                action_succeeded=True,
                screen_changed=True,
                progress_made=True,
            )


class GeminiClient(LLMClient):
    """LLM backend powered by Google Gemini (vision-capable)."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        if not HAS_GEMINI:
            raise ImportError(
                "google-generativeai package not installed. "
                "Run: pip install google-generativeai Pillow"
            )
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "GEMINI_API_KEY not set. "
                "Export it before running: export GEMINI_API_KEY=AIza..."
            )
        genai.configure(api_key=resolved_key)
        self.model = genai.GenerativeModel("gemini-2.5-flash")

    def analyze_screen(
        self,
        screenshot_b64: str,
        hierarchy: str,
        elements: List[Dict],
        game: str,
        objective: str,
    ) -> Dict:
        """Ask Gemini what action to take on the current screen."""
        import PIL.Image
        import io

        img_bytes = base64.standard_b64decode(screenshot_b64)
        img = PIL.Image.open(io.BytesIO(img_bytes))

        elements_summary = "\n".join(
            f"- '{e['text'] or e['content_desc']}' at {e['center']}"
            for e in elements[:15]
        )
        prompt = (
            f"Game: {game}, Objective: {objective}\n\n"
            f"Clickable elements:\n{elements_summary}\n\n"
            "What action should I take? Respond with JSON only:\n"
            '{"action": "tap|swipe|type|wait|back|done", "x": 0, "y": 0, "reason": "..."}'
        )
        response = self.model.generate_content([prompt, img])
        try:
            json_match = re.search(r"\{[^}]+\}", response.text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception:
            pass
        return {"action": "wait", "reason": "parse_error"}

    def analyze_failure(self, test_result: TestResult, code_context: str) -> AnalysisResult:
        """Ask Gemini to diagnose a test failure."""
        prompt = (
            f"Test failure analysis for {test_result.game}:\n\n"
            f"Failure reason: {test_result.failure_reason}\n"
            f"Steps taken: {test_result.steps_taken}\n\n"
            "UI Hierarchy at failure:\n"
            f"{test_result.failure_hierarchy[:2000] if test_result.failure_hierarchy else 'N/A'}\n\n"
            f"Relevant code:\n{code_context[:8000]}\n\n"
            "Analyse root cause and suggest fix. JSON response:\n"
            '{"root_cause": "...", "affected_files": [...], '
            '"suggested_fix": "...", "confidence": "high|medium|low"}'
        )
        response = self.model.generate_content(prompt)
        try:
            json_match = re.search(r"\{[^}]+\}", response.text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return AnalysisResult(
                    root_cause=data.get("root_cause", "Unknown"),
                    affected_files=data.get("affected_files", []),
                    suggested_fix=data.get("suggested_fix", ""),
                    confidence=data.get("confidence", "low"),
                )
        except Exception:
            pass
        return AnalysisResult(
            root_cause="Failed to parse",
            affected_files=[],
            suggested_fix=response.text,
            confidence="low",
        )


# =============================================================================
# Game Tester — orchestrates one full test run
# =============================================================================

class GameTester:
    """Runs a step-by-step LLM-guided test of a single game."""

    def __init__(
        self,
        adb: ADBController,
        llm: LLMClient,
        keep_screenshots: bool = False,
    ) -> None:
        self.adb = adb
        self.llm = llm
        self.keep_screenshots = keep_screenshots
        self.screenshots: List[Path] = []
        self.state_tracker: Optional[ScreenStateTracker] = None
        self.focus_recovery: Optional[FocusRecovery] = None
        self.reflection_engine: Optional[ReflectionEngine] = None
        self.action_filter: Optional[ActionSpaceFilter] = None

    def test_game(self, game: str, objective: str, max_steps: int = 30) -> TestResult:
        """Run a full LLM-guided test; return the outcome."""
        test_dir = OUTPUT_DIR / f"{game}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        test_dir.mkdir(parents=True, exist_ok=True)

        # Reset per-run state
        self.screenshots = []
        self.state_tracker  = ScreenStateTracker()
        self.focus_recovery = FocusRecovery(self.adb)
        self.reflection_engine = ReflectionEngine(self.llm) if REFLECTION_ENABLED else None
        w, h = self.adb.get_screen_size()
        self.action_filter = ActionSpaceFilter(w, h)

        all_anomalies: List[str] = []
        retry_count = 0
        max_retries = 2  # Max reflection-driven retries per action

        print(f"\n{'=' * 60}")
        print(f"Testing : {game}")
        print(f"Goal    : {objective}")
        print(f"{'=' * 60}")

        self.adb.launch_app()
        time.sleep(2)

        for step in range(1, max_steps + 1):
            print(f"\n[Step {step}/{max_steps}]")

            screenshot_b64 = self.adb.screenshot_base64()
            hierarchy      = self.adb.get_ui_hierarchy()
            elements       = UIParser.extract_elements(hierarchy)

            # --- Focus check ---
            focus_issue = self.state_tracker.check_focus(hierarchy)
            if focus_issue:
                print(f"  [!] {focus_issue}")
                all_anomalies.append(focus_issue)
                if not self.focus_recovery.check_and_recover(hierarchy):
                    result = TestResult(
                        success=False, game=game, steps_taken=step,
                        failure_reason="Focus lost and recovery failed",
                        failure_screenshot=self.screenshots[-1] if self.screenshots else None,
                        failure_hierarchy=hierarchy,
                        all_screenshots=self.screenshots,
                        anomalies=all_anomalies,
                    )
                    self._cleanup_screenshots(result)
                    return result
                continue  # Retry this step after recovery

            # --- Stuck / loop / timeout check ---
            anomalies = self.state_tracker.update(hierarchy, elements, step)
            if anomalies:
                for a in anomalies:
                    print(f"  [!] {a}")
                all_anomalies.extend(anomalies)

                if any("TIMEOUT" in a for a in anomalies):
                    result = TestResult(
                        success=False, game=game, steps_taken=step,
                        failure_reason="Test timeout exceeded",
                        failure_screenshot=self.screenshots[-1] if self.screenshots else None,
                        failure_hierarchy=hierarchy,
                        all_screenshots=self.screenshots,
                        anomalies=all_anomalies,
                    )
                    self._cleanup_screenshots(result)
                    return result

            # Save screenshot for this step
            screenshot_path = test_dir / f"step_{step:03d}.png"
            self.adb.save_screenshot(screenshot_path)
            self.screenshots.append(screenshot_path)

            # Ask LLM what to do
            print("  Analysing…")
            anomaly_ctx = f"\nDetected issues: {', '.join(anomalies)}" if anomalies else ""
            action = self.llm.analyze_screen(
                screenshot_b64, hierarchy, elements, game, objective + anomaly_ctx
            )
            print(f"  Action: {action.get('action')} — {action.get('reason', '')}")

            # Terminal action: LLM declares the test done
            if action.get("action") == "done":
                success = action.get("success", False)
                result = TestResult(
                    success=success, game=game, steps_taken=step,
                    failure_reason=None if success else action.get("reason"),
                    failure_screenshot=screenshot_path if not success else None,
                    failure_hierarchy=hierarchy if not success else None,
                    all_screenshots=self.screenshots,
                    anomalies=all_anomalies,
                )
                self._cleanup_screenshots(result)
                return result

            # --- Recovery for stuck / no-progress states ---
            if any("STUCK" in a or "NO_PROGRESS" in a for a in anomalies):
                if self.state_tracker.same_screen_count == MAX_SAME_SCREEN_COUNT:
                    print("  [RECOVERY] Trying back button…")
                    self.adb.press_back()
                    retry_count = 0
                elif self.state_tracker.same_screen_count > MAX_SAME_SCREEN_COUNT:
                    print("  [RECOVERY] Trying scroll…")
                    self.adb.swipe(w // 2, h * 3 // 4, w // 2, h // 4)
            else:
                before_screenshot = screenshot_b64
                before_elements   = elements

                # Validate / correct action before executing
                action = self.action_filter.validate(action, elements)

                self._execute_action(action)

                # --- Reflection ---
                if self.reflection_engine and action.get("action") not in ("wait", "done"):
                    time.sleep(0.3)
                    after_screenshot = self.adb.screenshot_base64()
                    after_hierarchy  = self.adb.get_ui_hierarchy()
                    after_elements   = UIParser.extract_elements(after_hierarchy)

                    print("  Reflecting…")
                    reflection = self.reflection_engine.reflect(
                        before_screenshot, action, after_screenshot,
                        before_elements, after_elements, game, objective,
                    )

                    if not reflection.action_succeeded:
                        print(
                            f"  [REFLECT] Action failed: "
                            f"{reflection.unexpected_state or 'unknown reason'}"
                        )
                        all_anomalies.append(
                            f"REFLECTION: {reflection.unexpected_state or 'action failed'}"
                        )
                        if reflection.should_retry and retry_count < max_retries:
                            retry_count += 1
                            print(f"  [REFLECT] Retrying ({retry_count}/{max_retries})…")
                            self._execute_action(action)
                        else:
                            retry_count = 0
                    else:
                        if reflection.progress_made:
                            print("  [REFLECT] Progress made ✓")
                        retry_count = 0

            time.sleep(0.5)

        # Exceeded max steps
        result = TestResult(
            success=False, game=game, steps_taken=max_steps,
            failure_reason="Max steps reached",
            failure_screenshot=self.screenshots[-1] if self.screenshots else None,
            failure_hierarchy=self.adb.get_ui_hierarchy(),
            all_screenshots=self.screenshots,
            anomalies=all_anomalies,
        )
        self._cleanup_screenshots(result)
        return result

    def _cleanup_screenshots(self, result: TestResult) -> None:
        """Delete screenshots after a run to save disk space.

        On success: delete all screenshots (nothing to debug).
        On failure: keep only the failure screenshot, delete the rest.
        Pass --keep-screenshots to disable all deletion.
        """
        if self.keep_screenshots:
            return

        if result.success:
            for path in self.screenshots:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            if self.screenshots:
                try:
                    self.screenshots[0].parent.rmdir()
                except Exception:
                    pass
            print(f"  [CLEANUP] Deleted {len(self.screenshots)} screenshots (success run)")
        else:
            keep = {result.failure_screenshot} if result.failure_screenshot else set()
            deleted = 0
            for path in self.screenshots:
                if path not in keep:
                    try:
                        path.unlink(missing_ok=True)
                        deleted += 1
                    except Exception:
                        pass
            kept = len(self.screenshots) - deleted
            if deleted > 0:
                print(f"  [CLEANUP] Kept {kept} failure screenshot(s), deleted {deleted} others")

    def _execute_action(self, action: Dict) -> None:
        """Dispatch an LLM action to the appropriate ADB call."""
        action_type = action.get("action")
        if action_type == "tap":
            self.adb.tap(action.get("x", 0), action.get("y", 0))
        elif action_type == "swipe":
            self.adb.swipe(
                action.get("x1", 0), action.get("y1", 0),
                action.get("x2", 0), action.get("y2", 0),
            )
        elif action_type == "type":
            self.adb.type_text(action.get("text", ""))
        elif action_type == "back":
            self.adb.press_back()
        elif action_type == "wait":
            time.sleep(1)


# =============================================================================
# Build Manager — compiles and installs the Android app
# =============================================================================

class BuildManager:
    """Builds the debug APK and installs it on the target device."""

    def __init__(self, yakkiedu_root: Path = YAKKIEDU_ROOT) -> None:
        self.root = yakkiedu_root

    def build_debug(self) -> bool:
        print("\nBuilding APK…")
        result = subprocess.run(
            ["./gradlew.bat", "assembleDebug"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  Build successful!")
        else:
            print(f"  Build failed: {result.stderr[:500]}")
        return result.returncode == 0

    def install(self, device: str = DEFAULT_DEVICE) -> bool:
        apk_path = self.root / "app/build/outputs/apk/debug/app-debug.apk"
        if not apk_path.exists():
            print("  APK not found!")
            return False
        result = subprocess.run(
            [ADB, "-s", device, "install", "-r", str(apk_path)],
            capture_output=True,
            text=True,
        )
        success = "Success" in result.stdout
        print(f"  Install: {'Success' if success else 'Failed'}")
        return success


# =============================================================================
# Game Polygon — top-level test-analyse-fix-rebuild controller
# =============================================================================

class GamePolygon:
    """Orchestrates the full test → analyse → (fix → rebuild → retest) loop."""

    def __init__(
        self,
        llm_backend: str = "claude",
        device: str = DEFAULT_DEVICE,
        keep_screenshots: bool = False,
    ) -> None:
        self.adb               = ADBController(device)
        self.context_builder   = CodeContextBuilder()
        self.build_manager     = BuildManager()
        self.complexity_assessor = ComplexityAssessor()

        if llm_backend == "claude":
            self.llm = ClaudeClient()
        elif llm_backend == "gemini":
            self.llm = GeminiClient()
        else:
            raise ValueError(f"Unknown LLM backend: {llm_backend!r}")

        self.tester = GameTester(self.adb, self.llm, keep_screenshots=keep_screenshots)

    def run_test(self, game: str, objective: str, max_steps: int = 30) -> TestResult:
        if not self.adb.is_connected():
            print(f"Connecting to {self.adb.device}…")
            if not self.adb.connect():
                raise RuntimeError("Failed to connect to device")
        return self.tester.test_game(game, objective, max_steps)

    def analyze_failure(self, result: TestResult) -> AnalysisResult:
        print("\nAnalysing failure with code context…")
        code_context = self.context_builder.build_context(result.game)
        return self.llm.analyze_failure(result, code_context)

    def run_cycle(
        self,
        game: str,
        objective: str,
        auto_fix: bool = False,
        max_iterations: int = 3,
    ) -> None:
        """Run up to *max_iterations* test → analyse → fix cycles."""
        for iteration in range(1, max_iterations + 1):
            print(f"\n{'#' * 60}")
            print(f"# ITERATION {iteration}/{max_iterations}")
            print(f"{'#' * 60}")

            result = self.run_test(game, objective)

            if result.success:
                print(f"\n[OK] Test passed in {result.steps_taken} steps!")
                return

            if result.anomalies:
                print("\n[!] Anomalies detected during test:")
                for a in result.anomalies:
                    print(f"    - {a}")

            print(f"\n[FAIL] {result.failure_reason}")
            analysis = self.analyze_failure(result)

            complexity = self.complexity_assessor.assess(
                result.failure_reason or "", analysis, result.anomalies
            )
            print(f"\n[COMPLEXITY] {complexity.upper()}")

            if self.complexity_assessor.should_stop(complexity):
                print("\n[CRITICAL] Problem requires immediate human attention!")
                self._save_incident_report(result, analysis, complexity)
                return

            if self.complexity_assessor.needs_committee(complexity):
                print(f"\n[ESCALATE] Complexity '{complexity}' → committee review needed.")
                self._request_committee(result, analysis, complexity)
                if not auto_fix:
                    return

            self._print_analysis(analysis)

            if not auto_fix:
                print("\nAuto-fix disabled. Stopping here.")
                return

            if analysis.confidence == "low":
                print("\nLow-confidence fix — skipping auto-apply.")
                continue

            if complexity != COMPLEXITY_SIMPLE:
                print(f"\nComplexity '{complexity}' too high for auto-fix. Manual review required.")
                continue

            # Placeholder — actual file patching is not yet implemented
            print("\nAuto-fix not yet implemented. Would apply:")
            print(f"  Files : {analysis.affected_files}")
            print(f"  Fix   : {analysis.suggested_fix[:200]}…")

        print(f"\nMax iterations ({max_iterations}) reached.")

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def _save_incident_report(
        self, result: TestResult, analysis: AnalysisResult, complexity: str
    ) -> Path:
        """Write a Markdown incident report for critical failures."""
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = OUTPUT_DIR / f"INCIDENT_{result.game}_{timestamp}.md"
        report = (
            "# CRITICAL INCIDENT REPORT\n\n"
            f"**Game:** {result.game}\n"
            f"**Time:** {datetime.now().isoformat()}\n"
            f"**Complexity:** {complexity}\n"
            f"**Steps Taken:** {result.steps_taken}\n\n"
            "## Failure Reason\n"
            f"{result.failure_reason}\n\n"
            "## Anomalies Detected\n"
            + (
                "\n".join(f"- {a}" for a in result.anomalies)
                if result.anomalies else "None"
            )
            + "\n\n"
            "## Analysis\n"
            f"**Root Cause:** {analysis.root_cause}\n"
            f"**Confidence:** {analysis.confidence}\n"
            f"**Affected Files:** {', '.join(analysis.affected_files) or 'Unknown'}\n\n"
            "## Suggested Fix\n"
            f"```\n{analysis.suggested_fix}\n```\n\n"
            "## Screenshots\n"
            + "\n".join(f"- {s}" for s in result.all_screenshots[-5:])
        )
        report_path.write_text(report, encoding="utf-8")
        print(f"\n[INCIDENT] Report saved: {report_path}")
        return report_path

    def _request_committee(
        self, result: TestResult, analysis: AnalysisResult, complexity: str
    ) -> None:
        """Write a committee review request for hard / critical problems."""
        timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_path = OUTPUT_DIR / f"COMMITTEE_REQUEST_{result.game}_{timestamp}.md"
        request = (
            "# COMMITTEE REVIEW REQUEST\n\n"
            f"**Game:** {result.game}\n"
            f"**Complexity:** {complexity}\n"
            f"**Generated:** {datetime.now().isoformat()}\n\n"
            "## Problem Summary\n"
            f"{result.failure_reason}\n\n"
            "## Anomalies\n"
            + (
                "\n".join(f"- {a}" for a in result.anomalies)
                if result.anomalies else "None"
            )
            + "\n\n"
            "## Initial Analysis (Single Agent)\n"
            f"**Root Cause:** {analysis.root_cause}\n"
            f"**Confidence:** {analysis.confidence}\n"
            "**Affected Files:**\n"
            + (
                "\n".join(f"- {f}" for f in analysis.affected_files)
                if analysis.affected_files else "- Unknown"
            )
            + "\n\n"
            "## Suggested Fix\n"
            f"```\n{analysis.suggested_fix}\n```\n\n"
            "## Request\n"
            f"Complexity level '{complexity}' requires multi-agent review.\n"
            f'Run: `python scripts/committee_run5.py --prompt "{request_path}"`\n\n'
            "## Code Contract Locations\n"
            + "\n".join(
                f"- {p}" for p in GAME_CONTRACTS.get(result.game.lower(), ["Unknown"])
            )
        )
        request_path.write_text(request, encoding="utf-8")
        print(f"\n[COMMITTEE] Review request saved: {request_path}")

    def _print_analysis(self, analysis: AnalysisResult) -> None:
        print(f"\n{'=' * 60}")
        print("FAILURE ANALYSIS")
        print(f"{'=' * 60}")
        print(f"Root Cause     : {analysis.root_cause}")
        print(f"Confidence     : {analysis.confidence}")
        print(f"Affected Files : {', '.join(analysis.affected_files) or 'Unknown'}")
        print(f"\nSuggested Fix:\n{analysis.suggested_fix[:1000]}")


# =============================================================================
# CLI entry point
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="YAKKI Game Polygon — LLM-driven Android game tester"
    )
    parser.add_argument(
        "--game", required=True,
        help=f"Game to test. Available: {', '.join(GAME_CONTRACTS)}",
    )
    parser.add_argument(
        "--objective", default="Complete one round",
        help="Natural-language test objective",
    )
    parser.add_argument("--max-steps", type=int, default=30, help="Max steps per run")
    parser.add_argument(
        "--auto-fix", action="store_true",
        help="Automatically apply simple fixes and rebuild",
    )
    parser.add_argument(
        "--analyze-only", action="store_true",
        help="Skip testing; only analyse an existing failure",
    )
    parser.add_argument(
        "--llm", choices=["claude", "gemini"], default="claude",
        help="LLM backend (default: claude)",
    )
    parser.add_argument(
        "--device", default=DEFAULT_DEVICE,
        help="ADB device address (overrides YAKKI_DEVICE env-var)",
    )
    parser.add_argument(
        "--keep-screenshots", action="store_true",
        help="Do not delete screenshots after a successful run",
    )
    parser.add_argument(
        "--list-games", action="store_true",
        help="Print available game names and exit",
    )
    args = parser.parse_args()

    if args.list_games:
        print("Available games:")
        for name in GAME_CONTRACTS:
            print(f"  - {name}")
        return 0

    polygon = GamePolygon(
        llm_backend=args.llm,
        device=args.device,
        keep_screenshots=args.keep_screenshots,
    )
    polygon.run_cycle(
        game=args.game,
        objective=args.objective,
        auto_fix=args.auto_fix,
        max_iterations=3,
    )
    return 0


if __name__ == "__main__":
    exit(main())
