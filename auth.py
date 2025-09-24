import asyncio
import os
import re
import time
import webbrowser
import subprocess
from typing import Optional

from mcp import ClientSession


class AuthManager:
    """Handles Outlook MCP authentication orchestration from the Python client."""

    def __init__(
        self,
        server_dir: str,
        *,
        timeout_seconds: Optional[int] = None,
        poll_interval_seconds: Optional[float] = None,
        start_auth_server: Optional[bool] = None,
        browser_open: Optional[bool] = None,
    ) -> None:
        # Where outlook-auth-server.js lives
        self.server_dir = server_dir
        self.auth_server_process: Optional[subprocess.Popen] = None

        # Config via env with sensible defaults
        self.timeout_seconds = (
            int(os.getenv("AUTH_TIMEOUT_SECONDS", "180"))
            if timeout_seconds is None
            else int(timeout_seconds)
        )
        self.poll_interval_seconds = (
            float(os.getenv("AUTH_POLL_INTERVAL_SECONDS", "2"))
            if poll_interval_seconds is None
            else float(poll_interval_seconds)
        )
        self.start_auth_server = (
            os.getenv("START_AUTH_SERVER", "true").lower() != "false"
            if start_auth_server is None
            else bool(start_auth_server)
        )
        self.browser_open = (
            os.getenv("BROWSER_OPEN", "true").lower() != "false"
            if browser_open is None
            else bool(browser_open)
        )

    def _contents_to_text(self, contents) -> str:
        parts = []
        for c in contents or []:
            if getattr(c, "type", None) == "text" and hasattr(c, "text"):
                parts.append(c.text)
            elif isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)

    def _start_auth_server_if_available(self) -> None:
        if self.auth_server_process is not None:
            return
        auth_server_path = os.path.join(self.server_dir, "outlook-auth-server.js")
        if not os.path.exists(auth_server_path):
            return
        try:
            env = os.environ.copy()
            # Map OUTLOOK_* into MS_* for the helper if not already set
            env.setdefault("MS_CLIENT_ID", env.get("OUTLOOK_CLIENT_ID", ""))
            env.setdefault("MS_CLIENT_SECRET", env.get("OUTLOOK_CLIENT_SECRET", ""))
            print("Starting local auth server:", auth_server_path)
            self.auth_server_process = subprocess.Popen(
                ["node", auth_server_path],
                env=env,
                cwd=self.server_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
        except Exception as e:
            print("Failed to start auth server:", str(e))

    def stop(self) -> None:
        if self.auth_server_process is not None:
            try:
                self.auth_server_process.terminate()
            except Exception:
                pass
            self.auth_server_process = None

    async def ensure_authenticated(self, session: ClientSession) -> None:
        """Ensure we are authenticated using server tools and local auth helper."""
        timeout = self.timeout_seconds

        # 1) Check current status first
        try:
            status = await session.call_tool("check-auth-status", {})
            status_text = self._contents_to_text(getattr(status, "content", None))
            if status_text and "Authenticated" in status_text:
                print("Authentication status:", status_text)
                return
            print("Authentication status:", status_text or "Unknown/Not authenticated")
        except Exception as e:
            print("Warning: check-auth-status failed:", str(e))

        # 2) Not authenticated: start local helper if configured
        if self.start_auth_server:
            self._start_auth_server_if_available()

        # 3) Trigger authentication to get URL
        try:
            auth = await session.call_tool("authenticate", {"force": True})
            auth_text = self._contents_to_text(getattr(auth, "content", None))
            print("Authenticate tool response:", auth_text or "<no text>")
            url_match = re.search(r"https?://[^\s]+", auth_text or "")
            if url_match:
                auth_url = url_match.group(0)
                print("Opening authentication URL:", auth_url)
                if self.browser_open:
                    try:
                        webbrowser.open(auth_url)
                    except Exception as e:
                        print("Could not open browser automatically:", str(e))
            else:
                print("Could not find authentication URL in tool response. Please check the server logs.")
        except Exception as e:
            print("Error calling authenticate tool:", str(e))

        # 4) Poll until authenticated or timeout
        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(self.poll_interval_seconds)
            try:
                status = await session.call_tool("check-auth-status", {})
                status_text = self._contents_to_text(getattr(status, "content", None))
                if status_text and "Authenticated" in status_text:
                    print("Authentication completed:", status_text)
                    return
                else:
                    print("Waiting for authentication...", status_text or "Not authenticated yet")
            except Exception as e:
                print("Polling error:", str(e))

        raise TimeoutError("Authentication timed out. Please complete the sign-in in your browser and try again.")
