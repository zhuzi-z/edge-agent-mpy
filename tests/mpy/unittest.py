"""Minimal unittest for MicroPython (adapted from micropython-lib's unittest).

Provides the unittest subset the unix-port functional tests use:
``TestCase`` with ``assertX`` methods, ``setUp``/``tearDown``/``addCleanup``,
``assertRaises`` (callable or context-manager form), and ``main()`` which
discovers every ``TestCase`` subclass in the caller's namespace, runs its
``test_*`` methods, prints a summary, and exits non-zero on any failure/error.

MicroPython note: the running script is NOT registered as ``__main__`` in
``sys.modules`` (and ``import __main__`` yields an empty module), so test
files must end with ``unittest.main(globals())`` to hand over their namespace.

Single file, no imports beyond ``sys`` -- runs on the bare unix port.
"""

import sys


class SkipTest(Exception):
    pass


class AssertRaisesContext:
    def __init__(self, expected):
        self.expected = expected
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is None:
            raise AssertionError("%r not raised" % self.expected)
        if issubclass(exc_type, self.expected):
            self.exception = exc_value
            return True
        return False


class TestCase:
    def __init__(self):
        self._cleanups = []

    # ---- lifecycle ----

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def addCleanup(self, func, *args, **kwargs):
        self._cleanups.append((func, args, kwargs))

    def doCleanups(self):
        while self._cleanups:
            func, args, kwargs = self._cleanups.pop(-1)
            func(*args, **kwargs)

    def skip(self, reason=""):
        raise SkipTest(reason)

    # ---- assertions ----

    def fail(self, msg=""):
        raise AssertionError(msg)

    def assertEqual(self, x, y, msg=""):
        if x != y:
            raise AssertionError(msg or "%r != (expected) %r" % (x, y))

    def assertNotEqual(self, x, y, msg=""):
        if x == y:
            raise AssertionError(msg or "%r == (unexpected) %r" % (x, y))

    def assertTrue(self, x, msg=""):
        if not x:
            raise AssertionError(msg or "%r is not True" % (x,))

    def assertFalse(self, x, msg=""):
        if x:
            raise AssertionError(msg or "%r is not False" % (x,))

    def assertIs(self, x, y, msg=""):
        if x is not y:
            raise AssertionError(msg or "%r is not %r" % (x, y))

    def assertIsNot(self, x, y, msg=""):
        if x is y:
            raise AssertionError(msg or "%r is %r" % (x, y))

    def assertIsNone(self, x, msg=""):
        if x is not None:
            raise AssertionError(msg or "%r is not None" % (x,))

    def assertIsNotNone(self, x, msg=""):
        if x is None:
            raise AssertionError(msg or "unexpected None")

    def assertIn(self, x, y, msg=""):
        if x not in y:
            raise AssertionError(msg or "%r not in %r" % (x, y))

    def assertNotIn(self, x, y, msg=""):
        if x in y:
            raise AssertionError(msg or "%r in %r" % (x, y))

    def assertIsInstance(self, x, y, msg=""):
        if not isinstance(x, y):
            raise AssertionError(msg or "%r is not an instance of %r" % (x, y))

    def assertGreater(self, x, y, msg=""):
        if not (x > y):
            raise AssertionError(msg or "%r not > %r" % (x, y))

    def assertGreaterEqual(self, x, y, msg=""):
        if not (x >= y):
            raise AssertionError(msg or "%r not >= %r" % (x, y))

    def assertLess(self, x, y, msg=""):
        if not (x < y):
            raise AssertionError(msg or "%r not < %r" % (x, y))

    def assertLessEqual(self, x, y, msg=""):
        if not (x <= y):
            raise AssertionError(msg or "%r not <= %r" % (x, y))

    def assertRaises(self, exc, func=None, *args, **kwargs):
        if func is None:
            return AssertRaisesContext(exc)
        try:
            func(*args, **kwargs)
        except exc:
            return
        except Exception as e:
            raise AssertionError("%r raised instead of %r" % (type(e).__name__, exc))
        raise AssertionError("%r not raised" % exc)


# ---- runner ----


def _discover(namespace):
    """Return sorted (cls, method_name) pairs for all test_* methods."""
    out = []
    seen = set()
    for obj in namespace.values():
        if (
            isinstance(obj, type)
            and issubclass(obj, TestCase)
            and obj is not TestCase
            and id(obj) not in seen
        ):
            seen.add(id(obj))
            for m in sorted(dir(obj)):
                if m.startswith("test") and callable(getattr(obj, m)):
                    out.append((obj, m))
    out.sort(key=lambda cm: (cm[0].__name__, cm[1]))
    return out


def main(namespace=None):
    """Run all TestCase subclasses found in ``namespace`` (a module globals
    dict). Test files call ``unittest.main(globals())`` at the end."""
    if namespace is None:
        import __main__

        namespace = vars(__main__)
    tests = _discover(namespace)
    ran = 0
    failures = []  # (label, traceback-ish str)
    errors = []
    skipped = 0

    for cls, meth in tests:
        label = "%s.%s" % (cls.__name__, meth)
        tc = cls()
        try:
            tc.setUp()
            try:
                getattr(tc, meth)()
            finally:
                tc.tearDown()
                tc.doCleanups()
            ran += 1
            print(".", end="")
        except SkipTest as e:
            skipped += 1
            ran += 1
            print("s", end="")
        except AssertionError as e:
            failures.append((label, str(e)))
            print("F", end="")
        except Exception as e:
            errors.append((label, "%s: %s" % (type(e).__name__, e)))
            print("E", end="")
            sys.print_exception(e)

    print("")
    for label, msg in failures:
        print("FAIL: %s\n  %s" % (label, msg))
    for label, msg in errors:
        print("ERROR: %s\n  %s" % (label, msg))
    print("-" * 40)
    extra = ""
    if skipped:
        extra = " (skipped=%d)" % skipped
    print("Ran %d tests%s" % (ran, extra))
    if failures or errors:
        print("FAILED (failures=%d, errors=%d)" % (len(failures), len(errors)))
        sys.exit(1)
    print("OK")
