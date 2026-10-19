"""Conservative time constraints from original messages, never model confirmation flags.

This is a limited English/Spanish grammar, not a general natural-language parser.
Recognized ambiguous or negative time references require a new clarified message.
"""
import re
import unicodedata
from datetime import date, timedelta


def normalized(body: str) -> str:
    text=''.join(c for c in unicodedata.normalize('NFKD',body.casefold()) if not unicodedata.combining(c))
    return text.replace('\u2019',"'").replace('\u2018',"'")


def validate_mutation_consent(body: str, operation: str) -> None:
    """Explicitly withholding a change wins over a model's proposed operation."""
    text=normalized(body)
    verbs=r'(?:book\w*|schedul\w*|reserv\w*|agend\w*|cancel\w*|reschedul\w*|mov\w*|reprogram\w*)'
    denied=re.search(r"\b(?:do not|don't|dont|never|no|not|without|sin)\s+(?:\w+\s+){0,3}"+verbs+r'\b',text)
    options_only=re.search(r'\b(?:just|only|solo|solamente)\b[^.!?;]*\b(?:options|opciones|show|mostrar|ver)\b',text)
    inquiry=re.search(r'\b(?:show|mostrar|muestre)\b[^.!?;]*\b(?:available|appointments|options|opciones|citas)\b',text)
    if denied or options_only or (inquiry and not re.search(r'\b'+verbs+r'\b',text)):
        raise ValueError('No mutation consent: request permission or confirmation before changing appointments')


def requested_operation(body: str) -> str | None:
    text=normalized(body)
    try:
        validate_mutation_consent(body,'book')
    except ValueError:
        return None
    if re.search(r'\b(?:reschedule|move|mueva|reprogram\w*)\b',text):return 'reschedule'
    if re.search(r'\b(?:cancel|cancele|cancelar)\b',text):return 'cancel'
    if re.search(r'\b(?:book|schedule|reserve|reservar|reserve|agendar|agende)\b',text):return 'book'
    if re.search(r'\b(?:need|necesito|necesita)\s+(?:an?\s+|una?\s+)?(?:appointment|cita)\b',text):return 'book'
    if re.search(r'\b(?:can|could)\s+(?:i|we)\s+get\s+(?:(?:him|her|me)\s+seen|an?\s+appointment)\b',text):return 'book'
    if re.search(r'\b(?:want to see|any chance of a physical)\b',text):return 'book'
    return None


WEEKDAYS=[('monday','lunes'),('tuesday','martes'),('wednesday','miercoles'),('thursday','jueves'),
          ('friday','viernes'),('saturday','sabado'),('sunday','domingo')]
MONTHS=[('january','enero'),('february','febrero'),('march','marzo'),('april','abril'),('may','mayo'),
        ('june','junio'),('july','julio'),('august','agosto'),('september','septiembre'),
        ('october','octubre'),('november','noviembre'),('december','diciembre')]


def requested_visit_types(body: str) -> set[str]:
    text=normalized(body)
    return {kind for pattern,kind in [
        (r'annual physical|\bphysical\b|chequeo anual','annual_physical'),
        (r'follow.up','follow_up'),(r'telehealth|teleconsulta','telehealth'),(r'sick visit','sick_visit')]
        if re.search(pattern,text)}


def _destination(body: str, operation: str) -> str:
    text=normalized(body)
    # Ignore the old appointment date when the sender supplies a destination.
    if operation=='reschedule':
        parts=re.split(r'\b(?:to|al)\b',text)
        if len(parts)>1:text=parts[-1]
    return text


def validate_requested_slot(body: str, slot: dict, today: date, providers: list[dict],
                            operation: str='book', original_date: str | None=None) -> None:
    """Bounded source-text constraints. Ambiguous recognized dates require clarification."""
    validate_mutation_consent(body,operation)
    text=_destination(body,operation)
    chosen=date.fromisoformat(slot['date'])
    visit_types=requested_visit_types(text)
    if visit_types and slot['visit_type'] not in visit_types:
        raise ValueError('Slot does not match the requested visit type')
    calendar_terms=[name for names in WEEKDAYS+MONTHS for name in names]
    calendar_terms += [p['id'].casefold() for p in providers]+['morning','mornings','afternoon','afternoons','today','tomorrow','hoy','manana','mananas','tarde','tardes']
    targets='|'.join(map(re.escape,calendar_terms))+r'|\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)'
    negative_pattern=r"\b(?:not|no|never|cannot|can't|except|excluding|unavailable|nunca)\b[^.!?;]{0,35}\b(?:"+targets+r')\b'
    if re.search(negative_pattern,text.replace('dr.','dr')):
        raise ValueError('Negative requested calendar references need clarification before a mutation')
    named=[]
    for provider in providers:
        words=normalized(provider['name']).replace(',',' ').split()
        surname=next((word for word in reversed(words) if word not in {'np','dr.','dr'}),provider['id'].casefold())
        if re.search(r'\b'+re.escape(surname)+r'\b',text) or re.search(r'\b'+re.escape(provider['id'].casefold())+r'\b',text):
            named.append(provider['id'])
    if named and slot['provider'] not in named:
        raise ValueError('Slot does not match the requested provider')
    explicit=[]
    for raw in re.findall(r'\b\d{4}-\d{2}-\d{2}\b',text):explicit.append(date.fromisoformat(raw))
    for month,names in enumerate(MONTHS,1):
        pattern='(?:'+'|'.join(names)+')'
        for match in re.finditer(r'\b'+pattern+r'\s*(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?',text):
            explicit.append(date(int(match[2] or today.year),month,int(match[1])))
        for match in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?'+pattern+r'\b(?:,?\s+(\d{4}))?',text):
            explicit.append(date(int(match[2] or today.year),month,int(match[1])))
    if not explicit:
        for raw in re.findall(r'\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b',text):
            explicit.append(date(today.year,today.month,int(raw)))
    if re.search(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b',text):
        raise ValueError('Ambiguous requested numeric date; ask for clarification')
    if explicit and (len(set(explicit))!=1 or chosen!=explicit[0]):
        raise ValueError('Slot does not match the requested date; new confirmation or clarification required')
    if not explicit:
        relative_text=text.replace('por la manana','')
        either_day = re.search(r'\b(?:(?:today|hoy)\s+(?:or|o)\s+(?:tomorrow|manana)|(?:tomorrow|manana)\s+(?:or|o)\s+(?:today|hoy))\b', relative_text)
        if either_day:
            if chosen not in {today, today + timedelta(days=1)}:
                raise ValueError('Slot does not match requested today or tomorrow')
        elif re.search(r'\b(?:tomorrow|manana)\b',relative_text):
            if chosen!=today+timedelta(days=1):raise ValueError('Slot does not match requested tomorrow')
        elif re.search(r'\b(?:today|hoy)\b',text) and chosen!=today:
            raise ValueError('Slot does not match requested today')
    weekdays={i for i,names in enumerate(WEEKDAYS) if re.search(r'\b(?:'+'|'.join(names)+r')\b',text)}
    if weekdays and (len(weekdays)!=1 or chosen.weekday() not in weekdays):
        raise ValueError('Slot does not match the requested weekday')
    week_reference=re.search(r'\b(?:next week|following week|proxima semana|semana proxima|semana que viene|semana siguiente)\b',text)
    if weekdays and not explicit and not week_reference:
        expected=today+timedelta(days=(next(iter(weekdays))-today.weekday())%7)
        if chosen!=expected:raise ValueError('Slot does not match the requested weekday date')
    start,end=slot['start'],slot['end']
    morning=bool(re.search(r'\bmornings?\b|\bpor la mananas?\b',text))
    afternoon=bool(re.search(r'\bafternoons?\b|\bpor la tardes?\b',text))
    if morning and (start<'09:00' or end>'12:00') or afternoon and start<'13:00':
        raise ValueError('Slot does not match the requested part of day')
    validate_requested_time(text,start,end)


def validate_requested_time(body: str, start: str, end: str) -> None:
    text=''.join(c for c in unicodedata.normalize('NFKD',body.casefold()) if not unicodedata.combining(c))
    text=text.replace('\u2019',"'").replace('\u2018',"'")
    text=re.sub(r'\bif\s+(?:it\s+is\s+)?unavailable\b','',text)
    text=re.sub(r'\bat(?=\d)','at ',text)
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
