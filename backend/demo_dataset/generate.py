#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Generate the AnyCompany demo dataset, one customer at a time, so it agrees.

Every customer is built end to end before the next: identity and relationship
manager, banking accounts and the transactions that produce their balances,
contact history with their own RM, and -- for wealth clients -- goals, the
accounts earmarked for them, the trades that built each position, and advisory
notes that quote the figures the rest of the data implies.

Pure: no I/O, no database. `generate()` returns plain rows keyed by table, and
`check()` asserts every invariant the schema cannot enforce. build.py loads and
re-checks them in the databases.

Named people are fictional and generic (the video team asked for named clients).
"""

import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

SEED = 20260927
AS_OF = date(2026, 9, 27)
WINDOW_START = date(2025, 1, 1)
NEXT_MONTH = (date(2026, 10, 1), date(2026, 10, 31))

SEGMENT_COUNTS = {"RETAIL": 288, "PREFERRED": 114, "PRIVATE_BANKING": 78, "INSTITUTIONAL": 20}
FIRST_CLIENT_ID = 101
RETIREMENT_TYPES = ("401K", "IRA", "ROTH_IRA")

# Plausible, fictional prices as of AS_OF. One price per security.
SECURITIES = [
    ("AAPL", "Apple Inc", "EQUITY", "231.40"), ("MSFT", "Microsoft Corp", "EQUITY", "448.20"),
    ("NVDA", "NVIDIA Corp", "EQUITY", "178.65"), ("AMZN", "Amazon.com", "EQUITY", "214.30"),
    ("GOOGL", "Alphabet Inc", "EQUITY", "192.75"), ("META", "Meta Platforms", "EQUITY", "641.10"),
    ("BRK.B", "Berkshire Hathaway B", "EQUITY", "482.90"), ("JPM", "JPMorgan Chase", "EQUITY", "287.35"),
    ("V", "Visa Inc", "EQUITY", "338.60"), ("UNH", "UnitedHealth Group", "EQUITY", "515.25"),
    ("XOM", "Exxon Mobil", "EQUITY", "114.80"), ("JNJ", "Johnson & Johnson", "EQUITY", "158.40"),
    ("PG", "Procter & Gamble", "EQUITY", "166.15"), ("HD", "Home Depot", "EQUITY", "402.70"),
    ("COST", "Costco", "EQUITY", "948.30"), ("PEP", "PepsiCo", "EQUITY", "149.85"),
    ("MRK", "Merck & Co", "EQUITY", "91.20"), ("ABBV", "AbbVie", "EQUITY", "201.55"),
    ("LLY", "Eli Lilly", "EQUITY", "842.10"), ("AVGO", "Broadcom", "EQUITY", "331.45"),
    ("SPY", "SPDR S&P 500", "ETF", "641.20"), ("VOO", "Vanguard S&P 500", "ETF", "589.70"),
    ("VTI", "Vanguard Total Market", "ETF", "314.90"), ("QQQ", "Invesco QQQ", "ETF", "568.35"),
    ("IWM", "iShares Russell 2000", "ETF", "229.60"), ("EFA", "iShares MSCI EAFE", "ETF", "89.75"),
    ("VWO", "Vanguard FTSE EM", "ETF", "49.80"), ("SCHD", "Schwab US Dividend", "ETF", "28.15"),
    ("XLF", "Financial Select SPDR", "ETF", "52.40"), ("ARKK", "ARK Innovation", "ETF", "74.90"),
    ("AGG", "iShares Core US Agg Bond", "FIXED_INCOME", "99.10"),
    ("BND", "Vanguard Total Bond", "FIXED_INCOME", "73.85"),
    ("TLT", "iShares 20+ Year Treasury", "FIXED_INCOME", "88.40"),
    ("VBTLX", "Vanguard Total Bond Index", "FIXED_INCOME", "9.92"),
    ("PIMIX", "PIMCO Income Fund", "FIXED_INCOME", "10.64"),
    ("PTTRX", "PIMCO Total Return", "FIXED_INCOME", "9.08"),
    ("DODIX", "Dodge & Cox Income", "FIXED_INCOME", "12.61"),
    ("TGLMX", "TCW Total Return Bond", "FIXED_INCOME", "8.93"),
    ("GLD", "SPDR Gold Shares", "ALTERNATIVE", "331.20"),
    ("VNQ", "Vanguard Real Estate", "ALTERNATIVE", "92.35"),
]
PRICE = {s: Decimal(p) for s, _, _, p in SECURITIES}
BY_CLASS = defaultdict(list)
for _s, _n, _c, _p in SECURITIES:
    BY_CLASS[_c].append(_s)
DIVIDEND_YIELD = {"EQUITY": Decimal("0.012"), "ETF": Decimal("0.015"),
                  "FIXED_INCOME": Decimal("0.038"), "ALTERNATIVE": Decimal("0.02")}

# Share of each asset class by risk profile.
ALLOCATION = {
    "CONSERVATIVE": {"FIXED_INCOME": 0.60, "EQUITY": 0.15, "ETF": 0.15, "ALTERNATIVE": 0.10},
    "MODERATE":     {"FIXED_INCOME": 0.40, "EQUITY": 0.25, "ETF": 0.25, "ALTERNATIVE": 0.10},
    "GROWTH":       {"FIXED_INCOME": 0.20, "EQUITY": 0.40, "ETF": 0.30, "ALTERNATIVE": 0.10},
    "AGGRESSIVE":   {"FIXED_INCOME": 0.08, "EQUITY": 0.52, "ETF": 0.32, "ALTERNATIVE": 0.08},
}

# Generic fictional names. "Sarah Chen" deliberately excluded.
FIRST = """Avery Blake Cameron Dana Elliot Frankie Gray Harper Indigo Jamie Kendall Logan
Morgan Noel Oakley Parker Quinn Reese Sawyer Taylor Val Wren Adrian Bianca Carlos Delia
Emil Farah Gideon Hana Ivan Jolene Kofi Lena Mateo Nadia Omar Priya Rafael Sienna Tomas
Uma Victor Willa Xavier Yara Zane Alma Bruno Celia Dmitri Elena Felix Greta Hugo Ines
Julian Keira Luca Mira Nico Olive Pablo Rosa Stefan Talia Ulrich Vera Wade Yusuf Zora""".split()
LAST = """Abbott Barnes Calloway Dalton Ellison Fairbanks Garland Hollis Ingram Jarvis
Kessler Lambert Mercer Norwood Oakes Prescott Quimby Radcliffe Sutter Thornton Upton
Vance Whitlock Yardley Ashby Brennan Castellano Delacroix Eastwood Fenwick Galloway
Hargrove Iverson Jessup Kowalski Lindqvist Marchetti Nakamura Okafor Pemberton Quintero
Rasmussen Sorensen Takahashi Underhill Valdez Winslow Zielinski Alvarado Bergstrom
Cardenas Donnelly Estrada Fitzgerald Gutierrez Halvorsen Iglesias Jansen Kaminski
Smith Moreau Nilsson Ortega Petrov Rinaldi Salazar Tanaka Ulloa Varga""".split()
ORG_FIRST = """Harborview Cedar Ridge Northgate Silver Lake Bluewater Summit Crescent
Oakmont Riverbend Westfield Pinecrest Stonebridge Maple Hollow Lakeshore Brightwater
Fairhaven Granite Peak Meadowbrook Redwood Kingsley""".split()
ORG_LAST = ["Foundation", "Endowment Fund", "Pension Trust", "Family Office",
            "Community Trust", "Hospital Foundation", "University Fund"]

TEAMS = [  # team, segment served, number of RMs
    ("Private Banking", "PRIVATE_BANKING", 4), ("Institutional", "INSTITUTIONAL", 2),
    ("Preferred Banking", "PREFERRED", 6), ("Retail Banking", "RETAIL", 8),
]
BRANCHES = [("Boston - Back Bay", "Northeast"), ("New York - Midtown", "Northeast"),
            ("Atlanta - Buckhead", "Southeast"), ("Miami - Brickell", "Southeast"),
            ("Chicago - Loop", "Midwest"), ("Minneapolis - Downtown", "Midwest"),
            ("San Francisco - FiDi", "West"), ("Seattle - Downtown", "West")]
EMAIL_DOMAIN = "anycompanybank.example"


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _qty(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def _add_business_days(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _between(rng, lo: date, hi: date) -> date:
    return lo + timedelta(days=rng.randint(0, max(0, (hi - lo).days)))


def _age(dob: date) -> int:
    return AS_OF.year - dob.year - ((AS_OF.month, AS_OF.day) < (dob.month, dob.day))


class _Ids:
    def __init__(self):
        self._n = defaultdict(int)

    def next(self, table: str) -> int:
        self._n[table] += 1
        return self._n[table]


def _names(rng, count: int, taken: set) -> list[tuple[str, str]]:
    out = []
    while len(out) < count:
        pair = (rng.choice(FIRST), rng.choice(LAST))
        if pair not in taken:
            taken.add(pair)
            out.append(pair)
    return out


# ── Core banking ─────────────────────────────────────────────────────────

def _relationship_managers(rng, taken) -> tuple[list[dict], dict[str, list[int]]]:
    rms, by_segment, rm_id = [], defaultdict(list), 0
    for team, segment, n in TEAMS:
        for first, last in _names(rng, n, taken):
            rm_id += 1
            branch, region = BRANCHES[(rm_id - 1) % len(BRANCHES)]
            rms.append({"RM_ID": rm_id, "NAME": f"{first} {last}",
                        "EMAIL": f"{first}.{last}@{EMAIL_DOMAIN}".lower(),
                        "BRANCH": branch, "REGION": region, "TEAM": team})
            by_segment[segment].append(rm_id)
    return rms, by_segment


def _client(rng, client_id, segment, rm_ids, name, org_names) -> dict:
    if segment == "INSTITUTIONAL":
        first, last = org_names.pop()
        dob, risk = None, rng.choice(["CONSERVATIVE", "MODERATE", "MODERATE"])
        onboarded = _between(rng, date(2004, 1, 1), date(2024, 12, 31))
        email = f"treasury@{(first + last).lower().replace(' ', '')}.example"
    else:
        first, last = name
        lo, hi = {"RETAIL": (22, 76), "PREFERRED": (30, 78), "PRIVATE_BANKING": (40, 82)}[segment]
        age = rng.randint(lo, hi)
        dob = _between(rng, date(AS_OF.year - age - 1, AS_OF.month, 1) + timedelta(days=1),
                       date(AS_OF.year - age, AS_OF.month, min(AS_OF.day, 28)))
        risk = rng.choices(
            ["CONSERVATIVE", "MODERATE", "GROWTH", "AGGRESSIVE"],
            weights=([1, 3, 4, 3] if age < 40 else [2, 4, 3, 1] if age < 60 else [5, 4, 1, 0.3]),
        )[0]
        earliest = max(date(2003, 1, 1), _add_years(dob, 18))
        onboarded = _between(rng, earliest, date(2025, 12, 31))
        email = f"{first}.{last}{client_id}@example.com".lower()
    return {"CLIENT_ID": client_id, "FIRST_NAME": first, "LAST_NAME": last, "EMAIL": email,
            # 555-0100..0199 is the range reserved for fiction.
            "PHONE": f"({rng.choice([212, 312, 415, 617, 305, 206])}) 555-01{client_id % 100:02d}",
            "DATE_OF_BIRTH": dob, "RISK_PROFILE": risk,
            "RELATIONSHIP_MANAGER_ID": rng.choice(rm_ids), "SEGMENT": segment,
            "ONBOARDED_DATE": onboarded, "STATUS": "ACTIVE"}


# (probability of holding one, balance range) per segment and account type.
BANKING_PLAN = {
    "RETAIL": {"CHECKING": (1.0, (400, 14_000)), "SAVINGS": (0.65, (500, 35_000)),
               "TERM_DEPOSIT": (0.12, (5_000, 50_000)), "MONEY_MARKET": (0.05, (5_000, 40_000)),
               "LINE_OF_CREDIT": (0.10, (500, 15_000))},
    "PREFERRED": {"CHECKING": (1.0, (5_000, 60_000)), "SAVINGS": (0.80, (10_000, 150_000)),
                  "TERM_DEPOSIT": (0.40, (25_000, 250_000)), "MONEY_MARKET": (0.30, (20_000, 200_000)),
                  "LINE_OF_CREDIT": (0.20, (5_000, 60_000))},
    # Private-banking amounts are a quarter of the original ranges (halved twice
    # on 2026-09-27): the originals read as tone-deaf on camera. Only the ranges
    # changed, so the random draws -- and with them every client, account and
    # event -- are the same as before.
    "PRIVATE_BANKING": {"CHECKING": (1.0, (6_250, 112_500)), "SAVINGS": (0.70, (12_500, 125_000)),
                        "TERM_DEPOSIT": (0.65, (25_000, 375_000)),
                        "MONEY_MARKET": (0.55, (25_000, 500_000)),
                        "LINE_OF_CREDIT": (0.30, (6_250, 100_000))},
    "INSTITUTIONAL": {"CHECKING": (1.0, (150_000, 2_500_000)), "SAVINGS": (0.30, (500_000, 3_000_000)),
                      "TERM_DEPOSIT": (0.70, (1_000_000, 5_000_000)),
                      "MONEY_MARKET": (1.0, (1_000_000, 10_000_000)),
                      "LINE_OF_CREDIT": (0.50, (250_000, 2_000_000))},
}
RATE = {"CHECKING": (0.0, 0.05), "SAVINGS": (1.5, 3.5), "TERM_DEPOSIT": (3.8, 5.0),
        "MONEY_MARKET": (3.0, 4.5), "LINE_OF_CREDIT": (7.5, 11.0)}
PREFIX = {"CHECKING": "CHK", "SAVINGS": "SAV", "TERM_DEPOSIT": "TDA",
          "MONEY_MARKET": "MMA", "LINE_OF_CREDIT": "LOC"}
MERCHANTS = ["Grocery", "Utilities", "Mortgage payment", "Insurance premium", "Card payment",
             "Travel", "Dining", "Home services", "Tuition", "Medical", "Subscriptions"]


def _months(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _post(txns, ids, account_id, when, txn_type, amount, balance, description) -> Decimal:
    amount = _money(amount)
    if amount == 0:
        return balance
    balance = _money(balance + amount)
    txn_id = ids.next("TRANSACTIONS")
    txns.append({"TXN_ID": txn_id, "ACCOUNT_ID": account_id, "TXN_DATE": when,
                 "TXN_TYPE": txn_type, "AMOUNT": amount, "RUNNING_BALANCE": balance,
                 "DESCRIPTION": description, "REFERENCE": f"TXN{txn_id:08d}"})
    return balance


def _banking(rng, ids, client, accounts, txns, force_td_next_month: bool):
    segment = client["SEGMENT"]
    n = 0
    for account_type, (p, (lo, hi)) in BANKING_PLAN[segment].items():
        forced = account_type == "TERM_DEPOSIT" and force_td_next_month
        if rng.random() >= p and not forced:
            continue
        n += 1
        account_id = ids.next("ACCOUNTS")
        target = Decimal(str(rng.uniform(lo, hi)))
        rate = Decimal(str(round(rng.uniform(*RATE[account_type]), 3)))
        opened = _between(rng, client["ONBOARDED_DATE"], AS_OF - timedelta(days=60))
        maturity = None
        if account_type == "TERM_DEPOSIT":
            maturity = (_between(rng, *NEXT_MONTH) if forced
                        else AS_OF + timedelta(days=rng.randint(45, 720)))
            term = rng.choice([6, 12, 18, 24, 36])
            opened = maturity - timedelta(days=round(term * 30.4))
            if opened < client["ONBOARDED_DATE"]:
                opened = client["ONBOARDED_DATE"]
        start = max(opened, WINDOW_START)
        # Balance carried into the window; accounts opened inside it start at 0
        # and are funded by a first deposit.
        balance = _money(0 if opened >= WINDOW_START else
                         (-target if account_type == "LINE_OF_CREDIT" else target))
        if opened >= WINDOW_START:
            first = -target * Decimal("0.6") if account_type == "LINE_OF_CREDIT" else target
            balance = _post(txns, ids, account_id, opened,
                            "WITHDRAWAL" if account_type == "LINE_OF_CREDIT" else "DEPOSIT",
                            first, balance,
                            "Initial draw" if account_type == "LINE_OF_CREDIT" else "Opening deposit")
        income = target * Decimal(str(rng.uniform(0.25, 0.45)))
        for month in _months(start, AS_OF):
            days = [d for d in (month + timedelta(days=i) for i in range(28)) if start <= d <= AS_OF]
            if not days:
                continue
            # Schedule the month first, then post in date order. Each amount is
            # computed when it posts, from the balance at that moment, so the
            # running balance chains and no deposit account is overdrawn.
            events = []
            if account_type == "CHECKING":
                events.append((days[0], 0, "income"))
                # Four spends of ~25% of income on average: spending tracks income,
                # so balances drift rather than compound month over month.
                events += [(rng.choice(days), 1, "spend") for _ in range(rng.randint(3, 5))]
            elif account_type == "LINE_OF_CREDIT":
                if rng.random() < 0.5:
                    events.append((rng.choice(days), 1, "payment"))
                if rng.random() < 0.3:
                    events.append((rng.choice(days), 1, "draw"))
                events.append((days[-1], 2, "interest"))
            else:
                if account_type != "TERM_DEPOSIT" and rng.random() < 0.25:
                    events.append((rng.choice(days), 1, "transfer"))
                events.append((days[-1], 2, "interest"))
            for when, _, kind in sorted(events, key=lambda e: (e[0], e[1])):
                if kind == "income":
                    balance = _post(txns, ids, account_id, when, "DEPOSIT", income, balance,
                                    "Payroll deposit" if segment != "INSTITUTIONAL" else "Receipts")
                elif kind == "spend":
                    spend = min(income * Decimal(str(rng.uniform(0.17, 0.33))), balance * Decimal("0.9"))
                    balance = _post(txns, ids, account_id, when, rng.choice(["PAYMENT", "WITHDRAWAL"]),
                                    -spend, balance, rng.choice(MERCHANTS))
                elif kind == "payment":
                    balance = _post(txns, ids, account_id, when, "PAYMENT",
                                    min(-balance, target * Decimal("0.08")), balance, "Payment received")
                elif kind == "draw":
                    balance = _post(txns, ids, account_id, when, "WITHDRAWAL",
                                    -target * Decimal(str(rng.uniform(0.02, 0.1))), balance, "Draw")
                elif kind == "transfer":
                    delta = max(target * Decimal(str(rng.uniform(-0.08, 0.12))), -balance * Decimal("0.9"))
                    balance = _post(txns, ids, account_id, when, "TRANSFER", delta, balance,
                                    "Transfer in" if delta > 0 else "Transfer out")
                else:
                    balance = _post(txns, ids, account_id, when, "INTEREST", balance * rate / 1200,
                                    balance, "Interest charged" if balance < 0 else "Interest credited")
        accounts.append({"ACCOUNT_ID": account_id, "CLIENT_ID": client["CLIENT_ID"],
                         "ACCOUNT_TYPE": account_type,
                         "ACCOUNT_NUMBER": f"{PREFIX[account_type]}-{client['CLIENT_ID']:04d}-{n:02d}",
                         "CURRENCY": "USD", "BALANCE": balance, "INTEREST_RATE": rate,
                         "OPENED_DATE": opened, "MATURITY_DATE": maturity, "STATUS": "ACTIVE"})


CONTACT_NOTES = {
    "MEETING": ["Relationship meeting at the branch; reviewed accounts and upcoming needs.",
                "Met to discuss cash management and short-term liquidity.",
                "In-person catch-up; no changes requested."],
    "CALL": ["Called to confirm recent large transfer.",
             "Check-in call; client asked about savings rates.",
             "Returned client call regarding card limits."],
    "EMAIL": ["Sent quarterly statement summary.", "Followed up with requested documents.",
              "Shared updated fee schedule."],
    "REVIEW": ["Annual relationship review completed.",
               "Reviewed KYC details; no changes.",
               "Reviewed account structure and beneficiaries."],
}
CONTACTS_PER_CLIENT = {"RETAIL": (0, 2), "PREFERRED": (1, 4),
                       "PRIVATE_BANKING": (4, 9), "INSTITUTIONAL": (4, 8)}


def _contacts(rng, ids, client, client_accounts, contacts):
    lo, hi = CONTACTS_PER_CLIENT[client["SEGMENT"]]
    start = max(client["ONBOARDED_DATE"], WINDOW_START)
    for _ in range(rng.randint(lo, hi)):
        kind = rng.choice(list(CONTACT_NOTES))
        contacts.append({"CONTACT_ID": ids.next("CLIENT_CONTACTS"), "CLIENT_ID": client["CLIENT_ID"],
                         "CONTACT_DATE": _between(rng, start, AS_OF), "CONTACT_TYPE": kind,
                         "NOTES": rng.choice(CONTACT_NOTES[kind]),
                         "RM_ID": client["RELATIONSHIP_MANAGER_ID"]})
    for a in client_accounts:
        if a["ACCOUNT_TYPE"] == "TERM_DEPOSIT" and NEXT_MONTH[0] <= a["MATURITY_DATE"] <= NEXT_MONTH[1]:
            contacts.append({
                "CONTACT_ID": ids.next("CLIENT_CONTACTS"), "CLIENT_ID": client["CLIENT_ID"],
                "CONTACT_DATE": AS_OF - timedelta(days=rng.randint(3, 20)), "CONTACT_TYPE": "CALL",
                "NOTES": (f"Discussed term deposit {a['ACCOUNT_NUMBER']} (${a['BALANCE']:,.2f}) "
                          f"maturing {a['MATURITY_DATE']:%b %d, %Y}; client wants options before rollover."),
                "RM_ID": client["RELATIONSHIP_MANAGER_ID"]})


# ── Wealth platform ──────────────────────────────────────────────────────

def _goals(rng, client) -> list[dict]:
    """(goal_type, target, target_date, priority, funded_ratio) for one client."""
    segment, age = client["SEGMENT"], None
    if client["DATE_OF_BIRTH"]:
        age = _age(client["DATE_OF_BIRTH"])
    goals = []
    if segment == "INSTITUTIONAL":
        goals.append(("WEALTH_PRESERVATION", rng.uniform(5e6, 40e6),
                      AS_OF + timedelta(days=365 * rng.randint(5, 20)), "HIGH", rng.uniform(0.4, 1.2)))
        return goals
    scale = {"RETAIL": (250_000, 1_200_000), "PREFERRED": (800_000, 2_500_000),
             "PRIVATE_BANKING": (500_000, 2_000_000)}[segment]
    if age < 72:
        target_date = _add_years(client["DATE_OF_BIRTH"], 65)
        if target_date <= AS_OF:
            target_date = AS_OF + timedelta(days=365 * rng.randint(2, 5))
        # Near-retirement savers are the ones who can plausibly be there already.
        if age >= 55 and rng.random() < 0.35:
            ratio = rng.uniform(1.0, 1.25)
        else:
            ratio = min(0.97, max(0.03, (age - 22) / 43) * rng.uniform(0.3, 1.1))
        goals.append(("RETIREMENT", rng.uniform(*scale), target_date, "HIGH", ratio))
    if 30 <= age <= 55 and rng.random() < 0.4:
        goals.append(("EDUCATION", rng.uniform(60_000, 320_000),
                      AS_OF + timedelta(days=365 * rng.randint(3, 15)), "MEDIUM", rng.uniform(0.1, 1.1)))
    if age < 45 and rng.random() < 0.35:
        goals.append(("HOME_PURCHASE", rng.uniform(80_000, 450_000),
                      AS_OF + timedelta(days=365 * rng.randint(1, 5)), "MEDIUM", rng.uniform(0.1, 1.05)))
    if segment == "PRIVATE_BANKING" and rng.random() < 0.6:
        goals.append(("WEALTH_PRESERVATION", rng.uniform(0.25e6, 2.5e6),
                      AS_OF + timedelta(days=365 * rng.randint(5, 20)), "MEDIUM", rng.uniform(0.3, 1.2)))
    if age >= 60 and rng.random() < 0.4:
        goals.append(("INCOME", rng.uniform(200_000, 1_500_000),
                      AS_OF + timedelta(days=365 * rng.randint(1, 10)), "LOW", rng.uniform(0.2, 1.1)))
    return goals


def _holdings(rng, ids, account, value: Decimal, risk: str, income_tilt: bool,
              positions, trades):
    """Positions for one account, and the trades that produced them."""
    alloc = dict(ALLOCATION[risk])
    if income_tilt:
        alloc = {"FIXED_INCOME": 0.7, "ETF": 0.15, "EQUITY": 0.1, "ALTERNATIVE": 0.05}
    symbols = []
    for _ in range(rng.randint(3, 7)):
        cls = rng.choices(list(alloc), weights=list(alloc.values()))[0]
        choices = [s for s in BY_CLASS[cls] if s not in symbols]
        if choices:
            symbols.append(rng.choice(choices))
    weights = [rng.uniform(0.5, 1.5) for _ in symbols]
    first_trade = max(account["opened_date"], date(2015, 1, 1))
    last_trade = AS_OF - timedelta(days=7)
    cls_of = {s: c for s, _, c, _ in SECURITIES}
    for symbol, w in zip(symbols, weights):
        price = PRICE[symbol]
        quantity = _qty(value * Decimal(str(w / sum(weights))) / price)
        if quantity <= 0:
            continue
        sold = _qty(quantity * Decimal(str(rng.uniform(0.1, 0.4)))) if rng.random() < 0.25 else Decimal(0)
        bought_total = quantity + sold
        n_buys = rng.randint(1, 3)
        lots, remaining = [], bought_total
        for i in range(n_buys - 1):
            lot = _qty(bought_total * Decimal(str(rng.uniform(0.2, 0.5))))
            if lot <= 0 or lot >= remaining:
                break
            lots.append(lot)
            remaining -= lot
        lots.append(remaining)
        buy_dates = sorted(_between(rng, first_trade, last_trade) for _ in lots)
        cost = Decimal(0)
        for lot, when in zip(lots, buy_dates):
            # Older purchases at lower prices, on average.
            years_ago = (AS_OF - when).days / 365
            trade_price = (price * Decimal(str(rng.uniform(0.75, 1.08) / (1 + 0.07 * years_ago)))
                           ).quantize(Decimal("0.0001"))
            total = _money(lot * trade_price)
            cost += total
            trades.append(_trade(ids, account["account_id"], symbol, when, "BUY", lot, trade_price, total))
        if sold:
            when = _between(rng, buy_dates[-1], last_trade)
            sell_price = (price * Decimal(str(rng.uniform(0.85, 1.05)))).quantize(Decimal("0.0001"))
            trades.append(_trade(ids, account["account_id"], symbol, when, "SELL", sold,
                                 sell_price, _money(sold * sell_price)))
        avg_cost = cost / bought_total
        positions.append({"position_id": ids.next("positions"), "account_id": account["account_id"],
                          "symbol": symbol, "quantity": quantity,
                          "cost_basis": _money(avg_cost * quantity)})
        if cls_of[symbol] != "ALTERNATIVE" or symbol == "VNQ":
            quarterly = quantity * price * DIVIDEND_YIELD[cls_of[symbol]] / 4
            for q_end in (date(2026, 3, 31), date(2026, 6, 30)):
                if buy_dates[0] < q_end:
                    trades.append(_trade(ids, account["account_id"], symbol, q_end, "DIVIDEND",
                                         Decimal(0), None, _money(quarterly)))


def _trade(ids, account_id, symbol, when, kind, quantity, price, total) -> dict:
    return {"trade_id": ids.next("trades"), "account_id": account_id, "symbol": symbol,
            "trade_date": when, "settlement_date": _add_business_days(when, 2),
            "trade_type": kind, "quantity": quantity, "price": price,
            "total_amount": total, "status": "SETTLED"}


def _wealth(rng, ids, client, banking, out):
    goals = _goals(rng, client)
    segment, risk, cid = client["SEGMENT"], client["RISK_PROFILE"], client["CLIENT_ID"]
    n = 0

    def open_account(account_type, goal_id):
        nonlocal n
        n += 1
        a = {"account_id": ids.next("investment_accounts"), "client_id": cid,
             "account_type": account_type, "account_number": f"WM-{cid:04d}-{n:04d}",
             "opened_date": _between(rng, client["ONBOARDED_DATE"], AS_OF - timedelta(days=120)),
             "status": "ACTIVE", "goal_id": goal_id}
        out["investment_accounts"].append(a)
        return a

    general = {"RETAIL": (5_000, 80_000), "PREFERRED": (40_000, 500_000),
               "PRIVATE_BANKING": (75_000, 750_000), "INSTITUTIONAL": (500_000, 5_000_000)}[segment]
    _holdings(rng, ids, open_account("BROKERAGE", None),
              Decimal(str(rng.uniform(*general))), risk, False,
              out["positions"], out["trades"])
    for goal_type, target, target_date, priority, ratio in goals:
        goal_id = ids.next("portfolio_goals")
        target = _money(target)
        out["portfolio_goals"].append({"goal_id": goal_id, "client_id": cid, "goal_type": goal_type,
                                       "target_amount": target, "target_date": target_date,
                                       "priority": priority})
        funded = target * Decimal(str(ratio))
        if goal_type == "RETIREMENT":
            kinds = rng.sample(RETIREMENT_TYPES, rng.choice([1, 1, 2]))
        elif goal_type == "WEALTH_PRESERVATION":
            kinds = ["TRUST"]
        else:
            kinds = ["BROKERAGE"]
        for k in kinds:
            _holdings(rng, ids, open_account(k, goal_id), funded / len(kinds), risk,
                      goal_type == "INCOME", out["positions"], out["trades"])
    _advisory(rng, ids, client, banking, out)


def _market_values(out, cid):
    accounts = {a["account_id"]: a for a in out["investment_accounts"] if a["client_id"] == cid}
    by_account = defaultdict(Decimal)
    by_class = defaultdict(Decimal)
    cls_of = {s: c for s, _, c, _ in SECURITIES}
    for p in out["positions"]:
        if p["account_id"] in accounts:
            mv = _money(p["quantity"] * PRICE[p["symbol"]])
            by_account[p["account_id"]] += mv
            by_class[cls_of[p["symbol"]]] += mv
    return accounts, by_account, by_class


ADVISORY_PER_CLIENT = {"RETAIL": (0, 2), "PREFERRED": (2, 4),
                       "PRIVATE_BANKING": (4, 8), "INSTITUTIONAL": (3, 6)}


def _advisory(rng, ids, client, banking, out):
    """Advisory notes that quote the figures the rest of the data implies."""
    cid, rm = client["CLIENT_ID"], client["RELATIONSHIP_MANAGER_ID"]
    accounts, by_account, by_class = _market_values(out, cid)
    total = sum(by_account.values())
    goals = [g for g in out["portfolio_goals"] if g["client_id"] == cid]
    checking = sum((a["BALANCE"] for a in banking if a["ACCOUNT_TYPE"] == "CHECKING"), Decimal(0))
    maturing = [a for a in banking if a["ACCOUNT_TYPE"] == "TERM_DEPOSIT"
                and NEXT_MONTH[0] <= a["MATURITY_DATE"] <= NEXT_MONTH[1]]
    first_open = min(a["opened_date"] for a in accounts.values())
    start = max(first_open, WINDOW_START)
    lo, hi = ADVISORY_PER_CLIENT[client["SEGMENT"]]

    def pct(cls):
        return round(100 * by_class[cls] / total) if total else 0

    def note(kind):
        if kind == "PORTFOLIO_REVIEW":
            return (f"Portfolio review. Invested assets ${total:,.0f} across {len(accounts)} accounts; "
                    f"allocation {pct('EQUITY') + pct('ETF')}% equity, {pct('FIXED_INCOME')}% fixed income, "
                    f"{pct('ALTERNATIVE')}% alternatives.")
        if kind == "GOAL_UPDATE" and goals:
            g = rng.choice(goals)
            value = sum(by_account[a] for a, acct in accounts.items() if acct["goal_id"] == g["goal_id"])
            return (f"Reviewed {g['goal_type'].replace('_', ' ').lower()} goal: target "
                    f"${g['target_amount']:,.0f} by {g['target_date']:%b %Y}; currently "
                    f"{100 * value / g['target_amount']:.0f}% funded (${value:,.0f}).")
        if kind == "RECOMMENDATION":
            if maturing:
                a = maturing[0]
                return (f"Recommended reinvestment options for term deposit {a['ACCOUNT_NUMBER']} "
                        f"maturing {a['MATURITY_DATE']:%b %d, %Y}.")
            if checking > 150_000:
                return (f"Recommended moving part of the ${checking:,.0f} checking balance into "
                        f"short-term fixed income; cash is well above operating needs.")
            return "Recommended adding to diversified bond exposure."
        if kind == "REBALANCE":
            return f"Rebalanced toward the client's {client['RISK_PROFILE'].lower()} target allocation."
        return f"Risk profile reviewed and confirmed as {client['RISK_PROFILE'].lower()}."

    kinds = ["PORTFOLIO_REVIEW", "REBALANCE", "RECOMMENDATION", "RISK_ASSESSMENT"] + (["GOAL_UPDATE"] if goals else [])
    for _ in range(rng.randint(lo, hi)):
        kind = rng.choice(kinds)
        out["advisory_activity"].append({"activity_id": ids.next("advisory_activity"), "client_id": cid,
                                         "activity_date": _between(rng, start, AS_OF),
                                         "activity_type": kind, "summary": note(kind), "rm_id": rm})


# ── Life events ──────────────────────────────────────────────────────────

def _life_events(out) -> list[dict]:
    """Milestones in each existing client's profile, aligned with their data.

    Its own random stream per client, run after everything else, so adding it
    changed no existing row. Planned events take the date of the goal they belong
    to; a child's college start is that child's 18th birthday.
    """
    goals = defaultdict(dict)
    for g in out["portfolio_goals"]:
        goals[g["client_id"]][g["goal_type"]] = g
    events, event_id = [], 0

    def add(client, kind, when, notes):
        nonlocal event_id
        event_id += 1
        events.append({"EVENT_ID": event_id, "CLIENT_ID": client["CLIENT_ID"], "EVENT_TYPE": kind,
                       "EVENT_DATE": when, "NOTES": notes, "RM_ID": client["RELATIONSHIP_MANAGER_ID"]})

    for c in out["CLIENTS"]:
        if c["SEGMENT"] == "INSTITUTIONAL":
            continue
        rng = random.Random(f"{SEED}:life:{c['CLIENT_ID']}")  # noqa: S311 - seeded synthetic demo data, not crypto
        dob, age, own = c["DATE_OF_BIRTH"], _age(c["DATE_OF_BIRTH"]), goals[c["CLIENT_ID"]]
        if age >= 25 and rng.random() < 0.55:
            add(c, "MARRIAGE", _between(rng, _add_years(dob, 23), min(_add_years(dob, 40), AS_OF)),
                "Married.")
        if age >= 28 and rng.random() < 0.5:
            for _ in range(rng.choice([1, 1, 2])):
                born = _between(rng, _add_years(dob, 24), min(_add_years(dob, 45), AS_OF - timedelta(days=365)))
                add(c, "NEW_CHILD", born, f"Child born {born:%B %Y}.")
                college = date(born.year + 18, 9, 1)
                if AS_OF - timedelta(days=4 * 365) <= college <= AS_OF + timedelta(days=6 * 365):
                    add(c, "CHILD_TO_COLLEGE", college,
                        f"Child born {born:%B %Y} starts college {'in' if college > AS_OF else 'since'} "
                        f"fall {college.year}.")
        if "HOME_PURCHASE" in own:
            g = own["HOME_PURCHASE"]
            add(c, "HOME_PURCHASE", g["target_date"],
                f"Planning a home purchase around {g['target_date']:%B %Y}, budget ${g['target_amount']:,.0f}.")
        elif age >= 30 and rng.random() < 0.3:
            when = _between(rng, _add_years(dob, 25), AS_OF - timedelta(days=180))
            add(c, "HOME_PURCHASE", when, f"Bought a home in {when:%B %Y}.")
        if "RETIREMENT" in own:
            g = own["RETIREMENT"]
            add(c, "PLANNED_RETIREMENT", g["target_date"],
                f"Plans to retire {g['target_date']:%B %Y}.")
    return events


# ── Assembly ─────────────────────────────────────────────────────────────

def generate(seed: int = SEED) -> dict[str, list[dict]]:
    rng, ids, taken = random.Random(seed), _Ids(), {("Sarah", "Chen")}  # noqa: S311 - seeded synthetic demo data, not crypto
    rms, rm_by_segment = _relationship_managers(rng, taken)
    segments = [s for s, n in SEGMENT_COUNTS.items() for _ in range(n)]
    rng.shuffle(segments)
    people = iter(_names(rng, sum(SEGMENT_COUNTS.values()), taken))
    orgs = rng.sample([(first, last) for first in ORG_FIRST for last in ORG_LAST], SEGMENT_COUNTS["INSTITUTIONAL"])

    out = defaultdict(list)
    out["RELATIONSHIP_MANAGERS"] = rms
    out["securities"] = [{"symbol": s, "security_name": n, "asset_class": c,
                          "current_price": Decimal(p), "price_date": AS_OF}
                         for s, n, c, p in SECURITIES]
    for i, segment in enumerate(segments):
        client = _client(rng, FIRST_CLIENT_ID + i, segment, rm_by_segment[segment], next(people), orgs)
        out["CLIENTS"].append(client)
        first_account = len(out["ACCOUNTS"])
        # A share of affluent clients have a term deposit maturing next month:
        # the "prepare me for my review" moment in the story.
        force_td = segment in ("PRIVATE_BANKING", "PREFERRED") and rng.random() < 0.2
        _banking(rng, ids, client, out["ACCOUNTS"], out["TRANSACTIONS"], force_td)
        banking = out["ACCOUNTS"][first_account:]
        _contacts(rng, ids, client, banking, out["CLIENT_CONTACTS"])
        if segment != "RETAIL" or rng.random() < 0.35:
            _wealth(rng, ids, client, banking, out)
    out["CLIENT_LIFE_EVENTS"] = _life_events(out)
    return dict(out)


# ── Invariants ───────────────────────────────────────────────────────────

def check(data: dict[str, list[dict]]) -> list[str]:
    """Every rule the schema cannot enforce. Returns failures; empty means pass."""
    fail = []
    clients = {c["CLIENT_ID"]: c for c in data["CLIENTS"]}
    rms = {r["RM_ID"]: r for r in data["RELATIONSHIP_MANAGERS"]}
    team_for = {seg: team for team, seg, _ in TEAMS}

    for c in clients.values():
        if rms[c["RELATIONSHIP_MANAGER_ID"]]["TEAM"] != team_for[c["SEGMENT"]]:
            fail.append(f"client {c['CLIENT_ID']} has an RM from the wrong team")
    for x in data["CLIENT_CONTACTS"]:
        if x["RM_ID"] != clients[x["CLIENT_ID"]]["RELATIONSHIP_MANAGER_ID"]:
            fail.append(f"contact {x['CONTACT_ID']} logged by another client's RM")

    by_account = defaultdict(list)
    for t in data["TRANSACTIONS"]:
        by_account[t["ACCOUNT_ID"]].append(t)
    for a in data["ACCOUNTS"]:
        ts = sorted(by_account[a["ACCOUNT_ID"]], key=lambda t: (t["TXN_DATE"], t["TXN_ID"]))
        missing = [t for t in ts if t["RUNNING_BALANCE"] is None]
        if missing:
            fail.append(f"txn {missing[0]['TXN_ID']}: running balance is missing")
            continue
        for prev, cur in zip(ts, ts[1:]):
            if prev["RUNNING_BALANCE"] + cur["AMOUNT"] != cur["RUNNING_BALANCE"]:
                fail.append(f"txn {cur['TXN_ID']}: running balance does not follow")
                break
        if ts and ts[-1]["RUNNING_BALANCE"] != a["BALANCE"]:
            fail.append(f"account {a['ACCOUNT_ID']}: balance != latest running balance")
        if a["ACCOUNT_TYPE"] == "LINE_OF_CREDIT" and a["BALANCE"] > 0:
            fail.append(f"account {a['ACCOUNT_ID']}: line of credit in credit")
        if a["ACCOUNT_TYPE"] != "LINE_OF_CREDIT" and a["BALANCE"] < 0:
            fail.append(f"account {a['ACCOUNT_ID']}: deposit account overdrawn")
        if a["ACCOUNT_TYPE"] == "TERM_DEPOSIT" and not (a["MATURITY_DATE"] and a["MATURITY_DATE"] > AS_OF):
            fail.append(f"account {a['ACCOUNT_ID']}: active term deposit already matured")

    accounts = {a["account_id"]: a for a in data["investment_accounts"]}
    net = defaultdict(Decimal)
    buys = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for t in data["trades"]:
        key = (t["account_id"], t["symbol"])
        if t["trade_type"] == "BUY":
            net[key] += t["quantity"]
            buys[key][0] += t["quantity"]
            buys[key][1] += t["total_amount"]
        elif t["trade_type"] == "SELL":
            net[key] -= t["quantity"]
        if t["settlement_date"] > AS_OF:
            fail.append(f"trade {t['trade_id']} not settled by AS_OF")
    held = {(p["account_id"], p["symbol"]): p for p in data["positions"]}
    for key, p in held.items():
        if net[key] != p["quantity"]:
            fail.append(f"position {p['position_id']}: quantity != net trades")
        avg = buys[key][1] / buys[key][0]
        if abs(_money(avg * p["quantity"]) - p["cost_basis"]) > Decimal("0.01"):
            fail.append(f"position {p['position_id']}: cost basis != average cost")
    for key, q in net.items():
        if key not in held and q != 0:
            fail.append(f"{key}: traded but not held")

    funded = defaultdict(list)
    for a in accounts.values():
        if a["client_id"] not in clients:
            fail.append(f"investment account {a['account_id']} for unknown client")
        if a["goal_id"]:
            funded[a["goal_id"]].append(a)
    for g in data["portfolio_goals"]:
        if not funded[g["goal_id"]]:
            fail.append(f"goal {g['goal_id']} has no funding account")
        if g["goal_type"] == "RETIREMENT" and any(a["account_type"] not in RETIREMENT_TYPES
                                                  for a in funded[g["goal_id"]]):
            fail.append(f"goal {g['goal_id']}: retirement funded by a non-retirement account")
        if any(a["client_id"] != g["client_id"] for a in funded[g["goal_id"]]):
            fail.append(f"goal {g['goal_id']} funded by another client's account")
    for x in data["advisory_activity"]:
        if x["rm_id"] != clients[x["client_id"]]["RELATIONSHIP_MANAGER_ID"]:
            fail.append(f"advisory {x['activity_id']} by another client's RM")

    goal_date = {(g["client_id"], g["goal_type"]): g["target_date"] for g in data["portfolio_goals"]}
    births = defaultdict(set)
    for e in data["CLIENT_LIFE_EVENTS"]:
        if e["EVENT_TYPE"] == "NEW_CHILD":
            births[e["CLIENT_ID"]].add(e["EVENT_DATE"])
    retirement_events = set()
    for e in data["CLIENT_LIFE_EVENTS"]:
        c = clients.get(e["CLIENT_ID"])
        if c is None:
            fail.append(f"life event {e['EVENT_ID']} for unknown client")
            continue
        if e["RM_ID"] != c["RELATIONSHIP_MANAGER_ID"]:
            fail.append(f"life event {e['EVENT_ID']} recorded by another client's RM")
        if c["DATE_OF_BIRTH"] is None or e["EVENT_DATE"] <= c["DATE_OF_BIRTH"]:
            fail.append(f"life event {e['EVENT_ID']} before the client was born")
        kind, key = e["EVENT_TYPE"], (e["CLIENT_ID"], e["EVENT_TYPE"])
        if kind == "PLANNED_RETIREMENT":
            retirement_events.add(e["CLIENT_ID"])
            if goal_date.get((e["CLIENT_ID"], "RETIREMENT")) != e["EVENT_DATE"]:
                fail.append(f"life event {e['EVENT_ID']}: planned retirement != retirement goal date")
        if kind == "HOME_PURCHASE" and e["EVENT_DATE"] > AS_OF \
                and goal_date.get(key) != e["EVENT_DATE"]:
            fail.append(f"life event {e['EVENT_ID']}: planned home purchase != home purchase goal date")
        if kind == "CHILD_TO_COLLEGE" and not any(
                date(b.year + 18, 9, 1) == e["EVENT_DATE"] for b in births[e["CLIENT_ID"]]):
            fail.append(f"life event {e['EVENT_ID']}: college start with no matching child")
    for (cid, kind) in goal_date:
        if kind == "RETIREMENT" and cid not in retirement_events:
            fail.append(f"client {cid}: retirement goal with no planned retirement event")
    return fail


def story(data: dict[str, list[dict]]) -> dict:
    """The figures the demo leans on, so a regression is visible before recording."""
    clients = {c["CLIENT_ID"]: c for c in data["CLIENTS"]}
    value = defaultdict(Decimal)
    for p in data["positions"]:
        value[p["account_id"]] += _money(p["quantity"] * PRICE[p["symbol"]])
    goal_value = defaultdict(Decimal)
    for a in data["investment_accounts"]:
        if a["goal_id"]:
            goal_value[a["goal_id"]] += value[a["account_id"]]
    retirement = [g for g in data["portfolio_goals"] if g["goal_type"] == "RETIREMENT"]
    met = [g for g in retirement if goal_value[g["goal_id"]] >= g["target_amount"]]
    pb_behind = {g["client_id"] for g in retirement
                 if clients[g["client_id"]]["SEGMENT"] == "PRIVATE_BANKING"
                 and goal_value[g["goal_id"]] < g["target_amount"]}
    wealth_clients = {a["client_id"] for a in data["investment_accounts"]}
    pb_with_wealth = sum(1 for c in clients.values()
                         if c["SEGMENT"] == "PRIVATE_BANKING" and c["CLIENT_ID"] in wealth_clients)
    goal_types = defaultdict(set)
    for g in data["portfolio_goals"]:
        goal_types[g["client_id"]].add(g["goal_type"])
    college_soon_no_plan = {e["CLIENT_ID"] for e in data["CLIENT_LIFE_EVENTS"]
                            if e["EVENT_TYPE"] == "CHILD_TO_COLLEGE"
                            and AS_OF < e["EVENT_DATE"] <= AS_OF + timedelta(days=3 * 365)
                            and "EDUCATION" not in goal_types[e["CLIENT_ID"]]}
    return {
        "life_events": len(data["CLIENT_LIFE_EVENTS"]),
        "clients_with_child_to_college_within_3y_and_no_education_goal": len(college_soon_no_plan),
        "rows": {t: len(r) for t, r in sorted(data.items())},
        "retirement_goals": len(retirement),
        "retirement_goals_on_target": len(met),
        "private_banking_clients_behind_on_retirement": len(pb_behind),
        "private_banking_clients_with_wealth_accounts":
            f"{pb_with_wealth}/{SEGMENT_COUNTS['PRIVATE_BANKING']}",
        "term_deposits_maturing_next_month": sum(
            1 for a in data["ACCOUNTS"] if a["ACCOUNT_TYPE"] == "TERM_DEPOSIT"
            and NEXT_MONTH[0] <= a["MATURITY_DATE"] <= NEXT_MONTH[1]),
        "private_banking_clients_with_checking_over_250k": len({
            a["CLIENT_ID"] for a in data["ACCOUNTS"]
            if a["ACCOUNT_TYPE"] == "CHECKING" and a["BALANCE"] > 250_000
            and clients[a["CLIENT_ID"]]["SEGMENT"] == "PRIVATE_BANKING"}),
    }
