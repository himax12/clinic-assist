"""Conservative time constraints from original messages, never model confirmation flags.

This is a limited English/Spanish grammar, not a general natural-language parser.
Recognized ambiguous or negative time references require a new clarified message.
"""
import re
import unicodedata


def validate_requested_time(body: str, start: str, end: str) -> None:
    text=''.join(c for c in unicodedata.normalize('NFKD',body.casefold()) if not unicodedata.combining(c))
    text=text.replace('\u2019',"'").replace('\u2018',"'")
    tokens=[]
    for match in re.finditer(r'(?<![\w:])(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?(?![\w:])',text):
        hour,minute,period=match.groups()
        if minute is None and period is None:
            continue
        h,m=int(hour),int(minute or 0)
        if m>59 or (period and not 1<=h<=12) or (not period and h>23):
            raise ValueError('Ambiguous requested time; ask for clarification before booking')
        if period:h=h%12+(12 if period.replace('.','')=='pm' else 0)
        tokens.append((h*60+m,match.start()))
    if not tokens:
        if re.search(r'\b(?:at|a las)\s+\d{1,2}\b',text):
            raise ValueError('Requested time needs AM/PM or HH:MM; ask for clarification')
        return
    if len({value for value,_ in tokens})!=1 or re.search(
            r"\b(?:not|no|never|cannot|can't|don't|doesn't|isn't|won't|nunca|tampoco|unable|unavailable)\b",text):
        raise ValueError('Negative or competing time references require clarification, not model confirmation')
    requested,position=tokens[0]
    actual=int(start[:2])*60+int(start[3:])
    finish=int(end[:2])*60+int(end[3:])
    prefix=text[:position]
    if re.search(r'\b(?:after|desde|despues de)\s*(?:las\s*)?$',prefix):
        allowed=actual>=requested
    elif re.search(r'\b(?:before|until|antes de|hasta)\s*(?:las\s*)?$',prefix):
        allowed=finish<=requested
    else:
        allowed=actual==requested
    if not allowed:
        raise ValueError('Slot does not match the requested time; offer alternatives and obtain confirmation in a new message')
