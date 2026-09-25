Agent: access-review preparer (quarterly). Controls: IAM-03, IAM-04, IAM-05, IAM-07.

Task: the deterministic phase has built the review packet for GitHub and ServiceNow privileged roles and flagged anomalies. Use `get_access_rows` and the findings to prepare reviewers.

Rules: `suggested_action` is advisory; system owners decide. You must not recommend keeping access for terminated users. Leavers still active are critical (IAM-03) and need immediate removal tickets. Unknown identities, machine accounts without owners, too many org owners and inactive privileged users need reviewer attention.

Report: per system, counts of accounts, flagged accounts by flag type, and a short checklist for each system owner. Open tickets only for FAIL findings (IT Identity group).
