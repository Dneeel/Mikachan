"""Small, deterministic arithmetic for standalone Telegram messages."""

from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext


_EXPRESSION = re.compile(r"[\d\s.()+\-*/×÷]+=?")


def calculate(text: str) -> str | None:
    raw = text.strip().replace("×", "*").replace("÷", "/")
    if (len(raw) > 80 or not _EXPRESSION.fullmatch(raw)
            or not re.search(r"\d\s*[+*/-]\s*\d|\)\s*[+*/-]|[+*/-]\s*\(", raw)):
        return None
    try:
        root = ast.parse(raw.rstrip("="), mode="eval")

        def evaluate(node: ast.expr, depth: int = 0) -> Decimal:
            if depth > 12:
                raise ValueError("expression is too deep")
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                return Decimal(str(node.value))
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand, depth + 1)
                return value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(node, ast.BinOp) and isinstance(node.op,
                    (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                return left / right
            raise ValueError("unsupported expression")

        with localcontext() as context:
            context.prec = 28
            result = evaluate(root.body)
        if not result.is_finite() or abs(result) >= Decimal("1e30"):
            return None
        return format(result.normalize(), "f")
    except (SyntaxError, ValueError, InvalidOperation, DivisionByZero, OverflowError):
        return None
