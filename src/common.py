"""Shared helpers for the camera tools.

Deliberately small. Anything that shells out lives here so there is one place
where "a command failed" is turned into None instead of an exception - these are
diagnostics, and a diagnostic that crashes when one reading is unavailable is
worse than no diagnostic.
"""

import os
import subprocess

CONFIG_ENV = "CAMERA_CONFIG"


def sh(args, timeout=8):
    """Run a command; return combined output as text, or None if it could not run.

    Some tools write useful output and still exit non-zero, so the return code
    is deliberately not checked here.
    """
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout or "") + (p.stderr or "")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path(explicit=None):
    if explicit:
        return explicit
    return os.environ.get(CONFIG_ENV) or os.path.join(repo_root(), "config", "config.yaml")


def load_config(explicit=None):
    """Load config.yaml. Returns (config, error_message).

    Never invents defaults for anything team- or device-specific. A missing
    config is reported, not papered over.
    """
    path = config_path(explicit)
    try:
        import yaml
    except ImportError:
        return None, "PyYAML not installed (apt install python3-yaml)"
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}, None
    except OSError as e:
        return None, "cannot read %s: %s" % (path, e)
    except Exception as e:                     # malformed YAML
        return None, "cannot parse %s: %s" % (path, e)


def device_from(cfg, override=None):
    """The camera path to use: explicit override, then by-id, then the fallback.

    by-id is preferred because it carries the serial and survives replugging;
    /dev/videoN does not.
    """
    if override:
        return override
    cam = (cfg or {}).get("camera", {})
    byid = cam.get("by_id")
    if byid and os.path.exists(byid):
        return byid
    return cam.get("fallback", "/dev/video0")


def service_state(name):
    out = sh(["systemctl", "is-active", name])
    return out.strip() if out else None


def throttled():
    """(raw_int, text) from vcgencmd, or (None, None) off a Pi."""
    out = sh(["vcgencmd", "get_throttled"])
    if not out or "=" not in out:
        return None, None
    try:
        raw = int(out.split("=")[1].strip(), 16)
    except ValueError:
        return None, None
    bits = {0: "under-voltage now", 1: "arm freq capped now", 2: "throttled now",
            3: "soft temp limit now", 16: "under-voltage has occurred",
            17: "arm freq capped has occurred", 18: "throttling has occurred",
            19: "soft temp limit has occurred"}
    hits = [t for b, t in bits.items() if raw & (1 << b)]
    return raw, "; ".join(hits) if hits else "clean"
