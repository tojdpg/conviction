---
name: Feature request
about: Suggest a feature for conviction
labels: enhancement
---

**Is your feature request within scope?**

conviction is a **buy/sell decision cockpit**, not a portfolio accounting tool.
The following are **out of scope** and will be rejected:

- [ ] Brokerage account integration (no broker APIs, no credentials)
- [ ] Transaction import (no CSV/OFX/QIF import of buy/sell transactions)
- [ ] Transaction history / accounting (no realized P&L, no tax-lot tracking)
- [ ] Authentication / multi-user (single-user, localhost-first — use a VPN)
- [ ] External database server (SQLite is the store, deliberately)

Check the [CONTRIBUTING.md scope guardrails](../../CONTRIBUTING.md) if unsure.

**Describe the feature**

A clear description of what you want to happen.

**Why**

What problem does this solve? How does it fit the "quant signals + agent
analysis + human judgment = decisions" design center?

**Alternatives considered**

What alternatives exist? Why are they insufficient?

**Mockup / sketch (optional)**

If the feature has a UI component, describe or sketch it.
