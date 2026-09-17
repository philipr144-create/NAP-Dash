"""Static setup PIN for the NAP phone navigation remote.

The six-digit PIN only authorizes initial browser pairing.
Successful pairing still returns the separate persistent .pairing-key,
which is used for authenticated navigation requests afterward.
"""
import hmac
import os


DEFAULT_PAIR_CODE = "087911"


def _pair_code():
    code = os.environ.get("NAP_PHONE_PAIR_CODE", DEFAULT_PAIR_CODE).strip()
    if len(code) != 6 or not code.isdigit():
        return DEFAULT_PAIR_CODE
    return code


def issue():
    """Return the configured static six-digit navigation setup PIN."""
    return _pair_code()


def redeem(code):
    """Validate the static setup PIN."""
    supplied = str(code).strip()
    expected = _pair_code()

    if not hmac.compare_digest(supplied, expected):
        raise ValueError(
            "Incorrect navigation setup code. "
            "Use the six-digit PIN configured for this comma."
        )


if __name__ == "__main__":
    print(issue())
