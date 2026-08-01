"""Kalshi fee schedule (effective 2026-07-07) — source: kalshi-fee-schedule July.pdf.

taker fee = roundup(Mt * 0.07   * C * P * (1-P))   default Mt = 1
maker fee = roundup(Mm * 0.0175 * C * P * (1-P))   default Mm = 0   <-- zero unless listed

So a maker pays NOTHING on a default series, and exactly a QUARTER of the taker
rate on the ~80 "non-standard" series that carry a maker multiplier of 1 (all the
per-game sports series: KXMLBGAME, KXNFLGAME, KXNCAAFGAME, KXWCGAME, KXUCLGAME,
KXWNBAGAME, KXATPMATCH, KXWTAMATCH...). A handful of series are fee-free on both
sides (multiplier 0/0). Rounding is to a centicent per ORDER; at scale the
continuous form below is what matters.

At p = 0.50 that is 1.75 c/contract for a taker and 0.4375 c/contract for a maker
on a maker-fee series — large numbers next to the edges measured in the
who_profits experiment, so always net them before calling a strategy profitable.
"""

TAKER_RATE = 0.07      # dollars per contract at p(1-p) = 1
MAKER_RATE = 0.0175

# series -> (maker_multiplier, taker_multiplier); anything not listed is (0, 1)
NONSTANDARD = {
    'KXAAAGASM': (1, 1),
    'KXATPMATCH': (1, 1),
    'KXBALLONDOR': (1, 1),
    'KXBTCMAX150': (1, 1),
    'KXBTCY': (0, 0),
    'KXCITRINI': (0, 0),
    'KXCPI': (1, 1),
    'KXCPIYOY': (1, 1),
    'KXDOED': (0, 0),
    'KXEGGS': (1, 1),
    'KXELECTIRAN': (0, 0),
    'KXEMMYCACTO': (1, 1),
    'KXEMMYCACTR': (1, 1),
    'KXEMMYCSERIES': (1, 1),
    'KXEMMYDACTO': (1, 1),
    'KXEMMYDACTR': (1, 1),
    'KXEMMYDSERIES': (1, 1),
    'KXETHY': (0, 0),
    'KXFED': (1, 1),
    'KXFEDDECISION': (1, 1),
    'KXGAMBLINGREPEAL': (0, 0),
    'KXGDP': (1, 1),
    'KXGREENLAND': (0, 0),
    'KXHEISMAN': (1, 1),
    'KXINXY': (1, 1),
    'KXIPO': (1, 1),
    'KXIRANDEMOCRACY': (0, 0),
    'KXLALIGA': (1, 1),
    'KXLAYOFFSYINFO': (0, 0),
    'KXLLM1': (1, 1),
    'KXMARMAD': (1, 1),
    'KXMENWORLDCUP': (1, 1),
    'KXMLB': (1, 1),
    'KXMLBAL': (1, 1),
    'KXMLBASGAME': (1, 1),
    'KXMLBGAME': (1, 1),
    'KXMLBNL': (1, 1),
    'KXNASDAQ100Y': (1, 1),
    'KXNBA': (1, 1),
    'KXNBAEAST': (1, 1),
    'KXNBAMVP': (1, 1),
    'KXNBAROY': (1, 1),
    'KXNBAWEST': (1, 1),
    'KXNCAAF': (1, 1),
    'KXNCAAFACC': (1, 1),
    'KXNCAAFB10': (1, 1),
    'KXNCAAFB12': (1, 1),
    'KXNCAAFGAME': (1, 1),
    'KXNCAAFPLAYOFF': (1, 1),
    'KXNCAAFSEC': (1, 1),
    'KXNFLAFCCHAMP': (1, 1),
    'KXNFLAFCEAST': (1, 1),
    'KXNFLAFCNORTH': (1, 1),
    'KXNFLAFCSOUTH': (1, 1),
    'KXNFLAFCWEST': (1, 1),
    'KXNFLCOTY': (1, 1),
    'KXNFLCPOTY': (1, 1),
    'KXNFLDPOTY': (1, 1),
    'KXNFLDROTY': (1, 1),
    'KXNFLGAME': (1, 1),
    'KXNFLMVP': (1, 1),
    'KXNFLNFCCHAMP': (1, 1),
    'KXNFLNFCEAST': (1, 1),
    'KXNFLNFCNORTH': (1, 1),
    'KXNFLNFCSOUTH': (1, 1),
    'KXNFLNFCWEST': (1, 1),
    'KXNFLOPOTY': (1, 1),
    'KXNFLOROTY': (1, 1),
    'KXNHL': (1, 1),
    'KXNHLEAST': (1, 1),
    'KXNHLWEST': (1, 1),
    'KXPAHLAVIHEAD': (0, 0),
    'KXPAYROLLS': (1, 1),
    'KXPGARYDER': (1, 1),
    'KXPGASOLHEIM': (1, 1),
    'KXPGATOUR': (1, 1),
    'KXRATECUTCOUNT': (1, 1),
    'KXSB': (1, 1),
    'KXSUPERBOWLHEADLINE': (1, 1),
    'KXU3': (1, 1),
    'KXUCL': (1, 1),
    'KXUCLGAME': (1, 1),
    'KXWCGAME': (1, 1),
    'KXWNBA': (1, 1),
    'KXWNBAGAME': (1, 1),
    'KXWTAMATCH': (1, 1),
}
DEFAULT = (0, 1)


def multipliers(series):
    return NONSTANDARD.get(series, DEFAULT)


def taker_fee_c(price_c, series=None):
    """Kalshi taker fee, cents per contract, at price p cents."""
    p = price_c / 100.0
    return 100 * TAKER_RATE * multipliers(series)[1] * p * (1 - p)


def maker_fee_c(price_c, series=None):
    """Kalshi maker fee, cents per contract (0 on every default series)."""
    p = price_c / 100.0
    return 100 * MAKER_RATE * multipliers(series)[0] * p * (1 - p)
