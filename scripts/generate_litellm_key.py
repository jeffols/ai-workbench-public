#!/usr/bin/env python3
import secrets

def generate_litellm_master_key(prefix: str = "sk-", nbytes: int = 32) -> str:
    """
    Generate a cryptographically secure LiteLLM master key.

    Args:
        prefix: Required LiteLLM key prefix.
        nbytes: Random byte length before URL-safe encoding.
                32 bytes is a good default for strong entropy.

    Returns:
        A string like: sk-<random>
    """
    return prefix + secrets.token_urlsafe(nbytes)

if __name__ == "__main__":
    key = generate_litellm_master_key()
    print("LITELLM_MASTER_KEY=" + key)
