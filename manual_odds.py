"""Input-format adapter only. Sport models continue receiving decimal odds."""
import math


def hk_to_decimal(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("不含本金水位必須為大於 0 的有限數字")
    return number + 1.0
