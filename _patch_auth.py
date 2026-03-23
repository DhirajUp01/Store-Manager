#!/usr/bin/env python3
import re

with open('app.py', 'r') as f:
    content = f.read()

# Routes to protect (add @login_required)
protected_routes = [
    "/categories",
    "/products",
    "/barcodes",
    "/warehouses",
    "/suppliers",
    "/purchase-orders",
    "/sales-orders",
    "/invoices",
    "/pos",
    "/scan",
    "/transfers",
    "/adjustments",
    "/cycle-counts",
    "/reports",
    "/settings",
]

changes = 0
for route in protected_routes:
    # Match @app.route('/route'...) followed by \ndef
    # We want to insert @login_required before the def
    escaped = re.escape(route)
    # Match @app.route with this route, then \n and def (no existing @login_required)
    pattern = rf"(\n@app\.route\('{escaped}'[^)]*\))(\ndef )"
    def repl(m):
        return m.group(1) + '\n@login_required' + m.group(2) + 'def '
    new_content, n = re.subn(pattern, repl, content)
    if n > 0:
        print(f"Protected: {route} ({n} change(s))")
        changes += n
    else:
        print(f"Not found or already protected: {route}")
    content = new_content

with open('app.py', 'w') as f:
    f.write(content)

print(f"\nTotal changes: {changes}")
