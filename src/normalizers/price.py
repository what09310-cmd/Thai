import re
from typing import Optional, Tuple


_PRICE_RE = re.compile(r"[\d,]+")


def _clean_int(s: str) -> int:
    """'14,000' -> 14000"""
    return int(s.replace(",", ""))


def parse_price_range(raw: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """
    Parse une chaîne de prix RentHub.

    Exemples:
        "14,000 - 36,000 THB/month" -> (14000, 36000)
        "4,500 THB/month"           -> (4500, 4500)
        "-"                         -> (None, None)
        None                        -> (None, None)
        ""                          -> (None, None)
    """
    if not raw or raw.strip() in ("-", ""):
        return None, None

    matches = _PRICE_RE.findall(raw)
    if not matches:
        return None, None

    nums = [_clean_int(m) for m in matches if m.replace(",", "").isdigit()]
    if not nums:
        return None, None

    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]
