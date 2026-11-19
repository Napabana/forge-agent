"""Pre-request reservations for a single LLM evaluation trial.

The caller must supply a trusted input upper bound and enforce the returned
output limit on the physical request. Estimates alone are not upper bounds.
"""
from dataclasses import dataclass
from threading import Lock


class RequestBudgetExhausted(RuntimeError):
    """The next model request was deliberately not sent."""


@dataclass(frozen=True, eq=False)
class Reservation:
    pool: str
    input_upper: int
    output_limit: int


class RequestBudget:
    def __init__(self, *, total: int, pools: dict[str, int]):
        if (type(total) is not int or total <= 0 or not pools
                or any(type(n) is not int or n < 0 for n in pools.values())
                or sum(pools.values()) != total):
            raise ValueError("invalid trial budget allocation")
        self._remaining = dict(pools)
        self._open: set[Reservation] = set()
        self._lock = Lock()
        self._invalidated = False

    def reserve(self, pool: str, *, input_upper: int, max_output: int) -> Reservation:
        if (type(input_upper) is not int or input_upper < 0
                or type(max_output) is not int or max_output <= 0):
            raise ValueError("a valid input ceiling and output limit are required")
        with self._lock:
            if self._invalidated:
                raise RuntimeError("Trial budget invalidated by inconsistent provider usage")
            output = min(max_output, self._remaining[pool] - input_upper)
            if output <= 0:
                raise RequestBudgetExhausted(f"Trial request budget exhausted: {pool}")
            reservation = Reservation(pool, input_upper, output)
            self._remaining[pool] -= input_upper + output
            self._open.add(reservation)
            return reservation

    def settle(self, reservation: Reservation, *, input_tokens: int | None,
               output_tokens: int | None) -> None:
        with self._lock:
            self._open.remove(reservation)
            if any(value is not None and (type(value) is not int or not 0 <= value <= limit)
                   for value, limit in ((input_tokens, reservation.input_upper),
                                        (output_tokens, reservation.output_limit))):
                self._invalidated = True
                raise RuntimeError("Provider usage exceeded or invalidated the reservation")
            if input_tokens is None or output_tokens is None:
                # Failed and unknown calls keep their full reservation.
                return
            self._remaining[reservation.pool] += (
                reservation.input_upper - input_tokens + reservation.output_limit - output_tokens
            )
