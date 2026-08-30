"""Shell access, config loading, and device resolution.

The one place where "a command failed" becomes None instead of an exception.
These are diagnostics: a tool that dies because one reading is unavailable is
worse than no tool.
"""

import os
import subprocess

CONFIG_ENV = "CAMERA_CONFIG"


def sh(args, timeout=8):
    """Run a command; return combined output as text, or None if it could not run.

    Some tools write useful output and still exit non-zero, so the return code
    is deliberately not checked.
    """
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout or "") + (p.stderr or "")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
        return None, "PyYAML not installed (sudo apt install python3-yaml)"
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


def find_file(cfg, name):
    """Locate a configured file, checking the repo and the Pi's working dir."""
    if not name:
        return None
    for cand in (name,
                 os.path.join(repo_root(), name),
                 os.path.join(repo_root(), "config", os.path.basename(name)),
                 os.path.expanduser("~/vision/calib/" + os.path.basename(name))):
        if os.path.exists(cand):
            return cand
    return None
