"""RSA-PSS request signing for the Kalshi API."""

from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiAuth:
    """Handles RSA-PSS signature generation for Kalshi API authentication."""

    def __init__(self, key_id: str, private_key_path: str):
        self._key_id = key_id
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
            )
        if not isinstance(self._private_key, rsa.RSAPrivateKey):
            raise TypeError("Private key must be an RSA key")

    def sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate the 3 authentication headers for a Kalshi API request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: API path WITHOUT query parameters (e.g., /trade-api/v2/markets)

        Returns:
            Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
        """
        # Strip query params if accidentally included
        path = path.split("?")[0]

        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    @property
    def key_id(self) -> str:
        return self._key_id
