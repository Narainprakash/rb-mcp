import re
from datetime import datetime, date
from src.core.time_utils import get_ny_time

class ParsedSignal:
    def __init__(self, raw_text, action=None, ticker=None, expiry=None, strike=None, 
                 option_type=None, price=None, trade_style=None, parse_status="needs_review"):
        self.raw_text = raw_text
        self.action = action
        self.ticker = ticker
        self.expiry = expiry
        self.strike = strike
        self.option_type = option_type
        self.price = price
        self.trade_style = trade_style
        self.parse_status = parse_status

    def to_dict(self):
        return {
            "raw_text": self.raw_text,
            "action": self.action,
            "ticker": self.ticker,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "price": self.price,
            "trade_style": self.trade_style,
            "parse_status": self.parse_status
        }

def infer_year(month, day, is_0dte):
    """
    Returns the ISO expiry date, or None if a 0DTE alert's stated date is not
    today (the contract is then ambiguous and must not be traded).
    """
    today = get_ny_time().date()
    if is_0dte:
        # Spec 3.2.5: a 0DTE expiry must equal today. Validate rather than
        # overwrite, so a typo or stale tweet can't silently retarget the trade.
        if (month, day) != (today.month, today.day):
            return None
        return f"{today.year}-{today.month:02d}-{today.day:02d}"

    target_date = date(today.year, month, day)
    # If the date has passed by more than a few days, it's likely next year's expiry.
    # Expiries are usually Fridays. If they meant today but it's passed, that's tricky.
    # But for options, if it's already in the past, it must be next year.
    if target_date < today:
        target_date = date(today.year + 1, month, day)
    return f"{target_date.year}-{target_date.month:02d}-{target_date.day:02d}"

def parse_alert(tweet_text: str) -> ParsedSignal:
    # Spec 2.3: only tweets tagged #ALERT are signals. Everything else is
    # ordinary account activity - recorded for audit, but not a parse failure
    # and not something to notify on.
    if "#ALERT" not in tweet_text.upper():
        return ParsedSignal(tweet_text, parse_status="ignored")

    signal = ParsedSignal(tweet_text)
    
    # 1. Action
    if re.search(r'#ALERT\s*\(ADD\)', tweet_text, re.IGNORECASE):
        signal.action = "ADD"
    elif re.search(r'BTO\s+\$[A-Z]+', tweet_text, re.IGNORECASE):
        signal.action = "BTO"
        
    # 2. Details
    contract_match = re.search(r'\$([A-Z]+)\s+(\d{1,2})/?(\d{1,2})\s+(\d+\.?\d*)([CP])', tweet_text, re.IGNORECASE)
    if contract_match:
        signal.ticker = contract_match.group(1).upper()
        month = int(contract_match.group(2))
        day = int(contract_match.group(3))
        signal.strike = float(contract_match.group(4))
        signal.option_type = contract_match.group(5).upper()
        
        # 4. Trade Style
        styles = []
        for style in ["0DTE", "SWING", "DAYTRADE", "LOTTO"]:
            if style.upper() in tweet_text.upper():
                styles.append(style)
        signal.trade_style = ",".join(styles) if styles else None
        
        # 5. Expiry (Year inference). Returns None for an invalid calendar date
        # or a 0DTE alert whose stated expiry isn't today; validation below then
        # leaves the signal as needs_review.
        is_0dte = "0DTE" in (signal.trade_style or "")
        try:
            signal.expiry = infer_year(month, day, is_0dte)
        except ValueError:
            signal.expiry = None
        
    # 3. Price
    if signal.action == "BTO":
        price_match = re.search(r'^\s*(\d*\.?\d+)\s*$', tweet_text, re.MULTILINE)
        if price_match:
            signal.price = float(price_match.group(1))
    elif signal.action == "ADD":
        price_match = re.search(r'AVG:\s*(\d*\.?\d+)', tweet_text, re.IGNORECASE)
        if price_match:
            signal.price = float(price_match.group(1))

    # 6. Validation
    if all([signal.action, signal.ticker, signal.expiry, signal.strike, signal.option_type, signal.price is not None]):
        signal.parse_status = "success"
        
    return signal
