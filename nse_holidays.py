from datetime import date

# NSE F&O segment holidays for 2026.
# Source: NSE circular (updated annually).
# Verify against official NSE notifications each year.
NSE_FO_HOLIDAYS = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 17): "Mahashivratri",
    date(2026, 3, 14): "Holi",
    date(2026, 3, 31): "Id-Ul-Fitr (Ramadan)",
    date(2026, 4, 2): "Ram Navami",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 25): "Buddha Purnima",
    date(2026, 6, 7): "Bakri Id (Eid-ul-Adha)",
    date(2026, 7, 7): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 9, 5): "Milad-un-Nabi (Prophet Mohammad Birthday)",
    date(2026, 9, 14): "Ganesh Chaturthi",  # BSE: Anant Chaturdashi
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 21): "Dussehra",
    date(2026, 11, 9): "Diwali (Laxmi Puja)",
    date(2026, 11, 10): "Diwali (Balipratipada)",
    date(2026, 11, 30): "Guru Nanak Jayanti",
    date(2026, 12, 25): "Christmas",
}


def is_nse_holiday(trading_date):
    """Return True if the given date is an NSE F&O holiday."""
    return trading_date in NSE_FO_HOLIDAYS


def get_holiday_name(trading_date):
    """Return the holiday name for the given date, or None."""
    return NSE_FO_HOLIDAYS.get(trading_date)
