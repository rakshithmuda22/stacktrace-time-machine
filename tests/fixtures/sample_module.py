"""Sample Python module for AST parsing tests.

This file is NOT imported — it is read as a string and passed
to ast.parse() in test_analyzer.py.
"""


def top_level_function(x, y):
    """A simple top-level function."""
    return x + y


def another_function(a):
    """Another top-level function."""
    result = a * 2
    return result


class Calculator:
    """A sample class with methods."""

    def __init__(self, precision=2):
        self.precision = precision

    def divide(self, a, b):
        """Division method — the one we blame in tests."""
        if b == 0:
            raise ZeroDivisionError("division by zero")
        return round(a / b, self.precision)

    def multiply(self, a, b):
        """Multiplication method."""
        return round(a * b, self.precision)


def outer_function():
    """Function with a nested function inside."""

    def inner_function():
        """Nested function — same-name disambiguation test."""
        return 42

    return inner_function()


def inner_function():
    """Top-level function with the same name as the nested one above."""
    return 99


@staticmethod
def decorated_function(value):
    """A decorated top-level function."""
    return value * 3


async def async_handler(request):
    """An async function for async detection tests."""
    data = await request.json()
    return data
