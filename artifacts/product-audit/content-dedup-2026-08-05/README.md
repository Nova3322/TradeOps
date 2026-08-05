# Console content deduplication audit

## Scope

- Opportunity cards
- Review queue
- Trading campaigns
- System status connections
- Capital dashboard
- Venue account connection details
- Risk recovery conditions

## Result

- Removed the second breakout-period sentence beneath opportunity-card metrics. The compact period chips remain the single signal summary and keep their accessible labels.
- Removed repeated counts or status phrases where the same fact was already visible in the immediately adjacent summary, tab, table column or card.
- Retained intentional hierarchy on Today: overview counts and the single actionable item are different navigation levels, not competing fact sources.
- No API, authorization, order, capital, signing, broadcast or Gate behavior changed.

## Evidence

- `01-opportunity-before.png`: reported duplicate period signal.
- `02-opportunity-after-desktop.jpg`: desktop card after deduplication.
- `03-opportunity-after-390.jpg`: requested 390 px viewport (browser effective layout width 433 px).
- `04-opportunity-after-430.jpg`: requested 430 px viewport (browser effective layout width 478 px).

Both responsive runs reported document `scrollWidth === innerWidth` and zero remaining lower breakout-summary lines.
