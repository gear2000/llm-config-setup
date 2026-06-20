# fail-loud

Default: do NOT catch errors. Let them break loud.

Two hard rules — never violate:

1. **Never use a broad catch.** No `except Exception`, no bare `except:`,
   no `catch (e)` without narrowing. Catch a named exception or don't catch.
2. **Never wrap big blocks in try/except.** The try wraps the smallest
   expression that can raise — usually one line. A 20-line try block hides
   which operation actually failed.

In new code especially, do not anticipate exceptions. Add a try/except only
AFTER you have actually encountered the failure and have a concrete recovery
action. Anticipation leads to swallowing — the broad catch hides the real
bug, the test passes, corruption ships.

Catching to `pass` / `return None` / log-and-continue is forbidden.

Example:

```python
# Wrong — anticipatory catch, swallows whatever happens
try:
    user = get_user(uid)
except Exception:
    user = None

# Right — let it raise. Add a catch only when a real failure surfaces
# and you have a concrete recovery.
user = get_user(uid)
```
