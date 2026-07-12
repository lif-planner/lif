from contextvars import ContextVar


PRIVACY_MODE_SESSION_KEY = "privacy_mode_enabled"
MASKED_MONEY = "•••••"

_privacy_mode = ContextVar("privacy_mode", default=False)


def set_privacy_mode(enabled):
    return _privacy_mode.set(bool(enabled))


def reset_privacy_mode(token):
    _privacy_mode.reset(token)


def privacy_mode_enabled():
    return _privacy_mode.get()


def masked_money(currency="EUR", sign=""):
    if sign:
        return f"{sign}{MASKED_MONEY} {currency}"
    return f"{MASKED_MONEY} {currency}"
