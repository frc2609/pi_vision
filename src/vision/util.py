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


def _merge(default, override):
    """Shallow-merge one level of dicts, so a camera can override part of a default."""
    out = dict(default or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def cameras(cfg, only=None):
    """Every configured camera, normalised, with defaults merged and the device
    path resolved. Returns a list of dicts; `only` filters by name.

    Accepts the older single-`camera:` shape as a one-element list, so a config
    that has not been migrated still works rather than failing obscurely.
    """
    cfg = cfg or {}
    defaults = cfg.get("camera_defaults", {})
    raw = cfg.get("cameras")
    if raw is None:
        legacy = cfg.get("camera")
        raw = [legacy] if legacy else []
    out = []
    for i, entry in enumerate(raw):
        cam = _merge(defaults, entry or {})
        cam.setdefault("name", "camera%d" % i)
        cam["device"] = device_for(cam)
        out.append(cam)
    if only:
        out = [c for c in out if c["name"] == only]
    return out


def device_for(cam, override=None):
    """The path to use: explicit override, then by-id, then by-path, then fallback.

    by-id is preferred because it carries the serial and survives replugging.
    by-path identifies the physical USB port instead, which is the fallback when
    two identical cameras ship with the same descriptor - and on a robot, "this
    camera is whatever is in this port" is arguably the better rule anyway.
    """
    if override:
        return override
    # by_path first: it names the physical USB port and is unambiguous, whereas
    # by_id depends on the USB descriptor being unique - which for two identical
    # Thriftiest Cams it is not, so its link resolves to whichever enumerated
    # last and silently points at the wrong camera.
    for key in ("by_path", "by_id"):
        p = (cam or {}).get(key)
        if p and os.path.exists(p):
            return p
    return (cam or {}).get("fallback", "/dev/video0")


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
