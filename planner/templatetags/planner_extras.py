from django import template

from planner.finance import real_value
from planner.privacy import masked_money, privacy_mode_enabled

register = template.Library()


@register.filter
def get_item(value, key):
    if not value:
        return ""
    return value.get(key, "")


@register.filter
def money(value, currency="EUR"):
    if privacy_mode_enabled():
        return masked_money(currency)
    if value is None:
        value = 0
    return f"{value:,.2f} {currency}"


@register.filter
def signed_money(value, currency="EUR"):
    if value is None:
        value = 0
    sign = "+" if value > 0 else ""
    if privacy_mode_enabled():
        return masked_money(currency, sign)
    return f"{sign}{value:,.2f} {currency}"


@register.filter
def account_effects(value, currency="EUR"):
    if not value:
        return "-"
    if privacy_mode_enabled():
        return "; ".join(f"{effect.get('account_name', 'Account')} {masked_money(currency)}" for effect in value)
    parts = []
    for effect in value:
        amount = effect.get("amount", 0)
        sign = "+" if amount > 0 else ""
        parts.append(f"{effect.get('account_name', 'Account')} {sign}{amount:,.2f} {currency}")
    return "; ".join(parts)


@register.simple_tag
def display_money(value, annual_rate, months_from_start, currency="EUR", display_mode="nominal"):
    if display_mode == "real":
        value = real_value(value, annual_rate, months_from_start)
    return money(value, currency)
