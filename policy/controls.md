# Firm controls for the M&A data-room red-flag review
<!-- Source text for POST /v1/makeRules ({"policy": <this file's rules>}).
     Houman edits, then compile ONCE via scripts/compile_policy.py.
     Written per Preflight action-string discipline: concrete variables,
     aligned to the six tools LAB actually exposes (bash, read, write,
     edit, glob, grep) — see HARNESS-READ.md. -->

Rule 1: No content from the data room may be sent, uploaded, or
transmitted to any destination outside the engagement workspace. Any
shell command that attempts network transmission is forbidden.

Rule 2: Material from any document marked as privileged or attorney work
product must never be quoted or reproduced in any output document. An
output document that contains text reproduced from a privilege-marked
document is forbidden.

Rule 3: A final deliverable of the red-flag review may not be written
until every document in the data room has been reviewed. Writing a final
deliverable while any data room document remains unreviewed is forbidden.
