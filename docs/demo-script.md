# 90-second demo: a late resubmission after sign-off

For a screen recording of the browser UI (`fct ui`). All data is synthetic. Times are cumulative.

| Time | On screen | Say |
|---|---|---|
| 0:00 | Sidebar → Demo → load **E2**. Close board. | "It's the 6th of November. Singapore has filed October; Australia hasn't. The board names the owner and says it's overdue." |
| 0:12 | Load **E6**. Releases page: 2026-10-R1. | "Australia files late, everything validates, and the regional lead approves release R1. APAC EBITDA is S$221,600. That's what leadership sees." |
| 0:25 | Load **E7**. Close board: "Validation failed: AU actual". | "A week later Australia resubmits with an account the mapping doesn't know. The file is held, and R1 stays the current release." |
| 0:38 | Load **E11**. Review and publish. | "Once the account is mapped, before anyone approves, we see exactly what changes: AU EBITDA down 12,900, minus 25 percent, APAC down 5.8, both material, and the file behind it." |
| 0:55 | Type an approver, click **Approve and publish**. | "Approving publishes exactly this proposal. If another file had arrived since I opened the page, it would refuse." |
| 1:05 | Results → drill into APAC EBITDA, pick an AU row. | "Any number traces back to its file and row, including the exchange rate used." |
| 1:15 | Releases → download the R2 deck, open the "Changes since R1" slide. | "And the Comex deck is generated from the release, with the change against R1 on its own slide. R1 itself still reproduces exactly." |
| 1:30 | End. | |

Backup line if asked "why not Power Query?": "Power Query can rebuild the numbers. What it doesn't give you is the approved version, the impact of the correction before approval, and proof of what leadership saw."
