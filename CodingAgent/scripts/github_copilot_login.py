#!/usr/bin/env python3
"""CLI helper to authenticate with GitHub Copilot via OAuth device flow.

Usage:
    python scripts/github_copilot_login.py          # login
    python scripts/github_copilot_login.py --status # check auth status
    python scripts/github_copilot_login.py --logout # clear stored token
"""

from __future__ import annotations

import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.inference.adapters.github_copilot_auth import (
    start_device_flow,
    poll_for_token,
    save_token,
    clear_token,
    is_authenticated,
    DeviceCodeExpired,
    AuthCancelled,
)


def cmd_status() -> None:
    if is_authenticated():
        print("GitHub Copilot: authenticated (token present)")
    else:
        print("GitHub Copilot: NOT authenticated — run without --status to log in")


def cmd_logout() -> None:
    clear_token()
    print("GitHub Copilot: token cleared (logged out)")


def cmd_login() -> None:
    if is_authenticated():
        print("GitHub Copilot: already authenticated.")
        print("Run with --logout first to re-authenticate.")
        return

    print("Starting GitHub OAuth device flow...")
    try:
        flow = start_device_flow()
    except Exception as e:
        print(f"Error starting device flow: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    print("  Open this URL in your browser:")
    print(f"  {flow.verification_uri}")
    print()
    print("  Enter this code when prompted:")
    print(f"  {flow.user_code}")
    print("=" * 60)
    print()
    print(f"Waiting for authorization (expires in {flow.expires_in}s)...")

    try:
        token = poll_for_token(
            flow.device_code, flow.interval, timeout=float(flow.expires_in)
        )
    except DeviceCodeExpired:
        print("Device code expired. Run the script again to retry.", file=sys.stderr)
        sys.exit(1)
    except AuthCancelled:
        print("Authorization cancelled.", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print("Timed out waiting for authorization.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)

    save_token(token)
    print("GitHub Copilot: authentication successful! Token saved.")
    print("The provider is now ready to use.")


def main() -> None:
    args = sys.argv[1:]
    if "--status" in args:
        cmd_status()
    elif "--logout" in args:
        cmd_logout()
    else:
        cmd_login()


if __name__ == "__main__":
    main()
