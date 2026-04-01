"""
FlexRadio + MacLoggerDX Spot Bridge
-----------------------------------

This script connects to a FlexRadio TCP control port (4992).

It listens for spots already present on the Flex panadapter and
when you tune a slice to an exact spotted frequency it:

• prints the matched callsign
• sends the callsign to MacLoggerDX
• optionally sets the radio mode

Tested environment:
Flex 8400
SmartSDR
MacLoggerDX
Python 3.14
"""

import socket
import subprocess
import time
import re
import threading
import tkinter as tk
from tkinter import colorchooser
import sys
import json
import os
import webbrowser
import tempfile
import glob
from version import APP_NAME, APP_VERSION, APP_PRERELEASE, app_version_label


current_freq = None
CURRENT_FILTER_BANDWIDTH_HZ = None

# ------------------------------------------------
# USER SETTINGS
# ------------------------------------------------

# Flex radio IP
FLEX_IP = "192.168.68.157"

# Flex API port
FLEX_PORT = 4992

# If True, do not change slice mode when a spot is matched.
KEEP_CURRENT_MODE = False

# If True, remove older Flex spots that share the same exact frequency.
REMOVE_DUPLICATE_SPOTS = True

# If True, keep older nearby spots and ignore newer ones within duplicate threshold.
CONTEST_MODE = False

# Any new spot within this many Hz of an older spot is treated as a duplicate.
DUPLICATE_SPOT_THRESHOLD_HZ = 25

# If True, show high-volume debug logging in the UI log window.
VERBOSE_LOGGING = False

# If True, automatically remove spots older than AUTO_CLEAR_SPOTS_AGE_MINUTES.
AUTO_CLEAR_SPOTS_ENABLED = False

# Age in minutes beyond which spots are automatically removed (1-99).
AUTO_CLEAR_SPOTS_AGE_MINUTES = 5

# Polling cadence for age-driven actions. Lower values track thresholds more closely.
AUTO_CLEAR_CHECK_INTERVAL_SECONDS = 2
SPOT_COLOR_UPDATE_INTERVAL_SECONDS = 1

# Spot age-based color thresholds and values.
# Ages are interpreted as:
# - 0 to SPOT_AGE_RED_MINUTES-1: SPOT_COLOR_NOW
# - SPOT_AGE_RED_MINUTES to SPOT_AGE_YELLOW_MINUTES-1: SPOT_COLOR_RED
# - SPOT_AGE_YELLOW_MINUTES to SPOT_AGE_GRAY_MINUTES-1: SPOT_COLOR_YELLOW
# - SPOT_AGE_GRAY_MINUTES and older: SPOT_COLOR_GRAY
DEFAULT_SPOT_AGE_RED_MINUTES = 5
DEFAULT_SPOT_AGE_YELLOW_MINUTES = 10
DEFAULT_SPOT_AGE_GRAY_MINUTES = 15
DEFAULT_SPOT_COLOR_NOW = "#FF00CC"
DEFAULT_SPOT_COLOR_RED = "#FF6633"
DEFAULT_SPOT_COLOR_YELLOW = "#999900"
DEFAULT_SPOT_COLOR_GRAY = "#808080"
DEFAULT_SPOT_BG_COLOR_NOW = "none"
DEFAULT_SPOT_BG_COLOR_RED = "none"
DEFAULT_SPOT_BG_COLOR_YELLOW = "none"
DEFAULT_SPOT_BG_COLOR_GRAY = "none"

SPOT_AGE_RED_MINUTES = DEFAULT_SPOT_AGE_RED_MINUTES
SPOT_AGE_YELLOW_MINUTES = DEFAULT_SPOT_AGE_YELLOW_MINUTES
SPOT_AGE_GRAY_MINUTES = DEFAULT_SPOT_AGE_GRAY_MINUTES
SPOT_COLOR_NOW = DEFAULT_SPOT_COLOR_NOW
SPOT_COLOR_RED = DEFAULT_SPOT_COLOR_RED
SPOT_COLOR_YELLOW = DEFAULT_SPOT_COLOR_YELLOW
SPOT_COLOR_GRAY = DEFAULT_SPOT_COLOR_GRAY
SPOT_BG_COLOR_NOW = DEFAULT_SPOT_BG_COLOR_NOW
SPOT_BG_COLOR_RED = DEFAULT_SPOT_BG_COLOR_RED
SPOT_BG_COLOR_YELLOW = DEFAULT_SPOT_BG_COLOR_YELLOW
SPOT_BG_COLOR_GRAY = DEFAULT_SPOT_BG_COLOR_GRAY

DEFAULT_SPOT_COLOR_WORKED = "#00CC44"
DEFAULT_SPOT_BG_COLOR_WORKED = "none"
SPOT_COLOR_WORKED = DEFAULT_SPOT_COLOR_WORKED
SPOT_BG_COLOR_WORKED = DEFAULT_SPOT_BG_COLOR_WORKED

# If True, color spots green when the callsign was logged in the current session.
ENABLE_WORKED_COLOR = True

# Callsigns logged via MLDX in the current session (in-memory only, cleared on restart).
session_logged_calls: set = set()

# If True, set Flex spot text color based on age bucket.
ENABLE_SPOT_TEXT_COLORS = True

# If True, also set Flex spot background_color based on the same age bucket color.
ENABLE_SPOT_BACKGROUND_COLORS = False

# If True, duplicate threshold follows current Flex slice filter bandwidth (Hz).
ENABLE_AUTO_DUPE_THRESHOLD = False

# If True, only use AppleScript lookup for MacLoggerDX (no URL fallback).
FORCE_APPLESCRIPT_LOOKUP_ONLY = True

# Protect a just-matched spot from duplicate cleanup for a short period.
RECENT_MATCH_PROTECTION_SECONDS = 8

# Track Flex panadapter spots by spot ID -> metadata.
# Example: {"23": {"freq_hz": 7030400, "call": "R4WCQ", "time": 1774154707}}
flex_spots = {}
flex_spots_lock = threading.Lock()
flex_command_seq = 2
flex_command_seq_lock = threading.Lock()


# Callsigns recently sent to MLDX for lookup, used to distinguish synthetic
# Lookup echo spots from genuine manually-entered MLDX spots.
_recent_mldx_lookups: dict = {}          # callsign_upper -> unix timestamp
_recent_mldx_lookups_lock = threading.Lock()
MLDX_LOOKUP_ECHO_WINDOW_SECONDS = 10    # Echo expected within 10 s of the lookup call


def next_flex_command_seq():
    """Return the next command sequence number for the listener socket."""
    global flex_command_seq
    with flex_command_seq_lock:
        flex_command_seq += 1
        return flex_command_seq


def send_flex_command(command):
    """Send a one-shot command to the Flex API."""
    command_seq = next_flex_command_seq()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((FLEX_IP, FLEX_PORT))
    sock.sendall(f"C{command_seq}|{command}\n".encode())
    sock.close()


def connect_flex_command_socket():
    """Open a persistent command socket to the Flex API."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((FLEX_IP, FLEX_PORT))
    return sock


def log_debug(*args, **kwargs):
    if VERBOSE_LOGGING:
        print(*args, **kwargs)


def log_mutation_trace(message):
    """Always print mutation guard events, even when verbose logging is disabled."""
    print(f"MutationGuard: {message}")


def remove_duplicate_flex_spots(freq_hz, keep_spot_id, command_sock=None):
    """Remove older Flex spots within DUPLICATE_SPOT_THRESHOLD_HZ, keeping one ID."""
    now = int(time.time())
    with flex_spots_lock:
        duplicate_ids = [
            spot_id
            for spot_id, spot in flex_spots.items()
            if (
                spot_id != keep_spot_id
                and spot.get("freq_hz") is not None
                and abs(int(spot.get("freq_hz")) - int(freq_hz)) <= DUPLICATE_SPOT_THRESHOLD_HZ
                and (now - int(spot.get("last_matched_time", 0))) > RECENT_MATCH_PROTECTION_SECONDS
            )
        ]

        for spot_id in duplicate_ids:
            flex_spots.pop(spot_id, None)

    for spot_id in duplicate_ids:
        try:
            if command_sock is None:
                send_flex_command(f"spot remove {spot_id}")
            else:
                command_seq = next_flex_command_seq()
                command_sock.sendall(f"C{command_seq}|spot remove {spot_id}\n".encode())
            print(
                f"Removed older Flex spot id={spot_id} within {DUPLICATE_SPOT_THRESHOLD_HZ} Hz of {freq_hz} Hz"
            )
        except Exception as e:
            print(f"Failed to remove Flex spot id={spot_id}: {e}")


def remove_newer_duplicate_spot(freq_hz, new_spot_id, command_sock=None):
    """Remove the newly arrived spot when an older nearby spot already exists."""
    with flex_spots_lock:
        has_older_duplicate = any(
            spot_id != new_spot_id
            and spot.get("freq_hz") is not None
            and abs(int(spot.get("freq_hz")) - int(freq_hz)) <= DUPLICATE_SPOT_THRESHOLD_HZ
            for spot_id, spot in flex_spots.items()
        )

    if not has_older_duplicate:
        return False

    try:
        if command_sock is None:
            send_flex_command(f"spot remove {new_spot_id}")
        else:
            command_seq = next_flex_command_seq()
            command_sock.sendall(f"C{command_seq}|spot remove {new_spot_id}\n".encode())
        print(
            f"Contest mode: ignored newer Flex spot id={new_spot_id} near {freq_hz} Hz "
            f"(threshold {DUPLICATE_SPOT_THRESHOLD_HZ} Hz)"
        )
        return True
    except Exception as e:
        print(f"Failed to remove newer Flex spot id={new_spot_id}: {e}")
        return False


def find_exact_flex_spot_call(freq_hz):
    """Return the newest callsign for an exact Flex spot frequency match."""
    with flex_spots_lock:
        candidates = [
            (spot_id, spot)
            for spot_id, spot in flex_spots.items()
            if spot.get("freq_hz") == freq_hz and spot.get("call")
        ]

    if not candidates:
        return None

    # Prefer the highest numeric spot ID as the newest event.
    def spot_sort_key(item):
        spot_id, spot = item
        try:
            return int(spot_id)
        except ValueError:
            return int(spot.get("time", 0))

    newest_id, newest_spot = max(candidates, key=spot_sort_key)
    return newest_spot.get("call"), newest_id

# ------------------------------------------------
# SETTINGS PERSISTENCE
# ------------------------------------------------

SETTINGS_FILE = os.path.expanduser("~/Library/Preferences/FlexSpotBridge.json")


def normalize_spot_age_thresholds():
    """Keep spot age thresholds ordered and within valid bounds."""
    global SPOT_AGE_RED_MINUTES, SPOT_AGE_YELLOW_MINUTES, SPOT_AGE_GRAY_MINUTES
    if SPOT_AGE_RED_MINUTES < 1:
        SPOT_AGE_RED_MINUTES = 1
    if SPOT_AGE_YELLOW_MINUTES <= SPOT_AGE_RED_MINUTES:
        SPOT_AGE_YELLOW_MINUTES = SPOT_AGE_RED_MINUTES + 1
    if SPOT_AGE_GRAY_MINUTES <= SPOT_AGE_YELLOW_MINUTES:
        SPOT_AGE_GRAY_MINUTES = SPOT_AGE_YELLOW_MINUTES + 1

def load_settings():
    global FLEX_IP, FLEX_PORT, KEEP_CURRENT_MODE, REMOVE_DUPLICATE_SPOTS, CONTEST_MODE, DUPLICATE_SPOT_THRESHOLD_HZ, VERBOSE_LOGGING, AUTO_CLEAR_SPOTS_ENABLED, AUTO_CLEAR_SPOTS_AGE_MINUTES
    global SPOT_AGE_RED_MINUTES, SPOT_AGE_YELLOW_MINUTES, SPOT_AGE_GRAY_MINUTES
    global SPOT_COLOR_NOW, SPOT_COLOR_RED, SPOT_COLOR_YELLOW, SPOT_COLOR_GRAY
    global SPOT_BG_COLOR_NOW, SPOT_BG_COLOR_RED, SPOT_BG_COLOR_YELLOW, SPOT_BG_COLOR_GRAY
    global SPOT_COLOR_WORKED, SPOT_BG_COLOR_WORKED, ENABLE_WORKED_COLOR
    global ENABLE_SPOT_TEXT_COLORS, ENABLE_SPOT_BACKGROUND_COLORS, ENABLE_AUTO_DUPE_THRESHOLD, FORCE_APPLESCRIPT_LOOKUP_ONLY
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            FLEX_IP = data.get("FLEX_IP", FLEX_IP)
            FLEX_PORT = int(data.get("FLEX_PORT", FLEX_PORT))
            KEEP_CURRENT_MODE = bool(data.get("KEEP_CURRENT_MODE", KEEP_CURRENT_MODE))
            REMOVE_DUPLICATE_SPOTS = bool(data.get("REMOVE_DUPLICATE_SPOTS", REMOVE_DUPLICATE_SPOTS))
            CONTEST_MODE = bool(data.get("CONTEST_MODE", CONTEST_MODE))
            DUPLICATE_SPOT_THRESHOLD_HZ = int(data.get("DUPLICATE_SPOT_THRESHOLD_HZ", DUPLICATE_SPOT_THRESHOLD_HZ))
            VERBOSE_LOGGING = bool(data.get("VERBOSE_LOGGING", VERBOSE_LOGGING))
            AUTO_CLEAR_SPOTS_ENABLED = bool(data.get("AUTO_CLEAR_SPOTS_ENABLED", AUTO_CLEAR_SPOTS_ENABLED))
            AUTO_CLEAR_SPOTS_AGE_MINUTES = int(data.get("AUTO_CLEAR_SPOTS_AGE_MINUTES", AUTO_CLEAR_SPOTS_AGE_MINUTES))
            SPOT_AGE_RED_MINUTES = int(data.get("SPOT_AGE_RED_MINUTES", SPOT_AGE_RED_MINUTES))
            SPOT_AGE_YELLOW_MINUTES = int(data.get("SPOT_AGE_YELLOW_MINUTES", SPOT_AGE_YELLOW_MINUTES))
            SPOT_AGE_GRAY_MINUTES = int(
                data.get(
                    "SPOT_AGE_GRAY_MINUTES",
                    max(DEFAULT_SPOT_AGE_GRAY_MINUTES, SPOT_AGE_YELLOW_MINUTES + 5),
                )
            )
            SPOT_COLOR_NOW = str(data.get("SPOT_COLOR_NOW", SPOT_COLOR_NOW))
            SPOT_COLOR_RED = str(data.get("SPOT_COLOR_RED", SPOT_COLOR_RED))
            SPOT_COLOR_YELLOW = str(data.get("SPOT_COLOR_YELLOW", SPOT_COLOR_YELLOW))
            SPOT_COLOR_GRAY = str(data.get("SPOT_COLOR_GRAY", SPOT_COLOR_GRAY))

            # On startup, initialize background colors to defaults when older
            # settings files do not yet include dedicated background colors.
            if "SPOT_BG_COLOR_NOW" in data:
                loaded_now_bg = str(data.get("SPOT_BG_COLOR_NOW", SPOT_BG_COLOR_NOW)).strip()
                SPOT_BG_COLOR_NOW = loaded_now_bg if loaded_now_bg else "none"
            else:
                SPOT_BG_COLOR_NOW = DEFAULT_SPOT_BG_COLOR_NOW

            if "SPOT_BG_COLOR_RED" in data:
                loaded_red_bg = str(data.get("SPOT_BG_COLOR_RED", SPOT_BG_COLOR_RED)).strip()
                SPOT_BG_COLOR_RED = loaded_red_bg if loaded_red_bg else "none"
            else:
                SPOT_BG_COLOR_RED = DEFAULT_SPOT_BG_COLOR_RED

            if "SPOT_BG_COLOR_YELLOW" in data:
                loaded_yellow_bg = str(data.get("SPOT_BG_COLOR_YELLOW", SPOT_BG_COLOR_YELLOW)).strip()
                SPOT_BG_COLOR_YELLOW = loaded_yellow_bg if loaded_yellow_bg else "none"
            else:
                SPOT_BG_COLOR_YELLOW = DEFAULT_SPOT_BG_COLOR_YELLOW

            if "SPOT_BG_COLOR_GRAY" in data:
                loaded_gray_bg = str(data.get("SPOT_BG_COLOR_GRAY", SPOT_BG_COLOR_GRAY)).strip()
                SPOT_BG_COLOR_GRAY = loaded_gray_bg if loaded_gray_bg else "none"
            else:
                SPOT_BG_COLOR_GRAY = DEFAULT_SPOT_BG_COLOR_GRAY
            ENABLE_SPOT_TEXT_COLORS = bool(data.get("ENABLE_SPOT_TEXT_COLORS", ENABLE_SPOT_TEXT_COLORS))
            ENABLE_SPOT_BACKGROUND_COLORS = bool(data.get("ENABLE_SPOT_BACKGROUND_COLORS", ENABLE_SPOT_BACKGROUND_COLORS))

            SPOT_COLOR_WORKED = str(data.get("SPOT_COLOR_WORKED", DEFAULT_SPOT_COLOR_WORKED))
            if "SPOT_BG_COLOR_WORKED" in data:
                loaded_worked_bg = str(data.get("SPOT_BG_COLOR_WORKED", SPOT_BG_COLOR_WORKED)).strip()
                SPOT_BG_COLOR_WORKED = loaded_worked_bg if loaded_worked_bg else "none"
            else:
                SPOT_BG_COLOR_WORKED = DEFAULT_SPOT_BG_COLOR_WORKED
            ENABLE_WORKED_COLOR = bool(data.get("ENABLE_WORKED_COLOR", ENABLE_WORKED_COLOR))

            # Backward compatibility: migrate prior generic experimental toggle.
            if "ENABLE_AUTO_DUPE_THRESHOLD" in data:
                ENABLE_AUTO_DUPE_THRESHOLD = bool(data.get("ENABLE_AUTO_DUPE_THRESHOLD", ENABLE_AUTO_DUPE_THRESHOLD))
            else:
                ENABLE_AUTO_DUPE_THRESHOLD = bool(data.get("ENABLE_EXPERIMENTAL_FEATURE", ENABLE_AUTO_DUPE_THRESHOLD))

            FORCE_APPLESCRIPT_LOOKUP_ONLY = bool(
                data.get("FORCE_APPLESCRIPT_LOOKUP_ONLY", FORCE_APPLESCRIPT_LOOKUP_ONLY)
            )

            if CONTEST_MODE and REMOVE_DUPLICATE_SPOTS:
                REMOVE_DUPLICATE_SPOTS = False

            normalize_spot_age_thresholds()
            if DUPLICATE_SPOT_THRESHOLD_HZ < 0:
                DUPLICATE_SPOT_THRESHOLD_HZ = 0
        except Exception as e:
            print(f"Failed to load settings: {e}")

def save_settings():
    try:
        data = {
            "FLEX_IP": FLEX_IP,
            "FLEX_PORT": FLEX_PORT,
            "KEEP_CURRENT_MODE": KEEP_CURRENT_MODE,
            "REMOVE_DUPLICATE_SPOTS": REMOVE_DUPLICATE_SPOTS,
            "CONTEST_MODE": CONTEST_MODE,
            "DUPLICATE_SPOT_THRESHOLD_HZ": DUPLICATE_SPOT_THRESHOLD_HZ,
            "VERBOSE_LOGGING": VERBOSE_LOGGING,
            "AUTO_CLEAR_SPOTS_ENABLED": AUTO_CLEAR_SPOTS_ENABLED,
            "AUTO_CLEAR_SPOTS_AGE_MINUTES": AUTO_CLEAR_SPOTS_AGE_MINUTES,
            "SPOT_AGE_RED_MINUTES": SPOT_AGE_RED_MINUTES,
            "SPOT_AGE_YELLOW_MINUTES": SPOT_AGE_YELLOW_MINUTES,
            "SPOT_AGE_GRAY_MINUTES": SPOT_AGE_GRAY_MINUTES,
            "SPOT_COLOR_NOW": SPOT_COLOR_NOW,
            "SPOT_COLOR_RED": SPOT_COLOR_RED,
            "SPOT_COLOR_YELLOW": SPOT_COLOR_YELLOW,
            "SPOT_COLOR_GRAY": SPOT_COLOR_GRAY,
            "SPOT_BG_COLOR_NOW": SPOT_BG_COLOR_NOW,
            "SPOT_BG_COLOR_RED": SPOT_BG_COLOR_RED,
            "SPOT_BG_COLOR_YELLOW": SPOT_BG_COLOR_YELLOW,
            "SPOT_BG_COLOR_GRAY": SPOT_BG_COLOR_GRAY,
            "ENABLE_SPOT_TEXT_COLORS": ENABLE_SPOT_TEXT_COLORS,
            "ENABLE_SPOT_BACKGROUND_COLORS": ENABLE_SPOT_BACKGROUND_COLORS,
            "SPOT_COLOR_WORKED": SPOT_COLOR_WORKED,
            "SPOT_BG_COLOR_WORKED": SPOT_BG_COLOR_WORKED,
            "ENABLE_WORKED_COLOR": ENABLE_WORKED_COLOR,
            "ENABLE_AUTO_DUPE_THRESHOLD": ENABLE_AUTO_DUPE_THRESHOLD,
            "FORCE_APPLESCRIPT_LOOKUP_ONLY": FORCE_APPLESCRIPT_LOOKUP_ONLY,
        }
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save settings: {e}")

load_settings()


def spot_color_for_age(age_seconds):
    """Choose the configured spot color based on spot age."""
    if age_seconds >= SPOT_AGE_GRAY_MINUTES * 60:
        return SPOT_COLOR_GRAY
    if age_seconds >= SPOT_AGE_YELLOW_MINUTES * 60:
        return SPOT_COLOR_YELLOW
    if age_seconds >= SPOT_AGE_RED_MINUTES * 60:
        return SPOT_COLOR_RED
    return SPOT_COLOR_NOW


def spot_background_color_for_age(age_seconds):
    """Choose the configured spot background color based on spot age."""
    if age_seconds >= SPOT_AGE_GRAY_MINUTES * 60:
        return SPOT_BG_COLOR_GRAY
    if age_seconds >= SPOT_AGE_YELLOW_MINUTES * 60:
        return SPOT_BG_COLOR_YELLOW
    if age_seconds >= SPOT_AGE_RED_MINUTES * 60:
        return SPOT_BG_COLOR_RED
    return SPOT_BG_COLOR_NOW


def spot_seen_time(spot, fallback_now):
    """Return local first-seen time used for color/auto-clear aging."""
    return float(spot.get("seen_time", spot.get("time", fallback_now)))


def parse_slice_filter_bandwidth_hz(slice_line):
    """Extract a filter bandwidth in Hz from a Flex slice status line."""
    width_keys = ("filter_bw", "filter_width", "filter_bandwidth")
    for key in width_keys:
        width_match = re.search(rf"\b{key}=(-?\d+)\b", slice_line)
        if width_match:
            bandwidth_hz = abs(int(width_match.group(1)))
            if bandwidth_hz > 0:
                return bandwidth_hz

    edge_key_pairs = (
        ("filter_hi", "filter_lo"),
        ("filter_high", "filter_low"),
        ("rx_filter_high", "rx_filter_low"),
    )
    for high_key, low_key in edge_key_pairs:
        high_match = re.search(rf"\b{high_key}=(-?\d+)\b", slice_line)
        low_match = re.search(rf"\b{low_key}=(-?\d+)\b", slice_line)
        if high_match and low_match:
            bandwidth_hz = abs(int(high_match.group(1)) - int(low_match.group(1)))
            if bandwidth_hz > 0:
                return bandwidth_hz

    return None

# ------------------------------------------------
# AUTO-CLEAR OLD SPOTS
# ------------------------------------------------

def clear_old_spots_task():
    """Periodically clear spots older than AUTO_CLEAR_SPOTS_AGE_MINUTES."""
    command_sock = None
    command_seq = 9000
    next_run_monotonic = time.monotonic()

    while True:
        now_monotonic = time.monotonic()
        if now_monotonic < next_run_monotonic:
            time.sleep(next_run_monotonic - now_monotonic)
        next_run_monotonic = time.monotonic() + AUTO_CLEAR_CHECK_INTERVAL_SECONDS
        
        if not AUTO_CLEAR_SPOTS_ENABLED:
            continue
        
        current_time = time.time()
        age_threshold_seconds = AUTO_CLEAR_SPOTS_AGE_MINUTES * 60
        clear_all_tracked_spots = False
        
        with flex_spots_lock:
            total_spot_count = len(flex_spots)
            spots_to_remove = [
                spot_id
                for spot_id, spot in flex_spots.items()
                if (current_time - spot_seen_time(spot, current_time)) >= age_threshold_seconds
            ]
            clear_all_tracked_spots = (
                total_spot_count > 0 and len(spots_to_remove) == total_spot_count
            )

            if clear_all_tracked_spots:
                flex_spots.clear()
            else:
                for spot_id in spots_to_remove:
                    flex_spots.pop(spot_id, None)
        
        if spots_to_remove:
            try:
                if clear_all_tracked_spots:
                    if command_sock is None:
                        command_sock = connect_flex_command_socket()

                    try:
                        command_seq += 1
                        command_sock.sendall(f"C{command_seq}|spot clear\n".encode())
                    except Exception:
                        try:
                            command_sock.close()
                        except Exception:
                            pass

                        command_sock = connect_flex_command_socket()
                        command_seq += 1
                        command_sock.sendall(f"C{command_seq}|spot clear\n".encode())
                else:
                    for spot_id in spots_to_remove:
                        if command_sock is None:
                            command_sock = connect_flex_command_socket()

                        try:
                            command_seq += 1
                            command_sock.sendall(f"C{command_seq}|spot remove {spot_id}\n".encode())
                        except Exception:
                            try:
                                command_sock.close()
                            except Exception:
                                pass

                            command_sock = connect_flex_command_socket()
                            command_seq += 1
                            command_sock.sendall(f"C{command_seq}|spot remove {spot_id}\n".encode())

                log_debug(f"Auto-cleared {len(spots_to_remove)} old spots")
            except Exception as e:
                log_debug(f"Failed to auto-clear old spots: {e}")
                if command_sock is not None:
                    try:
                        command_sock.close()
                    except Exception:
                        pass
                    command_sock = None


def update_spot_colors_task():
    """Periodically recolor Flex spots based on configurable spot age buckets."""
    command_sock = None
    command_seq = 12000
    next_run_monotonic = time.monotonic()

    while True:
        now_monotonic = time.monotonic()
        if now_monotonic < next_run_monotonic:
            time.sleep(next_run_monotonic - now_monotonic)
        next_run_monotonic = time.monotonic() + SPOT_COLOR_UPDATE_INTERVAL_SECONDS

        if not ENABLE_SPOT_TEXT_COLORS and not ENABLE_SPOT_BACKGROUND_COLORS:
            continue

        now = time.time()
        updates = []
        with flex_spots_lock:
            for spot_id, spot in flex_spots.items():
                spot_age_seconds = max(0, now - spot_seen_time(spot, now))
                call_upper = (spot.get("call") or "").upper()
                if ENABLE_WORKED_COLOR and call_upper in session_logged_calls:
                    target_text_color = SPOT_COLOR_WORKED
                    target_background_color = SPOT_BG_COLOR_WORKED
                else:
                    target_text_color = spot_color_for_age(spot_age_seconds)
                    target_background_color = spot_background_color_for_age(spot_age_seconds)
                if (
                    spot.get("last_text_color") != target_text_color
                    or spot.get("last_background_color") != target_background_color
                    or spot.get("last_text_enabled") != ENABLE_SPOT_TEXT_COLORS
                    or spot.get("last_background_enabled") != ENABLE_SPOT_BACKGROUND_COLORS
                ):
                    spot["last_text_color"] = target_text_color
                    spot["last_background_color"] = target_background_color
                    spot["last_text_enabled"] = ENABLE_SPOT_TEXT_COLORS
                    spot["last_background_enabled"] = ENABLE_SPOT_BACKGROUND_COLORS
                    updates.append((spot_id, target_text_color, target_background_color))

        for spot_id, target_text_color, target_background_color in updates:
            try:
                if command_sock is None:
                    command_sock = connect_flex_command_socket()

                command_seq += 1
                set_parts = []
                if ENABLE_SPOT_TEXT_COLORS:
                    set_parts.append(f"color={target_text_color}")
                if ENABLE_SPOT_BACKGROUND_COLORS:
                    if str(target_background_color).lower() == "none":
                        set_parts.append("background_color=")
                    else:
                        set_parts.append(f"background_color={target_background_color}")

                if not set_parts:
                    continue

                set_clause = " ".join(set_parts)
                command_sock.sendall(
                    f"C{command_seq}|spot set {spot_id} {set_clause}\n".encode()
                )
                log_debug(f"Updated spot id={spot_id} {set_clause}")
            except Exception as e:
                log_debug(f"Failed to update color for spot id={spot_id}: {e}")
                if command_sock is not None:
                    try:
                        command_sock.close()
                    except Exception:
                        pass
                    command_sock = None

def mldx_udp_listener_task():
    """Listen for MLDX UDP log reports and record callsigns worked this session.

    MacLoggerDX broadcasts a UDP packet on port 9932 whenever a QSO is logged:
        Log Report:Call:W1AW, RxMHz:14.025, ...
    When received, the callsign is added to session_logged_calls and the
    update_spot_colors_task loop will recolor any matching Flex panadapter spot
    green on its next tick.

    Requires "UDP Broadcast" enabled in MacLoggerDX Station Prefs.
    """
    MLDX_UDP_PORT = 9932
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", MLDX_UDP_PORT))
        print(f"MLDX UDP listener started on port {MLDX_UDP_PORT} (watching for Log Reports)")
    except Exception as e:
        print(f"MLDX UDP listener failed to start on port {MLDX_UDP_PORT}: {e}")
        return

    while True:
        try:
            data, _ = sock.recvfrom(4096)
            msg = data.decode(errors="ignore")
            log_debug(f"MLDX UDP: {msg.strip()}")
            match = re.search(r"Log Report:Call:([A-Z0-9/]+)", msg, re.IGNORECASE)
            if match:
                call = match.group(1).upper()
                session_logged_calls.add(call)
                print(f"Worked: {call} — spot color will update to Worked color")
        except Exception as e:
            log_debug(f"MLDX UDP listener error: {e}")


class TextRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, str):
        self.widget.insert(tk.END, str)
        self.widget.see(tk.END)

    def flush(self):
        pass


# ------------------------------------------------
# SEND CALLSIGN TO MACLOGGERDX
# ------------------------------------------------

def set_mldx_call(call):

    print(f"Sending to MLDX: {call}")
    
    # Get the currently focused app before opening MLDX
    try:
        result = subprocess.run(
            ["osascript", "-e", "tell application \"System Events\" to name of first application process whose frontmost is true"],
            capture_output=True,
            text=True,
            check=True
        )
        previous_app = result.stdout.strip()
    except subprocess.CalledProcessError:
        previous_app = None
    
    try:
        # Prefer direct AppleScript lookup, which avoids URL side effects
        # that can briefly create a synthetic "Lookup" spot on Flex.
        safe_call = str(call).replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f"tell application \"MacLoggerDX\" to lookup \"{safe_call}\""],
            check=True,
        )
        print("MLDX lookup command sent successfully")
        with _recent_mldx_lookups_lock:
            _recent_mldx_lookups[call.upper()] = int(time.time())
    except subprocess.CalledProcessError as e:
        if FORCE_APPLESCRIPT_LOOKUP_ONLY:
            print(f"Direct MLDX lookup failed (AppleScript-only mode): {e}")
        else:
            print(f"Direct MLDX lookup failed, falling back to URL: {e}")
            try:
                subprocess.run(["open", f"mldx://lookup?call={call}"], check=True)
                print("MLDX lookup URL opened successfully")
            except subprocess.CalledProcessError as fallback_error:
                print(f"Failed to open MLDX lookup URL: {fallback_error}")

    # Restore focus to the previously focused app.
    if previous_app:
        try:
            subprocess.run(
                ["osascript", "-e", f"tell application \"{previous_app}\" to activate"],
                check=True
            )
            print(f"Focus restored to {previous_app}")
        except subprocess.CalledProcessError as e:
            print(f"Failed to restore focus to {previous_app}: {e}")


# ------------------------------------------------
# FLEX MODE CONTROL
# ------------------------------------------------

def set_mode(sock, slice_id, mode):

    cmd = f"C slice set {slice_id} mode={mode}\n"
    sock.send(cmd.encode())


# ------------------------------------------------
# SIMPLE MODE DETECTION (example bands)
# ------------------------------------------------

def auto_mode(sock, slice_id, freq):

    mhz = freq / 1e6

    if 14.070 < mhz < 14.080:
        set_mode(sock, slice_id, "DIGU")

    elif 14.000 < mhz < 14.060:
        set_mode(sock, slice_id, "CW")

    elif 14.150 < mhz < 14.350:
        set_mode(sock, slice_id, "USB")


# ------------------------------------------------
# FLEX RADIO LISTENER
# ------------------------------------------------

def flex_listener():
    global current_freq, DUPLICATE_SPOT_THRESHOLD_HZ, CURRENT_FILTER_BANDWIDTH_HZ

    print("Connecting to Flex radio...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((FLEX_IP, FLEX_PORT))

    print("Flex connected")

    # subscribe to slice updates
    sock.sendall(b"C1|sub slice all\n")
    sock.sendall(b"C2|sub spot all\n")

    while True:

        data = sock.recv(4096).decode(errors="ignore")
        
        log_debug("Flex data:", data.strip())

        if not data:
            continue

        for line in data.splitlines():
            log_debug("Flex line:", line)

            spot_match = re.search(
                r"S[^|]+\|spot\s+(\d+)\b",
                line
            )

            removed_match = re.search(r"S[^|]+\|spot\s+removed\s+(\d+)", line)
            if removed_match:
                removed_id = removed_match.group(1)
                with flex_spots_lock:
                    removed_spot = flex_spots.pop(removed_id, None)
                if removed_spot:
                    last_matched_time = int(removed_spot.get("last_matched_time", 0))
                    if int(time.time()) - last_matched_time <= RECENT_MATCH_PROTECTION_SECONDS:
                        print(
                            "Flex removed recently matched spot: "
                            f"id={removed_id} call={removed_spot.get('call')} "
                            f"freq={removed_spot.get('freq_hz')}"
                        )
                continue

            if spot_match:
                spot_id = spot_match.group(1)

                # Parse exact key=value tokens to avoid partial-key matches
                # such as source_callsign or tx_freq.
                fields = dict(re.findall(r"\b([A-Za-z0-9_]+)=([^\s]+)", line))
                freq_value = fields.get("rx_freq") or fields.get("freq")
                call = fields.get("callsign")
                timestamp_value = fields.get("timestamp")
                source_value = fields.get("source", "")
                spotter_value = fields.get("spotter_callsign", "")

                # MLDX lookup actions can emit synthetic "Lookup" spots that are
                # later reused/updated, which appears as cross-frequency call
                # mutation in SmartSDR. Only remove these echo spots when the
                # callsign matches one we recently sent to MLDX for lookup;
                # otherwise this is a manually entered MLDX spot — keep it.
                if source_value.lower() == "mldx" and spotter_value.lower() == "lookup":
                    now = int(time.time())

                    # Determine whether this Lookup spot is a synthetic echo of
                    # a lookup we initiated, or a genuine manual MLDX entry.
                    call_upper = (call or "").upper()
                    with _recent_mldx_lookups_lock:
                        lookup_ts = _recent_mldx_lookups.get(call_upper, 0)
                    is_lookup_echo = (now - lookup_ts) <= MLDX_LOOKUP_ECHO_WINDOW_SECONDS

                    if not is_lookup_echo:
                        # Manual MLDX spot — fall through to normal spot processing.
                        log_mutation_trace(
                            f"Manual MLDX spot (spotter=Lookup but not a recent echo); keeping "
                            f"id={spot_id} call={call or 'unknown'}"
                        )
                    else:
                        recently_matched = False
                        recently_matched_call = None
                        recently_matched_freq_hz = None
                        with flex_spots_lock:
                            existing_lookup_spot = flex_spots.get(spot_id)
                            if existing_lookup_spot is not None:
                                recently_matched = (
                                    now - int(existing_lookup_spot.get("last_matched_time", 0))
                                ) <= RECENT_MATCH_PROTECTION_SECONDS
                                recently_matched_call = existing_lookup_spot.get("call")
                                recently_matched_freq_hz = existing_lookup_spot.get("freq_hz")

                            if recently_matched:
                                log_mutation_trace(
                                    "Lookup echo observed for recently matched spot; keeping it "
                                    f"id={spot_id} call={recently_matched_call} freq={recently_matched_freq_hz}"
                                )
                            else:
                                flex_spots.pop(spot_id, None)

                        if recently_matched:
                            continue

                        try:
                            command_seq = next_flex_command_seq()
                            sock.sendall(f"C{command_seq}|spot remove {spot_id}\n".encode())
                            log_mutation_trace(
                                "Removed synthetic MLDX Lookup spot "
                                f"id={spot_id} call={call or 'unknown'}"
                            )
                        except Exception as e:
                            log_mutation_trace(
                                f"Failed removing synthetic MLDX Lookup spot id={spot_id}: {e}"
                            )
                        continue

                if not freq_value or not call:
                    continue

                spot_freq_hz = int(round(float(freq_value) * 1e6))
                event_now = int(time.time())
                has_timestamp = bool(timestamp_value)
                parsed_spot_time = int(timestamp_value) if has_timestamp else None

                if CONTEST_MODE:
                    with flex_spots_lock:
                        existing_spot = flex_spots.get(spot_id)
                    if existing_spot is None and remove_newer_duplicate_spot(spot_freq_hz, spot_id, command_sock=sock):
                        continue

                with flex_spots_lock:
                    existing_spot = flex_spots.get(spot_id)
                    existing_source_time = 0
                    if existing_spot:
                        existing_source_time = int(
                            existing_spot.get("source_time", existing_spot.get("time", 0))
                        )
                        existing_freq_hz = existing_spot.get("freq_hz")
                        existing_call = existing_spot.get("call")

                        # Ignore any callsign/frequency mutation when no timestamp is
                        # present; these are frequently transient ID-reuse updates.
                        if (
                            (existing_freq_hz != spot_freq_hz or existing_call != call)
                            and not has_timestamp
                        ):
                            log_mutation_trace(
                                "Ignored mutation without timestamp "
                                f"id={spot_id} old=({existing_call}, {existing_freq_hz}, t={existing_source_time}) "
                                f"new=({call}, {spot_freq_hz}, t=missing) "
                                f"source={source_value or 'unknown'} spotter={spotter_value or 'unknown'}"
                            )
                            continue

                        # Flex may briefly reuse a spot ID with stale data. Ignore
                        # callsign/frequency mutations unless the timestamp advances.
                        if (
                            (existing_freq_hz != spot_freq_hz or existing_call != call)
                            and has_timestamp
                            and parsed_spot_time <= existing_source_time
                        ):
                            log_mutation_trace(
                                "Ignored stale mutation "
                                f"id={spot_id} old=({existing_call}, {existing_freq_hz}, t={existing_source_time}) "
                                f"new=({call}, {spot_freq_hz}, t={parsed_spot_time}) "
                                f"source={source_value or 'unknown'} spotter={spotter_value or 'unknown'}"
                            )
                            continue

                    mutation_accepted = bool(
                        existing_spot
                        and (existing_spot.get("freq_hz") != spot_freq_hz or existing_spot.get("call") != call)
                    )

                    source_time = parsed_spot_time if has_timestamp else existing_source_time
                    seen_time = event_now
                    if existing_spot and not mutation_accepted:
                        seen_time = int(existing_spot.get("seen_time", existing_spot.get("time", event_now)))

                    flex_spots[spot_id] = {
                        "freq_hz": spot_freq_hz,
                        "call": call,
                        "time": seen_time,
                        "seen_time": seen_time,
                        "source_time": source_time,
                        "last_text_color": existing_spot.get("last_text_color") if existing_spot else None,
                        "last_background_color": existing_spot.get("last_background_color") if existing_spot else None,
                        "last_text_enabled": existing_spot.get("last_text_enabled") if existing_spot else None,
                        "last_background_enabled": existing_spot.get("last_background_enabled") if existing_spot else None,
                        "last_matched_time": existing_spot.get("last_matched_time", 0) if existing_spot else 0,
                    }

                if REMOVE_DUPLICATE_SPOTS and not CONTEST_MODE:
                    remove_duplicate_flex_spots(spot_freq_hz, spot_id, command_sock=sock)

                # Immediately apply the configured color to brand-new spots.
                # Without this, the Flex panadapter shows the radio's own default
                # color (magenta) for up to SPOT_COLOR_UPDATE_INTERVAL_SECONDS
                # before the update_spot_colors_task tick fires.
                if existing_spot is None and ENABLE_SPOT_TEXT_COLORS:
                    initial_color = spot_color_for_age(0)
                    cmd_seq = next_flex_command_seq()
                    try:
                        sock.sendall(
                            f"C{cmd_seq}|spot set {spot_id} color={initial_color}\n".encode()
                        )
                        with flex_spots_lock:
                            tracked = flex_spots.get(spot_id)
                            if tracked is not None:
                                tracked["last_text_color"] = initial_color
                    except Exception as e:
                        log_debug(f"Failed immediate color for new spot id={spot_id}: {e}")

            slice_event_match = re.search(r"S[^|]+\|slice\s+(\d+)\b", line)
            if slice_event_match:
                bandwidth_hz = parse_slice_filter_bandwidth_hz(line)
                if bandwidth_hz:
                    CURRENT_FILTER_BANDWIDTH_HZ = bandwidth_hz

                if ENABLE_AUTO_DUPE_THRESHOLD and bandwidth_hz:
                    if bandwidth_hz != DUPLICATE_SPOT_THRESHOLD_HZ:
                        DUPLICATE_SPOT_THRESHOLD_HZ = bandwidth_hz
                        print(
                            "Auto-Dupe Threshold updated from filter bandwidth: "
                            f"{DUPLICATE_SPOT_THRESHOLD_HZ} Hz"
                        )

                freq_match = re.search(r"\bRF_frequency=(\d+\.\d+)\b", line)
                if not freq_match:
                    continue

                slice_id = slice_event_match.group(1)
                freq_hz = int(round(float(freq_match.group(1)) * 1e6))

                previous_freq = current_freq

                # Only evaluate when the VFO actually changes frequency.
                if previous_freq is None:
                    log_debug("Initial frequency captured; waiting for next VFO change")
                elif freq_hz != previous_freq:
                    log_debug(f"VFO change detected: {previous_freq} -> {freq_hz}")

                    match = find_exact_flex_spot_call(freq_hz)
                    if match:
                        call, match_spot_id = match
                        with flex_spots_lock:
                            matched_spot = flex_spots.get(match_spot_id)
                            if matched_spot is not None:
                                matched_spot["last_matched_time"] = int(time.time())
                        print(f"Matched exact Flex spot: {call} (id={match_spot_id}, freq={freq_hz} Hz)")
                        set_mldx_call(call)

                        if KEEP_CURRENT_MODE:
                            print("Keep current mode is enabled; not changing mode")
                        else:
                            auto_mode(sock, slice_id, freq_hz)

                current_freq = freq_hz
            
class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} {app_version_label()}")
        self.root.geometry("600x400")

        self.status_var = tk.StringVar()
        self.status_bar = tk.Label(
            root,
            textvariable=self.status_var,
            anchor="w",
            bg="#ECECEC",
            fg="#202020",
            padx=8,
            pady=4,
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.text = tk.Text(root, wrap=tk.WORD)
        self.text.pack(expand=True, fill=tk.BOTH)

        def refresh_status_line():
            auto_dupe_state = "ON" if ENABLE_AUTO_DUPE_THRESHOLD else "OFF"
            contest_mode_state = "ON" if CONTEST_MODE else "OFF"
            filter_bw_text = (
                f"{CURRENT_FILTER_BANDWIDTH_HZ} Hz"
                if CURRENT_FILTER_BANDWIDTH_HZ is not None
                else "n/a"
            )
            threshold_text = f"{DUPLICATE_SPOT_THRESHOLD_HZ} Hz"
            self.status_var.set(
                f"Contest mode: {contest_mode_state} | Auto-Dupe: {auto_dupe_state} | "
                f"Filter BW: {filter_bw_text} | Duplicate threshold: {threshold_text}"
            )
            if ENABLE_AUTO_DUPE_THRESHOLD:
                self.status_bar.configure(fg="#0B6E4F")
            else:
                self.status_bar.configure(fg="#5C5C5C")
            self.root.after(250, refresh_status_line)

        refresh_status_line()

        # Redirect stdout
        sys.stdout = TextRedirector(self.text)
        print(f"{APP_NAME} {app_version_label()} starting...")
        print(
            "Auto-Dupe Threshold: "
            + ("enabled" if ENABLE_AUTO_DUPE_THRESHOLD else "disabled")
        )

        # Menu
        menubar = tk.Menu(root)

        # App menu (standard macOS layout: Preferences + Quit)
        appmenu = tk.Menu(menubar, name='apple')


        def clear_spots():
            # Send 'spot clear' to Flex API
            try:
                send_flex_command("spot clear")
                global current_freq
                with flex_spots_lock:
                    removed_spots = len(flex_spots)
                    flex_spots.clear()
                current_freq = None
                print(f"All spots cleared on Flex panadapter. Local spot memory reset ({removed_spots} spots removed).")
            except Exception as e:
                print(f"Failed to clear spots on Flex: {e}")


        appmenu.add_command(label=f"About {APP_NAME}", command=self.open_about)
        appmenu.add_separator()
        appmenu.add_command(label="Preferences...", accelerator="Command+,", command=self.open_settings)
        appmenu.add_command(label="Clear All Spots", accelerator="Command-L", command=clear_spots)
        appmenu.add_separator()
        appmenu.add_command(label="Quit", command=root.quit)
        menubar.add_cascade(menu=appmenu)

        helpmenu = tk.Menu(menubar)
        helpmenu.add_command(
            label=f"{APP_NAME} Help",
            command=self.open_help
        )
        menubar.add_cascade(label="Help", menu=helpmenu)

        # Keyboard shortcut for Clear All Spots (Cmd-L)
        root.bind_all("<Command-l>", lambda e: clear_spots())

        root.config(menu=menubar)

        # Standard macOS shortcut for Preferences (Command+,)
        def open_settings_shortcut(_event=None):
            self.open_settings()
            return "break"

        root.bind_all("<Command-comma>", open_settings_shortcut)
        root.bind_all("<Command-KeyPress-comma>", open_settings_shortcut)
        root.bind_all("<Command-,>", open_settings_shortcut)

        # Start threads
        flex_thread = threading.Thread(target=flex_listener, daemon=True)
        flex_thread.start()
        
        clear_old_spots_thread = threading.Thread(target=clear_old_spots_task, daemon=True)
        clear_old_spots_thread.start()

        spot_color_thread = threading.Thread(target=update_spot_colors_task, daemon=True)
        spot_color_thread.start()

        mldx_udp_thread = threading.Thread(target=mldx_udp_listener_task, daemon=True)
        mldx_udp_thread.start()

    def _load_about_icon_image(self, size=72):
        """Load the app icon for use in the About dialog."""
        def _find_app_icon_path():
            base_dir = os.path.dirname(os.path.abspath(__file__))
            executable_dir = os.path.dirname(os.path.abspath(sys.executable))
            argv0_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else ""

            candidates = [
                os.path.join(base_dir, "FlexSpotBridge.icns"),
                os.path.join(os.getcwd(), "FlexSpotBridge.icns"),
                os.path.join(executable_dir, "..", "Resources", "FlexSpotBridge.icns"),
                os.path.join(argv0_dir, "..", "Resources", "FlexSpotBridge.icns"),
            ]

            for path in candidates:
                path = os.path.abspath(path)
                if os.path.exists(path):
                    return path

            # Last-resort search in app bundle resources.
            resource_glob = os.path.abspath(os.path.join(executable_dir, "..", "Resources", "*.icns"))
            matches = glob.glob(resource_glob)
            if matches:
                return matches[0]

            return None

        icon_path = _find_app_icon_path()
        if not icon_path:
            return None

        # Convert the .icns to .png explicitly so Tk can display it.
        tmp_png = os.path.join(tempfile.gettempdir(), f"{APP_NAME.lower()}_about_icon_{size}.png")
        try:
            subprocess.run(
                ["sips", "-s", "format", "png", "-z", str(size), str(size), icon_path, "--out", tmp_png],
                check=True,
                capture_output=True,
                text=True
            )
            if os.path.exists(tmp_png):
                return tk.PhotoImage(file=tmp_png)
        except Exception:
            pass

        return None

    def open_help(self):
        BG = "#FFFFFF"
        FG = "#111111"

        help_win = tk.Toplevel(self.root)
        help_win.title(f"{APP_NAME} Help")
        help_win.resizable(False, False)
        help_win.transient(self.root)
        help_win.configure(bg=BG)

        outer = tk.Frame(help_win, padx=20, pady=16, bg=BG)
        outer.pack(expand=True, fill=tk.BOTH)

        tk.Label(
            outer,
            text=f"{APP_NAME}",
            font=("Avenir Next", 24, "bold"),
            anchor="w",
            bg=BG,
            fg=FG
        ).pack(fill=tk.X, pady=(0, 4))

        tk.Label(
            outer,
            text=(
                "Bridges Mac SmartSDR panadapter spots to MacLoggerDX.\n"
                "Tune your VFO to any spotted frequency and the callsign is\n"
                "automatically sent to MacLoggerDX for lookup and logging."
            ),
            font=("Avenir Next", 15),
            justify=tk.LEFT,
            anchor="w",
            wraplength=540,
            bg=BG,
            fg=FG
        ).pack(fill=tk.X, pady=(0, 14))

        tk.Frame(outer, height=1, bg="#CCCCCC").pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            outer,
            text="Settings Summary",
            font=("Avenir Next", 18, "bold"),
            anchor="w",
            bg=BG,
            fg=FG
        ).pack(fill=tk.X, pady=(0, 8))

        settings_text = (
            "FLEX_IP / FLEX_PORT — IP address and TCP port of your FlexRadio (default 4992).\n\n"
            "Keep current mode — Prevents FlexSpotBridge from changing the slice mode when a spot is matched.\n\n"
            "Duplicate handling — Choose whether to remove older spots at the same frequency, ignore newer ones "
            "(Contest mode), or keep all spots.\n\n"
            "Duplicate threshold — Frequency distance in Hz within which two spots are considered duplicates.\n\n"
            "Auto-Dupe Threshold — Automatically sets the duplicate threshold to match the current Flex filter bandwidth.\n\n"
            "Auto-clear spots — Automatically removes spots older than a set number of minutes.\n\n"
            "Spot Age Colors — Configure text and background colors for four age buckets: Now, Red, Yellow, and Gray. "
            "Age thresholds are configurable in minutes.\n\n"
            "Worked — When a QSO is logged in MacLoggerDX, that callsign's panadapter spot turns the Worked color "
            "(default green) for the rest of the session. Requires UDP Broadcast enabled in MacLoggerDX Station Prefs."
        )

        text_frame = tk.Frame(outer, bd=1, relief=tk.SUNKEN, bg=BG)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 14))

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        text_box = tk.Text(
            text_frame,
            wrap=tk.WORD,
            font=("Avenir Next", 14),
            width=52,
            height=14,
            relief=tk.FLAT,
            padx=10,
            pady=10,
            bg=BG,
            fg=FG,
            yscrollcommand=scrollbar.set,
            state=tk.NORMAL
        )
        text_box.insert(tk.END, settings_text)
        text_box.configure(state=tk.DISABLED)
        text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=text_box.yview)

        tk.Frame(outer, height=1, bg="#CCCCCC").pack(fill=tk.X, pady=(0, 10))

        link_frame = tk.Frame(outer, bg=BG)
        link_frame.pack(fill=tk.X)

        tk.Label(
            link_frame,
            text="Full documentation: ",
            font=("Avenir Next", 14),
            bg=BG,
            fg=FG
        ).pack(side=tk.LEFT)

        github_url = "https://github.com/williamscody/FlexSpotBridge#readme"
        link_label = tk.Label(
            link_frame,
            text=github_url,
            font=("Avenir Next", 14, "underline"),
            fg="#0044CC",
            bg=BG,
            cursor="hand2"
        )
        link_label.pack(side=tk.LEFT)
        link_label.bind("<Button-1>", lambda _: webbrowser.open(github_url))

        tk.Button(
            outer,
            text="Close",
            command=help_win.destroy,
            font=("Avenir Next", 14),
            bg="#F7F7F7",
            fg="#111111",
            padx=20,
            pady=6
        ).pack(anchor="e", pady=(12, 0))

        help_win.update_idletasks()
        w = help_win.winfo_reqwidth() + 16
        h = help_win.winfo_reqheight() + 16
        self.root.update_idletasks()
        rx = self.root.winfo_x() + max((self.root.winfo_width() - w) // 2, 0)
        ry = self.root.winfo_y() + max((self.root.winfo_height() - h) // 2, 0)
        help_win.geometry(f"{w}x{h}+{rx}+{ry}")

        help_win.grab_set()
        help_win.focus_set()

    def open_about(self):
        about_win = tk.Toplevel(self.root)
        about_win.title(f"About {APP_NAME}")
        dialog_width = 520
        dialog_height = 320
        about_win.geometry(f"{dialog_width}x{dialog_height}")
        about_win.resizable(False, False)
        about_win.transient(self.root)

        # Center over the main app window.
        self.root.update_idletasks()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        pos_x = root_x + max((root_w - dialog_width) // 2, 0)
        pos_y = root_y + max((root_h - dialog_height) // 2, 0)
        about_win.geometry(f"{dialog_width}x{dialog_height}+{pos_x}+{pos_y}")

        # Give the dialog a bold, colorful look while keeping it lightweight.
        about_win.configure(bg="#FFF4D6")

        outer = tk.Frame(about_win, bg="#FFF4D6", padx=20, pady=20)
        outer.pack(expand=True, fill=tk.BOTH)

        banner = tk.Frame(outer, bg="#0A3D62", padx=16, pady=12)
        banner.pack(fill=tk.X, pady=(0, 14))

        banner_top = tk.Frame(banner, bg="#0A3D62")
        banner_top.pack(fill=tk.X)

        self.about_icon_image = self._load_about_icon_image(size=96)
        if self.about_icon_image is not None:
            tk.Label(
                banner_top,
                image=self.about_icon_image,
                bg="#0A3D62"
            ).pack(side=tk.LEFT, padx=(0, 12))
        else:
            # Fallback if icon loading fails.
            tk.Label(
                banner_top,
                text="FSB",
                font=("Avenir Next", 16, "bold"),
                fg="#F6E58D",
                bg="#0A3D62",
                width=4
            ).pack(side=tk.LEFT, padx=(0, 12))

        title_box = tk.Frame(banner_top, bg="#0A3D62")
        title_box.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(
            title_box,
            text=APP_NAME,
            font=("Avenir Next", 22, "bold"),
            fg="#F6E58D",
            bg="#0A3D62"
        ).pack(anchor="w")

        tk.Label(
            title_box,
            text=f"Version {app_version_label()}",
            font=("Avenir Next", 12, "bold"),
            fg="#DFF9FB",
            bg="#0A3D62"
        ).pack(anchor="w", pady=(2, 0))

        body = tk.Frame(outer, bg="#FFFFFF", padx=16, pady=14, highlightthickness=2, highlightbackground="#F0932B")
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            body,
            text="Created by Bill Cody, K3CDY",
            font=("Avenir Next", 13),
            fg="#130F40",
            bg="#FFFFFF"
        ).pack(anchor="w", pady=(0, 8))

        github_url = "https://github.com/williamscody/FlexSpotBridge"

        tk.Label(
            body,
            text=github_url,
            font=("Menlo", 12),
            fg="#0652DD",
            bg="#FFFFFF",
            cursor="hand2"
        ).pack(anchor="w")

        def open_github(_event=None):
            webbrowser.open(github_url)

        body.bind("<Button-1>", open_github)
        for child in body.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") == github_url:
                child.bind("<Button-1>", open_github)

        tk.Button(
            outer,
            text="Close",
            command=about_win.destroy,
            bg="#F7F7F7",
            fg="#111111",
            activebackground="#EDEDED",
            activeforeground="#000000",
            font=("Avenir Next", 13, "bold"),
            relief=tk.RAISED,
            bd=1,
            padx=16,
            pady=6
        ).pack(anchor="e", pady=(12, 0))

        about_win.grab_set()
        about_win.focus_set()

    def open_settings(self):
        settings_win = tk.Toplevel(self.root)
        settings_win.title("Preferences")

        # Settings fields
        settings = [
            ("FLEX_IP", "FLEX_IP"),
            ("FLEX_PORT", "FLEX_PORT"),
        ]

        entries = {}
        row = 0
        for label, var_name in settings:
            field_frame = tk.Frame(settings_win)
            field_frame.grid(row=row, column=0, columnspan=2, pady=(4, 4))
            tk.Label(field_frame, text=f"{label}:").pack(side=tk.LEFT)
            entry = tk.Entry(field_frame, width=20)
            entry.insert(0, str(globals()[var_name]))
            entry.pack(side=tk.LEFT, padx=(6, 0))
            entries[var_name] = entry
            row += 1

        keep_current_mode_var = tk.BooleanVar(value=KEEP_CURRENT_MODE)
        tk.Checkbutton(
            settings_win,
            text="Keep current mode",
            variable=keep_current_mode_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 8))
        row += 1

        duplicate_mode_frame = tk.Frame(settings_win, bg="SystemControlBackgroundColor")
        duplicate_mode_frame.grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        if CONTEST_MODE:
            initial_duplicate_mode = "contest"
        elif REMOVE_DUPLICATE_SPOTS:
            initial_duplicate_mode = "remove_older"
        else:
            initial_duplicate_mode = "off"

        duplicate_mode_var = tk.StringVar(value=initial_duplicate_mode)

        tk.Label(
            duplicate_mode_frame,
            text="Duplicate handling mode:",
            bg="SystemControlBackgroundColor"
        ).pack(anchor="w")

        tk.Radiobutton(
            duplicate_mode_frame,
            text="Off (keep all spots)",
            variable=duplicate_mode_var,
            value="off",
            bg="SystemControlBackgroundColor"
        ).pack(anchor="w", padx=(16, 0))

        tk.Radiobutton(
            duplicate_mode_frame,
            text="Remove older spots at same frequency",
            variable=duplicate_mode_var,
            value="remove_older",
            bg="SystemControlBackgroundColor"
        ).pack(anchor="w", padx=(16, 0))

        tk.Radiobutton(
            duplicate_mode_frame,
            text="Contest mode (ignore newer spots near same frequency)",
            variable=duplicate_mode_var,
            value="contest",
            bg="SystemControlBackgroundColor"
        ).pack(anchor="w", padx=(16, 0))

        row += 1

        duplicate_threshold_var = tk.IntVar(value=DUPLICATE_SPOT_THRESHOLD_HZ)
        duplicate_threshold_frame = tk.Frame(settings_win, bg="SystemControlBackgroundColor")
        duplicate_threshold_frame.grid(row=row, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 8))
        tk.Label(
            duplicate_threshold_frame,
            text="Duplicate threshold:",
            bg="SystemControlBackgroundColor"
        ).pack(side=tk.LEFT)
        duplicate_threshold_spinbox = tk.Spinbox(
            duplicate_threshold_frame,
            from_=0,
            to=1000,
            width=5,
            textvariable=duplicate_threshold_var
        )
        duplicate_threshold_spinbox.pack(side=tk.LEFT, padx=(6, 4))
        tk.Label(duplicate_threshold_frame, text="Hz", bg="SystemControlBackgroundColor").pack(side=tk.LEFT)
        row += 1

        enable_auto_dupe_threshold_var = tk.BooleanVar(value=ENABLE_AUTO_DUPE_THRESHOLD)
        tk.Checkbutton(
            settings_win,
            text="Enable Auto-Dupe Threshold",
            variable=enable_auto_dupe_threshold_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        row += 1

        def update_duplicate_threshold_control_state(*_args):
            if enable_auto_dupe_threshold_var.get():
                duplicate_threshold_spinbox.configure(state="disabled")
                duplicate_threshold_var.set(DUPLICATE_SPOT_THRESHOLD_HZ)
            else:
                duplicate_threshold_spinbox.configure(state="normal")

        enable_auto_dupe_threshold_var.trace_add("write", update_duplicate_threshold_control_state)
        update_duplicate_threshold_control_state()

        def refresh_duplicate_threshold_preview():
            if not settings_win.winfo_exists():
                return
            if enable_auto_dupe_threshold_var.get():
                current_threshold = DUPLICATE_SPOT_THRESHOLD_HZ
                if duplicate_threshold_var.get() != current_threshold:
                    duplicate_threshold_var.set(current_threshold)
            settings_win.after(250, refresh_duplicate_threshold_preview)

        settings_win.after(250, refresh_duplicate_threshold_preview)

        auto_clear_spots_var = tk.BooleanVar(value=AUTO_CLEAR_SPOTS_ENABLED)
        tk.Checkbutton(
            settings_win,
            text="Auto-clear spots older than:",
            variable=auto_clear_spots_var
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(6, 8))
        
        auto_clear_frame = tk.Frame(settings_win, bg="SystemControlBackgroundColor")
        auto_clear_frame.grid(row=row, column=1, sticky="w", padx=0)
        
        auto_clear_age_var = tk.IntVar(value=AUTO_CLEAR_SPOTS_AGE_MINUTES)
        auto_clear_spinbox = tk.Spinbox(
            auto_clear_frame,
            from_=1,
            to=99,
            width=3,
            textvariable=auto_clear_age_var
        )
        auto_clear_spinbox.pack(side=tk.LEFT, padx=(0, 4))
        
        tk.Label(auto_clear_frame, text="minutes", bg="SystemControlBackgroundColor").pack(side=tk.LEFT)
        row += 1

        verbose_logging_var = tk.BooleanVar(value=VERBOSE_LOGGING)
        tk.Checkbutton(
            settings_win,
            text="Verbose debug logging",
            variable=verbose_logging_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 8))
        row += 1

        applescript_only_lookup_var = tk.BooleanVar(value=FORCE_APPLESCRIPT_LOOKUP_ONLY)
        tk.Checkbutton(
            settings_win,
            text="AppleScript-only MLDX lookup (disable URL fallback)",
            variable=applescript_only_lookup_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        row += 1

        spot_age_frame = tk.LabelFrame(settings_win, text="Spot Age Colors", padx=10, pady=10)
        spot_age_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 6))
        settings_win.grid_columnconfigure(1, weight=1)

        now_color_var = tk.StringVar(value=SPOT_COLOR_NOW)
        red_color_var = tk.StringVar(value=SPOT_COLOR_RED)
        yellow_color_var = tk.StringVar(value=SPOT_COLOR_YELLOW)
        gray_color_var = tk.StringVar(value=SPOT_COLOR_GRAY)
        now_bg_color_var = tk.StringVar(value=SPOT_BG_COLOR_NOW)
        red_bg_color_var = tk.StringVar(value=SPOT_BG_COLOR_RED)
        yellow_bg_color_var = tk.StringVar(value=SPOT_BG_COLOR_YELLOW)
        gray_bg_color_var = tk.StringVar(value=SPOT_BG_COLOR_GRAY)
        worked_color_var = tk.StringVar(value=SPOT_COLOR_WORKED)
        worked_bg_color_var = tk.StringVar(value=SPOT_BG_COLOR_WORKED)
        enable_worked_color_var = tk.BooleanVar(value=ENABLE_WORKED_COLOR)

        red_age_var = tk.IntVar(value=SPOT_AGE_RED_MINUTES)
        yellow_age_var = tk.IntVar(value=SPOT_AGE_YELLOW_MINUTES)
        gray_age_var = tk.IntVar(value=SPOT_AGE_GRAY_MINUTES)

        def refresh_swatch(swatch_canvas, rect_id, color_var):
            color = color_var.get()
            label_id = swatch_canvas._none_label_id
            if str(color).lower() == "none":
                swatch_canvas.itemconfigure(rect_id, fill="#2E2E2E", outline="#9A9A9A")
                swatch_canvas.itemconfigure(label_id, text="NONE", fill="#FFFFFF")
            else:
                swatch_canvas.itemconfigure(rect_id, fill=color, outline=color)
                swatch_canvas.itemconfigure(label_id, text="")

        def choose_color(color_var, refresh_callback):
            current_color = color_var.get()
            initial_color = current_color if str(current_color).startswith("#") else "#000000"
            result = colorchooser.askcolor(color=initial_color, parent=settings_win, title="Choose spot color")
            if result and result[1]:
                color_var.set(result[1])
                refresh_callback()

        def make_swatch(parent, color_var):
            swatch = tk.Canvas(
                parent,
                width=110,
                height=30,
                highlightthickness=1,
                highlightbackground="#B8B8B8",
                bd=0,
                cursor="hand2"
            )
            swatch_rect = swatch.create_rectangle(4, 6, 106, 24)
            swatch._none_label_id = swatch.create_text(55, 15, text="", font=("Avenir Next", 10, "bold"))
            refresh_fn = lambda: refresh_swatch(swatch, swatch_rect, color_var)
            refresh_fn()
            color_var.trace_add("write", lambda *_args: refresh_fn())
            swatch.bind("<Button-1>", lambda _event: choose_color(color_var, refresh_fn))
            return swatch

        tk.Label(spot_age_frame, text="Text").grid(row=0, column=0, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, now_color_var).grid(row=0, column=1, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, red_color_var).grid(row=0, column=2, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, yellow_color_var).grid(row=0, column=3, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, gray_color_var).grid(row=0, column=4, padx=(0, 12), pady=(0, 6), sticky="w")

        tk.Label(spot_age_frame, text="Background").grid(row=1, column=0, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, now_bg_color_var).grid(row=1, column=1, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, red_bg_color_var).grid(row=1, column=2, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, yellow_bg_color_var).grid(row=1, column=3, padx=(0, 12), pady=(0, 6), sticky="w")
        make_swatch(spot_age_frame, gray_bg_color_var).grid(row=1, column=4, padx=(0, 12), pady=(0, 6), sticky="w")

        tk.Button(spot_age_frame, text="None", command=lambda: now_bg_color_var.set("none"), padx=8).grid(
            row=2, column=1, padx=(0, 12), pady=(0, 6), sticky="w"
        )
        tk.Button(spot_age_frame, text="None", command=lambda: red_bg_color_var.set("none"), padx=8).grid(
            row=2, column=2, padx=(0, 12), pady=(0, 6), sticky="w"
        )
        tk.Button(spot_age_frame, text="None", command=lambda: yellow_bg_color_var.set("none"), padx=8).grid(
            row=2, column=3, padx=(0, 12), pady=(0, 6), sticky="w"
        )
        tk.Button(spot_age_frame, text="None", command=lambda: gray_bg_color_var.set("none"), padx=8).grid(
            row=2, column=4, padx=(0, 12), pady=(0, 6), sticky="w"
        )

        tk.Label(spot_age_frame, text="Age").grid(row=3, column=0, padx=(0, 12), pady=(2, 0), sticky="w")
        tk.Label(spot_age_frame, text="Fixed at 0 min").grid(row=3, column=1, padx=(0, 12), pady=(2, 0), sticky="w")
        red_age_spin = tk.Spinbox(spot_age_frame, from_=1, to=240, width=4, textvariable=red_age_var)
        red_age_spin.grid(row=3, column=2, padx=(0, 12), pady=(2, 0), sticky="w")
        yellow_age_spin = tk.Spinbox(spot_age_frame, from_=2, to=240, width=4, textvariable=yellow_age_var)
        yellow_age_spin.grid(row=3, column=3, padx=(0, 12), pady=(2, 0), sticky="w")
        gray_age_spin = tk.Spinbox(spot_age_frame, from_=3, to=240, width=4, textvariable=gray_age_var)
        gray_age_spin.grid(row=3, column=4, padx=(0, 12), pady=(2, 0), sticky="w")
        tk.Label(spot_age_frame, text="minutes").grid(row=3, column=5, sticky="w", pady=(2, 0))

        def set_age_defaults():
            auto_clear_age_var.set(20)
            red_age_var.set(DEFAULT_SPOT_AGE_RED_MINUTES)
            yellow_age_var.set(DEFAULT_SPOT_AGE_YELLOW_MINUTES)
            gray_age_var.set(DEFAULT_SPOT_AGE_GRAY_MINUTES)
            now_color_var.set(DEFAULT_SPOT_COLOR_NOW)
            red_color_var.set(DEFAULT_SPOT_COLOR_RED)
            yellow_color_var.set(DEFAULT_SPOT_COLOR_YELLOW)
            gray_color_var.set(DEFAULT_SPOT_COLOR_GRAY)
            now_bg_color_var.set(DEFAULT_SPOT_BG_COLOR_NOW)
            red_bg_color_var.set(DEFAULT_SPOT_BG_COLOR_RED)
            yellow_bg_color_var.set(DEFAULT_SPOT_BG_COLOR_YELLOW)
            gray_bg_color_var.set(DEFAULT_SPOT_BG_COLOR_GRAY)
            worked_color_var.set(DEFAULT_SPOT_COLOR_WORKED)
            worked_bg_color_var.set(DEFAULT_SPOT_BG_COLOR_WORKED)

        tk.Button(spot_age_frame, text="Default", command=set_age_defaults).grid(
            row=4, column=0, columnspan=6, pady=(10, 0)
        )

        enable_text_colors_var = tk.BooleanVar(value=ENABLE_SPOT_TEXT_COLORS)
        tk.Checkbutton(
            spot_age_frame,
            text="Update spot text color",
            variable=enable_text_colors_var
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))

        enable_background_colors_var = tk.BooleanVar(value=ENABLE_SPOT_BACKGROUND_COLORS)
        tk.Checkbutton(
            spot_age_frame,
            text="Update spot background color",
            variable=enable_background_colors_var
        ).grid(row=6, column=0, columnspan=6, sticky="w", pady=(2, 0))

        mode_preview_var = tk.StringVar()

        def update_mode_preview(*_args):
            text_enabled = enable_text_colors_var.get()
            background_enabled = enable_background_colors_var.get()

            if text_enabled and background_enabled:
                mode_preview_var.set("Active mode: text + background")
            elif text_enabled:
                mode_preview_var.set("Active mode: text only")
            elif background_enabled:
                mode_preview_var.set("Active mode: background only")
            else:
                mode_preview_var.set("Active mode: disabled")

        enable_text_colors_var.trace_add("write", update_mode_preview)
        enable_background_colors_var.trace_add("write", update_mode_preview)
        update_mode_preview()

        tk.Label(
            spot_age_frame,
            textvariable=mode_preview_var,
            fg="#FFFFFF"
        ).grid(row=7, column=0, columnspan=6, sticky="w", pady=(8, 0))

        tk.Frame(spot_age_frame, height=1, bg="#CCCCCC").grid(
            row=8, column=0, columnspan=6, sticky="ew", pady=(10, 0)
        )

        worked_row_frame = tk.Frame(spot_age_frame)
        worked_row_frame.grid(row=9, column=0, columnspan=6, sticky="w", pady=(6, 4))
        tk.Label(worked_row_frame, text="Worked:").pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(worked_row_frame, text="Text").pack(side=tk.LEFT, padx=(0, 4))
        make_swatch(worked_row_frame, worked_color_var).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(worked_row_frame, text="Background").pack(side=tk.LEFT, padx=(0, 4))
        make_swatch(worked_row_frame, worked_bg_color_var).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(
            worked_row_frame, text="None",
            command=lambda: worked_bg_color_var.set("none"), padx=8
        ).pack(side=tk.LEFT, padx=(0, 12))
        tk.Checkbutton(
            worked_row_frame, text="Enable Worked color",
            variable=enable_worked_color_var
        ).pack(side=tk.LEFT)

        row += 1

        def save():
            global KEEP_CURRENT_MODE, REMOVE_DUPLICATE_SPOTS, CONTEST_MODE, DUPLICATE_SPOT_THRESHOLD_HZ, VERBOSE_LOGGING, AUTO_CLEAR_SPOTS_ENABLED, AUTO_CLEAR_SPOTS_AGE_MINUTES
            global SPOT_AGE_RED_MINUTES, SPOT_AGE_YELLOW_MINUTES, SPOT_AGE_GRAY_MINUTES
            global SPOT_COLOR_NOW, SPOT_COLOR_RED, SPOT_COLOR_YELLOW, SPOT_COLOR_GRAY
            global SPOT_BG_COLOR_NOW, SPOT_BG_COLOR_RED, SPOT_BG_COLOR_YELLOW, SPOT_BG_COLOR_GRAY
            global SPOT_COLOR_WORKED, SPOT_BG_COLOR_WORKED, ENABLE_WORKED_COLOR
            global ENABLE_SPOT_TEXT_COLORS, ENABLE_SPOT_BACKGROUND_COLORS, ENABLE_AUTO_DUPE_THRESHOLD, FORCE_APPLESCRIPT_LOOKUP_ONLY
            for var_name, entry in entries.items():
                value = entry.get()
                if var_name in ["FLEX_PORT"]:
                    globals()[var_name] = int(value)
                else:
                    globals()[var_name] = value

            try:
                red_minutes = int(red_age_var.get())
                yellow_minutes = int(yellow_age_var.get())
                gray_minutes = int(gray_age_var.get())
                duplicate_threshold_hz = int(duplicate_threshold_var.get())
            except ValueError:
                print("Spot age and duplicate threshold settings must be whole numbers")
                return

            if red_minutes < 1:
                print("Red spot age must be at least 1 minute")
                return

            if yellow_minutes <= red_minutes:
                print("Yellow spot age must be greater than red spot age")
                return

            if gray_minutes <= yellow_minutes:
                print("Gray spot age must be greater than yellow spot age")
                return

            if duplicate_threshold_hz < 0:
                print("Duplicate threshold must be 0 Hz or greater")
                return

            KEEP_CURRENT_MODE = keep_current_mode_var.get()
            selected_duplicate_mode = duplicate_mode_var.get()
            REMOVE_DUPLICATE_SPOTS = selected_duplicate_mode == "remove_older"
            CONTEST_MODE = selected_duplicate_mode == "contest"
            ENABLE_AUTO_DUPE_THRESHOLD = enable_auto_dupe_threshold_var.get()
            if not ENABLE_AUTO_DUPE_THRESHOLD:
                DUPLICATE_SPOT_THRESHOLD_HZ = duplicate_threshold_hz
            AUTO_CLEAR_SPOTS_ENABLED = auto_clear_spots_var.get()
            AUTO_CLEAR_SPOTS_AGE_MINUTES = auto_clear_age_var.get()
            VERBOSE_LOGGING = verbose_logging_var.get()
            FORCE_APPLESCRIPT_LOOKUP_ONLY = applescript_only_lookup_var.get()
            SPOT_AGE_RED_MINUTES = red_minutes
            SPOT_AGE_YELLOW_MINUTES = yellow_minutes
            SPOT_AGE_GRAY_MINUTES = gray_minutes
            SPOT_COLOR_NOW = now_color_var.get()
            SPOT_COLOR_RED = red_color_var.get()
            SPOT_COLOR_YELLOW = yellow_color_var.get()
            SPOT_COLOR_GRAY = gray_color_var.get()
            SPOT_BG_COLOR_NOW = now_bg_color_var.get()
            SPOT_BG_COLOR_RED = red_bg_color_var.get()
            SPOT_BG_COLOR_YELLOW = yellow_bg_color_var.get()
            SPOT_BG_COLOR_GRAY = gray_bg_color_var.get()
            SPOT_COLOR_WORKED = worked_color_var.get()
            SPOT_BG_COLOR_WORKED = worked_bg_color_var.get()
            ENABLE_WORKED_COLOR = enable_worked_color_var.get()
            ENABLE_SPOT_TEXT_COLORS = enable_text_colors_var.get()
            ENABLE_SPOT_BACKGROUND_COLORS = enable_background_colors_var.get()

            # Force all tracked spots to be recolored against the new settings.
            with flex_spots_lock:
                for spot in flex_spots.values():
                    spot["last_text_color"] = None
                    spot["last_background_color"] = None
                    spot["last_text_enabled"] = None
                    spot["last_background_enabled"] = None

            save_settings()
            settings_win.destroy()
            print("Settings saved")

        tk.Button(settings_win, text="OK", command=save).grid(row=row, column=0, columnspan=3, pady=(8, 0))

        settings_win.update_idletasks()
        requested_width = settings_win.winfo_reqwidth() + 16
        requested_height = settings_win.winfo_reqheight() + 16
        settings_win.geometry(f"{requested_width}x{requested_height}")
        settings_win.minsize(requested_width, requested_height)

        def save_from_keyboard(_event=None):
            save()
            return "break"

        settings_win.bind("<Return>", save_from_keyboard)
        settings_win.bind("<KP_Enter>", save_from_keyboard)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()