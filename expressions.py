"""Translate Informatica/Oracle expression syntax into standard SQL.

PowerCenter port expressions are written in Informatica's own function
language, which overlaps with Oracle SQL. Copying them into a dbt model
verbatim produces SQL that either fails to parse or, worse, parses and means
something different. This module rewrites the constructs we recognise:

    IIF(a > 0, 'yes', 'no')      -> case when a > 0 then 'yes' else 'no' end
    DECODE(k, 1, 'a', 2, 'b, 'z')-> case k when 1 then 'a' when 2 then 'b' else 'z' end
    NVL(x, 0)                    -> coalesce(x, 0)
    SYSDATE                      -> current_timestamp
    TRUNC(d, 'MM')               -> date_trunc('month', d)
    ADD_TO_DATE(d, 'MM', 3)      -> d + (3) * interval '1' month
    GET_DATE_PART(d, 'YYYY')     -> extract(year from d)
    LAST_DAY(d)                  -> date_trunc('month', d) + interval '1' month - interval '1' day
    TO_DATE(s)                   -> cast(s as date)

Anything unrecognised is passed through unchanged and reported as a warning, so
it shows up as a TODO in the generated model rather than silently shipping.

:func:`translate` returns ``(sql, warnings)``.
"""

from __future__ import annotations

import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# Informatica keywords used without call syntax.
_BARE = {
    "SYSDATE": "current_timestamp",
    "SYSTIMESTAMP": "current_timestamp",
    "SESSSTARTTIME": "current_timestamp",
    "TRUE": "true",
    "FALSE": "false",
    "NULL": "null",
}

# Oracle/Informatica date format codes -> standard SQL datetime fields.
_DATE_PARTS = {
    "D": "day", "DD": "day", "DDD": "day", "DY": "day", "DAY": "day", "J": "day",
    "MM": "month", "MON": "month", "MONTH": "month", "RM": "month",
    "Y": "year", "YY": "year", "YYY": "year", "YYYY": "year",
    "RR": "year", "RRRR": "year", "SYYYY": "year",
    "Q": "quarter",
    "W": "week", "WW": "week", "IW": "week",
    "HH": "hour", "HH12": "hour", "HH24": "hour",
    "MI": "minute",
    "SS": "second", "SSSSS": "second",
}

# Date formats that a plain cast already handles.
_ISO_DATE_FORMATS = {"YYYY-MM-DD", "YYYY-MM-DD HH24:MI:SS", "YYYY-MM-DD HH:MI:SS"}

# Informatica-specific functions with no reasonable standard SQL rendering.
_NO_EQUIVALENT = {
    "ABORT", "ERROR", "LOOKUP", "SETVARIABLE", "SETMAXVARIABLE",
    "SETMINVARIABLE", "SETCOUNTVARIABLE", "IS_DATE", "IS_NUMBER",
    "IS_SPACES", "REPLACECHR", "REPLACESTR", "METAPHONE", "CRC32",
    "DEC_BASE64", "ENC_BASE64", "AES_DECRYPT", "AES_ENCRYPT",
}


def translate(expression: str) -> tuple[str, list[str]]:
    """Rewrite ``expression`` as standard SQL. Returns ``(sql, warnings)``."""
    warnings: list[str] = []
    if not expression or not expression.strip():
        return expression, warnings
    return _rewrite(expression, warnings), warnings


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------


def _rewrite(expr: str, warnings: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(expr)

    while i < n:
        char = expr[i]

        if char == "'":  # a literal: copy verbatim, never rewrite inside it
            end = _string_end(expr, i)
            out.append(expr[i:end])
            i = end
            continue

        match = _IDENT.match(expr, i)
        if not match:
            out.append(char)
            i += 1
            continue

        name, after = match.group(0), match.end()
        call = after
        while call < n and expr[call].isspace():
            call += 1

        if call < n and expr[call] == "(":
            close = _match_paren(expr, call)
            if close is None:
                warnings.append(f"unbalanced parentheses after {name}(")
                out.append(expr[i:])
                break
            args = [
                _rewrite(arg, warnings).strip()
                for arg in _split_args(expr[call + 1 : close])
            ]
            out.append(_apply(name, args, warnings))
            i = close + 1
            continue

        out.append(_BARE.get(name.upper(), name))
        i = after

    return "".join(out)


def _string_end(expr: str, start: int) -> int:
    """Index just past the literal opening at ``start``. Handles '' escapes."""
    i = start + 1
    while i < len(expr):
        if expr[i] == "'":
            if i + 1 < len(expr) and expr[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return len(expr)


def _match_paren(expr: str, start: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``start``, or None."""
    depth = 0
    i = start
    while i < len(expr):
        char = expr[i]
        if char == "'":
            i = _string_end(expr, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_args(raw: str) -> list[str]:
    """Split on top-level commas, ignoring those inside strings or parens."""
    if not raw.strip():
        return []

    args: list[str] = []
    depth, start, i = 0, 0, 0
    while i < len(raw):
        char = raw[i]
        if char == "'":
            i = _string_end(raw, i)
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(raw[start:i])
            start = i + 1
        i += 1
    args.append(raw[start:])
    return args


# --------------------------------------------------------------------------
# Function rewrites
# --------------------------------------------------------------------------


def _apply(name: str, args: list[str], warnings: list[str]) -> str:
    handler = _HANDLERS.get(name.upper())
    if handler:
        return handler(args, warnings)
    if name.upper() in _NO_EQUIVALENT:
        warnings.append(
            f"{name}() is Informatica-specific and has no standard SQL "
            "equivalent; left unchanged"
        )
    return f"{name}({', '.join(args)})"


def _literal(arg: str) -> str | None:
    """The text of a single-quoted literal argument, or None if it isn't one."""
    text = arg.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return None


def _date_part(arg: str, warnings: list[str], caller: str) -> str | None:
    code = _literal(arg)
    if code is None:
        warnings.append(
            f"{caller}() format argument {arg} is not a literal; cannot map it "
            "to a SQL datetime field"
        )
        return None
    part = _DATE_PARTS.get(code.upper())
    if part is None:
        warnings.append(f"{caller}() format '{code}' has no standard SQL field")
    return part


def _wrong_arity(name: str, args: list[str], warnings: list[str]) -> str:
    warnings.append(f"{name}() called with {len(args)} argument(s); left unchanged")
    return f"{name}({', '.join(args)})"


def _iif(args: list[str], warnings: list[str]) -> str:
    if len(args) == 2:
        return f"case when {args[0]} then {args[1]} end"
    if len(args) == 3:
        return f"case when {args[0]} then {args[1]} else {args[2]} end"
    return _wrong_arity("IIF", args, warnings)


def _decode(args: list[str], warnings: list[str]) -> str:
    if len(args) < 3:
        return _wrong_arity("DECODE", args, warnings)

    value, rest = args[0], args[1:]
    default = None
    if len(rest) % 2:
        default, rest = rest[-1], rest[:-1]

    whens = " ".join(
        f"when {search} then {result}"
        for search, result in zip(rest[0::2], rest[1::2])
    )
    tail = f" else {default}" if default is not None else ""
    return f"case {value} {whens}{tail} end"


def _nvl(args: list[str], warnings: list[str]) -> str:
    if len(args) < 2:
        return _wrong_arity("NVL", args, warnings)
    return f"coalesce({', '.join(args)})"


def _isnull(args: list[str], warnings: list[str]) -> str:
    if len(args) != 1:
        return _wrong_arity("ISNULL", args, warnings)
    return f"{args[0]} is null"


def _trunc(args: list[str], warnings: list[str]) -> str:
    if len(args) == 1:
        return f"cast({args[0]} as date)"
    if len(args) == 2:
        if _literal(args[1]) is None:
            # TRUNC(number, precision) - a numeric truncation, not a date one.
            warnings.append(
                "TRUNC() on a number is dialect-specific; left unchanged"
            )
            return f"trunc({', '.join(args)})"
        part = _date_part(args[1], warnings, "TRUNC")
        if part is None:
            return f"trunc({', '.join(args)})"
        return f"date_trunc('{part}', {args[0]})"
    return _wrong_arity("TRUNC", args, warnings)


def _add_to_date(args: list[str], warnings: list[str]) -> str:
    if len(args) != 3:
        return _wrong_arity("ADD_TO_DATE", args, warnings)
    part = _date_part(args[1], warnings, "ADD_TO_DATE")
    if part is None:
        return f"ADD_TO_DATE({', '.join(args)})"
    return f"{args[0]} + ({args[2]}) * interval '1' {part}"


def _last_day(args: list[str], warnings: list[str]) -> str:
    if len(args) != 1:
        return _wrong_arity("LAST_DAY", args, warnings)
    return (
        f"date_trunc('month', {args[0]}) + interval '1' month - interval '1' day"
    )


def _get_date_part(args: list[str], warnings: list[str]) -> str:
    if len(args) != 2:
        return _wrong_arity("GET_DATE_PART", args, warnings)
    part = _date_part(args[1], warnings, "GET_DATE_PART")
    if part is None:
        return f"GET_DATE_PART({', '.join(args)})"
    return f"extract({part} from {args[0]})"


def _months_between(args: list[str], warnings: list[str]) -> str:
    if len(args) != 2:
        return _wrong_arity("MONTHS_BETWEEN", args, warnings)
    left, right = args
    return (
        f"((extract(year from {left}) - extract(year from {right})) * 12"
        f" + (extract(month from {left}) - extract(month from {right})))"
    )


def _date_diff(args: list[str], warnings: list[str]) -> str:
    if len(args) != 3:
        return _wrong_arity("DATE_DIFF", args, warnings)
    part = _date_part(args[2], warnings, "DATE_DIFF")
    if part == "month":
        return _months_between(args[:2], warnings)
    if part == "year":
        return f"(extract(year from {args[0]}) - extract(year from {args[1]}))"
    if part == "day":
        warnings.append(
            "DATE_DIFF() in days is rendered as date subtraction, which is "
            "dialect-specific; verify against your warehouse"
        )
        return f"(cast({args[0]} as date) - cast({args[1]} as date))"
    warnings.append(
        f"DATE_DIFF() in units of '{part or 'unknown'}' has no portable "
        "rendering; left unchanged"
    )
    return f"DATE_DIFF({', '.join(args)})"


def _to_date(args: list[str], warnings: list[str]) -> str:
    if not args:
        return _wrong_arity("TO_DATE", args, warnings)
    if len(args) >= 2:
        fmt = _literal(args[1])
        if fmt is None or fmt.upper() not in _ISO_DATE_FORMATS:
            warnings.append(
                f"TO_DATE() format {args[1]} is dropped; a plain cast assumes "
                "the string is already ISO-8601"
            )
    return f"cast({args[0]} as date)"


def _to_char(args: list[str], warnings: list[str]) -> str:
    if len(args) == 1:
        return f"cast({args[0]} as varchar)"
    if len(args) == 2 and _literal(args[1]) is not None:
        part = _DATE_PARTS.get((_literal(args[1]) or "").upper())
        if part:
            return f"cast(extract({part} from {args[0]}) as varchar)"
        warnings.append(
            f"TO_CHAR() format {args[1]} has no standard SQL equivalent; the "
            "format is dropped"
        )
        return f"cast({args[0]} as varchar)"
    return _wrong_arity("TO_CHAR", args, warnings)


def _cast_to(sql_type: str, name: str):
    def handler(args: list[str], warnings: list[str]) -> str:
        if not args:
            return _wrong_arity(name, args, warnings)
        if len(args) > 1:
            warnings.append(
                f"{name}() extra argument(s) dropped by the cast to {sql_type}"
            )
        return f"cast({args[0]} as {sql_type})"

    return handler


def _substr(args: list[str], warnings: list[str]) -> str:
    if len(args) == 2:
        return f"substring({args[0]} from {args[1]})"
    if len(args) == 3:
        return f"substring({args[0]} from {args[1]} for {args[2]})"
    return _wrong_arity("SUBSTR", args, warnings)


def _instr(args: list[str], warnings: list[str]) -> str:
    if len(args) == 2:
        return f"position({args[1]} in {args[0]})"
    warnings.append(
        "INSTR() with an occurrence or start position has no standard SQL "
        "equivalent; left unchanged"
    )
    return f"INSTR({', '.join(args)})"


def _trim_side(side: str, name: str):
    def handler(args: list[str], warnings: list[str]) -> str:
        if len(args) == 1:
            return f"trim({side} from {args[0]})"
        if len(args) == 2:
            return f"trim({side} {args[1]} from {args[0]})"
        return _wrong_arity(name, args, warnings)

    return handler


def _concat(args: list[str], warnings: list[str]) -> str:
    if len(args) < 2:
        return _wrong_arity("CONCAT", args, warnings)
    return "(" + " || ".join(args) + ")"


_HANDLERS = {
    "IIF": _iif,
    "DECODE": _decode,
    "NVL": _nvl,
    "IFNULL": _nvl,
    "ISNULL": _isnull,
    "TRUNC": _trunc,
    "ADD_TO_DATE": _add_to_date,
    "LAST_DAY": _last_day,
    "GET_DATE_PART": _get_date_part,
    "DATE_PART": _get_date_part,
    "MONTHS_BETWEEN": _months_between,
    "DATE_DIFF": _date_diff,
    "TO_DATE": _to_date,
    "TO_TIMESTAMP": _to_date,
    "TO_CHAR": _to_char,
    "TO_INTEGER": _cast_to("integer", "TO_INTEGER"),
    "TO_BIGINT": _cast_to("bigint", "TO_BIGINT"),
    "TO_DECIMAL": _cast_to("decimal", "TO_DECIMAL"),
    "TO_FLOAT": _cast_to("double precision", "TO_FLOAT"),
    "SUBSTR": _substr,
    "INSTR": _instr,
    "LTRIM": _trim_side("leading", "LTRIM"),
    "RTRIM": _trim_side("trailing", "RTRIM"),
    "CONCAT": _concat,
}
