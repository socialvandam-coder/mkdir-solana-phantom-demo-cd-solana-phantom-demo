"""City name (as used in Polymarket titles) -> IANA timezone of the resolution station.

The market's "day" is the local calendar day at the station, so we need this to
know which observations count.
"""

CITY_TZ = {
    "Amsterdam": "Europe/Amsterdam",
    "Ankara": "Europe/Istanbul",
    "Atlanta": "America/New_York",
    "Austin": "America/Chicago",
    "Beijing": "Asia/Shanghai",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Busan": "Asia/Seoul",
    "Cape Town": "Africa/Johannesburg",
    "Chengdu": "Asia/Shanghai",
    "Chicago": "America/Chicago",
    "Chongqing": "Asia/Shanghai",
    "Dallas": "America/Chicago",
    "Denver": "America/Denver",
    "Guangzhou": "Asia/Shanghai",
    "Helsinki": "Europe/Helsinki",
    "Hong Kong": "Asia/Hong_Kong",
    "Houston": "America/Chicago",
    "Istanbul": "Europe/Istanbul",
    "Jeddah": "Asia/Riyadh",
    "Jinan": "Asia/Shanghai",
    "Karachi": "Asia/Karachi",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "London": "Europe/London",
    "Los Angeles": "America/Los_Angeles",
    "Lucknow": "Asia/Kolkata",
    "Madrid": "Europe/Madrid",
    "Manila": "Asia/Manila",
    "Mexico City": "America/Mexico_City",
    "Miami": "America/New_York",
    "Milan": "Europe/Rome",
    "Moscow": "Europe/Moscow",
    "Munich": "Europe/Berlin",
    "NYC": "America/New_York",
    "New York City": "America/New_York",
    "Panama City": "America/Panama",
    "Paris": "Europe/Paris",
    "Qingdao": "Asia/Shanghai",
    "San Francisco": "America/Los_Angeles",
    "Sao Paulo": "America/Sao_Paulo",
    "Seattle": "America/Los_Angeles",
    "Seoul": "Asia/Seoul",
    "Seoul (Incheon)": "Asia/Seoul",
    "Shanghai": "Asia/Shanghai",
    "Shenzhen": "Asia/Shanghai",
    "Singapore": "Asia/Singapore",
    "Taipei": "Asia/Taipei",
    "Tel Aviv": "Asia/Jerusalem",
    "Tokyo": "Asia/Tokyo",
    "Toronto": "America/Toronto",
    "Warsaw": "Europe/Warsaw",
    "Wellington": "Pacific/Auckland",
    "Wuhan": "Asia/Shanghai",
    "Zhengzhou": "Asia/Shanghai",
}


# When each station's routine report usually becomes available: the UTC minute past the hour
# where HighTempTation's buys on that station start (his Aug-Oct 2026 trades). Many stations
# publish a few minutes BEFORE the time printed on the report (Sao Paulo's hh:00 report is out
# at about hh:55, Wellington's at hh:28/hh:58). The bot looks at a station every 2 s from
# 3 minutes before until 6 minutes after each of these minutes. Once it has seen 6 or more of
# the station's own routine reports come in, it uses the measured timing instead.
STATION_MINUTES: dict[str, tuple[int, ...]] = {
    "VILK": (0, 30), "FACT": (53,), "MMMX": (40,), "RCSS": (0, 30), "SBGR": (54,), "LTAC": (21, 51),
    "NZWN": (28, 58), "MPMG": (53,), "LLBG": (20, 50), "LFPB": (0, 30), "SAEZ": (0,), "KMIA": (53,),
    "LTFM": (20, 50), "KLGA": (52,), "KNYC": (52,), "RPLL": (0,), "OPKC": (0, 30), "WSSS": (0, 30),
    "WMKK": (2, 32), "RKPK": (0,), "KAUS": (53,), "CYYZ": (0,), "KATL": (52,), "KLAX": (53,),
    "EGLC": (23, 53), "OEJN": (0,), "KSFO": (56,), "LEMD": (3, 33), "KORD": (51,), "KSEA": (53,),
    "KBKF": (55, 3, 9),     # Denver is late and irregular: together these cover :52 to :15
    "KDAL": (53,), "KHOU": (53,), "EDDM": (22, 52), "EFHK": (20, 50), "UUWW": (0, 30), "RJTT": (0, 30),
    "LIMC": (24, 54),
}
CHINA_MINUTES = (0, 30)         # ZBAA, ZSQD, ZUUU and the other mainland Chinese stations (Z...)
DEFAULT_MINUTES = (50, 20)      # unknown station, nothing measured yet


def seed_minutes(station: str) -> tuple[int, ...] | None:
    """The starting guess for a station, or None when it is not in the list."""
    station = station.upper()
    if station in STATION_MINUTES:
        return STATION_MINUTES[station]
    if station.startswith("Z"):
        return CHINA_MINUTES
    return None
