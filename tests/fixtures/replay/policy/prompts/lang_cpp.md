<!-- Adapted in part from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/principle-type-system-discipline/SKILL.md. MIT License; see THIRD_PARTY_NOTICES.md. -->
## C and C++

- Memory: out-of-bounds access, use after free or move, `string_view`s or spans to temporaries, iterator invalidation, double free, leaks on early returns.
- Undefined behavior: signed overflow, oversized shifts, uninitialized reads, strict aliasing, misaligned access, null dereference, `memcpy` of non-trivially-copyable types.
- Ownership: prefer RAII and `unique_ptr`; flag raw `new`/`delete`, a raw pointer outliving its owner, a by-reference lambda capture run after its scope, `shared_ptr` cycles.
- Integers: signed/unsigned mixing, narrowing, `size_t` underflow in `len - 1` loops, 32-bit overflow in sizes.
- Concurrency: unlocked shared state, lock-order inversions, a condition-variable wait without a predicate, a future outliving its data.
- Errors: an ignored returned error, a resource leaked when an exception passes, `noexcept` on code that can throw.
- Types: `enum class`, `std::variant` with exhaustive `std::visit` and strong typedefs over mixable flags and ints; `reinterpret_cast` and C-style casts that bypass the type system.
- ABI and API: a changed public header, struct layout, default argument or virtual function existing callers compiled against.
- Macros with side-effecting arguments or missing parentheses; one-definition-rule breaks in headers.
