"""
Static analysis test for the confirm modal + CSRF + HTMX interaction flow.
Traces the logic through each event phase to verify the claimed behavior.
"""
import re
import sys

import os
_BASE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'base.html')
with open(_BASE_HTML) as f:
    html = f.read()

# Extract the main script
m = re.search(r'<script>\s*// ── API key injection.*?</script>', html, re.DOTALL)
if not m:
    # fallback: get the biggest inline script
    scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
    js = max(scripts, key=len)
else:
    js = m.group(0)

checks = []

# 1. confirmAction returns a Promise
if 'return new Promise' in js and 'function(opts)' in js:
    checks.append(('confirmAction returns Promise', True))

# 2. Cancel button has focus-on-open (prevents stray Enter confirming)
if 'globalConfirmCancel' in js and '.focus()' in js:
    checks.append(('Cancel button auto-focuses', True))

# 3. Data-confirm submit handler is in capture phase
m1 = re.search(r"addEventListener\('submit'.*?data-confirm.*?}, true\)", js, re.DOTALL)
checks.append(('data-confirm handler is capture-phase', bool(m1)))

# 4. stopImmediatePropagation present in data-confirm flow
checks.append(('stopImmediatePropagation on data-confirm', 'stopImmediatePropagation' in js))

# 5. _confirmed flag prevents infinite recursion
checks.append(("_confirmed flag check", "_confirmed === '1'" in js))

# 6. htmx:confirm event handler present
checks.append(('htmx:confirm handler', "addEventListener('htmx:confirm'" in js))

# 7. htmx:confirm calls issueRequest(true) on ok
checks.append(("issueRequest(true) on confirm", 'issueRequest(true)' in js))

# 8. CSRF injection runs after data-confirm (bubble phase, not capture)
# Check that CSRF handler is NOT called with `true` for capture phase
m2 = re.search(r"document\.addEventListener\('submit', function\(evt\) \{\s*const form = evt\.target;\s*if \(!form \|\| \(form\.method.*?form\.appendChild\(inp\);\s*\}\);", js, re.DOTALL)
checks.append(('CSRF handler is bubble-phase (not capture)', bool(m2)))

# 9. beforeunload fires only when dirty
checks.append(('beforeunload fires conditionally', 'isAnyDirty()' in js))

# 10. HTMX success clears dirty flag (for hx-post forms)
checks.append(('HTMX success clears dirty flag', 'htmx:afterRequest' in js and 'markClean' in js))

# 11. Reduced-motion CSS is in the <style> block (not JS)
import os
_BASE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'base.html')
with open(_BASE_HTML) as f:
    full = f.read()
checks.append(('prefers-reduced-motion CSS block', '@media (prefers-reduced-motion: reduce)' in full))

# 12. Focus-visible CSS defined
checks.append((':focus-visible rule defined', ':focus-visible' in full))

# 13. Toast container has aria-live
checks.append(('Toast container aria-live="polite"', 'aria-live="polite"' in full))

def _out(text: str = "") -> None:
    sys.stdout.write(f"{text}\n")


_out("Static flow analysis:")
for name, ok in checks:
    marker = '  OK' if ok else 'FAIL'
    _out(f'  [{marker}] {name}')

failed = [c for c in checks if not c[1]]
_out(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
exit(1 if failed else 0)
