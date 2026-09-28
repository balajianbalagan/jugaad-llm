from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class NgrokTunnel:
    """Manages temporary public HTTPS tunnel via ngrok."""

    def __init__(self, port: int = 4000, authtoken: str | None = None):
        self.port = port
        self.authtoken = authtoken or os.environ.get("NGROK_AUTHTOKEN")
        self.public_url: str | None = None
        self._tunnel: Any = None

    def start(self) -> str:
        """Start ngrok tunnel and return public HTTPS URL."""
        try:
            from pyngrok import conf, ngrok

            if self.authtoken:
                ngrok.set_auth_token(self.authtoken)
            elif os.environ.get("NGROK_AUTHTOKEN"):
                ngrok.set_auth_token(os.environ["NGROK_AUTHTOKEN"])

            # Open HTTP tunnel
            self._tunnel = ngrok.connect(self.port, "http")
            self.public_url = self._tunnel.public_url

            # Ensure HTTPS url
            if self.public_url and self.public_url.startswith("http://"):
                self.public_url = self.public_url.replace("http://", "https://", 1)

            logger.info(f"Ngrok tunnel established: {self.public_url}")
            return self.public_url
        except Exception as e:
            logger.error(f"Failed to start ngrok tunnel: {e}")
            raise

    def stop(self) -> None:
        """Terminate ngrok tunnel."""
        if self._tunnel:
            try:
                from pyngrok import ngrok
                ngrok.disconnect(self._tunnel.public_url)
                ngrok.kill()
                logger.info("Ngrok tunnel closed.")
            except Exception as e:
                logger.warning(f"Error closing ngrok tunnel: {e}")
            finally:
                self._tunnel = None
                self.public_url = None

    @property
    def api_base(self) -> str | None:
        if self.public_url:
            return f"{self.public_url}/v1"
        return None
