"""Decimal money helpers for paper trading. SQLite stores amounts as decimal text."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

MONEY_QUANT = Decimal("0.00000001")
QTY_QUANT = Decimal("0.000000000000000001")
BPS = Decimal("10000")
ZERO = Decimal("0")


class PaperMoneyError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise PaperMoneyError("INVALID_NUMBER", "Amount is missing")
    return Decimal(str(value))


def money(value: object) -> Decimal:
    amount = to_decimal(value)
    if not amount.is_finite():
        raise PaperMoneyError("INVALID_NUMBER", "Amount is not finite")
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)


def quantity(value: object) -> Decimal:
    amount = to_decimal(value)
    if not amount.is_finite():
        raise PaperMoneyError("INVALID_NUMBER", "Quantity is not finite")
    return amount.quantize(QTY_QUANT, rounding=ROUND_DOWN)


def money_text(value: object) -> str:
    return format(money(value), "f")


def qty_text(value: object) -> str:
    return format(quantity(value), "f")


def bps_fraction(bps: object) -> Decimal:
    points = money(bps)
    if points < ZERO:
        raise PaperMoneyError("INVALID_BPS", "Basis points must be >= 0")
    return points / BPS
