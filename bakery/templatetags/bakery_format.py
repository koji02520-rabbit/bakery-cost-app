"""画面表示用の数値の書式。丸めは表示時だけ行う（spec.md 第62項1）。1g単価は小数第3位まで（P58）。"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format(value, places):
    value = _decimal(value)
    if value is None:
        return "—"
    quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{quantized:,.{places}f}"


@register.filter
def yen_per_g(value):
    """1g単価：0.200"""
    return _format(value, 3)


@register.filter
def yen(value):
    """金額：1,234（小数があれば第2位まで）"""
    value = _decimal(value)
    if value is None:
        return "—"
    return _format(value, 0 if value == value.to_integral_value() else 2)


@register.filter
def grams(value):
    """重量：25,000（小数があれば第2位まで）"""
    return yen(value)


@register.filter
def cost(value, places=2):
    """原価：1,234.56（小数第2位まで）。商品原価は |cost:1 で小数第1位まで"""
    return _format(value, int(places))


@register.filter
def percent(value):
    """原価率：32.5"""
    return _format(value, 1)


@register.filter
def signed_cost(value, places=2):
    """原価差額：+12.34 / -5.00。商品原価の差額は |signed_cost:1"""
    value = _decimal(value)
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + _format(value, int(places))


@register.filter
def grams_input(value):
    """入力欄に入れる重量：1000.00 → 1000、12.50 → 12.5（カンマなし）"""
    value = _decimal(value)
    if value is None:
        return ""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@register.filter
def rate_percent(value):
    """歩留まりなどの割合：0.98 → 98"""
    value = _decimal(value)
    if value is None:
        return "—"
    return grams_input((value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
