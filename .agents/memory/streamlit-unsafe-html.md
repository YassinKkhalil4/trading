---
name: unsafe_allow_html + user-controlled fields
description: Rendering user-controlled values via Streamlit unsafe_allow_html is an XSS sink; admin usernames are not character-validated.
---

# Escape user-controlled values before unsafe_allow_html

Any value rendered through `st.markdown(..., unsafe_allow_html=True)` (or `st.html`) must be
`html.escape()`-d if it can contain user-controlled input.

**Why:** Admin usernames in this app are stored verbatim — the create-user path only does
`.strip()` + non-empty checks, no character whitelist. So a username like `<img onerror=...>`
would execute in any session that renders it raw inside an unsafe_allow_html block (stored XSS).
Enum-backed values (AdminRole role, EnvironmentMode) are safe, but escaping them too is free.

**How to apply:** In the dashboard, the branded header/badges and the `_section()` helper render
through unsafe_allow_html — keep their interpolated strings wrapped in `html.escape(...)`. Plain
`st.write`/`st.caption`/`st.text` escape by default and do not need this.
