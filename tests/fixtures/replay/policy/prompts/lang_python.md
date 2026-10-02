## Python

- Mutable default arguments and mutable class attributes shared between instances.
- Exceptions: a bare `except:` or `except Exception` swallowing errors, a cause lost (no `from`), cleanup skipped (a file, lock or connection opened without `with`).
- `None`: an optional value used unchecked; a function returning `None` on one path and a value on others.
- Truthiness: `if x:` where `0`, `""`, an empty array or a NumPy array are valid values.
- Iteration: mutating a dict or list while iterating it, a generator consumed twice, late-binding closures in loops.
- Numerics: integer vs float division, float equality, NumPy dtype overflow or silent upcasting, broadcasting to the wrong shape, NaN propagation, `inplace` changes to a caller's array.
- Public API: a changed parameter name, order, default or return type; a removed or renamed public symbol without a deprecation cycle.
- Types: annotations that lie (`-> str` returning `None`), `Any` crossing a module boundary, `cast` without a check.
- Async: a blocking call in `async` code, a coroutine never awaited, shared state across tasks without a lock.
- Django and web code: queries in loops (N+1), user input in raw SQL or templates.
