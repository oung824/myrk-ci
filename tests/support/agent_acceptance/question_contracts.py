"""Frozen, deliberately narrow factual assertions for question and attachment goals."""

from decimal import Decimal
import re
from typing import Literal

FactStatus = Literal["passed", "failed", "unknown"]
CRITERIA_VERSION = "question-v2"


def question_fact(answer: str) -> tuple[FactStatus, str]:
    text = answer.casefold().replace("×", "*").replace("−", "-")
    for word, value in (("forty-two", "42"), ("forty two", "42"), ("six", "6"), ("seven", "7"), ("multiplied by", "*"), ("times", "*"), ("plus", "+"), ("minus", "-")):
        text = re.sub(r"\b" + re.escape(word) + r"\b", value, text)
    surface = text.replace("**", "").replace("__", "").replace("`", "").strip(" \t\r\n.!。")
    number = r"[-+]?\d+(?:\.\d+)?"
    claim = re.fullmatch(rf"(?:(?:the\s+)?(?:result|answer)|6\s*\*\s*7)\s*(?:is|:|=|equals)\s*(not\s+)?({number})", surface)
    if claim:
        if claim[1] and Decimal(claim[2]) != 42:
            return "unknown", "Denying another result does not establish the requested result."
        correct = Decimal(claim[2]) == 42 and not claim[1]
        return ("passed", "The answer states the independent result 42.") if correct else ("failed", "The answer directly contradicts the independent result 42.")
    if re.fullmatch(r"42(?:\s+is\s+(?:the\s+)?(?:answer|result))?", surface):
        return "passed", "The answer states the independent result 42."
    equation = re.fullmatch(rf"({number})\s*([+*/-])\s*({number})\s*=\s*({number})", surface)
    if equation:
        a, operation, b, claimed = Decimal(equation[1]), equation[2], Decimal(equation[3]), Decimal(equation[4])
        if operation == "/" and b == 0:
            return "failed", "The entire answer asserts an undefined division."
        actual = a + b if operation == "+" else a - b if operation == "-" else a * b if operation == "*" else a / b
        if actual != claimed:
            return "failed", "The entire answer asserts a false arithmetic equation."
    return "unknown", "This wording, quotation or explanation is outside the frozen simple assertion forms; review the original answer."


def threshold_fact(answer: str) -> tuple[FactStatus, str]:
    text = answer.casefold()
    citation = r"purchase\.md:1(?![\d:-])"
    if not re.search(citation, text):
        return "failed", "The required source location purchase.md:1 is missing or different."
    text = re.sub(citation, "", text).replace("[", "").replace("]", "")
    surface = text.replace("**", "").replace("__", "").replace("`", "").strip(" \t\r\n.!。")
    claim = re.fullmatch(r"(?:the\s+selected\s+purchase\s+rule\s+states\s+)?(?:the\s+)?(?:purchase\s+)?threshold\s*(?:(?:is|:|=|equals)\s*)?(not\s+)?([-+]?\d+(?:\.\d+)?)", surface)
    if claim:
        if claim[1] and Decimal(claim[2]) != 800:
            return "unknown", "Denying another threshold does not establish the selected threshold."
        correct = Decimal(claim[2]) == 800 and not claim[1]
        return ("passed", "The answer states 800 and cites the actual source line.") if correct else ("failed", "The answer directly contradicts the selected threshold 800.")
    if surface == "800":
        return "passed", "The answer states 800 and cites the actual source line."
    return "unknown", "The source is present, but this wording, quotation or explanation needs human review."
